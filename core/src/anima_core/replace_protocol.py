from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .path_safety import PathSafetyError, safe_relative_path


REQUIRED_PROJECTION_FIELDS = frozenset({"quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl"})


class ReplaceProtocolError(ValueError):
    pass


def _identity(value: object, name: str, expected: object) -> None:
    if value != expected:
        raise ReplaceProtocolError(f"replace result {name} does not match its lease")


def parse_replace_projection(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != REQUIRED_PROJECTION_FIELDS:
        raise ReplaceProtocolError("replace projection must contain exactly the nine fields")
    projection = value
    for field in ("quality", "appearance", "tags", "environment"):
        tags = projection[field]
        if not isinstance(tags, list) or len(tags) > 16_384 or len(tags) != len(set(tags)) or any(not isinstance(tag, str) or not tag or tag != tag.strip() or any(c in tag for c in ",\r\n\x00") for tag in tags):
            raise ReplaceProtocolError(f"replace projection {field} is invalid")
    if projection["count"] not in {"", "solo", "duo", "trio", "group"}:
        raise ReplaceProtocolError("replace projection count is invalid")
    for field in ("character", "series", "artist", "nl"):
        if not isinstance(projection[field], str) or len(projection[field].encode("utf-8")) > 16_384 or "\x00" in projection[field]:
            raise ReplaceProtocolError(f"replace projection {field} must be a string")
    return projection


REPLACE_COUNTS = ("replaced", "dropped", "passthrough", "keepRewritten")
MAX_REPLACE_PROCESS_ITEMS = 500


@dataclass(frozen=True)
class ReplaceWorkItemV1:
    sampleId: int
    leaseId: str
    relativeImagePath: str
    projection: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> "ReplaceWorkItemV1":
        if not isinstance(value, dict) or set(value) != {
            "schemaVersion", "sampleId", "leaseId", "source", "relativeImagePath", "projection",
        }:
            raise ReplaceProtocolError("replace work item is invalid")
        if value.get("schemaVersion") != 1 or value.get("source") != "e621":
            raise ReplaceProtocolError("replace work item identity is invalid")
        sample_id = value.get("sampleId")
        lease_id = value.get("leaseId")
        relative_path = value.get("relativeImagePath")
        if type(sample_id) is not int or sample_id < 1 or not isinstance(lease_id, str) or not lease_id:
            raise ReplaceProtocolError("replace work item identity is invalid")
        if not isinstance(relative_path, str):
            raise ReplaceProtocolError("replace work item path is invalid")
        try:
            safe_relative_path(relative_path)
        except PathSafetyError as exc:
            raise ReplaceProtocolError("replace work item path is unsafe") from exc
        return cls(sample_id, lease_id, relative_path, parse_replace_projection(value.get("projection")))

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sampleId": self.sampleId,
            "leaseId": self.leaseId,
            "source": "e621",
            "relativeImagePath": self.relativeImagePath,
            "projection": self.projection,
        }


@dataclass(frozen=True)
class ReplaceProcessRequestV1:
    items: tuple[ReplaceWorkItemV1, ...]

    @classmethod
    def from_dict(cls, value: object) -> "ReplaceProcessRequestV1":
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "payloadType", "items"}:
            raise ReplaceProtocolError("replace process request is invalid")
        raw_items = value.get("items")
        if (
            value.get("schemaVersion") != 1
            or value.get("payloadType") != "replace_process_request"
            or not isinstance(raw_items, list)
            or not 1 <= len(raw_items) <= MAX_REPLACE_PROCESS_ITEMS
        ):
            raise ReplaceProtocolError("replace process request is invalid")
        items = tuple(ReplaceWorkItemV1.from_dict(item) for item in raw_items)
        if len({(item.sampleId, item.leaseId) for item in items}) != len(items):
            raise ReplaceProtocolError("replace process request has duplicate sample/lease identities")
        return cls(items)

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "replace_process_request",
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ReplaceProcessResultV1:
    outcomes: tuple[dict[str, object], ...]

    @classmethod
    def from_dict(cls, value: object) -> "ReplaceProcessResultV1":
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "payloadType", "outcomes"}:
            raise ReplaceProtocolError("replace process result is invalid")
        outcomes = value.get("outcomes")
        if (
            value.get("schemaVersion") != 1
            or value.get("payloadType") != "replace_process_result"
            or not isinstance(outcomes, list)
            or not 1 <= len(outcomes) <= MAX_REPLACE_PROCESS_ITEMS
            or not all(isinstance(outcome, dict) for outcome in outcomes)
        ):
            raise ReplaceProtocolError("replace process result is invalid")
        return cls(tuple(outcomes))

    def to_dict(self) -> dict[str, object]:
        return {"schemaVersion": 1, "payloadType": "replace_process_result", "outcomes": list(self.outcomes)}


def validate_replace_outcome(payload: object, *, sample_id: int, lease_id: str, relative_image_path: str, original_projection: dict[str, object]) -> tuple[str, dict[str, object] | None, dict[str, int], str | None]:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ReplaceProtocolError("replace result schemaVersion is invalid")
    _identity(payload.get("sampleId"), "sampleId", sample_id)
    _identity(payload.get("leaseId"), "leaseId", lease_id)
    _identity(payload.get("source"), "source", "e621")
    _identity(payload.get("relativeImagePath"), "relativeImagePath", relative_image_path)
    try:
        safe_relative_path(relative_image_path)
    except PathSafetyError as exc:
        raise ReplaceProtocolError("replace relative image path is unsafe") from exc
    payload_type = payload.get("payloadType")
    if payload_type == "replace_issue":
        expected = {"schemaVersion", "payloadType", "sampleId", "leaseId", "source", "relativeImagePath", "code", "severity", "blocking", "retriable", "message"}
        if set(payload) != expected or payload["code"] != "replace_json_invalid" or payload["severity"] != "error" or payload["blocking"] is not True or payload["retriable"] is not False or not isinstance(payload["message"], str) or not payload["message"]:
            raise ReplaceProtocolError("replace issue payload is invalid")
        return "issue", None, {}, payload["message"][:1024]
    expected = {"schemaVersion", "payloadType", "sampleId", "leaseId", "source", "relativeImagePath", "projection", *REPLACE_COUNTS}
    if payload_type != "replace_result" or set(payload) != expected:
        raise ReplaceProtocolError("replace result payload is invalid")
    projection = parse_replace_projection(payload["projection"])
    for field in ("count", "series", "artist", "nl"):
        if projection[field] != original_projection[field]:
            raise ReplaceProtocolError(f"replace result modified protected field {field}")
    for field in REPLACE_COUNTS:
        if type(payload[field]) is not int or not 0 <= payload[field] <= 81_920:
            raise ReplaceProtocolError(f"replace result {field} is invalid")
    return "result", projection, {field: payload[field] for field in REPLACE_COUNTS}, None


def validate_replace_outcomes(
    result: ReplaceProcessResultV1,
    items: tuple[ReplaceWorkItemV1, ...],
) -> tuple[tuple[str, dict[str, object] | None, dict[str, int], str | None], ...]:
    """Validate a batch as one exact identity set, then restore request ordering."""
    if len(result.outcomes) != len(items):
        raise ReplaceProtocolError("replace process result count does not match the request")
    expected: Mapping[tuple[int, str], ReplaceWorkItemV1] = {
        (item.sampleId, item.leaseId): item for item in items
    }
    validated: dict[tuple[int, str], tuple[str, dict[str, object] | None, dict[str, int], str | None]] = {}
    for outcome in result.outcomes:
        sample_id = outcome.get("sampleId")
        lease_id = outcome.get("leaseId")
        if type(sample_id) is not int or not isinstance(lease_id, str):
            raise ReplaceProtocolError("replace process result identity is invalid")
        identity = (sample_id, lease_id)
        item = expected.get(identity)
        if item is None or identity in validated:
            raise ReplaceProtocolError("replace process result identities do not match the request")
        validated[identity] = validate_replace_outcome(
            outcome,
            sample_id=item.sampleId,
            lease_id=item.leaseId,
            relative_image_path=item.relativeImagePath,
            original_projection=item.projection,
        )
    if set(validated) != set(expected):
        raise ReplaceProtocolError("replace process result identities do not match the request")
    return tuple(validated[(item.sampleId, item.leaseId)] for item in items)
