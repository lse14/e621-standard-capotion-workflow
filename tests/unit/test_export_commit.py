from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.annotation_backup import write_backup as write_backup_impl
from anima_core.contracts import JobConfig, SampleRunState
from anima_core.db import StateDatabase
from anima_core.export_commit import ExportCommitCoordinator, ExportCommitError
from anima_core.job_preflight import JobPreparationService
from anima_core.overlay import OverlayLayout
from anima_core.pipeline import PipelineService
from anima_core.replace_overlay import ReplaceOverlayWriter
from anima_core.replace_provenance import (
    DatasetReplaceProvenance,
    ReplaceProvenanceChange,
    apply_provenance_changes,
    provenance_database_path,
)
from anima_core.scheduler import BoundedScheduler


def _ocr_sidecar(relative_image_path: str, *, status: str = "success") -> bytes:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "relativeImagePath": relative_image_path,
        "image": {"width": 2, "height": 2, "sizeBytes": 4, "sha256": "a" * 64},
        "status": status,
        "engine": {"backend": "paddle", "resourceId": "ocr-ppocrv5-server-paddle-v1", "resourceFingerprint": "b" * 64},
        "settings": {"llmMinConfidence": 0.5, "inference": {
            "useDocOrientationClassify": False, "useDocUnwarping": False, "useTextlineOrientation": True,
            "textRecScoreThresh": 0, "textDetLimitSideLen": 1920, "textDetLimitType": "max",
        }},
        "items": [], "error": None,
    }
    if status == "success":
        value["items"] = [{
            "index": 0, "text": "Hello", "confidence": 0.9,
            "polygonPixels": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "polygon": [[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]],
            "bboxPixels": [0, 0, 1, 1], "bbox": [0, 0, 0.5, 0.5],
            "position": "top-left", "textlineOrientationDegrees": 0, "includedForLlm": True,
        }]
    elif status == "failed":
        value["error"] = {"code": "ocr_inference_failed", "message": "OCR engine failed.", "retriable": True}
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class ExportCommitTests(unittest.TestCase):
    def _prepared_job(self, root: Path, *, format_value: str = "both", extra_images: tuple[str, ...] = (), ocr_enabled: bool = False):
        dataset = root / "dataset"
        dataset.mkdir()
        Image.new("RGB", (2, 2), (20, 30, 40)).save(dataset / "sample.png")
        (dataset / "sample.json").write_bytes(b'{"legacy":true}\n')
        (dataset / "sample.txt").write_bytes(b"legacy\n")
        for image_name in extra_images:
            Image.new("RGB", (2, 2), (50, 60, 70)).save(dataset / image_name)
            annotation = Path(image_name).with_suffix("")
            (dataset / f"{annotation}.json").write_bytes(b'{"legacy":true}\n')
            (dataset / f"{annotation}.txt").write_bytes(b"legacy\n")
        config = JobConfig(workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset), recursive=True, schemaVersion=10)
        config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
        config.ocr["enabled"] = ocr_enabled
        config.countReview["enabled"] = False  # type: ignore[index]
        assert config.tokenBudget is not None
        config.tokenBudget["enabled"] = False
        config.export["format"] = format_value
        service = JobPreparationService(root / "state.db")
        job_id = service.preflight(config.to_dict()).jobId
        service.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
        database = StateDatabase.open(root / "state.db")
        scheduler = BoundedScheduler(database)
        for module_id in (
            "caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget",
        ):
            scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
        scheduler.start_module(job_id, "export", enabled=True, profile="e621")
        layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
        return database, service, job_id, dataset, layout

    def _complete_export(self, database: StateDatabase, job_id: str, layout: OverlayLayout, *, format_value: str, include_all: bool = True) -> None:
        artifacts = []
        samples = database.page_samples(job_id, limit=500)
        for sample in samples:
            sample_id = int(sample["sample_id"])
            lease = f"lease-{sample_id}"
            json_path, json_digest = layout.write_prepared("export", lease, ".json", b'{\n  "tags": [\n    "verified"\n  ]\n}\n')
            current = [(job_id, sample_id, "json", str(json_path.relative_to(layout.root)).replace("/", "\\"), json_digest)]
            if format_value in {"flat_txt", "both"} and include_all:
                txt_path, txt_digest = layout.write_prepared("export", f"{lease}-txt", ".txt", b"verified.\n")
                current.append((job_id, sample_id, "txt", str(txt_path.relative_to(layout.root)).replace("/", "\\"), txt_digest))
            if format_value == "flat_txt":
                current = [artifact for artifact in current if artifact[2] == "txt"]
            artifacts.extend(current)
        database.connection.executemany(
            "INSERT INTO export_artifacts(job_id,sample_id,kind,relative_path,sha256) VALUES (?,?,?,?,?)", artifacts,
        )
        for sample in samples:
            sample_id = int(sample["sample_id"])
            database.set_sample_state(job_id, sample_id, SampleRunState(
                sampleId=sample_id, currentModuleId="export", status="completed", attempt=1,
            ))
        database.set_module_summary(job_id, "export", status="completed", completed=len(samples), finished=True)

    @staticmethod
    def _staging_residue(dataset: Path) -> list[Path]:
        return [entry for entry in dataset.parent.iterdir() if ".anima-stage-" in entry.name]

    @staticmethod
    def _rollback_residue(dataset: Path) -> list[Path]:
        return [entry for entry in dataset.parent.iterdir() if ".anima-rollback-" in entry.name]

    @staticmethod
    def _business_bytes(dataset: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(dataset)).replace("/", "\\"): path.read_bytes()
            for path in dataset.rglob("*")
            if path.is_file() and path.suffix in {".json", ".txt"} and "ocr_annotations" not in path.parts
        }

    def test_commit_replaces_only_business_annotations_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                source_image_id = (dataset / "sample.png").stat().st_ino
                self._complete_export(database, job_id, layout, format_value="both")
                result = ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(1, result.exported)
                self.assertEqual("succeeded", database.get_job(job_id)["status"])
                self.assertEqual(b'{\n  "tags": [\n    "verified"\n  ]\n}\n', (dataset / "sample.json").read_bytes())
                self.assertEqual(b"verified.\n", (dataset / "sample.txt").read_bytes())
                self.assertEqual(source_image_id, (dataset / "sample.png").stat().st_ino)
                self.assertTrue(result.backupZip.is_file())
                self.assertFalse(layout.root.exists())
                job = database.get_job(job_id)
                self.assertIsNone(job["overlay_root"])
                self.assertIsNone(job["commit_journal_path"])
                if __import__("os").name == "nt":
                    attributes = __import__("ctypes").windll.kernel32.GetFileAttributesW(str(dataset))
                    self.assertFalse(bool(attributes & 0x2), "the committed dataset must not inherit the staging hidden attribute")
            finally:
                database.close()
                service.close()

    def test_incomplete_artifact_index_never_touches_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                original = (dataset / "sample.json").read_bytes()
                self._complete_export(database, job_id, layout, format_value="both", include_all=False)
                with self.assertRaises(ExportCommitError):
                    ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse(layout.commit_journal_path().exists())
            finally:
                database.close()
                service.close()

    def test_second_directory_rename_failure_restores_original_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                original = (dataset / "sample.json").read_bytes()
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="failed"))
                self._complete_export(database, job_id, layout, format_value="both")
                real_replace = __import__("os").replace
                def fail_second_replace(source, target):
                    if ".anima-stage-" in Path(source).name and Path(target) == dataset:
                        raise OSError("injected staging rename failure")
                    return real_replace(source, target)

                with patch("anima_core.export_commit.os.replace", side_effect=fail_second_replace), patch(
                    "anima_core.export_commit.replace_ocr_sidecar", wraps=__import__("anima_core.export_commit", fromlist=["replace_ocr_sidecar"]).replace_ocr_sidecar,
                ) as copied:
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                copied.assert_called_once()
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse((dataset / "ocr_annotations" / "sample.png.ocr.json").exists())
                self.assertEqual("failed", database.get_job(job_id)["status"])
                self.assertEqual([], self._staging_residue(dataset))
                PipelineService(Path(temporary) / "state.db", install_root=ROOT / ".runtime-build").recover_pending_commits()
                self.assertEqual("rolled_back", json.loads(layout.commit_journal_path().read_text(encoding="utf-8"))["state"])
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_first_rename_journal_failure_restores_the_complete_old_ocr_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                original = (dataset / "sample.json").read_bytes()
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="failed"))
                self._complete_export(database, job_id, layout, format_value="both")
                real_write = __import__("anima_core.export_commit", fromlist=["write_journal"]).write_journal
                failed = [False]

                def fail_once_after_first_rename(current_layout, journal):
                    if journal.state == "rollback_created" and not failed[0]:
                        failed[0] = True
                        raise OSError("injected first-rename journal failure")
                    return real_write(current_layout, journal)

                with patch("anima_core.export_commit.write_journal", side_effect=fail_once_after_first_rename):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertTrue(failed[0])
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse((dataset / "ocr_annotations" / "sample.png.ocr.json").exists())
                self.assertEqual([], self._staging_residue(dataset))
                self.assertEqual([], self._rollback_residue(dataset))
                self.assertEqual("rolled_back", json.loads(layout.commit_journal_path().read_text(encoding="utf-8"))["state"])
            finally:
                database.close()
                service.close()

    def test_second_rename_journal_failure_recovers_the_complete_new_ocr_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                sidecar = _ocr_sidecar("sample.png", status="failed")
                layout.write_ocr_sidecar("sample.png", sidecar)
                self._complete_export(database, job_id, layout, format_value="both")
                real_write = __import__("anima_core.export_commit", fromlist=["write_journal"]).write_journal
                failed = [False]

                def fail_once_after_second_rename(current_layout, journal):
                    if journal.state == "committed" and not failed[0]:
                        failed[0] = True
                        raise OSError("injected second-rename journal failure")
                    return real_write(current_layout, journal)

                with patch("anima_core.export_commit.write_journal", side_effect=fail_once_after_second_rename):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertTrue(failed[0])
                self.assertEqual(b'{\n  "tags": [\n    "verified"\n  ]\n}\n', (dataset / "sample.json").read_bytes())
                self.assertEqual(sidecar, (dataset / "ocr_annotations" / "sample.png.ocr.json").read_bytes())
                self.assertEqual("committed", json.loads(layout.commit_journal_path().read_text(encoding="utf-8"))["state"])
                PipelineService(Path(temporary) / "state.db", install_root=ROOT / ".runtime-build").recover_pending_commits()
                self.assertEqual("committed", json.loads(layout.commit_journal_path().read_text(encoding="utf-8"))["state"])
                self.assertEqual([], self._staging_residue(dataset))
                self.assertEqual([], self._rollback_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_staging_write_failure_never_changes_dataset_or_creates_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                original = (dataset / "sample.json").read_bytes()
                self._complete_export(database, job_id, layout, format_value="both")
                with patch("anima_core.export_commit.replace_business_annotation", side_effect=OSError("injected disk full")):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual("failed", database.get_job(job_id)["status"])
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_backup_failure_never_creates_journal_or_renames_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                original = (dataset / "sample.json").read_bytes()
                self._complete_export(database, job_id, layout, format_value="both")
                with patch("anima_core.export_commit.write_backup", side_effect=OSError("injected backup full")):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual("failed", database.get_job(job_id)["status"])
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_failure_after_backup_leaves_a_complete_old_directory_without_ocr_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="success"))
                self._complete_export(database, job_id, layout, format_value="both")
                def fail_after_backup(*args, **kwargs):
                    write_backup_impl(*args, **kwargs)
                    raise OSError("injected post-backup failure")

                with patch("anima_core.export_commit.write_backup", side_effect=fail_after_backup):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertFalse((dataset / "ocr_annotations" / "sample.png.ocr.json").exists())
                self.assertEqual(b'{"legacy":true}\n', (dataset / "sample.json").read_bytes())
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_externally_changed_baseline_annotation_aborts_before_any_directory_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                self._complete_export(database, job_id, layout, format_value="both")
                external = b"changed by another program\n"
                (dataset / "sample.txt").write_bytes(external)
                with self.assertRaises(ExportCommitError):
                    ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(external, (dataset / "sample.txt").read_bytes())
                self.assertEqual(b'{"legacy":true}\n', (dataset / "sample.json").read_bytes())
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_commit_records_replace_provenance_for_the_final_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                fingerprint = "a" * 64
                self._complete_export(database, job_id, layout, format_value="both")
                ReplaceOverlayWriter(database, layout, job_id).mark_provenance("sample", fingerprint)
                ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                final_digest = hashlib.sha256((dataset / "sample.json").read_bytes()).hexdigest()
                with DatasetReplaceProvenance.open(dataset) as provenance:
                    self.assertTrue(provenance.matches("sample", fingerprint, final_digest))
            finally:
                database.close()
                service.close()

    def test_flat_txt_commit_removes_replace_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(
                Path(temporary), format_value="flat_txt",
            )
            try:
                fingerprint = "b" * 64
                digest = hashlib.sha256((dataset / "sample.json").read_bytes()).hexdigest()
                apply_provenance_changes(dataset, [
                    ReplaceProvenanceChange.upsert("sample", fingerprint, digest),
                ])
                self._complete_export(database, job_id, layout, format_value="flat_txt")
                ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertFalse((dataset / "sample.json").exists())
                with DatasetReplaceProvenance.open(dataset) as provenance:
                    self.assertFalse(provenance.matches("sample", fingerprint, digest))
            finally:
                database.close()
                service.close()

    def test_failed_commit_does_not_change_dataset_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                old_fingerprint = "c" * 64
                old_digest = hashlib.sha256((dataset / "sample.json").read_bytes()).hexdigest()
                apply_provenance_changes(dataset, [
                    ReplaceProvenanceChange.upsert("sample", old_fingerprint, old_digest),
                ])
                database_bytes = provenance_database_path(dataset).read_bytes()
                self._complete_export(database, job_id, layout, format_value="both")
                ReplaceOverlayWriter(database, layout, job_id).mark_provenance("sample", "d" * 64)
                (dataset / "sample.txt").write_bytes(b"changed outside the task\n")
                with self.assertRaises(ExportCommitError):
                    ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(database_bytes, provenance_database_path(dataset).read_bytes())
                with DatasetReplaceProvenance.open(dataset) as provenance:
                    self.assertTrue(provenance.matches("sample", old_fingerprint, old_digest))
            finally:
                database.close()
                service.close()

    def test_missing_hardlink_support_or_free_space_stops_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary))
            try:
                self._complete_export(database, job_id, layout, format_value="both")
                with patch("anima_core.export_commit.os.link", side_effect=OSError("hard links unavailable")):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                usage = __import__("shutil").disk_usage(dataset.parent)
                with patch("anima_core.export_commit.shutil.disk_usage", return_value=usage._replace(free=0)):
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual([], self._staging_residue(dataset))
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual(b'{"legacy":true}\n', (dataset / "sample.json").read_bytes())
            finally:
                database.close()
                service.close()

    def test_commit_preserves_existing_replaces_new_and_keeps_failed_ocr_sidecars_out_of_business_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(
                Path(temporary), extra_images=("new.jpg", "failed.png"), ocr_enabled=True,
            )
            try:
                formal = dataset / "ocr_annotations"
                formal.mkdir()
                (formal / "sample.png.ocr.json").write_bytes(_ocr_sidecar("sample.png", status="no_text"))
                layout.write_ocr_sidecar("new.jpg", _ocr_sidecar("new.jpg", status="success"))
                layout.write_ocr_sidecar("failed.png", _ocr_sidecar("failed.png", status="failed"))
                self._complete_export(database, job_id, layout, format_value="both")
                result = ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(3, result.exported)
                self.assertEqual(_ocr_sidecar("sample.png", status="no_text"), (formal / "sample.png.ocr.json").read_bytes())
                self.assertEqual(_ocr_sidecar("new.jpg", status="success"), (formal / "new.jpg.ocr.json").read_bytes())
                self.assertEqual(_ocr_sidecar("failed.png", status="failed"), (formal / "failed.png.ocr.json").read_bytes())
                self.assertEqual(
                    {"sample.json", "new.json", "failed.json"},
                    {path.name for path in dataset.glob("*.json")},
                )
                self.assertTrue(all(path.read_bytes() == b'{\n  "tags": [\n    "verified"\n  ]\n}\n' for path in dataset.glob("*.json")))
                with __import__("zipfile").ZipFile(result.backupZip) as archive:
                    self.assertFalse(any(name.startswith("ocr_annotations/") for name in archive.namelist()))
            finally:
                database.close()
                service.close()

    def test_invalid_overlay_sidecar_fails_before_any_directory_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                layout.write_ocr_sidecar("sample.png", b"not-json")
                self._complete_export(database, job_id, layout, format_value="both")
                with self.assertRaises(ExportCommitError):
                    ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual(b'{"legacy":true}\n', (dataset / "sample.json").read_bytes())
            finally:
                database.close()
                service.close()

    def test_sidecar_copy_failure_after_staging_and_backup_recovers_to_complete_old_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="failed"))
                self._complete_export(database, job_id, layout, format_value="both")
                copy = getattr(__import__("anima_core.export_commit", fromlist=["replace_ocr_sidecar"]), "replace_ocr_sidecar", None)
                self.assertTrue(callable(copy), "OCR commit copy API is missing")
                with patch("anima_core.export_commit.write_backup", side_effect=OSError("injected backup full")), patch(
                    "anima_core.export_commit.replace_ocr_sidecar", wraps=copy,
                ) as copied:
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                copied.assert_called_once()
                self.assertFalse((dataset / "ocr_annotations" / "sample.png.ocr.json").exists())
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_sidecar_copy_failure_after_staging_does_not_switch_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            try:
                original = (dataset / "sample.json").read_bytes()
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="failed"))
                self._complete_export(database, job_id, layout, format_value="both")
                with patch(
                    "anima_core.export_commit.replace_ocr_sidecar",
                    side_effect=OSError("injected sidecar copy failure"),
                ) as copied:
                    with self.assertRaises(ExportCommitError):
                        ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                copied.assert_called_once()
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse((dataset / "ocr_annotations" / "sample.png.ocr.json").exists())
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                database.close()
                service.close()

    def test_all_export_formats_commit_ocr_sidecars_without_changing_business_artifact_rules(self) -> None:
        for format_value in ("json", "flat_txt", "both"):
            with self.subTest(format=format_value), tempfile.TemporaryDirectory() as temporary:
                database, service, job_id, dataset, layout = self._prepared_job(
                    Path(temporary), format_value=format_value, ocr_enabled=True,
                )
                try:
                    sidecar = _ocr_sidecar("sample.png", status="no_text")
                    layout.write_ocr_sidecar("sample.png", sidecar)
                    self._complete_export(database, job_id, layout, format_value=format_value)
                    ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                    self.assertEqual(sidecar, (dataset / "ocr_annotations" / "sample.png.ocr.json").read_bytes())
                    self.assertEqual(format_value != "flat_txt", (dataset / "sample.json").is_file())
                    self.assertEqual(format_value != "json", (dataset / "sample.txt").is_file())
                finally:
                    database.close()
                    service.close()

    def test_ocr_enabled_and_disabled_commits_have_byte_identical_business_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disabled_root, enabled_root = root / "disabled", root / "enabled"
            disabled_root.mkdir()
            enabled_root.mkdir()
            disabled_db, disabled_service, disabled_job, disabled_dataset, disabled_layout = self._prepared_job(
                disabled_root, ocr_enabled=False,
            )
            enabled_db, enabled_service, enabled_job, enabled_dataset, enabled_layout = self._prepared_job(
                enabled_root, ocr_enabled=True,
            )
            try:
                self._complete_export(disabled_db, disabled_job, disabled_layout, format_value="both")
                ExportCommitCoordinator(disabled_db, disabled_layout, job_id=disabled_job).commit()
                enabled_layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="success"))
                self._complete_export(enabled_db, enabled_job, enabled_layout, format_value="both")
                ExportCommitCoordinator(enabled_db, enabled_layout, job_id=enabled_job).commit()
                self.assertEqual(self._business_bytes(disabled_dataset), self._business_bytes(enabled_dataset))
                self.assertTrue((enabled_dataset / "ocr_annotations" / "sample.png.ocr.json").is_file())
            finally:
                disabled_db.close()
                disabled_service.close()
                enabled_db.close()
                enabled_service.close()

    def test_ocr_disabled_does_not_commit_a_stale_overlay_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(
                Path(temporary), ocr_enabled=False,
            )
            try:
                self._complete_export(database, job_id, layout, format_value="both")
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="success"))
                ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertFalse((dataset / "ocr_annotations" / "sample.png.ocr.json").exists())
            finally:
                database.close()
                service.close()

    def test_unscoped_overlay_sidecar_is_not_committed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(
                Path(temporary), extra_images=("unscoped.jpg",), ocr_enabled=True,
            )
            try:
                database.connection.execute(
                    "UPDATE samples SET in_processing_scope=0 WHERE job_id=? AND relative_image_path=?",
                    (job_id, "unscoped.jpg"),
                )
                layout.write_ocr_sidecar("sample.png", _ocr_sidecar("sample.png", status="no_text"))
                layout.write_ocr_sidecar("unscoped.jpg", _ocr_sidecar("unscoped.jpg", status="failed"))
                self._complete_export(database, job_id, layout, format_value="both")
                ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertTrue((dataset / "ocr_annotations" / "sample.png.ocr.json").is_file())
                self.assertFalse((dataset / "ocr_annotations" / "unscoped.jpg.ocr.json").exists())
            finally:
                database.close()
                service.close()

    def test_overlay_sidecar_reparse_point_blocks_commit_before_directory_switch(self) -> None:
        if os.name != "nt":
            self.skipTest("the reparse-point fixture is Windows-specific")
        with tempfile.TemporaryDirectory() as temporary:
            database, service, job_id, dataset, layout = self._prepared_job(Path(temporary), ocr_enabled=True)
            sidecar_directory = layout.root / "ocr_annotations"
            try:
                original = (dataset / "sample.json").read_bytes()
                target = Path(temporary) / "outside-sidecars"
                target.mkdir()
                (target / "sample.png.ocr.json").write_bytes(_ocr_sidecar("sample.png", status="success"))
                sidecar_directory.rmdir()
                junction = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(sidecar_directory), str(target)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if junction.returncode != 0:
                    self.fail(f"unable to create reparse-point fixture: {junction.stderr or junction.stdout}")
                self._complete_export(database, job_id, layout, format_value="both")
                with self.assertRaises(ExportCommitError):
                    ExportCommitCoordinator(database, layout, job_id=job_id).commit()
                self.assertEqual(original, (dataset / "sample.json").read_bytes())
                self.assertFalse(layout.commit_journal_path().exists())
                self.assertEqual([], self._staging_residue(dataset))
            finally:
                if sidecar_directory.exists():
                    sidecar_directory.rmdir()
                database.close()
                service.close()


if __name__ == "__main__":
    unittest.main()
