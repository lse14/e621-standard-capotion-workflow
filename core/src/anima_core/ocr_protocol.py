from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal

from .ocr_sidecar import (
    ABSOLUTE_WINDOWS_PATH,
    FIXED_OCR_INFERENCE_SETTINGS,
    MAX_OCR_IMAGE_PIXELS,
    MAX_OCR_IMAGE_SIDE,
)
from .worker_protocol import MAX_FRAME_BYTES


OCR_PROTOCOL_SCHEMA_VERSION = 1
MAX_PATH_BYTES = 16_384
MAX_TEXT_BYTES = 16_384
MAX_PROCESS_ITEMS = 1_024
MAX_ERROR_MESSAGE_BYTES = 1_024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_GEOMETRY_TOLERANCE = 1e-6


class OcrProtocolError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OcrProtocolError(f"{field} must be an object")
    return value


def _keys(value: dict[str, object], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise OcrProtocolError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise OcrProtocolError(f"unknown fields: {', '.join(sorted(extra))}")


def _utf8(value: object, field: str, maximum: int, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str) or (not allow_blank and not value):
        raise OcrProtocolError(f"{field} is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise OcrProtocolError(f"{field} is not valid UTF-8") from exc
    if size > maximum:
        raise OcrProtocolError(f"{field} exceeds its byte limit")
    return value


def _identifier(value: object, field: str) -> str:
    result = _utf8(value, field, 128)
    if not IDENTIFIER.fullmatch(result):
        raise OcrProtocolError(f"{field} is invalid")
    return result


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise OcrProtocolError(f"{field} must be a lowercase SHA-256")
    return value


def _resource_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not RESOURCE_ID.fullmatch(value):
        raise OcrProtocolError(f"{field} is invalid")
    return value


def _requested_device(value: object, field: str = "requestedDevice") -> Literal["auto", "cuda", "cpu"]:
    if value not in {"auto", "cuda", "cpu"}:
        raise OcrProtocolError(f"{field} is invalid")
    return value  # type: ignore[return-value]


def _runtime_id(value: object, field: str) -> Literal["ocr-paddle", "ocr-paddle-gpu"]:
    if value not in {"ocr-paddle", "ocr-paddle-gpu"}:
        raise OcrProtocolError(f"{field} is invalid")
    return value  # type: ignore[return-value]


def _number(value: object, field: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OcrProtocolError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise OcrProtocolError(f"{field} must be a finite number")
    if minimum is not None and result < minimum:
        raise OcrProtocolError(f"{field} is below its minimum")
    if maximum is not None and result > maximum:
        raise OcrProtocolError(f"{field} exceeds its maximum")
    return result


def _integer(value: object, field: str, *, minimum: int = 0, maximum: int = 9_223_372_036_854_775_807) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise OcrProtocolError(f"{field} is invalid")
    return value


def _relative(value: object, field: str, *, extension_required: bool = False) -> str:
    text = _utf8(value, field, MAX_PATH_BYTES)
    normalized = text.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or normalized.startswith("\\"):
        raise OcrProtocolError(f"{field} must be relative")
    if extension_required and not path.suffix:
        raise OcrProtocolError(f"{field} must include an extension")
    for component in normalized.split("\\"):
        if not component or component in {".", ".."} or ":" in component or component.endswith((".", " ")):
            raise OcrProtocolError(f"{field} contains an unsafe component")
        if component.split(".", 1)[0].upper() in _RESERVED:
            raise OcrProtocolError(f"{field} contains a reserved component")
    return normalized


def _absolute_image_path(value: object) -> str:
    text = _utf8(value, "imagePath", MAX_PATH_BYTES)
    path = PureWindowsPath(text)
    if not path.is_absolute() or not path.drive or not path.root:
        raise OcrProtocolError("imagePath must be an absolute Windows path")
    if any(component in {".", ".."} for component in path.parts):
        raise OcrProtocolError("imagePath contains traversal")
    return str(path)


def _clean_text(value: object) -> str:
    text = _utf8(value, "OCR item text", MAX_TEXT_BYTES)
    cleaned = "".join(character for character in text if unicodedata.category(character) != "Cc")
    _utf8(cleaned, "OCR item text", MAX_TEXT_BYTES)
    if not cleaned.strip():
        raise OcrProtocolError("OCR item text is blank after control-character removal")
    return cleaned


def _tuning(value: object) -> tuple[int, int]:
    item = _object(value, "executionTuning")
    _keys(item, {"textDetLimitSideLen", "textBatchSize"})
    limit = _integer(item["textDetLimitSideLen"], "executionTuning.textDetLimitSideLen", minimum=1920, maximum=3840)
    if limit % 32:
        raise OcrProtocolError("executionTuning.textDetLimitSideLen is invalid")
    batch = _integer(item["textBatchSize"], "executionTuning.textBatchSize", minimum=1, maximum=8)
    return limit, batch


def _inference(value: object, *, text_det_limit_side_len: int | None = None) -> dict[str, object]:
    item = _object(value, "inference")
    _keys(item, set(FIXED_OCR_INFERENCE_SETTINGS))
    for name in ("useDocOrientationClassify", "useDocUnwarping", "useTextlineOrientation"):
        if type(item[name]) is not bool or item[name] is not FIXED_OCR_INFERENCE_SETTINGS[name]:
            raise OcrProtocolError(f"inference.{name} is invalid")
    if _number(item["textRecScoreThresh"], "inference.textRecScoreThresh") != 0:
        raise OcrProtocolError("inference.textRecScoreThresh is invalid")
    limit = _integer(item["textDetLimitSideLen"], "inference.textDetLimitSideLen", minimum=1920, maximum=3840)
    if limit % 32 or limit != (1920 if text_det_limit_side_len is None else text_det_limit_side_len):
        raise OcrProtocolError("inference.textDetLimitSideLen is invalid")
    if type(item["textDetLimitType"]) is not str or item["textDetLimitType"] != "max":
        raise OcrProtocolError("inference.textDetLimitType is invalid")
    return {**FIXED_OCR_INFERENCE_SETTINGS, "textDetLimitSideLen": limit}


def _frame_bound(value: dict[str, object]) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise OcrProtocolError("OCR protocol payload cannot be encoded") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise OcrProtocolError("OCR protocol payload exceeds 1 MiB")


@dataclass(frozen=True)
class OcrHelloRequestV1:
    jobId: str
    configHash: str
    resourceId: str
    resourceManifestRelativePath: str
    resourceFingerprint: str
    inference: dict[str, object]
    textDetLimitSideLen: int | None = None
    textBatchSize: int | None = None
    requestedDevice: Literal["auto", "cuda", "cpu"] | None = None
    expectedRuntimeId: Literal["ocr-paddle", "ocr-paddle-gpu"] | None = None
    expectedRuntimeFingerprint: str | None = None
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "OcrHelloRequestV1":
        item = _object(value, "OCR hello request")
        _keys(
            item,
            {
                "schemaVersion",
                "payloadType",
                "jobId",
                "configHash",
                "resourceId",
                "resourceManifestRelativePath",
                "resourceFingerprint",
                "inference",
            }, {"requestedDevice", "expectedRuntimeId", "expectedRuntimeFingerprint", "executionTuning"},
        )
        if item["schemaVersion"] != OCR_PROTOCOL_SCHEMA_VERSION or item["payloadType"] != "ocr_hello_request":
            raise OcrProtocolError("OCR hello request identity is invalid")
        device_fields = {"requestedDevice", "expectedRuntimeId", "expectedRuntimeFingerprint"}
        present = device_fields & set(item)
        if present and present != device_fields:
            raise OcrProtocolError("OCR hello request device fields must be complete")
        requested_device = _requested_device(item["requestedDevice"]) if present else None
        expected_runtime_id = _runtime_id(item["expectedRuntimeId"], "expectedRuntimeId") if present else None
        if (
            (requested_device == "cuda" and expected_runtime_id != "ocr-paddle-gpu")
            or (requested_device == "cpu" and expected_runtime_id != "ocr-paddle")
        ):
            raise OcrProtocolError("OCR hello request device and runtime are incompatible")
        tuning = _tuning(item["executionTuning"]) if "executionTuning" in item else None
        result = cls(
            jobId=_identifier(item["jobId"], "jobId"),
            configHash=_sha256(item["configHash"], "configHash"),
            resourceId=_resource_id(item["resourceId"], "resourceId"),
            resourceManifestRelativePath=_relative(
                item["resourceManifestRelativePath"], "resourceManifestRelativePath"
            ),
            resourceFingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
            inference=_inference(item["inference"], text_det_limit_side_len=tuning[0] if tuning else None),
            textDetLimitSideLen=tuning[0] if tuning else None,
            textBatchSize=tuning[1] if tuning else None,
            requestedDevice=requested_device,
            expectedRuntimeId=expected_runtime_id,
            expectedRuntimeFingerprint=_sha256(item["expectedRuntimeFingerprint"], "expectedRuntimeFingerprint") if present else None,
        )
        _frame_bound(result.to_dict())
        return result

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schemaVersion,
            "payloadType": "ocr_hello_request",
            "jobId": self.jobId,
            "configHash": self.configHash,
            "resourceId": self.resourceId,
            "resourceManifestRelativePath": self.resourceManifestRelativePath,
            "resourceFingerprint": self.resourceFingerprint,
            "inference": dict(self.inference),
        }
        if self.requestedDevice is not None:
            result.update({
                "requestedDevice": self.requestedDevice,
                "expectedRuntimeId": self.expectedRuntimeId,
                "expectedRuntimeFingerprint": self.expectedRuntimeFingerprint,
            })
        if self.textDetLimitSideLen is not None:
            result["executionTuning"] = {
                "textDetLimitSideLen": self.textDetLimitSideLen,
                "textBatchSize": self.textBatchSize,
            }
        return result


@dataclass(frozen=True)
class OcrHelloResultV1:
    executable: str
    resourceFingerprint: str
    requestedDevice: Literal["auto", "cuda", "cpu"] | None = None
    observedDevice: Literal["cpu", "cuda"] | None = None
    runtimeId: Literal["ocr-paddle", "ocr-paddle-gpu"] | None = None
    runtimeFingerprint: str | None = None
    paddleVersion: str | None = None
    compiledWithCuda: bool | None = None
    cudaVersion: str | None = None
    gpuName: str | None = None
    totalVramBytes: int | None = None
    pythonVersion: Literal["3.11.15"] = "3.11.15"
    modelSessionLoads: Literal[1] = 1
    ready: Literal[True] = True
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "OcrHelloResultV1":
        item = _object(value, "OCR hello result")
        _keys(
            item,
            {"schemaVersion", "payloadType", "ready", "executable", "pythonVersion", "modelSessionLoads", "resourceFingerprint"},
            {"requestedDevice", "observedDevice", "runtimeId", "runtimeFingerprint", "paddleVersion", "compiledWithCuda", "cudaVersion", "gpuName", "totalVramBytes"},
        )
        if (
            item["schemaVersion"] != OCR_PROTOCOL_SCHEMA_VERSION
            or item["payloadType"] != "ocr_hello_result"
            or item["ready"] is not True
            or item["pythonVersion"] != "3.11.15"
            or item["modelSessionLoads"] != 1
        ):
            raise OcrProtocolError("OCR hello result identity is invalid")
        device_fields = {"requestedDevice", "observedDevice", "runtimeId", "runtimeFingerprint", "paddleVersion", "compiledWithCuda", "cudaVersion", "gpuName", "totalVramBytes"}
        present = device_fields & set(item)
        if present and present != device_fields:
            raise OcrProtocolError("OCR hello result device fields must be complete")
        if present:
            requested = _requested_device(item["requestedDevice"])
            observed = item["observedDevice"]
            if observed not in {"cpu", "cuda"}:
                raise OcrProtocolError("observedDevice is invalid")
            runtime_id = _runtime_id(item["runtimeId"], "runtimeId")
            runtime_fingerprint = _sha256(item["runtimeFingerprint"], "runtimeFingerprint")
            paddle_version = _utf8(item["paddleVersion"], "paddleVersion", 128)
            compiled = item["compiledWithCuda"]
            if type(compiled) is not bool:
                raise OcrProtocolError("compiledWithCuda must be boolean")
            cuda_version = item["cudaVersion"]
            gpu_name = item["gpuName"]
            total_vram = item["totalVramBytes"]
            if total_vram is not None:
                total_vram = _integer(total_vram, "totalVramBytes", minimum=0)
            if observed == "cpu":
                if runtime_id != "ocr-paddle" or compiled is not False or cuda_version is not None or gpu_name is not None or total_vram is not None:
                    raise OcrProtocolError("CPU OCR hello device evidence is invalid")
            elif (
                runtime_id != "ocr-paddle-gpu" or compiled is not True
                or not isinstance(cuda_version, str) or not cuda_version
                or not isinstance(gpu_name, str) or not gpu_name
            ):
                raise OcrProtocolError("CUDA OCR hello device evidence is invalid")
            if observed == "cuda":
                _utf8(cuda_version, "cudaVersion", 128)
                _utf8(gpu_name, "gpuName", 256)
        else:
            requested = observed = runtime_id = runtime_fingerprint = paddle_version = compiled = cuda_version = gpu_name = total_vram = None
        result = cls(
            executable=_utf8(item["executable"], "executable", MAX_PATH_BYTES),
            resourceFingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
            requestedDevice=requested,
            observedDevice=observed,
            runtimeId=runtime_id,
            runtimeFingerprint=runtime_fingerprint,
            paddleVersion=paddle_version,
            compiledWithCuda=compiled,
            cudaVersion=cuda_version,
            gpuName=gpu_name,
            totalVramBytes=total_vram,
        )
        _frame_bound(result.to_dict())
        return result

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schemaVersion,
            "payloadType": "ocr_hello_result",
            "ready": self.ready,
            "executable": self.executable,
            "pythonVersion": self.pythonVersion,
            "modelSessionLoads": self.modelSessionLoads,
            "resourceFingerprint": self.resourceFingerprint,
        }
        if self.requestedDevice is not None:
            result.update({
                "requestedDevice": self.requestedDevice,
                "observedDevice": self.observedDevice,
                "runtimeId": self.runtimeId,
                "runtimeFingerprint": self.runtimeFingerprint,
                "paddleVersion": self.paddleVersion,
                "compiledWithCuda": self.compiledWithCuda,
                "cudaVersion": self.cudaVersion,
                "gpuName": self.gpuName,
                "totalVramBytes": self.totalVramBytes,
            })
        return result


def validate_hello_result(result: OcrHelloResultV1, request: OcrHelloRequestV1) -> None:
    if not isinstance(result, OcrHelloResultV1) or not isinstance(request, OcrHelloRequestV1):
        raise OcrProtocolError("OCR hello values are invalid")
    if result.resourceFingerprint != request.resourceFingerprint:
        raise OcrProtocolError("OCR hello resource fingerprint does not match the frozen request")
    if request.requestedDevice is not None and (
        result.requestedDevice != request.requestedDevice
        or result.runtimeId != request.expectedRuntimeId
        or result.runtimeFingerprint != request.expectedRuntimeFingerprint
    ):
        raise OcrProtocolError("OCR hello runtime evidence does not match the frozen request")


@dataclass(frozen=True)
class OcrWorkItemV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    imagePath: str
    imageSize: int
    imageSha256: str
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "OcrWorkItemV1":
        item = _object(value, "OCR work item")
        _keys(item, {"schemaVersion", "sampleId", "leaseId", "relativeImagePath", "imagePath", "imageSize", "imageSha256"})
        if item["schemaVersion"] != OCR_PROTOCOL_SCHEMA_VERSION:
            raise OcrProtocolError("OCR work item schemaVersion is invalid")
        return cls(
            sampleId=_integer(item["sampleId"], "sampleId", minimum=1),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=_relative(item["relativeImagePath"], "relativeImagePath", extension_required=True),
            imagePath=_absolute_image_path(item["imagePath"]),
            imageSize=_integer(item["imageSize"], "imageSize", minimum=1),
            imageSha256=_sha256(item["imageSha256"], "imageSha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "sampleId": self.sampleId,
            "leaseId": self.leaseId,
            "relativeImagePath": self.relativeImagePath,
            "imagePath": self.imagePath,
            "imageSize": self.imageSize,
            "imageSha256": self.imageSha256,
        }


@dataclass(frozen=True)
class OcrProcessRequestV1:
    items: tuple[OcrWorkItemV1, ...]
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "OcrProcessRequestV1":
        item = _object(value, "OCR process request")
        _keys(item, {"schemaVersion", "payloadType", "items"})
        values = item["items"]
        if (
            item["schemaVersion"] != OCR_PROTOCOL_SCHEMA_VERSION
            or item["payloadType"] != "ocr_process_request"
            or not isinstance(values, list)
            or not 1 <= len(values) <= MAX_PROCESS_ITEMS
        ):
            raise OcrProtocolError("OCR process request is invalid")
        parsed = tuple(OcrWorkItemV1.from_dict(entry) for entry in values)
        if len({(entry.sampleId, entry.leaseId) for entry in parsed}) != len(parsed):
            raise OcrProtocolError("OCR process request has duplicate sample/lease identities")
        result = cls(items=parsed)
        _frame_bound(result.to_dict())
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "payloadType": "ocr_process_request",
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class OcrResultImageV1:
    width: int
    height: int
    sizeBytes: int
    sha256: str

    @classmethod
    def from_dict(cls, value: object) -> "OcrResultImageV1":
        item = _object(value, "OCR outcome image")
        _keys(item, {"width", "height", "sizeBytes", "sha256"})
        return cls(
            width=_integer(item["width"], "image.width", minimum=1, maximum=1_000_000),
            height=_integer(item["height"], "image.height", minimum=1, maximum=1_000_000),
            sizeBytes=_integer(item["sizeBytes"], "image.sizeBytes", minimum=1),
            sha256=_sha256(item["sha256"], "image.sha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"width": self.width, "height": self.height, "sizeBytes": self.sizeBytes, "sha256": self.sha256}


def _image_exceeds_limits(image: OcrResultImageV1) -> bool:
    return (
        image.width * image.height > MAX_OCR_IMAGE_PIXELS
        or image.width > MAX_OCR_IMAGE_SIDE
        or image.height > MAX_OCR_IMAGE_SIDE
    )


def _coordinates(value: object, field: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise OcrProtocolError(f"{field} must contain four coordinates")
    return tuple(_number(entry, f"{field}[{index}]") for index, entry in enumerate(value))  # type: ignore[return-value]


def _polygon(value: object, field: str) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise OcrProtocolError(f"{field} must contain four points")
    points: list[tuple[float, float]] = []
    for index, point in enumerate(value):
        if not isinstance(point, list) or len(point) != 2:
            raise OcrProtocolError(f"{field}[{index}] must be a coordinate pair")
        points.append((_number(point[0], f"{field}[{index}][0]"), _number(point[1], f"{field}[{index}][1]")))
    return tuple(points)  # type: ignore[return-value]


@dataclass(frozen=True)
class OcrRawItemV1:
    text: str
    confidence: float
    polygonPixels: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
    bboxPixels: tuple[float, float, float, float]
    textlineOrientationDegrees: float

    @classmethod
    def from_dict(cls, value: object) -> "OcrRawItemV1":
        item = _object(value, "OCR raw item")
        _keys(item, {"text", "confidence", "polygonPixels", "bboxPixels", "textlineOrientationDegrees"})
        return cls(
            text=_clean_text(item["text"]),
            confidence=_number(item["confidence"], "confidence", minimum=0, maximum=1),
            polygonPixels=_polygon(item["polygonPixels"], "polygonPixels"),
            bboxPixels=_coordinates(item["bboxPixels"], "bboxPixels"),
            textlineOrientationDegrees=_number(item["textlineOrientationDegrees"], "textlineOrientationDegrees"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "polygonPixels": [list(point) for point in self.polygonPixels],
            "bboxPixels": list(self.bboxPixels),
            "textlineOrientationDegrees": self.textlineOrientationDegrees,
        }

    def validate_geometry(self, image: OcrResultImageV1) -> None:
        left, top, right, bottom = self.bboxPixels
        if not 0 <= left < right <= image.width or not 0 <= top < bottom <= image.height:
            raise OcrProtocolError("OCR raw bbox is outside the image")
        for x, y in self.polygonPixels:
            if not 0 <= x <= image.width or not 0 <= y <= image.height:
                raise OcrProtocolError("OCR raw polygon is outside the image")
            if x < left - _GEOMETRY_TOLERANCE or x > right + _GEOMETRY_TOLERANCE:
                raise OcrProtocolError("OCR raw polygon escapes its bbox")
            if y < top - _GEOMETRY_TOLERANCE or y > bottom + _GEOMETRY_TOLERANCE:
                raise OcrProtocolError("OCR raw polygon escapes its bbox")


@dataclass(frozen=True)
class OcrInferenceFailureV1:
    code: Literal["ocr_inference_failed"]
    message: str
    retriable: Literal[True] = True

    @classmethod
    def from_dict(cls, value: object) -> "OcrInferenceFailureV1":
        item = _object(value, "OCR inference failure")
        _keys(item, {"code", "message", "retriable"})
        message = _utf8(item["message"], "error.message", MAX_ERROR_MESSAGE_BYTES)
        if (
            item["code"] != "ocr_inference_failed"
            or item["retriable"] is not True
            or not message.strip()
            or any(unicodedata.category(character) == "Cc" for character in message)
            or ABSOLUTE_WINDOWS_PATH.search(message)
        ):
            raise OcrProtocolError("OCR inference failure is invalid")
        return cls(code="ocr_inference_failed", message=message)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retriable": self.retriable}


@dataclass(frozen=True)
class OcrImageTooLargeDetailsV1:
    actualPixels: int
    maxPixels: Literal[40_000_000] = 40_000_000
    maxSide: Literal[16_384] = 16_384

    @classmethod
    def from_dict(cls, value: object, *, image: OcrResultImageV1) -> "OcrImageTooLargeDetailsV1":
        item = _object(value, "OCR image-too-large details")
        _keys(item, {"actualPixels", "maxPixels", "maxSide"})
        actual_pixels = _integer(item["actualPixels"], "error.details.actualPixels")
        max_pixels = _integer(item["maxPixels"], "error.details.maxPixels")
        max_side = _integer(item["maxSide"], "error.details.maxSide")
        if actual_pixels != image.width * image.height:
            raise OcrProtocolError("error.details.actualPixels does not match image dimensions")
        if max_pixels != MAX_OCR_IMAGE_PIXELS or max_side != MAX_OCR_IMAGE_SIDE:
            raise OcrProtocolError("OCR image-too-large safety limits are invalid")
        if (
            actual_pixels <= MAX_OCR_IMAGE_PIXELS
            and image.width <= MAX_OCR_IMAGE_SIDE
            and image.height <= MAX_OCR_IMAGE_SIDE
        ):
            raise OcrProtocolError("ocr_image_too_large requires an oversize image")
        return cls(actualPixels=actual_pixels)

    def to_dict(self) -> dict[str, object]:
        return {
            "actualPixels": self.actualPixels,
            "maxPixels": self.maxPixels,
            "maxSide": self.maxSide,
        }


@dataclass(frozen=True)
class OcrImageTooLargeFailureV1:
    code: Literal["ocr_image_too_large"]
    message: str
    details: OcrImageTooLargeDetailsV1
    retriable: Literal[False] = False

    @classmethod
    def from_dict(cls, value: object, *, image: OcrResultImageV1) -> "OcrImageTooLargeFailureV1":
        item = _object(value, "OCR image-too-large failure")
        _keys(item, {"code", "message", "retriable", "details"})
        message = _utf8(item["message"], "error.message", MAX_ERROR_MESSAGE_BYTES)
        if (
            item["code"] != "ocr_image_too_large"
            or item["retriable"] is not False
            or not message.strip()
            or any(unicodedata.category(character) == "Cc" for character in message)
            or ABSOLUTE_WINDOWS_PATH.search(message)
        ):
            raise OcrProtocolError("OCR image-too-large failure is invalid")
        return cls(
            code="ocr_image_too_large",
            message=message,
            details=OcrImageTooLargeDetailsV1.from_dict(item["details"], image=image),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
            "details": self.details.to_dict(),
        }


@dataclass(frozen=True)
class OcrOutcomeV1:
    status: Literal["success", "no_text", "failed"]
    sampleId: int
    leaseId: str
    relativeImagePath: str
    image: OcrResultImageV1
    items: tuple[OcrRawItemV1, ...]
    error: OcrInferenceFailureV1 | OcrImageTooLargeFailureV1 | None = None
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "OcrOutcomeV1":
        item = _object(value, "OCR outcome")
        required = {"schemaVersion", "status", "sampleId", "leaseId", "relativeImagePath", "image", "items"}
        _keys(item, required, {"error"})
        status = item["status"]
        values = item["items"]
        if item["schemaVersion"] != OCR_PROTOCOL_SCHEMA_VERSION or status not in {"success", "no_text", "failed"}:
            raise OcrProtocolError("OCR outcome identity is invalid")
        if not isinstance(values, list) or len(values) > MAX_PROCESS_ITEMS:
            raise OcrProtocolError("OCR outcome items are invalid")
        image = OcrResultImageV1.from_dict(item["image"])
        if status != "failed" and _image_exceeds_limits(image):
            raise OcrProtocolError("oversize OCR image must use a failed outcome")
        raw_items = tuple(OcrRawItemV1.from_dict(entry) for entry in values)
        for raw_item in raw_items:
            raw_item.validate_geometry(image)
        error_value = item.get("error")
        if status == "success":
            if not raw_items or error_value is not None:
                raise OcrProtocolError("successful OCR outcome must have items and no error")
            error = None
        elif status == "no_text":
            if raw_items or error_value is not None:
                raise OcrProtocolError("no_text OCR outcome must have no items or error")
            error = None
        else:
            if raw_items or error_value is None:
                raise OcrProtocolError("failed OCR outcome must have a complete error and no items")
            error_item = _object(error_value, "OCR failure")
            if error_item.get("code") == "ocr_inference_failed":
                if _image_exceeds_limits(image):
                    raise OcrProtocolError("oversize OCR image must use ocr_image_too_large")
                error = OcrInferenceFailureV1.from_dict(error_item)
            elif error_item.get("code") == "ocr_image_too_large":
                error = OcrImageTooLargeFailureV1.from_dict(error_item, image=image)
            else:
                raise OcrProtocolError("OCR failure code is invalid")
        return cls(
            status=status,  # type: ignore[arg-type]
            sampleId=_integer(item["sampleId"], "sampleId", minimum=1),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=_relative(item["relativeImagePath"], "relativeImagePath", extension_required=True),
            image=image,
            items=raw_items,
            error=error,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schemaVersion,
            "status": self.status,
            "sampleId": self.sampleId,
            "leaseId": self.leaseId,
            "relativeImagePath": self.relativeImagePath,
            "image": self.image.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result


def parse_ocr_process_result(value: object) -> tuple[OcrOutcomeV1, ...]:
    item = _object(value, "OCR process result")
    _keys(item, {"schemaVersion", "payloadType", "items"})
    values = item["items"]
    if (
        item["schemaVersion"] != OCR_PROTOCOL_SCHEMA_VERSION
        or item["payloadType"] != "ocr_process_result"
        or not isinstance(values, list)
        or not 1 <= len(values) <= MAX_PROCESS_ITEMS
    ):
        raise OcrProtocolError("OCR process result is invalid")
    parsed = tuple(OcrOutcomeV1.from_dict(entry) for entry in values)
    if len({(entry.sampleId, entry.leaseId) for entry in parsed}) != len(parsed):
        raise OcrProtocolError("OCR process result has duplicate sample/lease identities")
    _frame_bound({"schemaVersion": 1, "payloadType": "ocr_process_result", "items": [entry.to_dict() for entry in parsed]})
    return parsed


def validate_outcome_for_item(
    outcome: OcrOutcomeV1,
    item: OcrWorkItemV1,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> None:
    if not isinstance(outcome, OcrOutcomeV1) or not isinstance(item, OcrWorkItemV1):
        raise OcrProtocolError("OCR outcome or work item is invalid")
    if (
        outcome.sampleId != item.sampleId
        or outcome.leaseId != item.leaseId
        or outcome.relativeImagePath != item.relativeImagePath
        or outcome.image.sizeBytes != item.imageSize
        or outcome.image.sha256 != item.imageSha256
    ):
        raise OcrProtocolError("OCR outcome does not match the work item identity")
    if expected_width is not None and outcome.image.width != _integer(expected_width, "expected_width", minimum=1):
        raise OcrProtocolError("OCR outcome width does not match the original image")
    if expected_height is not None and outcome.image.height != _integer(expected_height, "expected_height", minimum=1):
        raise OcrProtocolError("OCR outcome height does not match the original image")


def validate_outcomes_for_items(
    outcomes: tuple[OcrOutcomeV1, ...],
    items: tuple[OcrWorkItemV1, ...],
) -> tuple[OcrOutcomeV1, ...]:
    """Require one exact `(sampleId, leaseId)` result for every requested image."""
    if len(outcomes) != len(items):
        raise OcrProtocolError("OCR process result count does not match the request")
    expected = {(item.sampleId, item.leaseId): item for item in items}
    mapped: dict[tuple[int, str], OcrOutcomeV1] = {}
    for outcome in outcomes:
        identity = (outcome.sampleId, outcome.leaseId)
        item = expected.get(identity)
        if item is None or identity in mapped:
            raise OcrProtocolError("OCR process result identities do not match the request")
        validate_outcome_for_item(outcome, item)
        mapped[identity] = outcome
    if set(mapped) != set(expected):
        raise OcrProtocolError("OCR process result identities do not match the request")
    return tuple(mapped[(item.sampleId, item.leaseId)] for item in items)
