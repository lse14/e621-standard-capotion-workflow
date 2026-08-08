from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any


SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
MAX_PATH_BYTES = 16_384
MAX_TEXT_BYTES = 16_384
MAX_PROCESS_ITEMS = 1_024
RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}


class OcrPayloadError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OcrPayloadError(f"{field} must be an object")
    return value


def _keys(value: dict[str, object], required: set[str], optional: set[str] | None = None) -> None:
    allowed = required | (optional or set())
    missing = required - set(value)
    extra = set(value) - allowed
    if missing:
        raise OcrPayloadError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise OcrPayloadError(f"unknown fields: {', '.join(sorted(extra))}")


def _text(value: object, field: str, maximum: int, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str) or (not allow_blank and not value):
        raise OcrPayloadError(f"{field} is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise OcrPayloadError(f"{field} is not valid UTF-8") from exc
    if size > maximum:
        raise OcrPayloadError(f"{field} exceeds its byte limit")
    return value


def _identifier(value: object, field: str) -> str:
    result = _text(value, field, 128)
    if not IDENTIFIER.fullmatch(result):
        raise OcrPayloadError(f"{field} is invalid")
    return result


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise OcrPayloadError(f"{field} must be a lowercase SHA-256")
    return value


def _requested_device(value: object) -> str:
    if value not in {"auto", "cuda", "cpu"}:
        raise OcrPayloadError("requestedDevice is invalid")
    return value


def _runtime_id(value: object, field: str) -> str:
    if value not in {"ocr-paddle", "ocr-paddle-gpu"}:
        raise OcrPayloadError(f"{field} is invalid")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise OcrPayloadError(f"{field} is invalid")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OcrPayloadError(f"{field} is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise OcrPayloadError(f"{field} is invalid")
    return result


def _relative(value: object, field: str, *, extension_required: bool = False) -> str:
    result = _text(value, field, MAX_PATH_BYTES).replace("/", "\\")
    path = PureWindowsPath(result)
    if path.is_absolute() or path.drive or path.root or result.startswith("\\"):
        raise OcrPayloadError(f"{field} must be relative")
    if extension_required and not path.suffix:
        raise OcrPayloadError(f"{field} must include an extension")
    for component in result.split("\\"):
        if not component or component in {".", ".."} or ":" in component or component.endswith((".", " ")):
            raise OcrPayloadError(f"{field} contains an unsafe component")
        if component.split(".", 1)[0].upper() in RESERVED:
            raise OcrPayloadError(f"{field} contains a reserved component")
    return result


def _absolute_image_path(value: object) -> str:
    result = _text(value, "imagePath", MAX_PATH_BYTES)
    path = PureWindowsPath(result)
    if not path.is_absolute() or not path.drive or not path.root or any(part in {".", ".."} for part in path.parts):
        raise OcrPayloadError("imagePath must be an absolute Windows path")
    return str(path)


def _tuning(value: object) -> tuple[int, int]:
    result = _object(value, "executionTuning")
    _keys(result, {"textDetLimitSideLen", "textBatchSize"})
    limit = _integer(result["textDetLimitSideLen"], "executionTuning.textDetLimitSideLen", minimum=1920)
    if limit > 3840 or limit % 32:
        raise OcrPayloadError("executionTuning.textDetLimitSideLen is invalid")
    batch = _integer(result["textBatchSize"], "executionTuning.textBatchSize", minimum=1)
    if batch > 8:
        raise OcrPayloadError("executionTuning.textBatchSize is invalid")
    return limit, batch


def _inference(value: object, *, text_det_limit_side_len: int | None = None) -> dict[str, object]:
    result = _object(value, "inference")
    _keys(result, set(INFERENCE))
    for name in ("useDocOrientationClassify", "useDocUnwarping", "useTextlineOrientation"):
        if type(result[name]) is not bool or result[name] is not INFERENCE[name]:
            raise OcrPayloadError(f"inference.{name} is invalid")
    if _number(result["textRecScoreThresh"], "inference.textRecScoreThresh") != 0:
        raise OcrPayloadError("inference.textRecScoreThresh is invalid")
    limit = _integer(result["textDetLimitSideLen"], "inference.textDetLimitSideLen", minimum=1920)
    if limit > 3840 or limit % 32 or limit != (1920 if text_det_limit_side_len is None else text_det_limit_side_len):
        raise OcrPayloadError("inference.textDetLimitSideLen is invalid")
    if result["textDetLimitType"] != "max":
        raise OcrPayloadError("inference.textDetLimitType is invalid")
    return {**INFERENCE, "textDetLimitSideLen": limit}


def ensure_frame_bound(value: object, *, limit: int = MAX_FRAME_BYTES) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise OcrPayloadError("OCR protocol payload cannot be encoded") from exc
    if len(encoded) > limit:
        raise OcrPayloadError("OCR protocol payload exceeds its frame limit")


@dataclass(frozen=True)
class OcrHelloRequest:
    job_id: str
    config_hash: str
    resource_id: str
    resource_manifest_relative_path: str
    resource_fingerprint: str
    inference: dict[str, object]
    text_det_limit_side_len: int | None = None
    text_batch_size: int | None = None
    requested_device: str | None = None
    expected_runtime_id: str | None = None
    expected_runtime_fingerprint: str | None = None


@dataclass(frozen=True)
class OcrWorkItem:
    sample_id: int
    lease_id: str
    relative_image_path: str
    image_path: str
    image_size: int
    image_sha256: str


@dataclass(frozen=True)
class OcrProcessRequest:
    items: tuple[OcrWorkItem, ...]


def parse_hello(value: object) -> OcrHelloRequest:
    item = _object(value, "OCR hello request")
    _keys(
        item,
        {
            "schemaVersion", "payloadType", "jobId", "configHash", "resourceId",
            "resourceManifestRelativePath", "resourceFingerprint", "inference",
        }, {"requestedDevice", "expectedRuntimeId", "expectedRuntimeFingerprint", "executionTuning"},
    )
    if item["schemaVersion"] != SCHEMA_VERSION or item["payloadType"] != "ocr_hello_request":
        raise OcrPayloadError("OCR hello request identity is invalid")
    if item["resourceId"] != RESOURCE_ID:
        raise OcrPayloadError("OCR hello resourceId is invalid")
    device_fields = {"requestedDevice", "expectedRuntimeId", "expectedRuntimeFingerprint"}
    present = device_fields & set(item)
    if present and present != device_fields:
        raise OcrPayloadError("OCR hello request device fields must be complete")
    requested_device = _requested_device(item["requestedDevice"]) if present else None
    expected_runtime_id = _runtime_id(item["expectedRuntimeId"], "expectedRuntimeId") if present else None
    if (
        (requested_device == "cuda" and expected_runtime_id != "ocr-paddle-gpu")
        or (requested_device == "cpu" and expected_runtime_id != "ocr-paddle")
    ):
        raise OcrPayloadError("OCR hello request device and runtime are incompatible")
    tuning = _tuning(item["executionTuning"]) if "executionTuning" in item else None
    result = OcrHelloRequest(
        job_id=_identifier(item["jobId"], "jobId"),
        config_hash=_sha256(item["configHash"], "configHash"),
        resource_id=RESOURCE_ID,
        resource_manifest_relative_path=_relative(item["resourceManifestRelativePath"], "resourceManifestRelativePath"),
        resource_fingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
        inference=_inference(item["inference"], text_det_limit_side_len=tuning[0] if tuning else None),
        text_det_limit_side_len=tuning[0] if tuning else None,
        text_batch_size=tuning[1] if tuning else None,
        requested_device=requested_device,
        expected_runtime_id=expected_runtime_id,
        expected_runtime_fingerprint=_sha256(item["expectedRuntimeFingerprint"], "expectedRuntimeFingerprint") if present else None,
    )
    ensure_frame_bound(hello_to_dict(result))
    return result


def hello_to_dict(value: OcrHelloRequest) -> dict[str, object]:
    result: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "payloadType": "ocr_hello_request",
        "jobId": value.job_id,
        "configHash": value.config_hash,
        "resourceId": value.resource_id,
        "resourceManifestRelativePath": value.resource_manifest_relative_path,
        "resourceFingerprint": value.resource_fingerprint,
        "inference": dict(value.inference),
    }
    if value.requested_device is not None:
        result.update({
            "requestedDevice": value.requested_device,
            "expectedRuntimeId": value.expected_runtime_id,
            "expectedRuntimeFingerprint": value.expected_runtime_fingerprint,
        })
    if value.text_det_limit_side_len is not None:
        result["executionTuning"] = {
            "textDetLimitSideLen": value.text_det_limit_side_len,
            "textBatchSize": value.text_batch_size,
        }
    return result


def _work_item(value: object) -> OcrWorkItem:
    item = _object(value, "OCR work item")
    _keys(item, {"schemaVersion", "sampleId", "leaseId", "relativeImagePath", "imagePath", "imageSize", "imageSha256"})
    if item["schemaVersion"] != SCHEMA_VERSION:
        raise OcrPayloadError("OCR work item schemaVersion is invalid")
    return OcrWorkItem(
        sample_id=_integer(item["sampleId"], "sampleId", minimum=1),
        lease_id=_identifier(item["leaseId"], "leaseId"),
        relative_image_path=_relative(item["relativeImagePath"], "relativeImagePath", extension_required=True),
        image_path=_absolute_image_path(item["imagePath"]),
        image_size=_integer(item["imageSize"], "imageSize", minimum=1),
        image_sha256=_sha256(item["imageSha256"], "imageSha256"),
    )


def parse_process(value: object) -> OcrProcessRequest:
    item = _object(value, "OCR process request")
    _keys(item, {"schemaVersion", "payloadType", "items"})
    raw_items = item["items"]
    if (
        item["schemaVersion"] != SCHEMA_VERSION
        or item["payloadType"] != "ocr_process_request"
        or not isinstance(raw_items, list)
        or not 1 <= len(raw_items) <= MAX_PROCESS_ITEMS
    ):
        raise OcrPayloadError("OCR process request is invalid")
    items = tuple(_work_item(raw) for raw in raw_items)
    if len({(entry.sample_id, entry.lease_id) for entry in items}) != len(items):
        raise OcrPayloadError("OCR process request has duplicate sample/lease identities")
    result = OcrProcessRequest(items)
    ensure_frame_bound(process_to_dict(result))
    return result


def work_item_to_dict(value: OcrWorkItem) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sampleId": value.sample_id,
        "leaseId": value.lease_id,
        "relativeImagePath": value.relative_image_path,
        "imagePath": value.image_path,
        "imageSize": value.image_size,
        "imageSha256": value.image_sha256,
    }


def process_to_dict(value: OcrProcessRequest) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "payloadType": "ocr_process_request",
        "items": [work_item_to_dict(item) for item in value.items],
    }


def process_result(items: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "payloadType": "ocr_process_result",
        "items": items,
    }
    ensure_frame_bound(result)
    return result
