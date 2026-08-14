from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "packaging" / "installer" / "paths.py"


def _load_module():
    if not MODULE_PATH.is_file():
        return None
    name = "source_bootstrap_paths_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("paths module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SourceBootstrapPathTests(unittest.TestCase):
    def _module(self):
        module = _load_module()
        self.assertIsNotNone(module, "source bootstrap paths module must exist")
        return module

    def test_safe_relative_rejects_escape_drive_device_and_trailing_space(self) -> None:
        module = self._module()
        for value in ("..\\outside", "C:\\outside", "\\\\server\\share", "COM1\\payload", "safe\\name. "):
            with self.subTest(value=value):
                with self.assertRaisesRegex(module.PathSafetyError, "relative path"):
                    module.safe_relative(value)
        self.assertEqual("runtime\\core", module.safe_relative("runtime/core"))

    def test_safe_extract_rejects_parent_member_before_creating_output(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            archive = root / "escape.zip"
            with zipfile.ZipFile(archive, "w") as value:
                value.writestr("..\\outside.txt", b"unsafe")

            with self.assertRaisesRegex(module.PathSafetyError, "unsafe archive member"):
                module.safe_extract_zip(archive, root / "stage")

            self.assertFalse((root.parent / "outside.txt").exists())
            self.assertFalse((root / "stage").exists())

    def test_safe_extract_rejects_case_collision_and_link_member(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            collision = root / "collision.zip"
            with zipfile.ZipFile(collision, "w") as value:
                value.writestr("model.bin", b"one")
                value.writestr("MODEL.BIN", b"two")
            with self.assertRaisesRegex(module.PathSafetyError, "case collision"):
                module.safe_extract_zip(collision, root / "collision-stage")

            link = root / "link.zip"
            with zipfile.ZipFile(link, "w") as value:
                entry = zipfile.ZipInfo("link")
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
                value.writestr(entry, b"target")
            with self.assertRaisesRegex(module.PathSafetyError, "link"):
                module.safe_extract_zip(link, root / "link-stage")

    def test_safe_extract_rejects_non_regular_posix_member(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            archive = root / "special.zip"
            with zipfile.ZipFile(archive, "w") as value:
                entry = zipfile.ZipInfo("named-pipe")
                entry.create_system = 3
                entry.external_attr = 0o010644 << 16
                value.writestr(entry, b"unsafe")

            with self.assertRaisesRegex(module.PathSafetyError, "unsupported"):
                module.safe_extract_zip(archive, root / "special-stage")

            self.assertFalse((root / "special-stage").exists())

    def test_publish_replaces_target_only_after_stage_is_complete(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = module.ProjectLayout.create(root)
            layout.ensure_directories()
            target = root / ".runtime-build" / "runtimes" / "core"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="ascii")
            stage = layout.staging / "core-stage"
            stage.mkdir(parents=True)
            (stage / "new.txt").write_text("new", encoding="ascii")

            module.publish_directories(layout, {".runtime-build/runtimes/core": stage})

            self.assertEqual("new", (target / "new.txt").read_text(encoding="ascii"))
            self.assertFalse((target / "old.txt").exists())
            self.assertFalse(stage.exists())
            self.assertEqual([], list(layout.transactions.glob("*.json")))

    def test_recovery_restores_old_target_after_interrupted_promotion(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = module.ProjectLayout.create(root)
            layout.ensure_directories()
            target = root / "runtimes" / "core"
            target.mkdir(parents=True)
            (target / "new.txt").write_text("new", encoding="ascii")
            backup = layout.transactions / "backups" / "core-backup"
            backup.mkdir(parents=True)
            (backup / "old.txt").write_text("old", encoding="ascii")
            stage = layout.staging / "core-stage"
            stage.mkdir(parents=True)
            (stage / "stage.txt").write_text("stage", encoding="ascii")
            journal = layout.transactions / "interrupted.json"
            journal.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "entries": [
                            {
                                "targetRelativePath": "runtimes\\core",
                                "stageRelativePath": str(stage.relative_to(root)).replace("/", "\\"),
                                "backupRelativePath": str(backup.relative_to(root)).replace("/", "\\"),
                                "previousMoved": True,
                                "promoted": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            module.recover_transactions(layout)

            self.assertEqual("old", (target / "old.txt").read_text(encoding="ascii"))
            self.assertFalse((target / "new.txt").exists())
            self.assertFalse(journal.exists())

    def test_publish_rejects_data_or_output_target_without_replacing_it(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = module.ProjectLayout.create(root)
            layout.ensure_directories()
            protected = root / "data"
            protected.mkdir()
            (protected / "user-file.txt").write_text("keep", encoding="ascii")
            stage = layout.staging / "replacement"
            stage.mkdir()
            (stage / "new-file.txt").write_text("unsafe", encoding="ascii")

            with self.assertRaisesRegex(module.PathSafetyError, "publish target"):
                module.publish_directories(layout, {"data": stage})

            self.assertEqual("keep", (protected / "user-file.txt").read_text(encoding="ascii"))
            self.assertTrue(stage.exists())

    def test_failure_cleanup_preserves_only_partial_and_logs(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = module.ProjectLayout.create(root)
            layout.ensure_directories()
            (layout.bootstrap / "python.exe").write_bytes(b"bootstrap")
            (layout.cache / "complete").write_bytes(b"complete")
            (layout.cache / "resume.partial").write_bytes(b"partial")
            (layout.staging / "failed").mkdir()
            (layout.transactions / "unfinished.json").write_text("{}", encoding="utf-8")
            log = layout.logs / "install.log"
            log.write_text("failure", encoding="utf-8")

            module.cleanup_failure(layout)

            self.assertFalse(layout.bootstrap.exists())
            self.assertFalse((layout.cache / "complete").exists())
            self.assertEqual(b"partial", (layout.cache / "resume.partial").read_bytes())
            self.assertFalse(layout.staging.exists())
            self.assertFalse(layout.transactions.exists())
            self.assertEqual("failure", log.read_text(encoding="utf-8"))

    def test_failure_cleanup_can_preserve_running_bootstrap_runtime(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = module.ProjectLayout.create(root)
            layout.ensure_directories()
            (layout.bootstrap / "python.exe").write_bytes(b"bootstrap")
            (layout.cache / "verified").write_bytes(b"verified")
            (layout.staging / "failed").mkdir()
            (layout.transactions / "unfinished.json").write_text("{}", encoding="utf-8")

            module.cleanup_failure(layout, preserve_bootstrap=True, preserve_cache=True)

            self.assertTrue((layout.bootstrap / "python.exe").is_file())
            self.assertTrue((layout.cache / "verified").is_file())
            self.assertFalse(layout.staging.exists())
            self.assertFalse(layout.transactions.exists())

    def test_success_cleanup_can_preserve_running_bootstrap_runtime(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = module.ProjectLayout.create(root)
            layout.ensure_directories()
            (layout.bootstrap / "python.exe").write_bytes(b"bootstrap")
            (layout.cache / "verified").write_bytes(b"verified")
            (layout.staging / "complete").mkdir()
            (layout.transactions / "complete.json").write_text("{}", encoding="utf-8")

            module.cleanup_success(layout, preserve_bootstrap=True)

            self.assertTrue((layout.bootstrap / "python.exe").is_file())
            self.assertFalse(layout.cache.exists())
            self.assertFalse(layout.staging.exists())
            self.assertFalse(layout.transactions.exists())


if __name__ == "__main__":
    unittest.main()
