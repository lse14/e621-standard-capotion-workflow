from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from PIL import Image

from anima_caption_format import flat_txt_sha256
from anima_caption_format.normalizer import CaptionDisplayPolicy
from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.job_preflight import JobPreparationService
from anima_core.overlay import BaselineView, OverlayLayout, WorkingAnnotationView
from anima_core.pipeline import PipelineService
from anima_core.pipeline_dispatch import PipelineError
from anima_core.ocr_runtime_binding import OcrRuntimeBindingV1, read_runtime_binding, write_runtime_binding
from anima_core.scheduler import BoundedScheduler
from anima_core.contracts import SampleIssue, SampleRunState
from anima_core.classify_overlay import serialize_annotation_json
from anima_core.ocr_sidecar import FIXED_OCR_INFERENCE_SETTINGS, parse_ocr_sidecar, serialize_ocr_sidecar
from anima_core.stdio_transport import StdioJsonlTransportError
from anima_core.token_budget_overlay import TokenBudgetOverlayWriter
from anima_core.token_budget_protocol import validate_token_budget_outcome


class PipelineTests(unittest.TestCase):
    @staticmethod
    def _v7_cpu_ocr_binding() -> OcrRuntimeBindingV1:
        return OcrRuntimeBindingV1.from_dict({
            "schemaVersion": 1,
            "requested": {
                "device": "auto",
                "textDetLimitSideLen": {"mode": "auto", "value": None},
                "textBatchSize": {"mode": "auto", "value": None},
            },
            "recommended": {"source": "cpu", "totalVramBytes": None, "textDetLimitSideLen": 1920, "textBatchSize": 1},
            "effective": {"textDetLimitSideLen": 1920, "textBatchSize": 1},
            "runtime": {
                "runtimeId": "ocr-paddle", "runtimeFingerprint": "a" * 64, "observedDevice": "cpu",
                "paddleVersion": "3.2.2", "compiledWithCuda": False, "cudaVersion": None, "gpuName": None,
            },
            "resourceFingerprint": "b" * 64,
            "startupReason": "gpu_runtime_unavailable",
        })

    def test_v7_resume_and_repair_use_the_existing_binding_without_runtime_reselection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = self._v7_cpu_ocr_binding()
            parent_path = root / "parent" / "ocr-runtime-binding-v1.json"
            write_runtime_binding(parent_path, binding)
            dispatch = object.__new__(PipelineService)
            database = SimpleNamespace(
                repair_parent_job_id=lambda _job_id: "parent-job",
                get_job=lambda _job_id: {"overlay_root": str(root / "parent-overlay")},
            )
            child_path = root / "child" / "ocr-runtime-binding-v1.json"
            child_layout = SimpleNamespace(resource_path=lambda _filename: child_path)
            parent_layout = SimpleNamespace(resource_path=lambda _filename: parent_path)
            with patch("anima_core.pipeline_dispatch.OverlayLayout.open_existing", return_value=parent_layout):
                self.assertEqual(child_path, dispatch._ocr_binding_path(database, "repair-job", child_layout))
            self.assertEqual(binding, read_runtime_binding(child_path))

            selection = dispatch._select_ocr_runtime(database, "repair-job", {"ocr": {"device": "auto"}}, child_path)
            self.assertEqual(("ocr-paddle", "a" * 64, None, "gpu_runtime_unavailable"), (
                selection.runtime_id, selection.runtime_fingerprint, selection.total_vram_bytes, selection.startup_reason,
            ))

    def test_v7_repair_missing_parent_binding_is_reported_as_a_pipeline_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dispatch = object.__new__(PipelineService)
            database = SimpleNamespace(
                repair_parent_job_id=lambda _job_id: "parent-job",
                get_job=lambda _job_id: {"overlay_root": None},
            )
            layout = SimpleNamespace(resource_path=lambda filename: root / "child" / filename)

            try:
                dispatch._ocr_binding_path(database, "repair-job", layout)
            except Exception as exc:
                self.assertIsInstance(exc, PipelineError)
            else:
                self.fail("a repair without its parent OCR binding must be blocked")

    def _interrupted_caption_job(self, root: Path) -> tuple[JobPreparationService, str, Path]:
        dataset = root / "dataset"
        dataset.mkdir()
        Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
        config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
        config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
        config.countReview["enabled"] = False  # type: ignore[index]
        preparation = JobPreparationService(root / "state.db")
        job_id = preparation.preflight(config.to_dict()).jobId
        preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
        database = StateDatabase.open(root / "state.db")
        try:
            scheduler = BoundedScheduler(database)
            scheduler.start_module(job_id, "caption", enabled=True, profile="e621")
            lease = scheduler.claim_batch(job_id, "caption", "caption-test", str(database.get_job(job_id)["config_hash"]))[0]
            layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
            prepared, digest = layout.write_prepared("caption", str(lease.leaseId), ".txt", b"tag_a, tag_b")
            relative = str(prepared.relative_to(layout.root)).replace("/", "\\")
            database.stage_prepared_artifact(job_id, lease.sampleId, lease_id=str(lease.leaseId), relative_path=relative, sha256=digest)
            database.mark_interrupted(job_id)
        finally:
            database.close()
        return preparation, job_id, dataset

    @staticmethod
    def _dropout_only_job(root: Path) -> tuple[JobPreparationService, str]:
        dataset = root / "dataset"
        dataset.mkdir()
        Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
        (dataset / "image.json").write_bytes(serialize_annotation_json({
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": ["white hair"], "tags": ["smile"], "environment": [], "nl": "A person smiles.",
        }))
        config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
        config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = False
        config.countReview["enabled"] = False  # type: ignore[index]
        config.dropout["enabled"] = True
        config.dropout["quality"]["enabled"] = False
        preparation = JobPreparationService(root / "state.db")
        job_id = preparation.preflight(config.to_dict()).jobId
        preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
        return preparation, job_id

    def test_abnormal_worker_exit_restarts_the_module_twice_then_fails_it(self) -> None:
        for crashes, expected in ((2, "paused"), (9, "failed")):
            with self.subTest(crashes=crashes), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                preparation, job_id = self._dropout_only_job(root)
                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                attempts: list[str] = []

                def crashing(_database, _scheduler, _job_id, module_id, _config, attempts=attempts) -> str:
                    attempts.append(module_id)
                    if len(attempts) <= crashes:
                        raise StdioJsonlTransportError("worker exited before request")
                    return "paused"

                pipeline._run_active_module = crashing  # type: ignore[method-assign]
                try:
                    if expected == "failed":
                        # The third abnormal exit stops the module instead of
                        # restarting an embedded worker forever.
                        with self.assertRaises(StdioJsonlTransportError):
                            pipeline._run(job_id)
                    else:
                        pipeline._run(job_id)
                    database = StateDatabase.open(root / "state.db")
                    try:
                        summary = database.module_summary(job_id, "dropout")
                        self.assertEqual(3, len(attempts))
                        self.assertEqual(min(crashes, 3), int(summary["worker_restart_count"]))
                        if expected == "failed":
                            self.assertEqual("failed", summary["status"])
                            self.assertEqual("failed", database.get_job(job_id)["status"])
                        else:
                            self.assertEqual("running", summary["status"])
                    finally:
                        database.close()
                finally:
                    preparation.close()

    def test_caption_production_wiring_passes_install_root_to_resource_coverage_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")

            class TransportContext:
                def __enter__(self):
                    return object()

                def __exit__(self, *_args):
                    return False

            pipeline._spawn_transport = lambda _module_id: TransportContext()  # type: ignore[method-assign]
            try:
                scheduler = BoundedScheduler(database)
                scheduler.start_module(job_id, "caption", enabled=True, profile="e621")
                frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                with patch("anima_core.pipeline_dispatch.CaptionRunner") as runner_type:
                    runner_type.return_value.run.return_value = SimpleNamespace(status="completed")
                    result = pipeline._run_active_module(database, scheduler, job_id, "caption", frozen)
                self.assertEqual("completed", result)
                self.assertEqual(ROOT / "resource-library", runner_type.call_args.kwargs["install_root"])
            finally:
                database.close()
                pipeline.close()
                preparation.close()

    def test_blocking_issues_from_upstream_modules_hold_the_job_in_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                sample = database.page_samples(job_id, limit=1)[0]
                database.upsert_issue(SampleIssue(
                    issueId="blocking-classify", jobId=job_id, sampleId=int(sample["sample_id"]),
                    relativeImagePath=str(sample["relative_image_path"]), moduleId="classify",
                    code="classify_input_missing", severity="error", blocking=True, retriable=True,
                    repairStartModule="classify", message="blocking", attempt=1,
                ))
            finally:
                database.close()
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                pipeline._run(job_id)
                database = StateDatabase.open(root / "state.db")
                try:
                    # Export must not bypass unresolved issues from upstream modules.
                    self.assertEqual("reviewing", database.get_job(job_id)["status"])
                    self.assertNotIn(
                        "export", {str(row["module_id"]) for row in database.module_summaries(job_id)}
                    )
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_startup_recovery_freezes_active_jobs_and_sweeps_finished_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preparation, job_id = self._dropout_only_job(root)
            database = StateDatabase.open(root / "state.db")
            try:
                database.set_job_status(job_id, "running", current_module_id="dropout")
                self.assertEqual(1, len(list(database.connection.execute("SELECT 1 FROM dataset_claims"))))
            finally:
                database.close()
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                result = pipeline.startup_recovery()
                self.assertEqual(1, result["interruptedJobs"])
                database = StateDatabase.open(root / "state.db")
                try:
                    job = database.get_job(job_id)
                    self.assertEqual(("interrupted", "running"), (job["status"], job["resume_status"]))
                    self.assertEqual(1, len(list(database.connection.execute("SELECT 1 FROM dataset_claims"))))
                    database.connection.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (job_id,))
                finally:
                    database.close()
                self.assertEqual(1, pipeline.startup_recovery()["clearedDatasetClaims"])
            finally:
                preparation.close()

    def test_commit_recovery_tolerates_an_overlay_directory_that_no_longer_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preparation, job_id = self._dropout_only_job(root)
            database = StateDatabase.open(root / "state.db")
            try:
                overlay = Path(str(database.get_job(job_id)["overlay_root"]))
                database.connection.execute(
                    "UPDATE jobs SET commit_journal_path=? WHERE job_id=?",
                    (str(overlay / "commit-journal.json"), job_id),
                )
            finally:
                database.close()
            import shutil

            shutil.rmtree(overlay)
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                # A pruned overlay used to abort the whole startup scan.
                pipeline.recover_pending_commits()
                database = StateDatabase.open(root / "state.db")
                try:
                    job = database.get_job(job_id)
                    self.assertIsNone(job["overlay_root"])
                    self.assertIsNone(job["commit_journal_path"])
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_recovery_commits_real_prepared_overlay_before_resuming_current_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preparation, job_id, _dataset = self._interrupted_caption_job(root)
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            pipeline._run = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            try:
                result = pipeline.recover_job(job_id, confirmed=True)
                self.assertEqual((1, 0, 0, True, "running"), (
                    result["committedPrepared"], result["returnedLeases"], result["pendingApiDecisions"], result["started"], result["status"],
                ))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("completed", database.get_sample_state(job_id, 1)["status"])
                    overlay = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                    self.assertEqual(b"tag_a, tag_b", overlay.annotation_path("image", ".txt").read_bytes())
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_recovery_commits_or_reuses_prepared_ocr_sidecar_before_resuming(self) -> None:
        for already_committed in (False, True):
            with self.subTest(already_committed=already_committed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                dataset = root / "dataset"
                dataset.mkdir()
                image = dataset / "image.png"
                Image.new("RGB", (10, 8), "white").save(image)
                image_bytes = image.read_bytes()
                config = JobConfig(
                    profile="e621", workMode="in_place", overwriteMode="incremental",
                    sourceRoot=str(dataset), schemaVersion=5,
                )
                config.nl["promptVersion"] = "nl-default-prompt-v3"
                config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
                config.nl["enabled"] = config.dropout["enabled"] = False
                config.countReview["enabled"] = False  # type: ignore[index]
                config.ocr["enabled"] = True
                preparation = JobPreparationService(root / "state.db")
                job_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    scheduler = BoundedScheduler(database, lease_id_factory=lambda: "ocr-recovery-lease")
                    for module_id in ("caption", "classify", "replace"):
                        scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                    scheduler.start_module(job_id, "ocr", enabled=True, profile="e621")
                    lease = scheduler.claim_batch(job_id, "ocr", "ocr-recovery-worker", str(database.get_job(job_id)["config_hash"]))[0]
                    layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                    frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                    sidecar = {
                        "schemaVersion": 1,
                        "relativeImagePath": "image.png",
                        "image": {"width": 10, "height": 8, "sizeBytes": len(image_bytes), "sha256": hashlib.sha256(image_bytes).hexdigest()},
                        "status": "no_text",
                        "engine": {"backend": "paddle", "resourceId": "ocr-ppocrv5-server-paddle-v1", "resourceFingerprint": frozen["ocr"]["resourceFingerprint"]},
                        "settings": {"llmMinConfidence": 0.5, "inference": dict(FIXED_OCR_INFERENCE_SETTINGS)},
                        "items": [],
                        "error": None,
                    }
                    payload = serialize_ocr_sidecar(parse_ocr_sidecar(json.dumps(sidecar).encode("utf-8")))
                    prepared, digest = layout.write_ocr_prepared(str(lease.leaseId), payload)
                    relative = str(prepared.relative_to(layout.root)).replace("/", "\\\\")
                    database.stage_prepared_artifact(job_id, lease.sampleId, lease_id=str(lease.leaseId), relative_path=relative, sha256=digest)
                    if already_committed:
                        layout.commit_ocr_prepared(relative, digest, "image.png")
                    database.mark_interrupted(job_id)
                finally:
                    database.close()

                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                pipeline._run = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
                try:
                    result = pipeline.recover_job(job_id, confirmed=True)
                    self.assertEqual((1, 0), (result["committedPrepared"], result["repeatedPrepared"]))
                    database = StateDatabase.open(root / "state.db")
                    try:
                        self.assertEqual("completed", database.get_sample_state(job_id, 1)["status"])
                        layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                        self.assertEqual(payload, layout.ocr_sidecar_path("image.png").read_bytes())
                    finally:
                        database.close()
                finally:
                    pipeline.close()
                    preparation.close()

    def test_recovery_settles_a_prepared_ocr_failure_as_one_nonblocking_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            image = dataset / "image.png"
            Image.new("RGB", (10, 8), "white").save(image)
            image_bytes = image.read_bytes()
            config = JobConfig(
                profile="e621", workMode="in_place", overwriteMode="incremental",
                sourceRoot=str(dataset), schemaVersion=5,
            )
            config.nl["promptVersion"] = "nl-default-prompt-v3"
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.ocr["enabled"] = True
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                scheduler = BoundedScheduler(database, lease_id_factory=lambda: "ocr-failure-recovery-lease")
                for module_id in ("caption", "classify", "replace"):
                    scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                scheduler.start_module(job_id, "ocr", enabled=True, profile="e621")
                lease = scheduler.claim_batch(
                    job_id, "ocr", "ocr-recovery-worker", str(database.get_job(job_id)["config_hash"])
                )[0]
                layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                sidecar = {
                    "schemaVersion": 1,
                    "relativeImagePath": "image.png",
                    "image": {
                        "width": 10, "height": 8, "sizeBytes": len(image_bytes),
                        "sha256": hashlib.sha256(image_bytes).hexdigest(),
                    },
                    "status": "failed",
                    "engine": {
                        "backend": "paddle", "resourceId": "ocr-ppocrv5-server-paddle-v1",
                        "resourceFingerprint": frozen["ocr"]["resourceFingerprint"],
                    },
                    "settings": {
                        "llmMinConfidence": 0.5,
                        "inference": dict(FIXED_OCR_INFERENCE_SETTINGS),
                    },
                    "items": [],
                    "error": {
                        "code": "ocr_inference_failed",
                        "message": "OCR inference failed for this image.",
                        "retriable": True,
                    },
                }
                payload = serialize_ocr_sidecar(parse_ocr_sidecar(json.dumps(sidecar).encode("utf-8")))
                prepared, digest = layout.write_ocr_prepared(str(lease.leaseId), payload)
                relative = str(prepared.relative_to(layout.root)).replace("/", "\\\\")
                database.stage_prepared_artifact(
                    job_id, lease.sampleId, lease_id=str(lease.leaseId), relative_path=relative, sha256=digest
                )
                database.mark_interrupted(job_id)
            finally:
                database.close()

            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            pipeline._run = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            try:
                result = pipeline.recover_job(job_id, confirmed=True)
                self.assertEqual((1, 0), (result["committedPrepared"], result["repeatedPrepared"]))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("completed", database.get_sample_state(job_id, 1)["status"])
                    issue = database.page_issues(job_id, limit=1)[0]
                    self.assertEqual(("ocr_inference_failed", 0, 1), (
                        issue["code"], int(issue["blocking"]), int(issue["retriable"]),
                    ))
                    summary = database.module_summary(job_id, "ocr")
                    self.assertEqual((1, 1), (int(summary["completed"]), int(summary["issue_count"])))
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_shutdown_cancels_only_jobs_owned_by_live_pipeline_threads(self) -> None:
        class JoinedThread:
            joined = False

            def join(self) -> None:
                self.joined = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active_dataset = root / "active"
            inactive_dataset = root / "inactive"
            active_dataset.mkdir()
            inactive_dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(active_dataset / "image.png")
            Image.new("RGB", (3, 3), "white").save(inactive_dataset / "image.png")
            preparation = JobPreparationService(root / "state.db")
            def config_for(dataset: Path) -> JobConfig:
                config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
                config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = False
                config.countReview["enabled"] = False  # type: ignore[index]
                return config

            active = preparation.preflight(config_for(active_dataset).to_dict()).jobId
            inactive = preparation.preflight(config_for(inactive_dataset).to_dict()).jobId
            preparation.confirm_workspace(active, confirmed=True, confirmed_rebuild=False)
            preparation.confirm_workspace(inactive, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                database.set_job_status(inactive, "running", current_module_id="caption")
            finally:
                database.close()
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            joined = JoinedThread()
            pipeline._threads[active] = joined  # type: ignore[assignment]
            try:
                pipeline.shutdown()
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertTrue(joined.joined)
                    self.assertEqual("cancelled_recoverable", database.get_job(active)["status"])
                    self.assertEqual("running", database.get_job(inactive)["status"])
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_recovery_rejects_changed_manifest_image_before_touching_prepared_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preparation, job_id, dataset = self._interrupted_caption_job(root)
            Image.new("RGB", (4, 4), "black").save(dataset / "image.png")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                with self.assertRaisesRegex(Exception, "fingerprints"):
                    pipeline.recover_job(job_id, confirmed=True)
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("interrupted", database.get_job(job_id)["status"])
                    self.assertEqual("prepared", database.get_sample_state(job_id, 1)["status"])
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_recovery_keeps_nl_request_started_waiting_for_explicit_api_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.nl["enabled"] = True
            config.nl["apiEnabled"] = True
            config.nl["systemPrompt"] = "describe"
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                scheduler = BoundedScheduler(database)
                for module_id in ("caption", "classify", "replace"):
                    scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                scheduler.start_module(job_id, "nl", enabled=True, profile="e621")
                lease = scheduler.claim_batch(job_id, "nl", "nl-test", str(database.get_job(job_id)["config_hash"]))[0]
                database.set_sample_state(job_id, lease.sampleId, SampleRunState(
                    sampleId=lease.sampleId, currentModuleId="nl", status="request_started", attempt=lease.attempt,
                    leaseId=lease.leaseId, workerInstanceId="nl-test", leaseExpiresAt=lease.leaseExpiresAt,
                ))
                database.mark_interrupted(job_id)
            finally:
                database.close()
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                result = pipeline.recover_job(job_id, confirmed=True)
                self.assertEqual((1, False, "interrupted"), (result["pendingApiDecisions"], result["started"], result["status"]))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("request_started", database.get_sample_state(job_id, 1)["status"])
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_recovery_commits_prepared_policy_output_before_resuming(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            artist = dataset / "1_Artist"
            artist.mkdir(parents=True)
            Image.new("RGB", (3, 3), "white").save(artist / "image.png")
            baseline = {
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": ["white hair"], "tags": ["smile"], "environment": [], "nl": "A person smiles.",
            }
            (artist / "image.json").write_bytes(serialize_annotation_json(baseline))
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset), recursive=True)
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.dropout["enabled"] = True
            config.dropout["quality"]["enabled"] = False
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                scheduler = BoundedScheduler(database, lease_id_factory=lambda: "policy-recovery-lease")
                for module_id in ("caption", "classify", "replace", "nl"):
                    scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                scheduler.start_module(job_id, "count_review", enabled=False, profile="e621")
                scheduler.start_module(job_id, "dropout", enabled=True, profile="e621")
                lease = scheduler.claim_batch(
                    job_id, "dropout", "policy-test", str(database.get_job(job_id)["config_hash"])
                )[0]
                layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                repaired = {**baseline, "artist": "@Artist"}
                prepared, digest = layout.write_prepared("dropout", str(lease.leaseId), ".json", serialize_annotation_json(repaired))
                database.stage_prepared_artifact(
                    job_id, lease.sampleId, lease_id=str(lease.leaseId),
                    relative_path=str(prepared.relative_to(layout.root)).replace("/", "\\"), sha256=digest,
                )
                database.mark_interrupted(job_id)
            finally:
                database.close()
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            pipeline._run = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            try:
                result = pipeline.recover_job(job_id, confirmed=True)
                self.assertEqual((1, 0, "running"), (result["committedPrepared"], result["repeatedPrepared"], result["status"]))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("completed", database.get_sample_state(job_id, 1)["status"])
                    layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                    self.assertEqual("@Artist", __import__("json").loads(layout.annotation_path("1_Artist\\image", ".json").read_text(encoding="utf-8"))["artist"])
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_cancellation_return_from_a_module_settles_to_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            def cancel_after_start(database, _scheduler, candidate_job_id, _module_id, _config):
                database.begin_cancellation(candidate_job_id)
                return "cancelling"
            pipeline._run_active_module = cancel_after_start  # type: ignore[method-assign]
            try:
                pipeline._run(job_id)
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("cancelled_recoverable", database.get_job(job_id)["status"])
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_resume_starts_a_paused_policy_module_from_its_persisted_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            artist = dataset / "1_Artist"
            artist.mkdir(parents=True)
            Image.new("RGB", (3, 3), "white").save(artist / "image.png")
            (artist / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": ["white hair"], "tags": ["smile"], "environment": [], "nl": "A person smiles.",
            }))
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.dropout["enabled"] = True
            config.dropout["quality"]["enabled"] = False
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                scheduler = BoundedScheduler(database)
                for module_id in ("caption", "classify", "replace", "nl"):
                    scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                scheduler.start_module(job_id, "count_review", enabled=False, profile="e621")
                scheduler.start_module(job_id, "dropout", enabled=True, profile="e621")
                database.set_module_summary(job_id, "dropout", status="paused")
                database.set_job_status(job_id, "paused", current_module_id="dropout", resume_status="running")
            finally:
                database.close()
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                self.assertTrue(pipeline.resume(job_id))
                for _ in range(200):
                    if not pipeline.is_running(job_id):
                        break
                    time.sleep(0.01)
                self.assertFalse(pipeline.is_running(job_id))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("succeeded", database.get_job(job_id)["status"])
                    self.assertEqual("completed", database.module_summary(job_id, "dropout")["status"])
                    self.assertEqual("completed", database.module_summary(job_id, "export")["status"])
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_policy_overlay_is_committed_only_by_export_after_the_pipeline_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            artist = dataset / "1_Artist"
            artist.mkdir(parents=True)
            Image.new("RGB", (3, 3), "white").save(artist / "image.png")
            baseline = {
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": ["white hair"], "tags": ["smile"], "environment": [], "nl": "A person smiles.",
            }
            (artist / "image.json").write_bytes(serialize_annotation_json(baseline))
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset), recursive=True)
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.nl["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.dropout["enabled"] = True
            config.dropout["quality"]["enabled"] = False
            config.dropout["appearanceNl"]["enabled"] = False
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            try:
                pipeline.start(job_id)
                for _ in range(200):
                    if not pipeline.is_running(job_id):
                        break
                    time.sleep(0.01)
                self.assertFalse(pipeline.is_running(job_id))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("succeeded", database.get_job(job_id)["status"])
                    self.assertEqual("completed", database.module_summary(job_id, "dropout")["status"])
                    self.assertEqual("completed", database.module_summary(job_id, "export")["status"])
                    overlay = Path(str(database.get_job(job_id)["overlay_root"]))
                    self.assertFalse((overlay / "annotations" / "1_Artist" / "image.json").exists())
                finally:
                    database.close()
                committed = json.loads((artist / "image.json").read_text(encoding="utf-8"))
                self.assertEqual("@Artist", committed["artist"])
                self.assertEqual(baseline["count"], committed["count"])
            finally:
                pipeline.close()
                preparation.close()

    def test_versioned_pipeline_traverses_six_or_seven_module_order(self) -> None:
        cases = (
            (2, ("caption", "classify", "replace", "nl", "dropout", "export")),
            (3, ("caption", "classify", "replace", "nl", "count_review", "dropout", "export")),
            (6, ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget", "export")),
            (7, ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget", "export")),
            (8, ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget", "export")),
        )
        for schema_version, expected_order in cases:
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                dataset = root / "dataset"
                dataset.mkdir()
                Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
                (dataset / "image.json").write_bytes(serialize_annotation_json({
                    "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                    "appearance": ["white hair"], "tags": ["smile"], "environment": [],
                    "nl": "A person smiles.",
                }))
                config = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot=str(dataset),
                    schemaVersion=schema_version,
                    countReview=None if schema_version == 2 else {"enabled": False, "protocolVersion": "count-review-v1"},
                )
                if schema_version == 2:
                    config.nl["promptVersion"] = "nl-default-prompt-v1"
                if schema_version in (6, 7, 8):
                    config.tokenBudget["enabled"] = False  # type: ignore[index]
                config.caption["enabled"] = False
                config.classify["enabled"] = False
                config.replace["enabled"] = False
                config.nl["enabled"] = False
                config.dropout["enabled"] = False
                preparation = JobPreparationService(root / "state.db")
                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                try:
                    job_id = preparation.preflight(config.to_dict()).jobId
                    preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                    visited: list[str] = []
                    original_start_module = BoundedScheduler.start_module

                    def recording_start_module(scheduler, candidate_job_id, module_id, *, enabled, profile):
                        if candidate_job_id == job_id:
                            visited.append(module_id)
                        return original_start_module(
                            scheduler, candidate_job_id, module_id, enabled=enabled, profile=profile
                        )

                    with patch("anima_core.pipeline.BoundedScheduler.start_module", new=recording_start_module):
                        pipeline.start(job_id)
                        for _ in range(200):
                            if not pipeline.is_running(job_id):
                                break
                            time.sleep(0.01)
                    self.assertFalse(pipeline.is_running(job_id))
                    database = StateDatabase.open(root / "state.db")
                    try:
                        self.assertEqual("succeeded", database.get_job(job_id)["status"])
                        self.assertEqual(list(expected_order), visited)
                        summaries = {str(row["module_id"]): str(row["status"]) for row in database.module_summaries(job_id)}
                        self.assertEqual(set(expected_order), set(summaries))
                        self.assertEqual("completed", summaries["export"])
                        self.assertTrue(all(summaries[module_id] == "skipped" for module_id in expected_order[:-1]))
                    finally:
                        database.close()
                finally:
                    pipeline.close()
                    preparation.close()

    def test_v8_input_nl_dispatches_without_resolving_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            config = JobConfig(
                schemaVersion=8,
                profile="e621",
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset),
            )
            config.caption["inputTxtMode"] = "nl"
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.ocr["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            config.nl.update({"enabled": True, "apiEnabled": True, "systemPrompt": ""})
            preparation = JobPreparationService(root / "state.db")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            database: StateDatabase | None = None
            try:
                job_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                scheduler = BoundedScheduler(database)
                for module_id in ("caption", "classify", "replace", "ocr"):
                    scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                scheduler.start_module(job_id, "nl", enabled=True, profile="e621")
                frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                pipeline._nl_credentials = lambda _: self.fail("v8 input NL must not resolve credentials")  # type: ignore[method-assign]
                with patch("anima_core.pipeline_dispatch.NlRunner") as runner_type:
                    runner_type.return_value.run.return_value = "completed"
                    self.assertEqual("completed", pipeline._run_active_module(database, scheduler, job_id, "nl", frozen))
                    self.assertEqual("_NoExchangeTransport", type(runner_type.call_args.args[2]).__name__)
            finally:
                if database is not None:
                    database.close()
                pipeline.close()
                preparation.close()

    def test_disabled_modules_reach_export_and_missing_json_returns_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = False
            config.classify["enabled"] = False
            config.replace["enabled"] = False
            config.nl["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.dropout["enabled"] = True
            config.dropout["artist"]["enabled"] = False
            config.dropout["quality"]["enabled"] = False
            config.dropout["appearanceNl"]["enabled"] = False
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            pipeline.start(job_id)
            for _ in range(100):
                if not pipeline.is_running(job_id):
                    break
                time.sleep(0.01)
            self.assertFalse(pipeline.is_running(job_id))
            database = StateDatabase.open(root / "state.db")
            try:
                self.assertEqual("reviewing", database.get_job(job_id)["status"])
                summaries = {str(row["module_id"]): str(row["status"]) for row in database.module_summaries(job_id)}
                self.assertEqual({"caption", "classify", "replace", "nl", "count_review", "dropout", "export"}, set(summaries))
                self.assertEqual({"caption": "skipped", "classify": "skipped", "replace": "skipped", "nl": "skipped", "count_review": "skipped", "dropout": "skipped", "export": "completed_with_issues"}, summaries)
                self.assertEqual(1, database.count("issues", job_id))
                self.assertFalse((dataset / "image.json").exists())
                self.assertFalse((dataset / "image.txt").exists())
                self.assertFalse((dataset.parent / ".dataset.anima-backups").exists())
                overlay = Path(str(database.get_job(job_id)["overlay_root"]))
                self.assertFalse((overlay / "commit-journal.json").exists())
            finally:
                database.close()
                preparation.close()

    def test_v6_enabled_missing_token_budget_record_blocks_before_export_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            (dataset / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                database.set_job_status(job_id, "running", current_module_id="token_budget")
                self.assertFalse(PipelineService._token_budget_export_gate(database, job_id, json.loads(str(database.get_job(job_id)["config_json"]))))
                issue = database.page_issues(job_id, limit=10)[0]
                self.assertEqual(("reviewing", "token_budget"), (database.get_job(job_id)["status"], database.get_job(job_id)["current_module_id"]))
                self.assertEqual(("token_budget", "token_budget_export_gate_failed", "token_budget"), (issue["module_id"], issue["code"], issue["repair_start_module"]))
            finally:
                database.close()
                preparation.close()

    def test_v7_enabled_missing_token_budget_record_blocks_before_export_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            (dataset / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }))
            config = JobConfig(schemaVersion=7, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                database.set_job_status(job_id, "running", current_module_id="token_budget")
                frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                self.assertFalse(PipelineService._token_budget_export_gate(database, job_id, frozen))
                self.assertEqual(("reviewing", "token_budget"), (database.get_job(job_id)["status"], database.get_job(job_id)["current_module_id"]))
            finally:
                database.close()
                preparation.close()

    def test_review_resume_restores_token_budget_reviewing_state_when_thread_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            (dataset / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            pipeline: PipelineService | None = None
            try:
                job_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    database.initialize_module_summary(job_id, "token_budget", total=1, status="completed_with_issues")
                    database.set_job_status(job_id, "running", current_module_id="token_budget")
                    database.set_job_status(job_id, "reviewing", current_module_id="token_budget")
                finally:
                    database.close()
                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                pipeline._token_budget_export_gate = lambda *_: True  # type: ignore[method-assign]
                with patch("anima_core.pipeline.threading.Thread.start", side_effect=RuntimeError("thread start failed")):
                    with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                        pipeline.resume(job_id)
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual(
                        ("reviewing", "token_budget"),
                        (database.get_job(job_id)["status"], database.get_job(job_id)["current_module_id"]),
                    )
                finally:
                    database.close()
            finally:
                if pipeline is not None:
                    pipeline.close()
                preparation.close()

    def test_v6_overflow_blocker_does_not_create_a_second_export_gate_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            (dataset / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                database.set_job_status(job_id, "running", current_module_id="token_budget")
                database.upsert_issue(SampleIssue(
                    issueId="overflow", jobId=job_id, sampleId=1, relativeImagePath="image.png",
                    moduleId="token_budget", code="token_budget_overflow", severity="error", blocking=True,
                    retriable=True, repairStartModule="token_budget", message="Token Budget exceeded the frozen maximum", attempt=1,
                ))
                self.assertFalse(PipelineService._token_budget_export_gate(database, job_id, json.loads(str(database.get_job(job_id)["config_json"]))))
                self.assertEqual(1, len(database.page_issues(job_id, limit=10)))
                self.assertEqual("token_budget_overflow", database.page_issues(job_id, limit=10)[0]["code"])
            finally:
                database.close()
                preparation.close()

    def test_v6_disabled_token_budget_skips_runtime_and_overlay_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            (dataset / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            called: list[str] = []
            pipeline._run_module_with_restarts = lambda _database, _scheduler, _job_id, module_id, _config: called.append(module_id) or "paused"  # type: ignore[method-assign]
            try:
                pipeline._run(job_id)
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual(["export"], called)
                    self.assertEqual("skipped", database.module_summary(job_id, "token_budget")["status"])
                    overlay = Path(str(database.get_job(job_id)["overlay_root"]))
                    self.assertFalse((overlay / "resources" / "token-budget").exists())
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_v6_token_budget_export_gate_accepts_json_and_both_with_the_same_flat_text_record(self) -> None:
        for export_format in ("json", "both"):
            with self.subTest(export_format=export_format), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                dataset = root / "dataset"
                dataset.mkdir()
                Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
                annotation = {"quality": [], "count": "solo", "character": "", "series": "", "artist": "", "appearance": [], "tags": ["smile"], "environment": [], "nl": ""}
                (dataset / "image.json").write_bytes(serialize_annotation_json(annotation))
                config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
                config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
                config.countReview["enabled"] = False  # type: ignore[index]
                config.export["format"] = export_format
                preparation = JobPreparationService(root / "state.db")
                job_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    scheduler = BoundedScheduler(database, lease_id_factory=lambda: "lease-1")
                    for module_id in ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout"):
                        scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                    scheduler.start_module(job_id, "token_budget", enabled=True, profile="e621")
                    lease = scheduler.claim_batch(job_id, "token_budget", "worker", str(database.get_job(job_id)["config_hash"]))[0]
                    row = database.get_leased_sample(job_id, "token_budget", lease.sampleId, lease_id=str(lease.leaseId), worker_instance_id="worker")
                    frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                    layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                    view = WorkingAnnotationView(BaselineView(dataset), layout)
                    policy = CaptionDisplayPolicy.from_mapping(frozen["captionFormat"])
                    outcome = validate_token_budget_outcome({"schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": lease.sampleId, "leaseId": lease.leaseId, "status": "within_budget", "originalTokens": 1, "finalTokens": 1, "removed": {"quality": [], "environment": [], "tags": [], "appearance": []}, "annotation": annotation, "flatTextSha256": flat_txt_sha256(annotation, policy)}, expected_sample_id=lease.sampleId, expected_lease_id=str(lease.leaseId), original_annotation=annotation, caption_format=frozen["captionFormat"], max_tokens=int(frozen["tokenBudget"]["maxTokens"]))
                    TokenBudgetOverlayWriter(database, layout, view, job_id).prepare_and_commit(sample_id=lease.sampleId, lease_id=str(lease.leaseId), annotation_key=str(row["annotation_key"]), outcome=outcome, caption_format=frozen["captionFormat"], max_tokens=int(frozen["tokenBudget"]["maxTokens"]))
                    scheduler.complete(lease)
                    self.assertTrue(PipelineService._token_budget_export_gate(database, job_id, frozen))
                    self.assertEqual([], database.page_issues(job_id, limit=10))
                finally:
                    database.close()
                    preparation.close()

    def test_v6_token_budget_export_gate_rejects_over_limit_record_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            annotation = {"quality": [], "count": "solo", "character": "", "series": "", "artist": "", "appearance": [], "tags": ["smile"], "environment": [], "nl": ""}
            (dataset / "image.json").write_bytes(serialize_annotation_json(annotation))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            job_id = preparation.preflight(config.to_dict()).jobId
            preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
            database = StateDatabase.open(root / "state.db")
            try:
                scheduler = BoundedScheduler(database, lease_id_factory=lambda: "lease-1")
                for module_id in ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout"):
                    scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                scheduler.start_module(job_id, "token_budget", enabled=True, profile="e621")
                lease = scheduler.claim_batch(job_id, "token_budget", "worker", str(database.get_job(job_id)["config_hash"]))[0]
                row = database.get_leased_sample(job_id, "token_budget", lease.sampleId, lease_id=str(lease.leaseId), worker_instance_id="worker")
                frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                view = WorkingAnnotationView(BaselineView(dataset), layout)
                policy = CaptionDisplayPolicy.from_mapping(frozen["captionFormat"])
                outcome = validate_token_budget_outcome({"schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": lease.sampleId, "leaseId": lease.leaseId, "status": "within_budget", "originalTokens": 1, "finalTokens": 1, "removed": {"quality": [], "environment": [], "tags": [], "appearance": []}, "annotation": annotation, "flatTextSha256": flat_txt_sha256(annotation, policy)}, expected_sample_id=lease.sampleId, expected_lease_id=str(lease.leaseId), original_annotation=annotation, caption_format=frozen["captionFormat"], max_tokens=int(frozen["tokenBudget"]["maxTokens"]))
                writer = TokenBudgetOverlayWriter(database, layout, view, job_id)
                writer.prepare_and_commit(sample_id=lease.sampleId, lease_id=str(lease.leaseId), annotation_key=str(row["annotation_key"]), outcome=outcome, caption_format=frozen["captionFormat"], max_tokens=int(frozen["tokenBudget"]["maxTokens"]))
                record_path = layout.resource_path("token-budget\\records\\1.json")
                record = json.loads(record_path.read_text(encoding="utf-8"))
                record["finalTokens"] = int(frozen["tokenBudget"]["maxTokens"]) + 1
                record_path.write_text(json.dumps(record), encoding="utf-8")
                self.assertFalse(PipelineService._token_budget_export_gate(database, job_id, frozen))
                self.assertEqual(("reviewing", "token_budget"), (database.get_job(job_id)["status"], database.get_job(job_id)["current_module_id"]))
            finally:
                database.close()
                preparation.close()

    def test_recover_prepared_token_budget_review_apply_finalizes_once_for_export(self) -> None:
        """Recovery must not settle a review apply as a generic Token Budget completion."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            annotation = {
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }
            (dataset / "image.json").write_bytes(serialize_annotation_json(annotation))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            pipeline: PipelineService | None = None
            try:
                job_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    scheduler = BoundedScheduler(database, lease_id_factory=lambda: "review-apply-1-2")
                    for module_id in ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout"):
                        scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                    scheduler.start_module(job_id, "token_budget", enabled=True, profile="e621")
                    lease = scheduler.claim_batch(job_id, "token_budget", "token-budget-review", str(database.get_job(job_id)["config_hash"]))[0]
                    row = database.get_leased_sample(job_id, "token_budget", lease.sampleId, lease_id=str(lease.leaseId), worker_instance_id="token-budget-review")
                    frozen = json.loads(str(database.get_job(job_id)["config_json"]))
                    layout = OverlayLayout.open_existing(str(database.get_job(job_id)["overlay_root"]), job_id)
                    view = WorkingAnnotationView(BaselineView(dataset), layout)
                    policy = CaptionDisplayPolicy.from_mapping(frozen["captionFormat"])
                    outcome = validate_token_budget_outcome(
                        {
                            "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": lease.sampleId,
                            "leaseId": lease.leaseId, "status": "within_budget", "originalTokens": 1, "finalTokens": 1,
                            "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                            "annotation": annotation, "flatTextSha256": flat_txt_sha256(annotation, policy),
                        },
                        expected_sample_id=lease.sampleId, expected_lease_id=str(lease.leaseId), original_annotation=annotation,
                        caption_format=frozen["captionFormat"], max_tokens=int(frozen["tokenBudget"]["maxTokens"]),
                    )
                    TokenBudgetOverlayWriter(database, layout, view, job_id).prepare_and_commit(
                        sample_id=lease.sampleId, lease_id=str(lease.leaseId), annotation_key=str(row["annotation_key"]),
                        outcome=outcome, caption_format=frozen["captionFormat"], max_tokens=int(frozen["tokenBudget"]["maxTokens"]),
                    )
                    database.upsert_issue(SampleIssue(
                        issueId="overflow", jobId=job_id, sampleId=lease.sampleId, relativeImagePath="image.png",
                        moduleId="token_budget", code="token_budget_overflow", severity="error", blocking=True,
                        retriable=True, repairStartModule="token_budget", message="Token Budget exceeded the frozen maximum", attempt=1,
                    ))
                    database.set_module_summary(job_id, "token_budget", status="completed_with_issues", completed=0, failed=1, issue_count=1)
                    database.set_job_status(job_id, "reviewing", current_module_id="token_budget")
                    database.mark_interrupted(job_id)
                finally:
                    database.close()

                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                first = pipeline.recover_job(job_id, confirmed=True)
                database = StateDatabase.open(root / "state.db")
                try:
                    state = database.get_sample_state(job_id, 1)
                    issue = database.connection.execute(
                        "SELECT resolved_at FROM issues WHERE job_id=? AND sample_id=1 AND code='token_budget_overflow'",
                        (job_id,),
                    ).fetchone()
                    summary = database.module_summary(job_id, "token_budget")
                    record = TokenBudgetOverlayWriter(database, layout, view, job_id).record_for_export(
                        sample_id=1, annotation_key="image", caption_format=frozen["captionFormat"],
                        max_tokens=int(frozen["tokenBudget"]["maxTokens"]),
                    )
                    self.assertEqual((1, 0, "reviewing"), (first["committedPrepared"], first["repeatedPrepared"], first["status"]))
                    self.assertEqual(("pending", "export"), (state["status"], state["current_module_id"]))
                    self.assertIsNotNone(issue["resolved_at"])
                    self.assertEqual((1, 0, 0), (summary["completed"], summary["failed"], summary["issue_count"]))
                    self.assertEqual(flat_txt_sha256(annotation, policy), record.flat_text_sha256)
                    database.mark_interrupted(job_id)
                finally:
                    database.close()
                second = pipeline.recover_job(job_id, confirmed=True)
                database = StateDatabase.open(root / "state.db")
                try:
                    state = database.get_sample_state(job_id, 1)
                    summary = database.module_summary(job_id, "token_budget")
                    self.assertEqual((0, 0, "reviewing"), (second["committedPrepared"], second["repeatedPrepared"], second["status"]))
                    self.assertEqual(("pending", "export"), (state["status"], state["current_module_id"]))
                    self.assertEqual((1, 0, 0), (summary["completed"], summary["failed"], summary["issue_count"]))
                finally:
                    database.close()
            finally:
                if pipeline is not None:
                    pipeline.close()
                preparation.close()

    def test_recover_leased_token_budget_review_apply_returns_to_failed_for_retry(self) -> None:
        """A crash before SQLite reaches prepared must preserve the review apply path."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            (dataset / "image.json").write_bytes(serialize_annotation_json({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["smile"], "environment": [], "nl": "",
            }))
            config = JobConfig(schemaVersion=6, profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = config.ocr["enabled"] = config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            pipeline: PipelineService | None = None
            try:
                job_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(job_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    scheduler = BoundedScheduler(database, lease_id_factory=lambda: "review-crash-before-prepared")
                    for module_id in ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout"):
                        scheduler.start_module(job_id, module_id, enabled=False, profile="e621")
                    scheduler.start_module(job_id, "token_budget", enabled=True, profile="e621")
                    lease = scheduler.claim_batch(job_id, "token_budget", "token-budget-review", str(database.get_job(job_id)["config_hash"]))[0]
                    database.upsert_issue(SampleIssue(
                        issueId="overflow", jobId=job_id, sampleId=lease.sampleId, relativeImagePath="image.png",
                        moduleId="token_budget", code="token_budget_overflow", severity="error", blocking=True,
                        retriable=True, repairStartModule="token_budget", message="Token Budget exceeded the frozen maximum", attempt=1,
                    ))
                    database.set_job_status(job_id, "reviewing", current_module_id="token_budget")
                    database.mark_interrupted(job_id)
                finally:
                    database.close()

                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                response = pipeline.recover_job(job_id, confirmed=True)
                database = StateDatabase.open(root / "state.db")
                try:
                    state = database.get_sample_state(job_id, 1)
                    issue = database.connection.execute(
                        "SELECT resolved_at FROM issues WHERE job_id=? AND sample_id=1 AND code='token_budget_overflow'",
                        (job_id,),
                    ).fetchone()
                    self.assertEqual((0, "reviewing"), (response["returnedLeases"], response["status"]))
                    self.assertEqual(("failed", "token_budget", None, None), (
                        state["status"], state["current_module_id"], state["lease_id"], state["worker_instance_id"],
                    ))
                    self.assertIsNone(issue["resolved_at"])
                finally:
                    database.close()
            finally:
                if pipeline is not None:
                    pipeline.close()
                preparation.close()


if __name__ == "__main__":
    unittest.main()
