from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "src"))

from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.lifecycle import JobLifecycle, JobLifecycleError
from anima_core.overlay import OverlayLayout
from anima_core.path_safety import windows_key
from anima_core.retention import RetentionManager
from anima_core.state_machine import InvalidTransition


def _job(job_id: str, root: Path, *, status: str = "failed", overlay_root: str | None = None) -> dict[str, object]:
    config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(root))
    return {
        "job_id": job_id, "config_schema_version": 2, "config_json": json.dumps(config.to_dict()),
        "config_hash": config.config_hash, "profile": "e621", "work_mode": "in_place", "overwrite_mode": "incremental",
        "source_root": str(root), "output_root": None, "dataset_root": str(root), "dataset_root_key": windows_key(root),
        "manifest_schema_version": 1, "recursive": 0, "sample_count": 0, "manifest_generated_at": None,
        "status": status, "current_module_id": None, "last_event_id": 0, "pinned": 0,
        "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": overlay_root, "commit_journal_path": None,
        "resume_status": None, "created_at": "2026-01-01T00:00:00Z", "started_at": None,
        "cancel_requested_at": None, "finished_at": "2026-01-01T00:00:00Z" if status == "succeeded" else None,
    }


class LifecycleAndRetentionTests(unittest.TestCase):
    def test_discard_needs_confirmation_and_resolved_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            layout = OverlayLayout.create(dataset, "job-1")
            database = StateDatabase.open(root / "state.db")
            try:
                database.insert_job(_job("job-1", dataset, overlay_root=str(layout.root)))
                lifecycle = JobLifecycle(database)
                with self.assertRaises(InvalidTransition):
                    lifecycle.discard("job-1", confirmed=False)
                layout.write_journal({"state": "rollback_created"})
                with self.assertRaises(JobLifecycleError):
                    lifecycle.discard("job-1", confirmed=True)
                layout.write_journal({"state": "rolled_back"})
                result = lifecycle.discard("job-1", confirmed=True)
                self.assertTrue(result.overlayDeleted)
                self.assertEqual("discarded", database.get_job("job-1")["status"])
            finally:
                database.close()

    def test_discard_and_startup_sweep_release_dataset_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            other = root / "other"
            dataset.mkdir()
            other.mkdir()
            database = StateDatabase.open(root / "state.db")
            try:
                database.insert_job(_job("job-discard", dataset))
                database.insert_job(_job("job-done", other, status="succeeded"))
                for job_id, path in (("job-discard", dataset), ("job-done", other)):
                    database.connection.execute(
                        """INSERT INTO dataset_claims(dataset_root,dataset_root_key,job_id,lock_path,acquired_at)
                           VALUES (?,?,?,?,?)""",
                        (str(path), str(path), job_id, str(path) + ".lock", "2026-01-01T00:00:00Z"),
                    )
                # Before the fix a discarded or finished job kept claiming its
                # dataset forever, so no later job could ever acquire it.
                JobLifecycle(database).discard("job-discard", confirmed=True)
                self.assertEqual(
                    ["job-done"],
                    [str(row["job_id"]) for row in database.connection.execute("SELECT job_id FROM dataset_claims")],
                )
                self.assertEqual(1, database.clear_stale_dataset_claims())
                self.assertEqual([], list(database.connection.execute("SELECT job_id FROM dataset_claims")))
            finally:
                database.close()

    def test_retention_keeps_newest_unpinned_and_never_touches_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            logs = root / "logs"
            logs.mkdir()
            backup = root / "annotation-backups" / "original.zip"
            backup.parent.mkdir()
            backup.write_bytes(b"permanent")
            database = StateDatabase.open(root / "state.db")
            try:
                for index in range(22):
                    job_id = f"job-{index:02d}"
                    database.insert_job(_job(job_id, dataset, status="succeeded"))
                    database.connection.execute(
                        "UPDATE jobs SET finished_at=? WHERE job_id=?", (f"2026-01-01T00:00:{index:02d}Z", job_id)
                    )
                    (logs / f"{job_id}.log").write_text("ordinary log", encoding="utf-8")
                database.set_pinned("job-00", True)
                manager = RetentionManager(database, logs_root=logs)
                candidates = manager.candidates()
                self.assertEqual({"job-01"}, {candidate.jobId for candidate in candidates})
                result = manager.cleanup()
                self.assertEqual({"job-01"}, set(result.deletedJobIds))
                self.assertTrue(backup.exists())
                self.assertEqual("succeeded", database.get_job("job-00")["status"])
            finally:
                database.close()

    def test_retention_protects_unresolved_overlay_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            layout = OverlayLayout.create(dataset, "job-1")
            layout.write_journal({"state": "prepared"})
            database = StateDatabase.open(root / "state.db")
            try:
                database.insert_job(_job("job-1", dataset, status="succeeded", overlay_root=str(layout.root)))
                database.connection.execute("UPDATE jobs SET commit_journal_path=? WHERE job_id=?", (str(layout.commit_journal_path()), "job-1"))
                manager = RetentionManager(database, keep_successes=0)
                self.assertEqual([], manager.candidates())
                layout.write_journal({"state": "rolled_back"})
                self.assertEqual(["job-1"], [candidate.jobId for candidate in manager.candidates()])
                manager.cleanup()
                with self.assertRaises(KeyError):
                    database.get_job("job-1")
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
