from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal

from .normalizer import CaptionDisplayPolicy


MAX_BATCH_SIZE = 500
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExportProtocolError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict): raise ExportProtocolError(f"{field} must be an object")
    return value


def _exact(value: dict[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected: raise ExportProtocolError(f"{field} fields are invalid")


def _string(value: object, field: str, *, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise ExportProtocolError(f"{field} is invalid")
    return value


def _identifier(value: object, field: str) -> str:
    value = _string(value, field, maximum=128)
    if not _IDENTIFIER.fullmatch(value): raise ExportProtocolError(f"{field} is invalid")
    return value


def _path(value: object, field: str, *, absolute: bool) -> str:
    value = _string(value, field)
    path = PureWindowsPath(value.replace("/", "\\"))
    invalid = (not path.is_absolute()) if absolute else (path.is_absolute() or path.drive or any(part in {"", ".", ".."} for part in path.parts))
    if invalid: raise ExportProtocolError(f"{field} is invalid")
    return str(path)


@dataclass(frozen=True)
class ExportHelloV1:
    job_id: str; config_hash: str; dataset_root: str; overlay_root: str
    format: Literal["json", "flat_txt", "both"]; caption_policy: CaptionDisplayPolicy


@dataclass(frozen=True)
class ExportWorkItemV1:
    sample_id: int; lease_id: str; relative_image_path: str; annotation_key: str


def parse_hello(value: object) -> ExportHelloV1:
    item = _object(value, "export hello")
    _exact(item, {"schemaVersion", "payloadType", "jobId", "configHash", "datasetRoot", "overlayRoot", "format", "captionFormat"}, "export hello")
    if item["schemaVersion"] != 1 or item["payloadType"] != "export_hello_request": raise ExportProtocolError("export hello identity is invalid")
    config_hash = _string(item["configHash"], "configHash", maximum=64)
    if not _SHA256.fullmatch(config_hash): raise ExportProtocolError("configHash is invalid")
    format_value = item["format"]
    if format_value not in {"json", "flat_txt", "both"}: raise ExportProtocolError("format is invalid")
    return ExportHelloV1(_identifier(item["jobId"], "jobId"), config_hash, _path(item["datasetRoot"], "datasetRoot", absolute=True), _path(item["overlayRoot"], "overlayRoot", absolute=True), format_value, CaptionDisplayPolicy.from_mapping(_object(item["captionFormat"], "captionFormat")))


def parse_process(value: object) -> tuple[ExportWorkItemV1, ...]:
    item = _object(value, "export process")
    _exact(item, {"schemaVersion", "payloadType", "items"}, "export process")
    if item["schemaVersion"] != 1 or item["payloadType"] != "export_process_request" or not isinstance(item["items"], list) or not 1 <= len(item["items"]) <= MAX_BATCH_SIZE:
        raise ExportProtocolError("export process identity is invalid")
    results: list[ExportWorkItemV1] = []; seen: set[tuple[int, str]] = set()
    for index, raw in enumerate(item["items"]):
        work = _object(raw, f"items[{index}]")
        _exact(work, {"schemaVersion", "sampleId", "leaseId", "relativeImagePath", "annotationKey"}, f"items[{index}]")
        if work["schemaVersion"] != 1 or type(work["sampleId"]) is not int or work["sampleId"] < 1: raise ExportProtocolError("work identity is invalid")
        result = ExportWorkItemV1(work["sampleId"], _identifier(work["leaseId"], "leaseId"), _path(work["relativeImagePath"], "relativeImagePath", absolute=False), _path(work["annotationKey"], "annotationKey", absolute=False))
        if (result.sample_id, result.lease_id) in seen: raise ExportProtocolError("duplicate export work identity")
        seen.add((result.sample_id, result.lease_id)); results.append(result)
    return tuple(results)
