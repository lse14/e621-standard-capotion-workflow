from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.contracts import JobConfig
from anima_core.contracts import SampleIssue
from anima_core.db import StateDatabase
from anima_core.job_preflight import JobPreparationService
from anima_core.ocr_overlay import OcrWorkingSidecarView
from anima_core.ocr_runner import OcrRunner
from anima_core.ocr_runtime_binding import OcrRuntimeBindingV1, normalize_ocr_execution, write_execution_request, write_runtime_binding
from anima_core.ocr_runtime_binding import GIB
from anima_core.overlay import OverlayLayout
from anima_core.repair import RepairPreparationService
from anima_core.pipeline import PipelineService
from anima_core.scheduler import BoundedScheduler
from anima_core.stdio_transport import StdioJsonlTransportError
from anima_core.worker_protocol import ProtocolEnvelopeV1


class _FakeOcrTransport:
    def __init__(self) -> None:
        self.hello_calls = 0
        self.process_calls = 0

    def __enter__(self) -> "_FakeOcrTransport":
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    @staticmethod
    def _response(
        request: ProtocolEnvelopeV1, method: str, payload: dict[str, object],
    ) -> ProtocolEnvelopeV1:
        return ProtocolEnvelopeV1(
            protocolVersion="1.0",
            kind="response",
            messageId=f"reply-{request.messageId}",
            runtimeId=request.runtimeId,
            owner="ocr",
            method=method,
            payload=payload,
            replyTo=request.messageId,
            jobId=request.jobId,
            configHash=request.configHash,
        )

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if request.method == "hello":
            self.hello_calls += 1
            expected_runtime = request.payload.get("expectedRuntimeId")
            observed_device = "cuda" if expected_runtime == "ocr-paddle-gpu" else "cpu"
            return self._response(
                request,
                "hello",
                {
                    "schemaVersion": 1,
                    "payloadType": "ocr_hello_result",
                    "ready": True,
                    "executable": r"C:\\ocr-paddle\\python.exe",
                    "pythonVersion": "3.11.15",
                    "modelSessionLoads": 1,
                    "resourceFingerprint": request.payload["resourceFingerprint"],
                    **({
                        "requestedDevice": request.payload["requestedDevice"],
                        "observedDevice": observed_device,
                        "runtimeId": expected_runtime,
                        "runtimeFingerprint": request.payload["expectedRuntimeFingerprint"],
                        "paddleVersion": "3.2.2",
                        "compiledWithCuda": observed_device == "cuda",
                        "cudaVersion": "12.6" if observed_device == "cuda" else None,
                        "gpuName": "Test GPU" if observed_device == "cuda" else None,
                        "totalVramBytes": 24 * GIB if observed_device == "cuda" else None,
                    } if expected_runtime is not None else {}),
                },
            )
        if request.method == "process_batch":
            self.process_calls += 1
            items = request.payload["items"]
            assert isinstance(items, list)
            return self._response(
                request,
                "result",
                {
                    "schemaVersion": 1,
                    "payloadType": "ocr_process_result",
                    "items": [
                        {
                            "schemaVersion": 1,
                            "status": "no_text",
                            "sampleId": item["sampleId"],
                            "leaseId": item["leaseId"],
                            "relativeImagePath": item["relativeImagePath"],
                            "image": {
                                "width": 8,
                                "height": 6,
                                "sizeBytes": item["imageSize"],
                                "sha256": item["imageSha256"],
                            },
                            "items": [],
                        }
                        for item in items
                    ],
                },
            )
        raise AssertionError(f"unexpected OCR method: {request.method}")


class OcrCoreRunnerIntegrationTests(unittest.TestCase):
    @staticmethod
    def _complete_current_module(
        database: StateDatabase,
        scheduler: BoundedScheduler,
        job_id: str,
        module_id: str,
    ) -> str:
        config_hash = str(database.get_job(job_id)["config_hash"])
        while leases := scheduler.claim_batch(job_id, module_id, f"fake-{module_id}", config_hash):
            for lease in leases:
                scheduler.complete(lease)
        return scheduler.finish_module(job_id, module_id)

    def test_v9_ocr_and_nl_four_mode_matrix_keeps_ocr_independent_and_serial(self) -> None:
        expected_order = ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget", "export")
        for ocr_enabled, nl_enabled in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(ocr=ocr_enabled, nl=nl_enabled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                dataset = root / "dataset"
                dataset.mkdir()
                Image.new("RGB", (8, 6), "white").save(dataset / "sample.png")
                config = JobConfig(
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot=str(dataset),
                )
                config.tokenBudget["enabled"] = False  # type: ignore[index]
                config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
                config.ocr["enabled"] = ocr_enabled
                config.ocr["device"] = "cpu"
                config.nl["enabled"] = nl_enabled
                config.nl["systemPrompt"] = "Describe the visible image."
                config.countReview["enabled"] = False  # type: ignore[index]
                config.dropout["enabled"] = False
                preparation = JobPreparationService(root / "state.db")
                pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
                transport = _FakeOcrTransport()
                started: list[str] = []
                completed: list[str] = []
                original_start = BoundedScheduler.start_module
                original_run_active = pipeline._run_active_module

                def record_start(scheduler, job_id, module_id, *, enabled, profile=None):
                    if job_id == summary.jobId:
                        started.append(module_id)
                    return original_start(scheduler, job_id, module_id, enabled=enabled, profile=profile)

                def run_active(database, scheduler, job_id, module_id, frozen):
                    if module_id == "ocr":
                        return original_run_active(database, scheduler, job_id, module_id, frozen)
                    completed.append(module_id)
                    return self._complete_current_module(database, scheduler, job_id, module_id)

                try:
                    summary = preparation.preflight(config.to_dict())
                    preparation.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                    hash_guard = (
                        patch("anima_core.ocr_runner.stream_sha256", side_effect=AssertionError("disabled OCR hashed an image"))
                        if not ocr_enabled
                        else contextlib.nullcontext()
                    )
                    with (
                        patch.object(BoundedScheduler, "start_module", new=record_start),
                        patch("anima_core.pipeline.ExportCommitCoordinator.commit", return_value=None),
                        hash_guard,
                    ):
                        pipeline._spawn_ocr_transport = lambda runtime_id: transport  # type: ignore[method-assign]
                        pipeline._run_active_module = run_active  # type: ignore[method-assign]
                        pipeline.start(summary.jobId)
                        deadline = time.monotonic() + 10.0
                        while pipeline.is_running(summary.jobId) and time.monotonic() < deadline:
                            time.sleep(0.01)
                    self.assertFalse(pipeline.is_running(summary.jobId))
                    self.assertEqual(list(expected_order), started)
                    self.assertEqual([module for module in ("nl", "export") if (module != "nl" or nl_enabled)], completed)
                    self.assertEqual((int(ocr_enabled), int(ocr_enabled)), (transport.hello_calls, transport.process_calls))
                    database = StateDatabase.open(root / "state.db")
                    try:
                        summaries = {
                            str(row["module_id"]): str(row["status"])
                            for row in database.module_summaries(summary.jobId)
                        }
                        self.assertEqual("skipped" if not ocr_enabled else "completed", summaries["ocr"])
                        self.assertEqual("skipped" if not nl_enabled else "completed", summaries["nl"])
                    finally:
                        database.close()
                finally:
                    pipeline.close()
                    preparation.close()

    def test_worker_restart_commits_an_already_prepared_ocr_sidecar_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (8, 6), "white").save(dataset / "sample.png")
            config = JobConfig(
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot=str(dataset),
                )
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.ocr["enabled"] = True
            config.ocr["device"] = "cpu"
            config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            original_run_active = pipeline._run_active_module
            original_persist = OcrRunner._persist_sidecar
            transports: list[_FakeOcrTransport] = []
            persist_calls = 0

            def run_active(database, scheduler, job_id, module_id, frozen):
                if module_id == "ocr":
                    return original_run_active(database, scheduler, job_id, module_id, frozen)
                return self._complete_current_module(database, scheduler, job_id, module_id)

            def crash_after_persist(runner, lease, relative_path, payload):
                nonlocal persist_calls
                original_persist(runner, lease, relative_path, payload)
                persist_calls += 1
                if persist_calls == 1:
                    raise StdioJsonlTransportError("simulated worker exit after OCR response persistence")

            try:
                summary = preparation.preflight(config.to_dict())
                preparation.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                with (
                    patch.object(OcrRunner, "_persist_sidecar", new=crash_after_persist),
                    patch("anima_core.pipeline.ExportCommitCoordinator.commit", return_value=None),
                ):
                    pipeline._spawn_ocr_transport = lambda runtime_id: transports.append(_FakeOcrTransport()) or transports[-1]  # type: ignore[method-assign]
                    pipeline._run_active_module = run_active  # type: ignore[method-assign]
                    pipeline.start(summary.jobId)
                    deadline = time.monotonic() + 10.0
                    while pipeline.is_running(summary.jobId) and time.monotonic() < deadline:
                        time.sleep(0.01)
                self.assertFalse(pipeline.is_running(summary.jobId))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertNotEqual("failed", database.get_job(summary.jobId)["status"])
                    module = database.module_summary(summary.jobId, "ocr")
                    self.assertEqual((1, 0), (int(module["completed"]), int(module["issue_count"])))
                    self.assertEqual(1, sum(transport.process_calls for transport in transports))
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_cancellation_stops_after_inflight_ocr_sidecar_is_safely_settled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (8, 6), "white").save(dataset / "sample.png")
            config = JobConfig(
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset),
            )
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.ocr["enabled"] = True
            config.ocr["device"] = "cpu"
            config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")

            class CancellingTransport(_FakeOcrTransport):
                cancelled = False

                def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
                    response = super().exchange(request)
                    if request.method == "process_batch" and not self.cancelled:
                        self.cancelled = True
                        database = StateDatabase.open(root / "state.db")
                        try:
                            database.begin_cancellation(summary.jobId)
                        finally:
                            database.close()
                    return response

            transport = CancellingTransport()
            try:
                summary = preparation.preflight(config.to_dict())
                preparation.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                pipeline._spawn_ocr_transport = lambda runtime_id: transport  # type: ignore[method-assign]
                pipeline.start(summary.jobId)
                deadline = time.monotonic() + 10.0
                while pipeline.is_running(summary.jobId) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(pipeline.is_running(summary.jobId))
                database = StateDatabase.open(root / "state.db")
                try:
                    job = database.get_job(summary.jobId)
                    self.assertEqual("cancelled_recoverable", job["status"])
                    self.assertEqual((1, 0), (
                        int(database.module_summary(summary.jobId, "ocr")["completed"]),
                        int(database.module_summary(summary.jobId, "ocr")["issue_count"]),
                    ))
                    sidecar = Path(str(job["overlay_root"])) / "ocr_annotations" / "sample.png.ocr.json"
                    self.assertTrue(sidecar.is_file())
                    self.assertEqual(1, transport.process_calls)
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_worker_restart_reclaims_a_lease_when_the_worker_exits_before_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (8, 6), "white").save(dataset / "sample.png")
            config = JobConfig(
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset),
            )
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.ocr["enabled"] = True
            config.ocr["device"] = "cpu"
            config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            original_run_active = pipeline._run_active_module
            transports: list[_FakeOcrTransport] = []
            crashes_remaining = 1

            class CrashBeforeResponseTransport(_FakeOcrTransport):
                def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
                    nonlocal crashes_remaining
                    if request.method == "process_batch" and crashes_remaining:
                        crashes_remaining -= 1
                        raise StdioJsonlTransportError("simulated worker exit before OCR response")
                    return super().exchange(request)

            def run_active(database, scheduler, job_id, module_id, frozen):
                if module_id == "ocr":
                    return original_run_active(database, scheduler, job_id, module_id, frozen)
                return self._complete_current_module(database, scheduler, job_id, module_id)

            try:
                summary = preparation.preflight(config.to_dict())
                preparation.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                with patch("anima_core.pipeline.ExportCommitCoordinator.commit", return_value=None):
                    pipeline._spawn_ocr_transport = lambda runtime_id: transports.append(CrashBeforeResponseTransport()) or transports[-1]  # type: ignore[method-assign]
                    pipeline._run_active_module = run_active  # type: ignore[method-assign]
                    pipeline.start(summary.jobId)
                    deadline = time.monotonic() + 10.0
                    while pipeline.is_running(summary.jobId) and time.monotonic() < deadline:
                        time.sleep(0.01)
                self.assertFalse(pipeline.is_running(summary.jobId))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertNotEqual("failed", database.get_job(summary.jobId)["status"])
                    self.assertEqual(1, int(database.module_summary(summary.jobId, "ocr")["completed"]))
                    self.assertEqual(2, len(transports))
                    self.assertEqual(1, sum(transport.process_calls for transport in transports))
                finally:
                    database.close()
            finally:
                pipeline.close()
                preparation.close()

    def test_expired_ocr_lease_is_reclaimed_before_the_next_core_runner_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (8, 6), "white").save(dataset / "sample.png")
            config = JobConfig(
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset),
            )
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.ocr["enabled"] = True
            config.ocr["device"] = "cpu"
            config.nl["enabled"] = config.dropout["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            preparation = JobPreparationService(root / "state.db")
            try:
                summary = preparation.preflight(config.to_dict())
                preparation.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    scheduler = BoundedScheduler(database, lease_id_factory=lambda: "stale-lease")
                    for module_id in ("caption", "classify", "replace"):
                        scheduler.start_module(summary.jobId, module_id, enabled=False, profile="e621")
                    scheduler.start_module(summary.jobId, "ocr", enabled=True, profile="e621")
                    stale = scheduler.claim_batch(
                        summary.jobId, "ocr", "crashed-worker", str(database.get_job(summary.jobId)["config_hash"])
                    )[0]
                    database.connection.execute(
                        "UPDATE sample_state SET lease_expires_at='1970-01-01T00:00:00Z' WHERE job_id=? AND sample_id=?",
                        (summary.jobId, stale.sampleId),
                    )
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))
                    layout = OverlayLayout.open_existing(
                        str(database.get_job(summary.jobId)["overlay_root"]), summary.jobId
                    )
                    binding_path = layout.resource_path("ocr-runtime-binding-v1.json")
                    write_execution_request(layout.resource_path("ocr-execution-request-v1.json"), normalize_ocr_execution(None))
                    transport = _FakeOcrTransport()
                    report = OcrRunner(
                        database,
                        scheduler,
                        transport,
                        OcrWorkingSidecarView(dataset, layout),
                        job_id=summary.jobId,
                        worker_instance_id="replacement-worker",
                        resource_manifest_relative_path=str(frozen["ocr"]["resourceManifestRelativePath"]),
                        resource_fingerprint=str(frozen["ocr"]["resourceFingerprint"]),
                        runtime_id="ocr-paddle",
                        runtime_fingerprint="a" * 64,
                        binding_path=binding_path,
                    ).run()
                    self.assertEqual(("completed", 1), (report.status, transport.process_calls))
                    self.assertEqual("completed", database.get_sample_state(summary.jobId, stale.sampleId)["status"])
                finally:
                    database.close()
            finally:
                preparation.close()

    def test_ocr_only_repair_targets_the_failed_sample_then_continues_to_nl_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (8, 6), "white").save(dataset / "retry.png")
            Image.new("RGB", (8, 6), "black").save(dataset / "untouched.png")
            config = JobConfig(
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset),
            )
            config.tokenBudget["enabled"] = False  # type: ignore[index]
            config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
            config.ocr["enabled"] = config.nl["enabled"] = True
            config.ocr["device"] = "cpu"
            config.nl["systemPrompt"] = "Describe the visible image."
            config.countReview["enabled"] = False  # type: ignore[index]
            config.dropout["enabled"] = False
            preparation = JobPreparationService(root / "state.db")
            repair = RepairPreparationService(root / "state.db")
            pipeline = PipelineService(root / "state.db", install_root=ROOT / ".runtime-build")
            original_run_active = pipeline._run_active_module
            transport = _FakeOcrTransport()

            def run_active(database, scheduler, job_id, module_id, frozen):
                if module_id == "ocr":
                    return original_run_active(database, scheduler, job_id, module_id, frozen)
                return self._complete_current_module(database, scheduler, job_id, module_id)

            try:
                parent_id = preparation.preflight(config.to_dict()).jobId
                preparation.confirm_workspace(parent_id, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    samples = {
                        str(row["relative_image_path"]): row
                        for row in database.page_samples(parent_id, limit=10)
                    }
                    retry = samples["retry.png"]
                    database.set_job_status(parent_id, "running", current_module_id="ocr")
                    database.set_job_status(parent_id, "reviewing", current_module_id="export")
                    database.upsert_issue(SampleIssue(
                        issueId="ocr-retry",
                        jobId=parent_id,
                        sampleId=int(retry["sample_id"]),
                        relativeImagePath="retry.png",
                        moduleId="ocr",
                        code="ocr_inference_failed",
                        severity="warning",
                        blocking=False,
                        retriable=True,
                        repairStartModule="ocr",
                        message="OCR inference failed for this image.",
                        attempt=1,
                    ))
                    parent_job = database.get_job(parent_id)
                    parent_config = json.loads(str(parent_job["config_json"]))
                    parent_layout = OverlayLayout.open_existing(str(parent_job["overlay_root"]), parent_id)
                    write_runtime_binding(parent_layout.resource_path("ocr-runtime-binding-v1.json"), OcrRuntimeBindingV1.from_dict({
                        "schemaVersion": 1,
                        "requested": {
                            "device": "cpu",
                            "textDetLimitSideLen": {"mode": "auto", "value": None},
                            "textBatchSize": {"mode": "auto", "value": None},
                        },
                        "recommended": {
                            "source": "cpu",
                            "totalVramBytes": None,
                            "textDetLimitSideLen": 1920,
                            "textBatchSize": 1,
                        },
                        "effective": {"textDetLimitSideLen": 1920, "textBatchSize": 1},
                        "runtime": {
                            "runtimeId": "ocr-paddle",
                            "runtimeFingerprint": "a" * 64,
                            "observedDevice": "cpu",
                            "paddleVersion": "3.2.2",
                            "compiledWithCuda": False,
                            "cudaVersion": None,
                            "gpuName": None,
                        },
                        "resourceFingerprint": str(parent_config["ocr"]["resourceFingerprint"]),
                        "startupReason": None,
                    }))
                finally:
                    database.close()
                self.assertTrue(preparation.release_lock_for_repair(parent_id))
                repair_job = repair.prepare(parent_id)
                with patch("anima_core.pipeline.ExportCommitCoordinator.commit", return_value=None):
                    pipeline._spawn_ocr_transport = lambda runtime_id: transport  # type: ignore[method-assign]
                    pipeline._run_active_module = run_active  # type: ignore[method-assign]
                    pipeline.start(repair_job.repairJobId)
                    deadline = time.monotonic() + 10.0
                    while pipeline.is_running(repair_job.repairJobId) and time.monotonic() < deadline:
                        time.sleep(0.01)
                self.assertFalse(pipeline.is_running(repair_job.repairJobId))
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual(1, database.count_repair_targets(repair_job.repairJobId))
                    target = database.connection.execute(
                        "SELECT repair_start_module FROM repair_targets WHERE repair_job_id=?",
                        (repair_job.repairJobId,),
                    ).fetchone()
                    self.assertEqual("ocr", target["repair_start_module"])
                    summaries = {
                        str(row["module_id"]): str(row["status"])
                        for row in database.module_summaries(repair_job.repairJobId)
                    }
                    self.assertEqual("completed", summaries["ocr"])
                    self.assertEqual("completed", summaries["nl"])
                    self.assertEqual("completed", summaries["export"])
                    parent_issue = database.page_issues(parent_id, limit=1)[0]
                    self.assertIsNotNone(parent_issue["resolved_at"])
                    self.assertEqual(1, transport.process_calls)
                finally:
                    database.close()
            finally:
                pipeline.close()
                repair.close()
                preparation.close()


if __name__ == "__main__":
    unittest.main()
