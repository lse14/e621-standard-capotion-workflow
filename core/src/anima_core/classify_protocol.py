from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import PureWindowsPath
from typing import Literal, Mapping

from .path_safety import PathSafetyError, safe_relative_path


CLASSIFY_PAYLOAD_SCHEMA_VERSION = 1
CLASSIFY_WIKI_DATA_SOURCE_ID = "e621-wiki-count-20260724-v1"
MAX_CLASSIFY_TXT_BYTES = 262_144
MAX_CLASSIFY_TAGS = 16_384
MAX_CLASSIFY_WARNINGS = 64
MAX_PATH_BYTES = 16_384
COUNT_VALUES = frozenset({"", "solo", "duo", "trio", "group"})
COUNT_NONEMPTY_VALUES = frozenset({"solo", "duo", "trio", "group"})
E621_LOWER_BOUND_VALUES = frozenset({"character", "e621_relationship"})
DANBOORU_LOWER_BOUND_VALUES = frozenset({"danbooru_girl", "danbooru_boy", "danbooru_other"})
LOWER_BOUND_VALUES = E621_LOWER_BOUND_VALUES | DANBOORU_LOWER_BOUND_VALUES
CLASSIFY_ISSUE_CODES = frozenset({
    "classify_original_count_unresolved",
    "count_sheet_multi_conflict",
    "classify_no_writable_tags",
    "classify_text_invalid",
    "classify_wiki_io_failed",
    "classify_processing_failed",
})
RETRIABLE_CLASSIFY_ISSUES = frozenset({"classify_wiki_io_failed", "classify_processing_failed"})
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ClassifyProtocolError(ValueError):
    pass


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ClassifyProtocolError(f"{field} must be an object")
    return value


def _keys(value: Mapping[str, object], *, required: set[str], optional: set[str] = set()) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ClassifyProtocolError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ClassifyProtocolError(f"unknown fields: {', '.join(sorted(extra))}")


def _schema(value: Mapping[str, object]) -> None:
    if value.get("schemaVersion") != CLASSIFY_PAYLOAD_SCHEMA_VERSION:
        raise ClassifyProtocolError("classify payload schemaVersion must be 1")


def _string(value: object, field: str, *, max_bytes: int, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ClassifyProtocolError(f"{field} must be a string")
    if not allow_blank and not value:
        raise ClassifyProtocolError(f"{field} must not be empty")
    if len(value.encode("utf-8")) > max_bytes or "\x00" in value:
        raise ClassifyProtocolError(f"{field} exceeds its limit or contains NUL")
    return value


def _identifier(value: object, field: str) -> str:
    result = _string(value, field, max_bytes=128)
    if not IDENTIFIER.fullmatch(result):
        raise ClassifyProtocolError(f"{field} is invalid")
    return result


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ClassifyProtocolError(f"{field} must be a lowercase SHA-256")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ClassifyProtocolError(f"{field} must be boolean")
    return value


def _int(value: object, field: str, *, minimum: int = 0, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ClassifyProtocolError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _relative(value: object, field: str) -> str:
    text = _string(value, field, max_bytes=MAX_PATH_BYTES)
    try:
        return safe_relative_path(text)
    except PathSafetyError as exc:
        raise ClassifyProtocolError(f"{field} is unsafe: {exc}") from exc


def _tag(value: object, field: str) -> str:
    tag = _string(value, field, max_bytes=512)
    if tag != tag.strip() or any(character in tag for character in ",\r\n"):
        raise ClassifyProtocolError(f"{field} must be a trimmed single tag")
    return tag


def _tag_list(value: object, field: str, *, require_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_CLASSIFY_TAGS:
        raise ClassifyProtocolError(f"{field} must be a bounded array")
    tags = tuple(_tag(item, f"{field} item") for item in value)
    if require_empty and tags:
        raise ClassifyProtocolError(f"{field} must be empty")
    if len(set(tags)) != len(tags):
        raise ClassifyProtocolError(f"{field} contains duplicate tags")
    return tags


def _count_raw(value: object) -> str | int | None:
    if value is None:
        return None
    if type(value) is int:
        if not -1_000_000_000 <= value <= 1_000_000_000:
            raise ClassifyProtocolError("originalCount integer is out of range")
        return value
    if isinstance(value, str):
        return _string(value, "originalCount", max_bytes=1_024, allow_blank=True)
    raise ClassifyProtocolError("originalCount must be null, a string, or an integer")


def _code_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_CLASSIFY_WARNINGS:
        raise ClassifyProtocolError(f"{field} must be a bounded string array")
    result: list[str] = []
    for raw in value:
        item = _string(raw, f"{field} item", max_bytes=1_024)
        if any(character in item for character in "\r\n"):
            raise ClassifyProtocolError(f"{field} item must be single-line")
        result.append(item)
    if len(set(result)) != len(result):
        raise ClassifyProtocolError(f"{field} contains duplicates")
    return tuple(result)


@dataclass(frozen=True)
class ClassifyCaptionFormatPolicyV1:
    replaceUnderscoresWithSpaces: bool
    preserveEscapes: bool
    triggersEnabled: bool
    triggerTerms: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyCaptionFormatPolicyV1":
        item = _object(value, "captionFormat")
        _keys(
            item,
            required={"replaceUnderscoresWithSpaces", "preserveEscapes", "triggersEnabled", "triggerTerms"},
        )
        terms = item["triggerTerms"]
        if not isinstance(terms, list) or len(terms) > 64:
            raise ClassifyProtocolError("triggerTerms must contain at most 64 strings")
        normalized: list[str] = []
        for raw in terms:
            term = _string(raw, "trigger term", max_bytes=512)
            if term != term.strip() or any(character in term for character in ",\r\n"):
                raise ClassifyProtocolError("trigger term is invalid")
            normalized.append(term)
        enabled = _bool(item["triggersEnabled"], "triggersEnabled")
        if enabled and not normalized:
            raise ClassifyProtocolError("enabled triggers require at least one term")
        return cls(
            _bool(item["replaceUnderscoresWithSpaces"], "replaceUnderscoresWithSpaces"),
            _bool(item["preserveEscapes"], "preserveEscapes"),
            enabled,
            tuple(normalized),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "replaceUnderscoresWithSpaces": self.replaceUnderscoresWithSpaces,
            "preserveEscapes": self.preserveEscapes,
            "triggersEnabled": self.triggersEnabled,
            "triggerTerms": list(self.triggerTerms),
        }


@dataclass(frozen=True)
class ClassifyHelloRequestV1:
    jobId: str
    configHash: str
    resourceManifestRelativePath: str
    resourceFingerprint: str
    overwriteCount: bool
    captionFormat: ClassifyCaptionFormatPolicyV1
    profile: Literal["e621", "danbooru"] = "e621"
    wikiDataSourceId: str = CLASSIFY_WIKI_DATA_SOURCE_ID
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyHelloRequestV1":
        item = _object(value, "classify hello payload")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "jobId", "configHash", "profile", "resourceManifestRelativePath",
                "resourceFingerprint", "wikiDataSourceId", "overwriteCount", "captionFormat",
            },
        )
        if (
            item["payloadType"] != "classify_hello_request"
            or item["profile"] not in {"e621", "danbooru"}
        ):
            raise ClassifyProtocolError("classify hello identity is invalid")
        return cls(
            jobId=_identifier(item["jobId"], "jobId"),
            configHash=_sha256(item["configHash"], "configHash"),
            resourceManifestRelativePath=_relative(
                item["resourceManifestRelativePath"], "resourceManifestRelativePath"
            ),
            resourceFingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
            wikiDataSourceId=_identifier(item["wikiDataSourceId"], "wikiDataSourceId"),
            overwriteCount=_bool(item["overwriteCount"], "overwriteCount"),
            captionFormat=ClassifyCaptionFormatPolicyV1.from_dict(item["captionFormat"]),
            profile=item["profile"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "classify_hello_request",
            "jobId": self.jobId,
            "configHash": self.configHash,
            "profile": self.profile,
            "resourceManifestRelativePath": self.resourceManifestRelativePath,
            "resourceFingerprint": self.resourceFingerprint,
            "wikiDataSourceId": self.wikiDataSourceId,
            "overwriteCount": self.overwriteCount,
            "captionFormat": self.captionFormat.to_dict(),
        }


@dataclass(frozen=True)
class ClassifyHelloResultV1:
    executable: str
    resourceFingerprint: str
    entryCount: int = 120_978
    pythonVersion: Literal["3.11.15"] = "3.11.15"
    ready: Literal[True] = True
    dictionaryLoads: Literal[1] = 1
    wikiConnectionLoads: Literal[1] = 1
    wikiSchemaVersion: Literal[1] = 1
    wikiDataSourceId: str = CLASSIFY_WIKI_DATA_SOURCE_ID
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyHelloResultV1":
        item = _object(value, "classify hello result")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "executable", "pythonVersion", "ready", "dictionaryLoads",
                "wikiConnectionLoads", "entryCount", "wikiSchemaVersion", "wikiDataSourceId", "resourceFingerprint",
            },
        )
        if (
            item["payloadType"] != "classify_hello_result"
            or item["pythonVersion"] != "3.11.15"
            or item["ready"] is not True
            or item["dictionaryLoads"] != 1
            or item["wikiConnectionLoads"] != 1
            or item["wikiSchemaVersion"] != 1
        ):
            raise ClassifyProtocolError("classify hello result identity is invalid")
        return cls(
            executable=_string(item["executable"], "executable", max_bytes=MAX_PATH_BYTES),
            resourceFingerprint=_sha256(item["resourceFingerprint"], "resourceFingerprint"),
            entryCount=_int(item["entryCount"], "entryCount", minimum=1),
            wikiDataSourceId=_identifier(item["wikiDataSourceId"], "wikiDataSourceId"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "classify_hello_result",
            "executable": self.executable,
            "pythonVersion": self.pythonVersion,
            "ready": self.ready,
            "dictionaryLoads": self.dictionaryLoads,
            "wikiConnectionLoads": self.wikiConnectionLoads,
            "entryCount": self.entryCount,
            "wikiSchemaVersion": self.wikiSchemaVersion,
            "wikiDataSourceId": self.wikiDataSourceId,
            "resourceFingerprint": self.resourceFingerprint,
        }


@dataclass(frozen=True)
class ClassifyWorkItemV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    annotationKey: str
    txtText: str
    txtProvenance: Literal["missing", "original_preserved", "module1_written"]
    originalCount: str | int | None
    source: Literal["e621", "danbooru"] = "e621"
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyWorkItemV1":
        item = _object(value, "classify work item")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "sampleId", "leaseId", "source", "relativeImagePath", "annotationKey", "txtText",
                "txtProvenance", "originalCount",
            },
        )
        if item["source"] not in {"e621", "danbooru"} or item["txtProvenance"] not in {
            "missing", "original_preserved", "module1_written"
        }:
            raise ClassifyProtocolError("classify work item identity is invalid")
        relative_image_path = _relative(item["relativeImagePath"], "relativeImagePath")
        annotation_key = _relative(item["annotationKey"], "annotationKey")
        if str(PureWindowsPath(relative_image_path).with_suffix("")) != annotation_key:
            raise ClassifyProtocolError("annotationKey does not match relativeImagePath")
        text = _string(item["txtText"], "txtText", max_bytes=MAX_CLASSIFY_TXT_BYTES)
        if not text.strip() or text.startswith("\ufeff\ufeff"):
            raise ClassifyProtocolError("txtText must contain non-blank caption text")
        return cls(
            sampleId=_int(item["sampleId"], "sampleId", minimum=1),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=relative_image_path,
            annotationKey=annotation_key,
            txtText=text,
            txtProvenance=item["txtProvenance"],  # type: ignore[arg-type]
            originalCount=_count_raw(item["originalCount"]),
            source=item["source"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ClassifyProcessRequestV1:
    item: ClassifyWorkItemV1
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyProcessRequestV1":
        payload = _object(value, "classify process payload")
        _schema(payload)
        _keys(payload, required={"schemaVersion", "payloadType", "items"})
        items = payload["items"]
        if payload["payloadType"] != "classify_process_request" or not isinstance(items, list) or len(items) != 1:
            raise ClassifyProtocolError("classify process v1 requires exactly one item")
        return cls(ClassifyWorkItemV1.from_dict(items[0]))

    def to_dict(self) -> dict[str, object]:
        return {"schemaVersion": 1, "payloadType": "classify_process_request", "items": [self.item.to_dict()]}


@dataclass(frozen=True)
class ClassifyProjectionV1:
    quality: tuple[str, ...]
    count: str
    character: str
    series: str
    artist: str
    appearance: tuple[str, ...]
    tags: tuple[str, ...]
    environment: tuple[str, ...]
    nl: str

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        profile: Literal["e621", "danbooru"] = "e621",
    ) -> "ClassifyProjectionV1":
        item = _object(value, "classify projection")
        required = {"quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl"}
        _keys(item, required=required)
        count = item["count"]
        if count not in COUNT_VALUES:
            raise ClassifyProtocolError("projection count is invalid")
        character = _string(item["character"], "character", max_bytes=262_144, allow_blank=True)
        if any(character_value in character for character_value in "\r\n\x00"):
            raise ClassifyProtocolError("character must be single-line")
        series = _string(item["series"], "series", max_bytes=262_144, allow_blank=True)
        if any(character_value in series for character_value in "\r\n\x00"):
            raise ClassifyProtocolError("series must be single-line")
        if item["artist"] != "" or item["nl"] != "" or (profile == "e621" and series):
            raise ClassifyProtocolError("classify projection contains a profile-forbidden text field")
        return cls(
            quality=_tag_list(item["quality"], "quality", require_empty=True),
            count=count,  # type: ignore[arg-type]
            character=character,
            series=series,
            artist="",
            appearance=_tag_list(item["appearance"], "appearance"),
            tags=_tag_list(item["tags"], "tags"),
            environment=_tag_list(item["environment"], "environment"),
            nl="",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "quality": list(self.quality),
            "count": self.count,
            "character": self.character,
            "series": self.series,
            "artist": self.artist,
            "appearance": list(self.appearance),
            "tags": list(self.tags),
            "environment": list(self.environment),
            "nl": self.nl,
        }


@dataclass(frozen=True)
class ClassifyCountDecisionV1:
    value: str
    baseValue: str
    selectedSource: Literal["original_json", "wiki_tags", "none"]
    originalRaw: str | int | None
    originalNormalized: str | None
    wikiValue: str | None
    matchedTags: tuple[str, ...]
    conflict: bool
    issueCodes: tuple[str, ...]
    warnings: tuple[str, ...]
    appliedLowerBounds: tuple[
        Literal["character", "e621_relationship", "danbooru_girl", "danbooru_boy", "danbooru_other"], ...
    ]

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyCountDecisionV1":
        item = _object(value, "countDecision")
        _keys(
            item,
            required={
                "value", "baseValue", "selectedSource", "originalRaw", "originalNormalized", "wikiValue",
                "matchedTags", "conflict", "issueCodes", "warnings", "appliedLowerBounds",
            },
        )
        if item["value"] not in COUNT_VALUES or item["baseValue"] not in COUNT_VALUES:
            raise ClassifyProtocolError("countDecision value is invalid")
        if item["selectedSource"] not in {"original_json", "wiki_tags", "none"}:
            raise ClassifyProtocolError("countDecision selectedSource is invalid")
        original_normalized = item["originalNormalized"]
        wiki_value = item["wikiValue"]
        if original_normalized is not None and original_normalized not in COUNT_NONEMPTY_VALUES:
            raise ClassifyProtocolError("originalNormalized is invalid")
        if wiki_value is not None and wiki_value not in COUNT_NONEMPTY_VALUES:
            raise ClassifyProtocolError("wikiValue is invalid")
        lower_bounds = item["appliedLowerBounds"]
        if (
            not isinstance(lower_bounds, list)
            or len(lower_bounds) > 3
            or any(bound not in LOWER_BOUND_VALUES for bound in lower_bounds)
            or len(set(lower_bounds)) != len(lower_bounds)
        ):
            raise ClassifyProtocolError("appliedLowerBounds is invalid")
        return cls(
            value=item["value"],  # type: ignore[arg-type]
            baseValue=item["baseValue"],  # type: ignore[arg-type]
            selectedSource=item["selectedSource"],  # type: ignore[arg-type]
            originalRaw=_count_raw(item["originalRaw"]),
            originalNormalized=original_normalized,  # type: ignore[arg-type]
            wikiValue=wiki_value,  # type: ignore[arg-type]
            matchedTags=_tag_list(item["matchedTags"], "matchedTags"),
            conflict=_bool(item["conflict"], "conflict"),
            issueCodes=_code_list(item["issueCodes"], "issueCodes"),
            warnings=_code_list(item["warnings"], "warnings"),
            appliedLowerBounds=tuple(lower_bounds),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "baseValue": self.baseValue,
            "selectedSource": self.selectedSource,
            "originalRaw": self.originalRaw,
            "originalNormalized": self.originalNormalized,
            "wikiValue": self.wikiValue,
            "matchedTags": list(self.matchedTags),
            "conflict": self.conflict,
            "issueCodes": list(self.issueCodes),
            "warnings": list(self.warnings),
            "appliedLowerBounds": list(self.appliedLowerBounds),
        }


@dataclass(frozen=True)
class ClassifyResultV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    projection: ClassifyProjectionV1
    countDecision: ClassifyCountDecisionV1
    inputTagCount: int
    outputTagCount: int
    droppedTagCount: int
    source: Literal["e621", "danbooru"] = "e621"
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyResultV1":
        item = _object(value, "classify result")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "sampleId", "leaseId", "source", "relativeImagePath", "projection",
                "countDecision", "inputTagCount", "outputTagCount", "droppedTagCount",
            },
        )
        if item["payloadType"] != "classify_result" or item["source"] not in {"e621", "danbooru"}:
            raise ClassifyProtocolError("classify result identity is invalid")
        projection = ClassifyProjectionV1.from_dict(
            item["projection"], profile=item["source"]  # type: ignore[arg-type]
        )
        decision = ClassifyCountDecisionV1.from_dict(item["countDecision"])
        if projection.count != decision.value:
            raise ClassifyProtocolError("projection count does not match countDecision")
        allowed_bounds = (
            E621_LOWER_BOUND_VALUES if item["source"] == "e621" else DANBOORU_LOWER_BOUND_VALUES
        )
        if any(bound not in allowed_bounds for bound in decision.appliedLowerBounds):
            raise ClassifyProtocolError("countDecision lower bounds do not match the result profile")
        return cls(
            sampleId=_int(item["sampleId"], "sampleId", minimum=1),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=_relative(item["relativeImagePath"], "relativeImagePath"),
            projection=projection,
            countDecision=decision,
            inputTagCount=_int(item["inputTagCount"], "inputTagCount", maximum=MAX_CLASSIFY_TAGS),
            outputTagCount=_int(item["outputTagCount"], "outputTagCount", maximum=MAX_CLASSIFY_TAGS),
            droppedTagCount=_int(item["droppedTagCount"], "droppedTagCount", maximum=MAX_CLASSIFY_TAGS),
            source=item["source"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "classify_result",
            "sampleId": self.sampleId,
            "leaseId": self.leaseId,
            "source": self.source,
            "relativeImagePath": self.relativeImagePath,
            "projection": self.projection.to_dict(),
            "countDecision": self.countDecision.to_dict(),
            "inputTagCount": self.inputTagCount,
            "outputTagCount": self.outputTagCount,
            "droppedTagCount": self.droppedTagCount,
        }


@dataclass(frozen=True)
class ClassifyIssueResultV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    code: str
    retriable: bool
    message: str
    source: Literal["e621", "danbooru"] = "e621"
    severity: Literal["error"] = "error"
    blocking: Literal[True] = True
    repairStartModule: Literal["classify"] | None = None
    schemaVersion: Literal[1] = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyIssueResultV1":
        item = _object(value, "classify issue")
        _schema(item)
        _keys(
            item,
            required={
                "schemaVersion", "payloadType", "sampleId", "leaseId", "source", "relativeImagePath", "code",
                "severity", "blocking", "retriable", "message",
            },
            optional={"repairStartModule"},
        )
        code = item["code"]
        if (
            item["payloadType"] != "classify_issue"
            or item["source"] not in {"e621", "danbooru"}
            or code not in CLASSIFY_ISSUE_CODES
            or item["severity"] != "error"
            or item["blocking"] is not True
        ):
            raise ClassifyProtocolError("classify issue identity is invalid")
        retriable = _bool(item["retriable"], "retriable")
        expected_retriable = code in RETRIABLE_CLASSIFY_ISSUES
        expected_repair = "classify" if expected_retriable else None
        if retriable != expected_retriable or item.get("repairStartModule") != expected_repair:
            raise ClassifyProtocolError("classify issue retry identity is invalid")
        return cls(
            sampleId=_int(item["sampleId"], "sampleId", minimum=1),
            leaseId=_identifier(item["leaseId"], "leaseId"),
            relativeImagePath=_relative(item["relativeImagePath"], "relativeImagePath"),
            code=code,  # type: ignore[arg-type]
            retriable=retriable,
            message=_string(item["message"], "message", max_bytes=1_024),
            repairStartModule=expected_repair,
            source=item["source"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schemaVersion": 1,
            "payloadType": "classify_issue",
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


def parse_classify_outcome(value: object) -> ClassifyResultV1 | ClassifyIssueResultV1:
    payload = _object(value, "classify outcome")
    if payload.get("payloadType") == "classify_result":
        return ClassifyResultV1.from_dict(payload)
    if payload.get("payloadType") == "classify_issue":
        return ClassifyIssueResultV1.from_dict(payload)
    raise ClassifyProtocolError("unknown classify outcome payloadType")


def validate_outcome_for_item(
    outcome: ClassifyResultV1 | ClassifyIssueResultV1,
    item: ClassifyWorkItemV1,
) -> None:
    if (
        outcome.sampleId != item.sampleId
        or outcome.leaseId != item.leaseId
        or outcome.source != item.source
        or outcome.relativeImagePath != item.relativeImagePath
    ):
        raise ClassifyProtocolError("classify outcome does not match the leased work item")
