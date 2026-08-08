from __future__ import annotations

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
