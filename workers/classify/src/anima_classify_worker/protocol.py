from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Mapping


SCHEMA_VERSION = 1
WIKI_DATA_SOURCE_ID = "e621-wiki-count-20260724-v1"
MAX_TXT_BYTES = 262_144
MAX_PATH_BYTES = 16_384
MAX_PROCESS_ITEMS = 500
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ClassifyPayloadError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ClassifyPayloadError(f"{field} must be an object")
    return value


def _keys(value: Mapping[str, object], required: set[str], optional: set[str] = set()) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ClassifyPayloadError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ClassifyPayloadError(f"unknown fields: {', '.join(sorted(extra))}")


def _schema(value: Mapping[str, object]) -> None:
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise ClassifyPayloadError("classify payload schemaVersion must be 1")


def _text(value: object, field: str, max_bytes: int, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ClassifyPayloadError(f"{field} must be a string")
    if not allow_blank and not value:
        raise ClassifyPayloadError(f"{field} must not be empty")
    if len(value.encode("utf-8")) > max_bytes or "\x00" in value:
        raise ClassifyPayloadError(f"{field} exceeds its limit or contains NUL")
    return value


def _id(value: object, field: str) -> str:
    result = _text(value, field, 128)
    if not IDENTIFIER.fullmatch(result):
        raise ClassifyPayloadError(f"{field} is invalid")
    return result


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ClassifyPayloadError(f"{field} must be a lowercase SHA-256")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ClassifyPayloadError(f"{field} must be boolean")
    return value


def _integer(value: object, field: str, minimum: int) -> int:
    if type(value) is not int or value < minimum or value > 1_000_000_000:
        raise ClassifyPayloadError(f"{field} is invalid")
    return value


def _relative(value: object, field: str) -> str:
    raw = _text(value, field, MAX_PATH_BYTES).replace("/", "\\")
    path = PureWindowsPath(raw)
    if path.is_absolute() or path.drive or path.root or any(part in {"", ".", ".."} for part in path.parts):
        raise ClassifyPayloadError(f"{field} is unsafe")
    return str(path)


def _original_count(value: object) -> str | int | None:
    if value is None:
        return None
    if type(value) is int:
        if not -1_000_000_000 <= value <= 1_000_000_000:
            raise ClassifyPayloadError("originalCount integer is out of range")
        return value
    if isinstance(value, str):
        return _text(value, "originalCount", 1_024, allow_blank=True)
    raise ClassifyPayloadError("originalCount must be null, a string, or an integer")


def validate_caption_format(value: object) -> dict[str, object]:
    item = _object(value, "captionFormat")
    required = {"replaceUnderscoresWithSpaces", "preserveEscapes", "triggersEnabled", "triggerTerms"}
    _keys(item, required)
    terms = item["triggerTerms"]
    if not isinstance(terms, list) or len(terms) > 64:
        raise ClassifyPayloadError("triggerTerms is invalid")
    validated: list[str] = []
    for raw in terms:
        term = _text(raw, "trigger term", 512)
        if term != term.strip() or any(character in term for character in ",\r\n"):
            raise ClassifyPayloadError("trigger term is invalid")
        validated.append(term)
    enabled = _boolean(item["triggersEnabled"], "triggersEnabled")
    if enabled and not validated:
        raise ClassifyPayloadError("enabled triggers require at least one term")
    return {
        "replaceUnderscoresWithSpaces": _boolean(
            item["replaceUnderscoresWithSpaces"], "replaceUnderscoresWithSpaces"
        ),
        "preserveEscapes": _boolean(item["preserveEscapes"], "preserveEscapes"),
        "triggersEnabled": enabled,
        "triggerTerms": validated,
    }


def validate_hello_payload(value: object) -> dict[str, object]:
    item = _object(value, "classify hello payload")
    _schema(item)
    required = {
        "schemaVersion", "payloadType", "jobId", "configHash", "profile", "resourceManifestRelativePath",
        "resourceFingerprint", "wikiDataSourceId", "overwriteCount", "captionFormat",
    }
    _keys(item, required)
    if (
        item["payloadType"] != "classify_hello_request"
        or item["profile"] not in {"e621", "danbooru"}
    ):
        raise ClassifyPayloadError("classify hello identity is invalid")
    return {
        **item,
        "jobId": _id(item["jobId"], "jobId"),
        "configHash": _digest(item["configHash"], "configHash"),
        "resourceManifestRelativePath": _relative(
            item["resourceManifestRelativePath"], "resourceManifestRelativePath"
        ),
        "resourceFingerprint": _digest(item["resourceFingerprint"], "resourceFingerprint"),
        "wikiDataSourceId": _id(item["wikiDataSourceId"], "wikiDataSourceId"),
        "overwriteCount": _boolean(item["overwriteCount"], "overwriteCount"),
        "captionFormat": validate_caption_format(item["captionFormat"]),
    }


def validate_work_item(value: object) -> dict[str, object]:
    item = _object(value, "classify work item")
    _schema(item)
    required = {
        "schemaVersion", "sampleId", "leaseId", "source", "relativeImagePath", "annotationKey", "txtText",
        "txtProvenance", "originalCount",
    }
    _keys(item, required)
    if item["source"] not in {"e621", "danbooru"} or item["txtProvenance"] not in {
        "missing", "original_preserved", "module1_written"
    }:
        raise ClassifyPayloadError("classify work item identity is invalid")
    relative_image_path = _relative(item["relativeImagePath"], "relativeImagePath")
    annotation_key = _relative(item["annotationKey"], "annotationKey")
    if str(PureWindowsPath(relative_image_path).with_suffix("")) != annotation_key:
        raise ClassifyPayloadError("annotationKey does not match relativeImagePath")
    text = _text(item["txtText"], "txtText", MAX_TXT_BYTES)
    if not text.strip() or text.startswith("\ufeff\ufeff"):
        raise ClassifyPayloadError("txtText must contain non-blank caption text")
    return {
        **item,
        "sampleId": _integer(item["sampleId"], "sampleId", 1),
        "leaseId": _id(item["leaseId"], "leaseId"),
        "relativeImagePath": relative_image_path,
        "annotationKey": annotation_key,
        "txtText": text,
        "originalCount": _original_count(item["originalCount"]),
    }


def validate_process_payload(value: object) -> dict[str, object]:
    item = _object(value, "classify process payload")
    _schema(item)
    _keys(item, {"schemaVersion", "payloadType", "items"})
    items = item["items"]
    if (
        item["payloadType"] != "classify_process_request"
        or not isinstance(items, list)
        or not 1 <= len(items) <= MAX_PROCESS_ITEMS
    ):
        raise ClassifyPayloadError(f"classify process v1 requires 1..{MAX_PROCESS_ITEMS} items")
    parsed = [validate_work_item(value) for value in items]
    if len({(int(value["sampleId"]), str(value["leaseId"])) for value in parsed}) != len(parsed):
        raise ClassifyPayloadError("classify process items contain duplicate sampleId and leaseId")
    return {"schemaVersion": 1, "payloadType": "classify_process_request", "items": parsed}
