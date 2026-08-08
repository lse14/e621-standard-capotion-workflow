from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "src"))

from PIL import Image

from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.locks import DatasetLock, DatasetLockError
from anima_core.manifest import ManifestBuilder, ManifestError
from anima_core.overlay import BaselineView, OverlayLayout, WorkingAnnotationView
from anima_core.path_safety import (
    PathSafetyError,
    assert_no_reparse_tree,
    validate_source_output,
    windows_compare,
    windows_key,
)
from anima_core.workspace import prepare_dataset


def _job(job_id: str, root: Path) -> dict[str, object]:
    config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(root))
    return {
        "job_id": job_id, "config_schema_version": 2, "config_json": json.dumps(config.to_dict()), "config_hash": config.config_hash,
        "profile": "e621", "work_mode": "in_place", "overwrite_mode": "incremental", "source_root": str(root),
        "output_root": None, "dataset_root": str(root), "dataset_root_key": windows_key(root), "manifest_schema_version": 1,
        "recursive": 0, "sample_count": 0, "manifest_generated_at": None, "status": "ready", "current_module_id": None,
        "last_event_id": 0, "pinned": 0, "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": None,
        "commit_journal_path": None, "resume_status": None, "created_at": "2026-01-01T00:00:00Z", "started_at": None,
        "cancel_requested_at": None, "finished_at": None,
    }


class PathLockMatrixTests(unittest.TestCase):
    def test_source_output_same_parent_child_and_empty_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            empty = root / "empty"
            empty.mkdir()
            _, output = validate_source_output(source, empty, "full_copy")
            self.assertEqual(empty, output.value if output else None)
            with self.assertRaises(PathSafetyError):
                validate_source_output(source, source, "full_copy")
            with self.assertRaises(PathSafetyError):
                validate_source_output(source, source / "child", "full_copy")
            (empty / "existing.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(PathSafetyError):
                validate_source_output(source, empty, "full_copy")

    def test_same_stem_collision_and_rebuild_view_do_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            Image.new("RGB", (1, 1), "white").save(source / "same.png")
            Image.new("RGB", (1, 1), "white").save(source / "same.jpg")
            with self.assertRaises(ManifestError):
                list(ManifestBuilder(source, recursive=False).iter_records())
            (source / "only.json").write_text('{"nl":"baseline"}', encoding="utf-8")
            layout = OverlayLayout.create(source, "job-1")
            try:
                rebuild = WorkingAnnotationView(BaselineView(source), layout, allow_baseline_fallback=False)
                self.assertIsNone(rebuild.read("only", ".json"))
            finally:
                layout.discard()

    @unittest.skipUnless(os.name == "nt", "dataset locking uses Win32 share-mode semantics")
    def test_same_dataset_cannot_be_claimed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            database = StateDatabase.open(root / "state.db")
            try:
                database.insert_job(_job("job-1", dataset))
                database.insert_job(_job("job-2", dataset))
                lock = DatasetLock.acquire(database, dataset, "job-1")
                try:
                    self.assertTrue(lock.lock_path.exists())
                    claim = database.connection.execute(
                        "SELECT job_id FROM dataset_claims WHERE dataset_root=?", (str(dataset).swapcase(),)
                    ).fetchone()
                    self.assertEqual("job-1", claim["job_id"] if claim else None)
                    self.assertEqual(0, windows_compare("Folder\\Image-😀", "folder\\image-😀"))
                    with self.assertRaises(DatasetLockError):
                        DatasetLock.acquire(database, dataset, "job-2")
                    with self.assertRaises(DatasetLockError):
                        lock.release(recovery_complete=False)
                finally:
                    lock.release(recovery_complete=True)
                self.assertFalse(lock.lock_path.exists())
                self.assertEqual(0, database.connection.execute("SELECT COUNT(*) FROM dataset_claims").fetchone()[0])
            finally:
                database.close()

    def test_reparse_point_is_blocking_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            link = source / "linked"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (NotImplementedError, OSError):
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if result.returncode != 0:
                    self.skipTest("current Windows account cannot create a symlink or junction")
            with self.assertRaises(PathSafetyError):
                assert_no_reparse_tree(source)

    def test_full_copy_permission_failure_leaves_source_and_output_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            original = source / "image.png"
            original.write_bytes(b"immutable image")
            output = root / "output"
            with patch("anima_core.workspace.Path.mkdir", side_effect=PermissionError("output parent is not writable")):
                with self.assertRaises(PermissionError):
                    prepare_dataset(source, output, "full_copy", "job-permission")
            self.assertEqual(b"immutable image", original.read_bytes())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
