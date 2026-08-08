from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.overlay import OverlayLayout
from anima_core.path_safety import windows_key
from anima_core.replace_overlay import ReplaceOverlayWriter
from anima_core.scheduler import BoundedScheduler


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("/", "\\"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def _projection() -> dict[str, object]:
    return {
        "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
        "appearance": [], "tags": ["replacement"], "environment": [], "nl": "",
    }


class ReplaceOverlayFixture:
    def __init__(self, root: Path) -> None:
        self.dataset = root / "dataset"
        self.dataset.mkdir()
        (self.dataset / "sample.png").write_bytes(b"immutable-image")
        (self.dataset / "sample.json").write_text(json.dumps(_projection()), encoding="utf-8")
        self.layout = OverlayLayout.create(self.dataset, "job-replace-overlay")
        self.database = StateDatabase.open(root / "state.db")
        self.config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(self.dataset))
        self.config.caption["enabled"] = False
        self.config.classify["enabled"] = False
        self.config.countReview["enabled"] = False  # type: ignore[index]
        self.database.insert_job({
            "job_id": "job-replace-overlay", "config_schema_version": self.config.schemaVersion, "config_json": json.dumps(self.config.to_dict()),
            "config_hash": self.config.config_hash, "profile": "e621", "work_mode": "in_place",
            "overwrite_mode": "incremental", "source_root": str(self.dataset), "output_root": None,
            "dataset_root": str(self.dataset), "dataset_root_key": windows_key(self.dataset), "manifest_schema_version": 1,
            "recursive": 0, "sample_count": 1, "manifest_generated_at": "2026-07-24T00:00:00Z",
            "status": "ready", "current_module_id": None, "last_event_id": 0, "pinned": 0,
            "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": str(self.layout.root),
            "commit_journal_path": None, "resume_status": None, "created_at": "2026-07-24T00:00:00Z",
            "started_at": None, "cancel_requested_at": None, "finished_at": None,
        })
        self.database.insert_samples("job-replace-overlay", [{
            "sample_id": 1, "relative_image_path": "sample.png", "annotation_key": "sample", "source": "e621",
            "in_processing_scope": True, "image_format": "png", "image_frame_count": 1,
            "original_txt_state": "missing_or_blank", "original_json_state": "nonblank", "image_file_id": "volume:1",
            "image_size": len(b"immutable-image"), "image_mtime_ns": 1_000_000,
        }])
        self.scheduler = BoundedScheduler(self.database, lease_id_factory=lambda: "lease-replace-overlay")
        self.scheduler.start_module("job-replace-overlay", "caption", enabled=False, profile="e621")
        self.scheduler.start_module("job-replace-overlay", "classify", enabled=False, profile="e621")
        self.scheduler.start_module("job-replace-overlay", "replace", enabled=True, profile="e621")
        self.lease = self.scheduler.claim_batch(
            "job-replace-overlay", "replace", "replace-worker-1", self.config.config_hash, limit=1,
        )[0]
        self.writer = ReplaceOverlayWriter(self.database, self.layout, "job-replace-overlay")

    def write_prepared(self) -> tuple[str, str]:
        data = json.dumps(_projection(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        prepared, digest = self.layout.write_prepared("replace", str(self.lease.leaseId), ".json", data)
        return os.path.relpath(prepared, self.layout.root).replace("/", "\\"), digest

    def recover(self):
        self.database.mark_interrupted("job-replace-overlay")
        return self.scheduler.recover(
            "job-replace-overlay", confirmed=True, expected_config_hash=self.config.config_hash,
            manifest_schema_version=1, protocol_version="1.0", verify_source_fingerprints=lambda: True,
            commit_prepared=self.writer.recover_prepared,
        )

    def close(self) -> None:
        self.database.close()
        if self.layout.root.exists():
            self.layout.discard()


class ReplaceOverlayTests(unittest.TestCase):
    def test_unmatched_tags_use_a_bounded_module_info_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceOverlayFixture(Path(temporary))
            try:
                fixture.database.increment_module_diagnostic(
                    "job-replace-overlay", "replace", "replace_passthrough", severity="info", amount=2,
                )
                fixture.database.increment_module_diagnostic(
                    "job-replace-overlay", "replace", "replace_passthrough", severity="info", amount=3,
                )
                diagnostic = fixture.database.module_diagnostics("job-replace-overlay", "replace")
                self.assertEqual((1, "replace_passthrough", "info", 5), (
                    len(diagnostic), diagnostic[0]["code"], diagnostic[0]["severity"], diagnostic[0]["count"],
                ))
                self.assertEqual([], fixture.database.page_issues("job-replace-overlay", limit=10))
            finally:
                fixture.close()

    def test_writer_only_changes_overlay_then_completes_replace_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceOverlayFixture(Path(temporary))
            before = _hashes(fixture.dataset)
            try:
                fixture.writer.write(sample_id=1, lease_id=str(fixture.lease.leaseId), annotation_key="sample", projection=_projection())
                self.assertEqual("prepared", fixture.database.get_sample_state("job-replace-overlay", 1)["status"])
                self.assertEqual(_projection(), json.loads(fixture.layout.annotation_path("sample", ".json").read_text(encoding="utf-8")))
                fixture.scheduler.complete(fixture.lease)
                self.assertEqual("completed", fixture.database.get_sample_state("job-replace-overlay", 1)["status"])
                self.assertEqual(before, _hashes(fixture.dataset))
            finally:
                fixture.close()

    def test_provenance_marker_is_written_next_to_the_overlay_not_the_dataset(self) -> None:
        # Guards the re-run path: Replace is a single round, so a sample it already replaced
        # must be recognizable before its own output is fed back in.
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceOverlayFixture(Path(temporary))
            before = _hashes(fixture.dataset)
            try:
                self.assertIsNone(fixture.writer.provenance("sample"))
                fixture.writer.write(
                    sample_id=1, lease_id=str(fixture.lease.leaseId), annotation_key="sample",
                    projection=_projection(), provenance="f" * 64,
                )
                self.assertEqual("f" * 64, fixture.writer.provenance("sample"))
                self.assertTrue((fixture.layout.root / "resources" / "replace" / "provenance" / "sample.txt").is_file())
                self.assertEqual(before, _hashes(fixture.dataset))
            finally:
                fixture.close()

    def test_recovery_handles_all_replace_prepared_crash_windows(self) -> None:
        for window in ("orphan", "prepared", "target", "tampered"):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as temporary:
                fixture = ReplaceOverlayFixture(Path(temporary))
                before = _hashes(fixture.dataset)
                try:
                    relative, digest = fixture.write_prepared()
                    if window != "orphan":
                        fixture.database.stage_prepared_artifact("job-replace-overlay", 1, lease_id=str(fixture.lease.leaseId), relative_path=relative, sha256=digest)
                    if window == "target":
                        fixture.layout.commit_prepared(relative, digest, "sample", ".json")
                    if window == "tampered":
                        fixture.layout.resolve_prepared(relative).write_bytes(b"tampered")
                    report = fixture.recover()
                    state = fixture.database.get_sample_state("job-replace-overlay", 1)
                    if window in {"prepared", "target"}:
                        self.assertEqual((1, 0, "completed"), (report.committedPrepared, report.repeatedPrepared, state["status"]))
                    elif window == "orphan":
                        self.assertEqual((1, 0, "pending"), (report.returnedLeases, report.repeatedPrepared, state["status"]))
                    else:
                        self.assertEqual((0, 1, "pending"), (report.returnedLeases, report.repeatedPrepared, state["status"]))
                    self.assertEqual(before, _hashes(fixture.dataset))
                finally:
                    fixture.close()


if __name__ == "__main__":
    unittest.main()
