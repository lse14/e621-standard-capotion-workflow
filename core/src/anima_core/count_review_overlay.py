from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .classify_overlay import ClassifyJsonError, parse_annotation_json, serialize_annotation_json
from .count_review_protocol import FINAL_COUNT_VALUES
from .contracts import WorkLease
from .db import StateDatabase
from .overlay import BaselineView, OverlayError, OverlayLayout, WorkingAnnotationView
from .path_safety import safe_relative_path, sha256_file, windows_paths_equal


SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CountReviewOverlayWriter:
    database: StateDatabase
    layout: OverlayLayout
    view: WorkingAnnotationView
    job_id: str

    def __post_init__(self) -> None:
        job = self.database.get_job(self.job_id)
        if self.layout.job_id != self.job_id or not windows_paths_equal(
            self.layout.dataset_root, str(job["dataset_root"])
        ):
            raise OverlayError("count review overlay does not match the immutable job")
        if not isinstance(job["overlay_root"], str) or not windows_paths_equal(
            self.layout.root, str(job["overlay_root"])
        ):
            raise OverlayError("count review overlay root does not match the persisted job")

    @classmethod
    def open_for_job(
        cls,
        database: StateDatabase,
        job_id: str,
    ) -> "CountReviewOverlayWriter":
        job = database.get_job(job_id)
        overlay_root = job["overlay_root"]
        if not isinstance(overlay_root, str) or not overlay_root:
            raise OverlayError("count review job has no annotation overlay")
        layout = OverlayLayout.open_existing(overlay_root, job_id)
        view = WorkingAnnotationView(
            baseline=BaselineView(layout.dataset_root),
            overlay=layout,
        )
        return cls(database, layout, view, job_id)

    def write(
        self,
        lease: WorkLease,
        *,
        annotation_key: str,
        final_count: str,
    ) -> None:
        if lease.jobId != self.job_id or lease.moduleId != "count_review" or not lease.leaseId:
            raise OverlayError("count review artifact does not belong to an active lease")
        if final_count not in FINAL_COUNT_VALUES:
            raise OverlayError("count review final count is invalid")
        try:
            projection = parse_annotation_json(self.view.read(annotation_key, ".json"))
        except ClassifyJsonError as exc:
            raise OverlayError("working JSON is invalid during count review") from exc
        if projection is None:
            raise OverlayError("working JSON is missing during count review")
        result = dict(projection)
        result["count"] = final_count
        data = serialize_annotation_json(result)
        prepared, digest = self.layout.write_prepared(
            "count_review", lease.leaseId, ".json", data
        )
        relative = os.path.relpath(prepared, self.layout.root).replace("/", "\\")
        self.database.stage_prepared_artifact(
            self.job_id,
            lease.sampleId,
            lease_id=lease.leaseId,
            relative_path=relative,
            sha256=digest,
        )
        self.layout.commit_prepared(relative, digest, annotation_key, ".json")

    def recover_prepared(
        self,
        job_id: str,
        sample_id: int,
        prepared_relative_path: str,
        expected_sha256: str,
    ) -> bool:
        if job_id != self.job_id or not SHA256.fullmatch(expected_sha256):
            raise OverlayError("count review recovery identity is invalid")
        row = self.database.get_sample_with_state(job_id, sample_id)
        if row["current_module_id"] != "count_review" or row["status"] != "prepared":
            raise OverlayError("count review recovery state is not prepared")
        stored_relative = row["prepared_artifact_relative_path"]
        if (
            not isinstance(stored_relative, str)
            or safe_relative_path(stored_relative) != safe_relative_path(prepared_relative_path)
            or row["prepared_artifact_sha256"] != expected_sha256
        ):
            raise OverlayError("count review recovery metadata does not match SQLite")
        target = self.layout.annotation_path(str(row["annotation_key"]), ".json")
        prepared = self.layout.resolve_prepared(prepared_relative_path)
        if target.is_file() and sha256_file(target) == expected_sha256:
            if prepared.is_file() and sha256_file(prepared) == expected_sha256:
                prepared.unlink()
            return True
        if not prepared.is_file() or sha256_file(prepared) != expected_sha256:
            return False
        committed = self.layout.commit_prepared(
            prepared_relative_path,
            expected_sha256,
            str(row["annotation_key"]),
            ".json",
        )
        return sha256_file(committed) == expected_sha256
