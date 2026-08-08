from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .classify_overlay import parse_annotation_json, serialize_annotation_json
from .db import StateDatabase
from .overlay import OverlayError, OverlayLayout
from .path_safety import safe_relative_path, sha256_file, windows_paths_equal


SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_PREFIX = "replace\\provenance\\"


def _provenance_relative(annotation_key: str) -> str:
    return PROVENANCE_PREFIX + safe_relative_path(annotation_key) + ".txt"


@dataclass(frozen=True)
class ReplaceOverlayWriter:
    """Persist one validated Replace JSON projection through the annotation overlay."""

    database: StateDatabase
    layout: OverlayLayout
    job_id: str

    def __post_init__(self) -> None:
        job = self.database.get_job(self.job_id)
        if self.layout.job_id != self.job_id or not windows_paths_equal(self.layout.dataset_root, str(job["dataset_root"])):
            raise OverlayError("replace overlay does not match the immutable job")
        if not isinstance(job["overlay_root"], str) or not windows_paths_equal(self.layout.root, str(job["overlay_root"])):
            raise OverlayError("replace overlay root does not match the persisted job")

    def provenance(self, annotation_key: str) -> str | None:
        """Replacement is a single non-recursive round, so a re-run must not replace twice."""
        path = self.layout.resource_path(_provenance_relative(annotation_key))
        try:
            return path.read_text(encoding="utf-8").strip() if path.is_file() else None
        except OSError:
            return None

    def mark_provenance(self, annotation_key: str, fingerprint: str) -> None:
        self.layout.write_resource(_provenance_relative(annotation_key), fingerprint.encode("utf-8"))

    def write(self, *, sample_id: int, lease_id: str, annotation_key: str, projection: dict[str, object], provenance: str | None = None) -> None:
        data = serialize_annotation_json(projection)
        if parse_annotation_json(data) != projection:
            raise OverlayError("replace projection does not round-trip as strict JSON")
        prepared, digest = self.layout.write_prepared("replace", lease_id, ".json", data)
        relative = os.path.relpath(prepared, self.layout.root).replace("/", "\\")
        self.database.stage_prepared_artifact(
            self.job_id, sample_id, lease_id=lease_id, relative_path=relative, sha256=digest,
        )
        self.layout.commit_prepared(relative, digest, annotation_key, ".json")
        if provenance is not None:
            self.mark_provenance(annotation_key, provenance)

    def recover_prepared(self, job_id: str, sample_id: int, prepared_relative_path: str, expected_sha256: str) -> bool:
        if job_id != self.job_id or not SHA256.fullmatch(expected_sha256):
            raise OverlayError("replace recovery identity is invalid")
        row = self.database.get_sample_with_state(job_id, sample_id)
        if row["current_module_id"] != "replace" or row["status"] != "prepared":
            raise OverlayError("replace recovery state is not prepared")
        if (
            not isinstance(row["prepared_artifact_relative_path"], str)
            or safe_relative_path(str(row["prepared_artifact_relative_path"])) != safe_relative_path(prepared_relative_path)
            or row["prepared_artifact_sha256"] != expected_sha256
        ):
            raise OverlayError("replace recovery metadata does not match SQLite")
        target = self.layout.annotation_path(str(row["annotation_key"]), ".json")
        prepared = self.layout.resolve_prepared(prepared_relative_path)
        if target.is_file() and sha256_file(target) == expected_sha256:
            if prepared.is_file() and sha256_file(prepared) == expected_sha256:
                prepared.unlink()
            return True
        if not prepared.is_file() or sha256_file(prepared) != expected_sha256:
            return False
        committed = self.layout.commit_prepared(prepared_relative_path, expected_sha256, str(row["annotation_key"]), ".json")
        return sha256_file(committed) == expected_sha256
