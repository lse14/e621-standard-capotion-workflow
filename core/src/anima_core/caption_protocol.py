from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import PureWindowsPath
from typing import Literal, Mapping

from .path_safety import PathSafetyError, safe_relative_path


CAPTION_PAYLOAD_SCHEMA_VERSION = 1
MAX_CAPTION_TAGS = 1_000_000
MAX_CAPTION_CATEGORIES = 64
MAX_FORMATTED_TXT_BYTES = 262_144
MAX_PATH_BYTES = 16_384
MAX_TRIGGER_TERMS = 64
MAX_TRIGGER_TERM_BYTES = 512
MAX_CAPTION_PROCESS_ITEMS = 64
CAPTION_ISSUE_CODES = frozenset({
    "caption_image_decode_failed",
    "caption_inference_failed",
    "caption_no_tags",
})
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
CATEGORY_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CaptionProtocolError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CaptionProtocolError(f"{field} must be an object")
    return value


def _keys(value: Mapping[str, object], *, required: set[str], optional: set[str] = set()) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing:
        raise CaptionProtocolError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise CaptionProtocolError(f"unknown fields: {', '.join(sorted(extra))}")


def _schema(value: Mapping[str, object]) -> None:
    if value.get("schemaVersion") != CAPTION_PAYLOAD_SCHEMA_VERSION:
        raise CaptionProtocolError("caption payload schemaVersion must be 1")


def _string(value: object, field: str, *, max_bytes: int, nonblank: bool = True) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise CaptionProtocolError(f"{field} must be a string without NUL")
    if nonblank and not value:
        raise CaptionProtocolError(f"{field} must not be empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise CaptionProtocolError(f"{field} exceeds its UTF-8 byte limit")
    return value


def _identifier(value: object, field: str) -> str:
    item = _string(value, field, max_bytes=128)
    if not IDENTIFIER.fullmatch(item):
        raise CaptionProtocolError(f"{field} is invalid")
    return item


def _category(value: object, field: str) -> str:
    item = _string(value, field, max_bytes=64)
    if not CATEGORY_ID.fullmatch(item):
        raise CaptionProtocolError(f"{field} is invalid")
    return item


def _sha256(value: object, field: str) -> str:
    item = _string(value, field, max_bytes=64)
    if not SHA256.fullmatch(item):
        raise CaptionProtocolError(f"{field} must be a lowercase SHA-256")
    return item


def _relative(value: object, field: str) -> str:
    item = _string(value, field, max_bytes=MAX_PATH_BYTES)
    try:
        return safe_relative_path(item)
    except PathSafetyError as exc:
        raise CaptionProtocolError(f"{field} is unsafe: {exc}") from exc


def _positive_int(
    value: object,
    field: str,
    *,
    allow_zero: bool = False,
    maximum: int = 9_223_372_036_854_775_807,
) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum or value > maximum:
        raise CaptionProtocolError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _finite_threshold(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptionProtocolError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise CaptionProtocolError(f"{field} must be finite and between 0 and 1")
    return result


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise CaptionProtocolError(f"{field} must be boolean")
    return value


@dataclass(frozen=True)
class CaptionThresholdPolicyV1:
    mode: Literal["model_default", "uniform", "per_category"]
    uniformThreshold: float | None = None
    categoryThresholds: dict[str, float] | None = None

    @classmethod
    def from_dict(cls, value: object) -> "CaptionThresholdPolicyV1":
        item = _object(value, "thresholdPolicy")
        mode = item.get("mode")
        if mode == "model_default":
            _keys(item, required={"mode"})
            return cls("model_default")
        if mode == "uniform":
            _keys(item, required={"mode", "uniformThreshold"})
            return cls("uniform", uniformThreshold=_finite_threshold(item["uniformThreshold"], "uniformThreshold"))
        if mode == "per_category":
            _keys(item, required={"mode", "categoryThresholds"})
            categories = _object(item["categoryThresholds"], "categoryThresholds")
            if not 1 <= len(categories) <= MAX_CAPTION_CATEGORIES:
                raise CaptionProtocolError("categoryThresholds must contain 1..64 categories")
            normalized: dict[str, float] = {}
            for key in sorted(categories):
                category = _category(key, "categoryThresholds key")
                normalized[category] = _finite_threshold(
                    categories[key], f"categoryThresholds.{category}",
                )
            return cls(
                "per_category",
                categoryThresholds=normalized,
            )
        raise CaptionProtocolError("unknown caption threshold mode")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"mode": self.mode}
        if self.uniformThreshold is not None:
            value["uniformThreshold"] = self.uniformThreshold
        if self.categoryThresholds is not None:
            value["categoryThresholds"] = dict(self.categoryThresholds)
        return value


@dataclass(frozen=True)
class CaptionFormatPolicyV1:
    replaceUnderscoresWithSpaces: bool
    preserveEscapes: bool
    triggersEnabled: bool
    triggerTerms: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> "CaptionFormatPolicyV1":
        item = _object(value, "captionFormat")
        _keys(item, required={"replaceUnderscoresWithSpaces", "preserveEscapes", "triggersEnabled", "triggerTerms"})
        terms = item["triggerTerms"]
        if not isinstance(terms, list) or len(terms) > MAX_TRIGGER_TERMS:
            raise CaptionProtocolError(f"triggerTerms must be a list with at most {MAX_TRIGGER_TERMS} items")
        normalized: list[str] = []
        for index, term_value in enumerate(terms):
            term = _string(term_value, f"triggerTerms[{index}]", max_bytes=MAX_TRIGGER_TERM_BYTES)
            if term != term.strip() or not term.strip() or any(character in term for character in ",\r\n"):
                raise CaptionProtocolError("trigger terms must be trimmed and cannot contain comma or line breaks")
            normalized.append(term)
        enabled = _bool(item["triggersEnabled"], "triggersEnabled")
        if enabled and not normalized:
            raise CaptionProtocolError("enabled triggers require at least one term")
        return cls(
            replaceUnderscoresWithSpaces=_bool(item["replaceUnderscoresWithSpaces"], "replaceUnderscoresWithSpaces"),
            preserveEscapes=_bool(item["preserveEscapes"], "preserveEscapes"),
            triggersEnabled=enabled,
            triggerTerms=tuple(normalized),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "replaceUnderscoresWithSpaces": self.replaceUnderscoresWithSpaces,
            "preserveEscapes": self.preserveEscapes,
            "triggersEnabled": self.triggersEnabled,
            "triggerTerms": list(self.triggerTerms),
        }


@dataclass(frozen=True)
class ImageDecodePolicyV1:
    extensions: tuple[str, ...]
    rejectMultiFrame: Literal[True]
    applyExifTranspose: Literal[True]
    alphaBackground: Literal["#FFFFFF"]

    @classmethod
    def from_dict(cls, value: object) -> "ImageDecodePolicyV1":
        item = _object(value, "imageDecode")
        _keys(item, required={"extensions", "rejectMultiFrame", "applyExifTranspose", "alphaBackground"})
        expected = [".jpg", ".jpeg", ".png", ".webp", ".bmp"]
        if item["extensions"] != expected:
            raise CaptionProtocolError("image extensions do not match the frozen first-release policy")
        if item["rejectMultiFrame"] is not True or item["applyExifTranspose"] is not True:
            raise CaptionProtocolError("multi-frame rejection and EXIF transpose must be enabled")
        if item["alphaBackground"] != "#FFFFFF":
            raise CaptionProtocolError("alphaBackground must be #FFFFFF")
        return cls(tuple(expected), True, True, "#FFFFFF")

    def to_dict(self) -> dict[str, object]:
        return {
            "extensions": list(self.extensions),
            "rejectMultiFrame": self.rejectMultiFrame,
            "applyExifTranspose": self.applyExifTranspose,
            "alphaBackground": self.alphaBackground,
        }


@dataclass(frozen=True)
class CaptionHelloRequestV1:
    jobId: str
    configHash: str
    datasetRoot: str
    resourceManifestRelativePath: str
    resourceFingerprint: str
    thresholdPolicy: CaptionThresholdPolicyV1
    captionFormat: CaptionFormatPolicyV1
    imageDecode: ImageDecodePolicyV1
    profile: Literal["e621", "danbooru"] = "e621"
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionHelloRequestV1":
        item = _object(value, "caption hello payload")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "jobId", "configHash", "profile", "datasetRoot",
                "resourceManifestRelativePath", "resourceFingerprint", "thresholdPolicy", "captionFormat", "imageDecode",
            },
        )
        if item["payloadType"] != "caption_hello_request" or item["profile"] not in {"e621", "danbooru"}:
            raise CaptionProtocolError("caption hello type or profile is invalid")
        dataset_root = _string(item["datasetRoot"], "datasetRoot", max_bytes=MAX_PATH_BYTES)
        if not PureWindowsPath(dataset_root).is_absolute():
            raise CaptionProtocolError("datasetRoot must be an absolute Windows path")
        return cls(
            jobId=_identifier(item["jobId"], "jobId"),
            configHash=_sha256(item["configHash"], "configHash"),
            datasetRoot=dataset_root,
            resourceManifestRelativePath=_relative(item["resourceManifestRelativePath"], "resourceManifestRelativePath"),
            resourceFingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
            thresholdPolicy=CaptionThresholdPolicyV1.from_dict(item["thresholdPolicy"]),
            captionFormat=CaptionFormatPolicyV1.from_dict(item["captionFormat"]),
            imageDecode=ImageDecodePolicyV1.from_dict(item["imageDecode"]),
            profile=item["profile"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "payloadType": "caption_hello_request",
            "jobId": self.jobId,
            "configHash": self.configHash,
            "profile": self.profile,
            "datasetRoot": self.datasetRoot,
            "resourceManifestRelativePath": self.resourceManifestRelativePath,
            "resourceFingerprint": self.resourceFingerprint,
            "thresholdPolicy": self.thresholdPolicy.to_dict(),
            "captionFormat": self.captionFormat.to_dict(),
            "imageDecode": self.imageDecode.to_dict(),
        }


@dataclass(frozen=True)
class CaptionHelloResultV1:
    executable: str
    provider: str
    resourceFingerprint: str
    pythonVersion: Literal["3.11.15"] = "3.11.15"
    ready: Literal[True] = True
    modelSessionLoads: Literal[1] = 1
    tagCount: int = 8_783
    gpuFallback: bool = False
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionHelloResultV1":
        item = _object(value, "caption hello result")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "executable", "pythonVersion", "ready", "provider",
                "modelSessionLoads", "tagCount", "resourceFingerprint",
            },
            optional={"gpuFallback"},
        )
        if (
            item["payloadType"] != "caption_hello_result"
            or item["pythonVersion"] != "3.11.15"
            or item["ready"] is not True
            or item["modelSessionLoads"] != 1
        ):
            raise CaptionProtocolError("caption hello result identity is invalid")
        gpu_fallback = item.get("gpuFallback", False)
        if type(gpu_fallback) is not bool:
            raise CaptionProtocolError("gpuFallback must be a boolean")
        return cls(
            executable=_string(item["executable"], "executable", max_bytes=MAX_PATH_BYTES),
            provider=_string(item["provider"], "provider", max_bytes=64),
            resourceFingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
            tagCount=_positive_int(item["tagCount"], "tagCount", maximum=MAX_CAPTION_TAGS),
            gpuFallback=gpu_fallback,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "caption_hello_result",
            "executable": self.executable,
            "pythonVersion": self.pythonVersion,
            "ready": self.ready,
            "provider": self.provider,
            "modelSessionLoads": self.modelSessionLoads,
            "tagCount": self.tagCount,
            "gpuFallback": self.gpuFallback,
            "resourceFingerprint": self.resourceFingerprint,
        }


@dataclass(frozen=True)
class CaptionWorkItemV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    annotationKey: str
    imageFormat: Literal["jpeg", "png", "webp", "bmp"]
    imageSize: int
    imageMtimeNs: int
    imageFileId: str | None = None
    source: Literal["e621", "danbooru"] = "e621"
    imageFrameCount: Literal[1] = 1
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionWorkItemV1":
        item = _object(value, "caption work item")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "sampleId", "leaseId", "source", "relativeImagePath", "annotationKey",
                "imageFormat", "imageFrameCount", "imageSize", "imageMtimeNs",
            },
            optional={"imageFileId"},
        )
        if item["source"] not in {"e621", "danbooru"} or item["imageFrameCount"] != 1:
            raise CaptionProtocolError("caption work item must be a supported single-frame sample")
        image_format = item["imageFormat"]
        if image_format not in {"jpeg", "png", "webp", "bmp"}:
            raise CaptionProtocolError("caption work item imageFormat is invalid")
        image_file_id = item.get("imageFileId")
        if image_file_id is not None:
            image_file_id = _string(image_file_id, "imageFileId", max_bytes=128)
        relative_image_path = _relative(item["relativeImagePath"], "relativeImagePath")
        annotation_key = _relative(item["annotationKey"], "annotationKey")
        image_path = PureWindowsPath(relative_image_path)
        expected_suffixes = {
            "jpeg": {".jpg", ".jpeg"},
            "png": {".png"},
            "webp": {".webp"},
            "bmp": {".bmp"},
        }
        if image_path.suffix.lower() not in expected_suffixes[image_format]:
            raise CaptionProtocolError("imageFormat does not match relativeImagePath extension")
        if str(image_path.with_suffix("")) != annotation_key:
            raise CaptionProtocolError("annotationKey does not match relativeImagePath")
        return cls(
            sampleId=_positive_int(item["sampleId"], "sampleId"),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=relative_image_path,
            annotationKey=annotation_key,
            imageFormat=image_format,
            imageSize=_positive_int(item["imageSize"], "imageSize"),
            imageMtimeNs=_positive_int(item["imageMtimeNs"], "imageMtimeNs", allow_zero=True),
            imageFileId=image_file_id,
            source=item["source"],
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = asdict(self)
        if self.imageFileId is None:
            value.pop("imageFileId")
        return value


@dataclass(frozen=True)
class CaptionProcessRequestV1:
    items: tuple[CaptionWorkItemV1, ...]
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionProcessRequestV1":
        payload = _object(value, "caption process payload")
        _schema(payload)
        _keys(payload, required={"schemaVersion", "payloadType", "items"})
        if payload["payloadType"] != "caption_process_request":
            raise CaptionProtocolError("caption process payload type is invalid")
        items = payload["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_CAPTION_PROCESS_ITEMS:
            raise CaptionProtocolError(
                f"caption process v1 requires 1..{MAX_CAPTION_PROCESS_ITEMS} items"
            )
        parsed = tuple(CaptionWorkItemV1.from_dict(item) for item in items)
        identities = {(item.sampleId, item.leaseId) for item in parsed}
        if len(identities) != len(parsed):
            raise CaptionProtocolError("caption process items contain duplicate sampleId and leaseId")
        return cls(parsed)

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "caption_process_request",
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class CaptionTagV1:
    rawTag: str
    score: float
    category: str

    @classmethod
    def from_dict(cls, value: object) -> "CaptionTagV1":
        item = _object(value, "caption tag")
        _keys(item, required={"rawTag", "score", "category"})
        raw_tag = _string(item["rawTag"], "rawTag", max_bytes=512)
        if raw_tag != raw_tag.strip() or any(character in raw_tag for character in ",\r\n"):
            raise CaptionProtocolError("rawTag must be trimmed and cannot contain comma or line breaks")
        category = _category(item["category"], "category")
        return cls(raw_tag, _finite_threshold(item["score"], "score"), category)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CaptionResultV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    tags: tuple[CaptionTagV1, ...]
    formattedTxt: str
    provider: str
    source: Literal["e621", "danbooru"] = "e621"
    modelSessionLoads: Literal[1] = 1
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionResultV1":
        item = _object(value, "caption result")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "sampleId", "leaseId", "source", "relativeImagePath",
                "tags", "formattedTxt", "provider", "modelSessionLoads",
            },
        )
        if (
            item["payloadType"] != "caption_result"
            or item["source"] not in {"e621", "danbooru"}
            or item["modelSessionLoads"] != 1
        ):
            raise CaptionProtocolError("caption result identity is invalid")
        tags = item["tags"]
        if not isinstance(tags, list) or not 1 <= len(tags) <= MAX_CAPTION_TAGS:
            raise CaptionProtocolError(f"caption result must contain 1..{MAX_CAPTION_TAGS} tags")
        formatted = _string(item["formattedTxt"], "formattedTxt", max_bytes=MAX_FORMATTED_TXT_BYTES)
        if formatted.startswith("\ufeff") or formatted != formatted.strip() or any(character in formatted for character in "\r\n\x00"):
            raise CaptionProtocolError("formattedTxt must be trimmed single-line UTF-8 text without BOM")
        provider = _string(item["provider"], "provider", max_bytes=64)
        return cls(
            sampleId=_positive_int(item["sampleId"], "sampleId"),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=_relative(item["relativeImagePath"], "relativeImagePath"),
            tags=tuple(CaptionTagV1.from_dict(tag) for tag in tags),
            formattedTxt=formatted,
            provider=provider,
            source=item["source"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "caption_result",
            "sampleId": self.sampleId,
            "leaseId": self.leaseId,
            "source": self.source,
            "relativeImagePath": self.relativeImagePath,
            "tags": [tag.to_dict() for tag in self.tags],
            "formattedTxt": self.formattedTxt,
            "provider": self.provider,
            "modelSessionLoads": self.modelSessionLoads,
        }


@dataclass(frozen=True)
class CaptionIssueResultV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    code: Literal["caption_image_decode_failed", "caption_inference_failed", "caption_no_tags"]
    retriable: bool
    message: str
    source: Literal["e621", "danbooru"] = "e621"
    severity: Literal["error"] = "error"
    blocking: Literal[True] = True
    repairStartModule: Literal["caption"] | None = "caption"
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionIssueResultV1":
        item = _object(value, "caption issue")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "sampleId", "leaseId", "source", "relativeImagePath",
                "code", "severity", "blocking", "retriable", "message",
            },
            optional={"repairStartModule"},
        )
        code = item["code"]
        if (
            item["payloadType"] != "caption_issue"
            or item["source"] not in {"e621", "danbooru"}
            or code not in CAPTION_ISSUE_CODES
            or item["severity"] != "error"
            or item["blocking"] is not True
        ):
            raise CaptionProtocolError("caption issue identity is invalid")
        retriable = _bool(item["retriable"], "retriable")
        expected_retriable = code != "caption_no_tags"
        if retriable != expected_retriable:
            raise CaptionProtocolError("caption issue retriable flag does not match its code")
        repair = item.get("repairStartModule")
        expected_repair = "caption" if expected_retriable else None
        if repair != expected_repair:
            raise CaptionProtocolError("caption issue repairStartModule does not match its code")
        return cls(
            sampleId=_positive_int(item["sampleId"], "sampleId"),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=_relative(item["relativeImagePath"], "relativeImagePath"),
            code=code,
            retriable=retriable,
            message=_string(item["message"], "message", max_bytes=1_024),
            repairStartModule=expected_repair,
            source=item["source"],
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schemaVersion": 1,
            "payloadType": "caption_issue",
            "sampleId": self.sampleId,
            "leaseId": self.leaseId,
            "source": self.source,
            "relativeImagePath": self.relativeImagePath,
            "code": self.code,
            "severity": self.severity,
            "blocking": self.blocking,
            "retriable": self.retriable,
            "message": self.message,
        }
        if self.repairStartModule is not None:
            value["repairStartModule"] = self.repairStartModule
        return value


@dataclass(frozen=True)
class CaptionProcessResultV1:
    outcomes: tuple[CaptionResultV1 | CaptionIssueResultV1, ...]
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "CaptionProcessResultV1":
        payload = _object(value, "caption process result")
        _schema(payload)
        _keys(payload, required={"schemaVersion", "payloadType", "outcomes"})
        if payload["payloadType"] != "caption_process_result":
            raise CaptionProtocolError("caption process result payload type is invalid")
        values = payload["outcomes"]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_CAPTION_PROCESS_ITEMS:
            raise CaptionProtocolError(
                f"caption process result requires 1..{MAX_CAPTION_PROCESS_ITEMS} outcomes"
            )
        outcomes = tuple(parse_caption_outcome(item) for item in values)
        identities = {(item.sampleId, item.leaseId) for item in outcomes}
        if len(identities) != len(outcomes):
            raise CaptionProtocolError("caption process result contains duplicate sampleId and leaseId")
        return cls(outcomes)

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "caption_process_result",
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def parse_caption_outcome(value: object) -> CaptionResultV1 | CaptionIssueResultV1:
    payload = _object(value, "caption outcome")
    payload_type = payload.get("payloadType")
    if payload_type == "caption_result":
        return CaptionResultV1.from_dict(payload)
    if payload_type == "caption_issue":
        return CaptionIssueResultV1.from_dict(payload)
    raise CaptionProtocolError("unknown caption outcome payloadType")


def validate_outcomes_for_items(
    outcomes: tuple[CaptionResultV1 | CaptionIssueResultV1, ...],
    items: tuple[CaptionWorkItemV1, ...],
) -> tuple[CaptionResultV1 | CaptionIssueResultV1, ...]:
    if len(outcomes) != len(items):
        raise CaptionProtocolError("caption process result count does not match the request")
    expected = {(item.sampleId, item.leaseId): item for item in items}
    actual = {(outcome.sampleId, outcome.leaseId): outcome for outcome in outcomes}
    if len(actual) != len(outcomes):
        raise CaptionProtocolError("caption process result contains duplicate sampleId and leaseId")
    if set(actual) != set(expected):
        raise CaptionProtocolError("caption process result identities do not match the request")
    ordered: list[CaptionResultV1 | CaptionIssueResultV1] = []
    for identity, item in expected.items():
        outcome = actual[identity]
        validate_outcome_for_item(outcome, item)
        ordered.append(outcome)
    return tuple(ordered)


def validate_outcome_for_item(
    outcome: CaptionResultV1 | CaptionIssueResultV1,
    item: CaptionWorkItemV1,
) -> None:
    if (
        outcome.sampleId != item.sampleId
        or outcome.leaseId != item.leaseId
        or outcome.source != item.source
        or outcome.relativeImagePath != item.relativeImagePath
    ):
        raise CaptionProtocolError("caption outcome does not match the leased work item")
