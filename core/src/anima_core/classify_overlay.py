from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .classify_protocol import ClassifyResultV1, ClassifyWorkItemV1
from .db import StateDatabase
from .overlay import OverlayError, OverlayLayout
from .path_safety import safe_relative_path, sha256_file, windows_paths_equal
from .raw_e621 import RawE621Annotation


MAX_JSON_BYTES = 1_048_576
SHA256 = re.compile(r"^[0-9a-f]{64}$")
JSON_FIELDS = ("quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl")
ARRAY_FIELDS = frozenset({"quality", "appearance", "tags", "environment"})
STRING_FIELDS = frozenset(JSON_FIELDS) - ARRAY_FIELDS


class ClassifyJsonError(ValueError):
    pass


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ClassifyJsonError(f"JSON object has duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ClassifyJsonError(f"JSON contains non-finite value: {value}")


def parse_annotation_json(raw: bytes | None) -> dict[str, object] | None:
    """Parse only a non-empty, strict UTF-8 annotation JSON object."""
    if raw is None or not raw:
        return None
    if len(raw) > MAX_JSON_BYTES:
        raise ClassifyJsonError("annotation JSON exceeds 1 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ClassifyJsonError("annotation JSON is not UTF-8") from exc
    if not text.strip():
        return None
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicate_object, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, ClassifyJsonError) as exc:
        raise ClassifyJsonError("annotation JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ClassifyJsonError("annotation JSON must be an object")
    if isinstance(value.get("tags"), Mapping):
        if set(value) != {"tags"} or set(value["tags"]) != set(JSON_FIELDS):
            raise ClassifyJsonError("legacy nested annotation JSON must contain exactly the nine known fields")
        value = dict(value["tags"])
    _validate_known_fields(value)
    return value


def _validate_known_fields(value: Mapping[str, object]) -> None:
    for field in ARRAY_FIELDS:
        if field in value and (
            not isinstance(value[field], list)
            or any(not isinstance(tag, str) for tag in value[field])
        ):
            raise ClassifyJsonError(f"annotation JSON field {field} must be a string array")
    for field in STRING_FIELDS:
        if field in value and not isinstance(value[field], str):
            raise ClassifyJsonError(f"annotation JSON field {field} must be a string")


def original_count(value: Mapping[str, object] | None) -> str | int | None:
    if value is None:
        return None
    direct = value.get("count")
    if direct is not None:
        if type(direct) is int or isinstance(direct, str):
            return direct
        raise ClassifyJsonError("annotation JSON count must be a string or integer")
    nested = value.get("tags")
    if isinstance(nested, Mapping) and "count" in nested:
        candidate = nested["count"]
        if type(candidate) is int or isinstance(candidate, str):
            return candidate
        raise ClassifyJsonError("legacy tags.count must be a string or integer")
    return None


def compose_classify_json(
    existing: Mapping[str, object] | None,
    result: ClassifyResultV1,
    *,
    overwrite_mode: str,
    overwrite_json: bool,
    raw_e621: RawE621Annotation | None = None,
    nl_override: str | None = None,
) -> dict[str, object]:
    if overwrite_mode not in {"incremental", "rebuild"} or type(overwrite_json) is not bool:
        raise ClassifyJsonError("classify JSON write policy is invalid")
    projection = result.projection.to_dict()
    if raw_e621 is not None:
        projection["artist"] = raw_e621.artist
        projection["character"] = raw_e621.character
        data = projection
    else:
        if existing is not None and "character" in existing:
            projection["character"] = existing["character"]
        if existing is None or overwrite_mode == "rebuild" or overwrite_json:
            data = projection
        else:
            # Fixed nine-field order first, extra fields appended in their original insertion
            # order, so the same payload always serializes to the same bytes.
            values = {**existing, "count": result.countDecision.value}
            data = {field: values[field] for field in JSON_FIELDS if field in values}
            data.update({key: value for key, value in values.items() if key not in JSON_FIELDS})
    return {**data, "nl": nl_override} if nl_override is not None else data


def serialize_annotation_json(value: Mapping[str, object]) -> bytes:
    _validate_known_fields(value)
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ClassifyJsonError("annotation JSON cannot be serialized") from exc
    data = text.encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ClassifyJsonError("serialized annotation JSON exceeds 1 MiB")
    return data


@dataclass(frozen=True)
class ClassifyOverlayWriter:
    """Durably stage and commit a core-composed classify JSON overlay artifact."""

    database: StateDatabase
    layout: OverlayLayout
    job_id: str

    def __post_init__(self) -> None:
        job = self.database.get_job(self.job_id)
        if self.layout.job_id != self.job_id:
            raise OverlayError("classify overlay does not match the job")
        if not windows_paths_equal(self.layout.dataset_root, str(job["dataset_root"])):
            raise OverlayError("classify overlay does not match the immutable dataset root")
        overlay_root = job["overlay_root"]
        if not isinstance(overlay_root, str) or not overlay_root or not windows_paths_equal(self.layout.root, overlay_root):
            raise OverlayError("classify overlay root does not match the persisted job")

    @classmethod
    def open_for_job(cls, database: StateDatabase, job_id: str) -> "ClassifyOverlayWriter":
        job = database.get_job(job_id)
        overlay_root = job["overlay_root"]
        if not isinstance(overlay_root, str) or not overlay_root:
            raise OverlayError("classify job has no prepared annotation overlay")
        return cls(database, OverlayLayout.open_existing(overlay_root, job_id), job_id)

    def write(
        self,
        item: ClassifyWorkItemV1,
        data: bytes,
        *,
        count_decision: Mapping[str, object],
    ) -> None:
        if not data or data.startswith(b"\xef\xbb\xbf"):
            raise OverlayError("classify JSON must be non-empty UTF-8 without BOM")
        parse_annotation_json(data)
        prepared, digest = self.layout.write_prepared("classify", item.leaseId, ".json", data)
        relative = os.path.relpath(prepared, self.layout.root).replace("/", "\\")
        self.database.stage_classify_prepared_artifact(
            self.job_id,
            item.sampleId,
            lease_id=item.leaseId,
            relative_path=relative,
            sha256=digest,
            count_decision=count_decision,
        )
        self.layout.commit_prepared(relative, digest, item.annotationKey, ".json")

    def recover_prepared(self, job_id: str, sample_id: int, prepared_relative_path: str, expected_sha256: str) -> bool:
        if job_id != self.job_id or not SHA256.fullmatch(expected_sha256):
            raise OverlayError("classify recovery identity is invalid")
        row = self.database.get_sample_with_state(job_id, sample_id)
        if row["current_module_id"] != "classify" or row["status"] != "prepared":
            raise OverlayError("classify recovery state is not prepared")
        stored_relative = row["prepared_artifact_relative_path"]
        if (
            not isinstance(stored_relative, str)
            or safe_relative_path(stored_relative) != safe_relative_path(prepared_relative_path)
            or row["prepared_artifact_sha256"] != expected_sha256
        ):
            raise OverlayError("classify recovery metadata does not match SQLite")
        destination = self.layout.annotation_path(str(row["annotation_key"]), ".json")
        prepared = self.layout.resolve_prepared(prepared_relative_path)
        if destination.is_file() and sha256_file(destination) == expected_sha256:
            if prepared.is_file() and sha256_file(prepared) == expected_sha256:
                prepared.unlink()
            return True
        if not prepared.is_file() or sha256_file(prepared) != expected_sha256:
            return False
        committed = self.layout.commit_prepared(prepared_relative_path, expected_sha256, str(row["annotation_key"]), ".json")
        return sha256_file(committed) == expected_sha256
