from __future__ import annotations

import math
import re
from pathlib import PureWindowsPath

from .formatting import CaptionFormattingError, display_tag


SCHEMA_VERSION = 1
MAX_PATH_BYTES = 16_384
MAX_CATEGORIES = 64
MAX_TRIGGER_TERMS = 64
MAX_TRIGGER_TERM_BYTES = 512
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
CATEGORY_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class CaptionPayloadError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CaptionPayloadError(f"{field} must be an object")
    return value


def _keys(value: dict[str, object], required: set[str], optional: set[str] = set()) -> None:
    if set(value) != required | (set(value) & optional):
        missing = required - set(value)
        extra = set(value) - required - optional
        if missing:
            raise CaptionPayloadError(f"missing fields: {', '.join(sorted(missing))}")
        if extra:
            raise CaptionPayloadError(f"unknown fields: {', '.join(sorted(extra))}")


def _schema(value: dict[str, object]) -> None:
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise CaptionPayloadError("caption payload schemaVersion must be 1")


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > limit:
        raise CaptionPayloadError(f"{field} is invalid")
    return value


def _id(value: object, field: str) -> str:
    item = _text(value, field, 128)
    if not IDENTIFIER.fullmatch(item):
        raise CaptionPayloadError(f"{field} is invalid")
    return item


def _category(value: object, field: str) -> str:
    item = _text(value, field, 64)
    if not CATEGORY_ID.fullmatch(item):
        raise CaptionPayloadError(f"{field} is invalid")
    return item


def _digest(value: object, field: str) -> str:
    item = _text(value, field, 64)
    if not SHA256.fullmatch(item):
        raise CaptionPayloadError(f"{field} is invalid")
    return item


def _relative(value: object, field: str) -> str:
    item = _text(value, field, MAX_PATH_BYTES).replace("/", "\\")
    path = PureWindowsPath(item)
    if path.is_absolute() or path.drive or path.root or item.startswith("\\"):
        raise CaptionPayloadError(f"{field} is not relative")
    for part in item.split("\\"):
        if not part or part in {".", ".."} or ":" in part or part.endswith((".", " ")):
            raise CaptionPayloadError(f"{field} contains an unsafe component")
        if part.split(".", 1)[0].upper() in RESERVED:
            raise CaptionPayloadError(f"{field} contains a reserved component")
    return item


def _integer(value: object, field: str, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= 9_223_372_036_854_775_807:
        raise CaptionPayloadError(f"{field} is invalid")
    return value


def _threshold(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptionPayloadError(f"{field} is invalid")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise CaptionPayloadError(f"{field} is invalid")
    return result


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise CaptionPayloadError(f"{field} is invalid")
    return value


def validate_threshold_policy(value: object) -> dict[str, object]:
    item = _object(value, "thresholdPolicy")
    mode = item.get("mode")
    if mode == "model_default":
        _keys(item, {"mode"})
        return {"mode": mode}
    if mode == "uniform":
        _keys(item, {"mode", "uniformThreshold"})
        return {"mode": mode, "uniformThreshold": _threshold(item["uniformThreshold"], "uniformThreshold")}
    if mode == "per_category":
        _keys(item, {"mode", "categoryThresholds"})
        values = _object(item["categoryThresholds"], "categoryThresholds")
        if not 1 <= len(values) <= MAX_CATEGORIES:
            raise CaptionPayloadError("categoryThresholds is invalid")
        normalized: dict[str, float] = {}
        for key in sorted(values):
            category = _category(key, "categoryThresholds key")
            normalized[category] = _threshold(values[key], category)
        return {"mode": mode, "categoryThresholds": normalized}
    raise CaptionPayloadError("threshold mode is invalid")


def validate_caption_format(value: object) -> dict[str, object]:
    item = _object(value, "captionFormat")
    required = {"replaceUnderscoresWithSpaces", "preserveEscapes", "triggersEnabled", "triggerTerms"}
    _keys(item, required)
    terms = item["triggerTerms"]
    if not isinstance(terms, list) or len(terms) > MAX_TRIGGER_TERMS:
        raise CaptionPayloadError("triggerTerms is invalid")
    display_policy = {
        "replaceUnderscoresWithSpaces": _boolean(item["replaceUnderscoresWithSpaces"], "replaceUnderscoresWithSpaces"),
        "preserveEscapes": _boolean(item["preserveEscapes"], "preserveEscapes"),
    }
    validated: list[str] = []
    for term_value in terms:
        term = _text(term_value, "trigger term", MAX_TRIGGER_TERM_BYTES)
        if term != term.strip() or any(character in term for character in ",\r\n"):
            raise CaptionPayloadError("trigger term is invalid")
        # Reject at hello what formatting would reject after every inference.
        try:
            display_tag(term, display_policy)
        except CaptionFormattingError as exc:
            raise CaptionPayloadError(f"trigger term is not representable after display: {term}") from exc
        validated.append(term)
    enabled = _boolean(item["triggersEnabled"], "triggersEnabled")
    if enabled and not validated:
        raise CaptionPayloadError("enabled triggers require a term")
    return {
        **display_policy,
        "triggersEnabled": enabled,
        "triggerTerms": validated,
    }


def validate_image_decode(value: object) -> dict[str, object]:
    item = _object(value, "imageDecode")
    required = {"extensions", "rejectMultiFrame", "applyExifTranspose", "alphaBackground"}
    _keys(item, required)
    extensions = [".jpg", ".jpeg", ".png", ".webp", ".bmp"]
    if item["extensions"] != extensions or item["rejectMultiFrame"] is not True or item["applyExifTranspose"] is not True:
        raise CaptionPayloadError("imageDecode is invalid")
    if item["alphaBackground"] != "#FFFFFF":
        raise CaptionPayloadError("imageDecode alphaBackground is invalid")
    return dict(item)


def validate_hello_payload(value: object) -> dict[str, object]:
    item = _object(value, "caption hello payload")
    _schema(item)
    required = {
        "schemaVersion", "payloadType", "jobId", "configHash", "profile", "datasetRoot",
        "resourceManifestRelativePath", "resourceFingerprint", "thresholdPolicy", "captionFormat", "imageDecode",
    }
    _keys(item, required)
    if item["payloadType"] != "caption_hello_request" or item["profile"] not in {"e621", "danbooru"}:
        raise CaptionPayloadError("caption hello identity is invalid")
    dataset_root = _text(item["datasetRoot"], "datasetRoot", MAX_PATH_BYTES)
    if not PureWindowsPath(dataset_root).is_absolute():
        raise CaptionPayloadError("datasetRoot must be absolute")
    return {
        **item,
        "jobId": _id(item["jobId"], "jobId"),
        "configHash": _digest(item["configHash"], "configHash"),
        "datasetRoot": dataset_root,
        "resourceManifestRelativePath": _relative(item["resourceManifestRelativePath"], "resourceManifestRelativePath"),
        "resourceFingerprint": _digest(item["resourceFingerprint"], "resourceFingerprint"),
        "thresholdPolicy": validate_threshold_policy(item["thresholdPolicy"]),
        "captionFormat": validate_caption_format(item["captionFormat"]),
        "imageDecode": validate_image_decode(item["imageDecode"]),
    }


def validate_work_item(value: object) -> dict[str, object]:
    item = _object(value, "caption work item")
    _schema(item)
    required = {
        "schemaVersion", "sampleId", "leaseId", "source", "relativeImagePath", "annotationKey",
        "imageFormat", "imageFrameCount", "imageSize", "imageMtimeNs",
    }
    _keys(item, required, {"imageFileId"})
    if (
        item["source"] not in {"e621", "danbooru"}
        or item["imageFrameCount"] != 1
        or item["imageFormat"] not in {"jpeg", "png", "webp", "bmp"}
    ):
        raise CaptionPayloadError("caption work item identity is invalid")
    relative_image_path = _relative(item["relativeImagePath"], "relativeImagePath")
    annotation_key = _relative(item["annotationKey"], "annotationKey")
    image_path = PureWindowsPath(relative_image_path)
    expected_suffixes = {
        "jpeg": {".jpg", ".jpeg"},
        "png": {".png"},
        "webp": {".webp"},
        "bmp": {".bmp"},
    }
    if image_path.suffix.lower() not in expected_suffixes[item["imageFormat"]]:
        raise CaptionPayloadError("imageFormat does not match image path")
    if str(image_path.with_suffix("")) != annotation_key:
        raise CaptionPayloadError("annotationKey does not match image path")
    validated = {
        **item,
        "sampleId": _integer(item["sampleId"], "sampleId", 1),
        "leaseId": _id(item["leaseId"], "leaseId"),
        "relativeImagePath": relative_image_path,
        "annotationKey": annotation_key,
        "imageSize": _integer(item["imageSize"], "imageSize", 1),
        "imageMtimeNs": _integer(item["imageMtimeNs"], "imageMtimeNs", 0),
    }
    if "imageFileId" in item:
        validated["imageFileId"] = _text(item["imageFileId"], "imageFileId", 128)
    return validated


def validate_process_payload(value: object) -> dict[str, object]:
    item = _object(value, "caption process payload")
    _schema(item)
    _keys(item, {"schemaVersion", "payloadType", "items"})
    if item["payloadType"] != "caption_process_request" or not isinstance(item["items"], list) or len(item["items"]) != 1:
        raise CaptionPayloadError("caption process v1 requires exactly one item")
    return {"schemaVersion": 1, "payloadType": "caption_process_request", "items": [validate_work_item(item["items"][0])]}
