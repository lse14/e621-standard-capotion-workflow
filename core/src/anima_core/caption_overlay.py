from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .caption_protocol import CaptionResultV1, CaptionWorkItemV1
from .db import StateDatabase
from .overlay import OverlayError, OverlayLayout
from .path_safety import safe_relative_path, sha256_file, windows_paths_equal


SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CaptionOverlayWriter:
    """Durably stage and commit one validated caption result inside its overlay."""

    database: StateDatabase
    layout: OverlayLayout
    job_id: str

    def __post_init__(self) -> None:
        job = self.database.get_job(self.job_id)
        if self.layout.job_id != self.job_id:
            raise OverlayError("caption overlay does not match the job")
        if not windows_paths_equal(self.layout.dataset_root, str(job["dataset_root"])):
            raise OverlayError("caption overlay does not match the immutable dataset root")
        overlay_root = job["overlay_root"]
        if not isinstance(overlay_root, str) or not overlay_root or not windows_paths_equal(
            self.layout.root,
            overlay_root,
        ):
            raise OverlayError("caption overlay root does not match the persisted job")

    @classmethod
    def open_for_job(cls, database: StateDatabase, job_id: str) -> "CaptionOverlayWriter":
        job = database.get_job(job_id)
        overlay_root = job["overlay_root"]
        if not isinstance(overlay_root, str) or not overlay_root:
            raise OverlayError("caption job has no prepared annotation overlay")
        return cls(database, OverlayLayout.open_existing(overlay_root, job_id), job_id)

    def __call__(self, item: CaptionWorkItemV1, result: CaptionResultV1) -> None:
        if (
            result.sampleId != item.sampleId
            or result.leaseId != item.leaseId
            or result.relativeImagePath != item.relativeImagePath
        ):
            raise OverlayError("caption result does not match its prepared work item")
        data = result.formattedTxt.encode("utf-8")
        if not data or data.startswith(b"\xef\xbb\xbf") or b"\r" in data or b"\n" in data:
            raise OverlayError("caption TXT must be non-empty UTF-8 without BOM or line breaks")
        prepared, digest = self.layout.write_prepared("caption", item.leaseId, ".txt", data)
        relative = os.path.relpath(prepared, self.layout.root).replace("/", "\\")
        self.database.stage_prepared_artifact(
            self.job_id,
            item.sampleId,
            lease_id=item.leaseId,
            relative_path=relative,
            sha256=digest,
        )
        self.layout.commit_prepared(relative, digest, item.annotationKey, ".txt")

    def recover_prepared(
        self,
        job_id: str,
        sample_id: int,
        prepared_relative_path: str,
        expected_sha256: str,
    ) -> bool:
        if job_id != self.job_id or not SHA256.fullmatch(expected_sha256):
            raise OverlayError("caption recovery identity is invalid")
        row = self.database.get_sample_with_state(job_id, sample_id)
        if row["current_module_id"] != "caption" or row["status"] != "prepared":
            raise OverlayError("caption recovery state is not prepared")
        stored_relative = row["prepared_artifact_relative_path"]
        stored_digest = row["prepared_artifact_sha256"]
        if (
            not isinstance(stored_relative, str)
            or safe_relative_path(stored_relative) != safe_relative_path(prepared_relative_path)
            or stored_digest != expected_sha256
        ):
            raise OverlayError("caption recovery metadata does not match SQLite")
        destination = self.layout.annotation_path(str(row["annotation_key"]), ".txt")
        if destination.is_file() and sha256_file(destination) == expected_sha256:
            prepared = self.layout.resolve_prepared(prepared_relative_path)
            if prepared.is_file() and sha256_file(prepared) == expected_sha256:
                prepared.unlink()
            return True
        prepared = self.layout.resolve_prepared(prepared_relative_path)
        if not prepared.is_file() or sha256_file(prepared) != expected_sha256:
            return False
        committed = self.layout.commit_prepared(
            prepared_relative_path,
            expected_sha256,
            str(row["annotation_key"]),
            ".txt",
        )
        return sha256_file(committed) == expected_sha256
