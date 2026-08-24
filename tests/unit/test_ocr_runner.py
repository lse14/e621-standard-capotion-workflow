from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.contracts import JobConfig, sha256_json  # noqa: E402
from anima_core.db import StateDatabase  # noqa: E402
from anima_core.ocr_overlay import OcrWorkingSidecarView  # noqa: E402
from anima_core.ocr_runtime_binding import GIB, normalize_ocr_execution, recommend_tuning, write_execution_request  # noqa: E402
from anima_core.ocr_sidecar import OcrSidecarError, is_reusable, parse_ocr_sidecar, serialize_ocr_sidecar  # noqa: E402
from anima_core.overlay import OverlayLayout  # noqa: E402
from anima_core.path_safety import windows_key  # noqa: E402
from anima_core.pipeline_dispatch import PipelineDispatchMixin  # noqa: E402
from anima_core.profiles import module_availability  # noqa: E402
from anima_core.scheduler import BoundedScheduler  # noqa: E402
from anima_core.worker_protocol import ProtocolEnvelopeV1  # noqa: E402


OCR_RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
OCR_RESOURCE_MANIFEST = r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json"
OCR_RESOURCE_FINGERPRINT = "b" * 64
_VRAM_REPORTING_TOLERANCE_BYTES = 16 * 1024 ** 2
INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("/", "\\"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class OcrRuntimeTuningTests(unittest.TestCase):
    def test_near_24_gib_cuda_report_uses_the_24_gib_tier(self) -> None:
        reported_vram = 24 * GIB - 12 * 1024 ** 2

        self.assertEqual(
            (2560, 4),
            (
                recommend_tuning(device="cuda", total_vram_bytes=reported_vram).textDetLimitSideLen,
                recommend_tuning(device="cuda", total_vram_bytes=reported_vram).textBatchSize,
            ),
        )
        self.assertEqual(
            (2304, 2),
            (
                recommend_tuning(
                    device="cuda",
                    total_vram_bytes=24 * GIB - _VRAM_REPORTING_TOLERANCE_BYTES - 1,
                ).textDetLimitSideLen,
                recommend_tuning(
                    device="cuda",
                    total_vram_bytes=24 * GIB - _VRAM_REPORTING_TOLERANCE_BYTES - 1,
                ).textBatchSize,
            ),
        )


def _success_sidecar(
    relative_path: str,
    image_bytes: bytes,
    *,
    resource_fingerprint: str = OCR_RESOURCE_FINGERPRINT,
    threshold: float = 0.5,
    text: str = "Hello",
    confidence: float = 0.9,
    inference: dict[str, object] | None = None,
) -> bytes:
    value = {
        "schemaVersion": 1,
        "relativeImagePath": relative_path,
        "image": {
            "width": 10,
            "height": 8,
            "sizeBytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        },
        "status": "success",
        "engine": {
            "backend": "paddle",
            "resourceId": OCR_RESOURCE_ID,
            "resourceFingerprint": resource_fingerprint,
        },
        "settings": {"llmMinConfidence": threshold, "inference": dict(INFERENCE if inference is None else inference)},
        "items": [
            {
                "index": 0,
                "text": text,
                "confidence": confidence,
                "polygonPixels": [[0, 0], [4, 0], [4, 4], [0, 4]],
                "polygon": [[0, 0], [0.4, 0], [0.4, 0.5], [0, 0.5]],
                "bboxPixels": [0, 0, 4, 4],
                "bbox": [0, 0, 0.4, 0.5],
                "position": "top-left",
                "textlineOrientationDegrees": 0,
                "includedForLlm": confidence >= threshold,
            }
        ],
        "error": None,
    }
    return serialize_ocr_sidecar(parse_ocr_sidecar(json.dumps(value).encode("utf-8")))


def _failed_sidecar(relative_path: str, image_bytes: bytes) -> bytes:
    value = json.loads(_success_sidecar(relative_path, image_bytes))
    value["status"] = "failed"
    value["items"] = []
    value["error"] = {
        "code": "ocr_inference_failed",
        "message": "OCR inference failed for this image.",
        "retriable": True,
    }
    return serialize_ocr_sidecar(parse_ocr_sidecar(json.dumps(value).encode("utf-8")))


def _success_outcome(item: dict[str, object], *, duplicate: bool = False) -> dict[str, object]:
    entries = [
        {
            "text": "Hello",
            "confidence": 0.9,
            "polygonPixels": [[0, 0], [4, 0], [4, 4], [0, 4]],
            "bboxPixels": [0, 0, 4, 4],
            "textlineOrientationDegrees": 0,
        }
    ]
    if duplicate:
        entries.append(
            {
                "text": "Hello",
                "confidence": 0.4,
                "polygonPixels": [[5, 4], [10, 4], [10, 8], [5, 8]],
                "bboxPixels": [5, 4, 10, 8],
                "textlineOrientationDegrees": 180,
            }
        )
    return {
        "schemaVersion": 1,
        "status": "success",
        "sampleId": item["sampleId"],
        "leaseId": item["leaseId"],
        "relativeImagePath": item["relativeImagePath"],
        "image": {
            "width": 10,
            "height": 8,
            "sizeBytes": item["imageSize"],
            "sha256": item["imageSha256"],
        },
        "items": entries,
    }


def _no_text_outcome(item: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "status": "no_text",
        "sampleId": item["sampleId"],
        "leaseId": item["leaseId"],
        "relativeImagePath": item["relativeImagePath"],
        "image": {
            "width": 10,
            "height": 8,
            "sizeBytes": item["imageSize"],
            "sha256": item["imageSha256"],
        },
        "items": [],
    }


def _failed_outcome(item: dict[str, object], *, oversize: bool = False) -> dict[str, object]:
    width, height = (20_000, 2_001) if oversize else (10, 8)
    error: dict[str, object] = {
        "code": "ocr_image_too_large" if oversize else "ocr_inference_failed",
        "message": (
            "OCR image dimensions exceed the first-release safety limit."
            if oversize
            else "OCR inference failed for this image."
        ),
        "retriable": not oversize,
    }
    if oversize:
        error["details"] = {
            "actualPixels": width * height,
            "maxPixels": 40_000_000,
            "maxSide": 16_384,
        }
    return {
        "schemaVersion": 1,
        "status": "failed",
        "sampleId": item["sampleId"],
        "leaseId": item["leaseId"],
        "relativeImagePath": item["relativeImagePath"],
        "image": {
            "width": width,
            "height": height,
            "sizeBytes": item["imageSize"],
            "sha256": item["imageSha256"],
        },
        "items": [],
        "error": error,
    }


class FakeOcrTransport:
    def __init__(self, outcome_factory=_success_outcome, *, error_code: str | None = None) -> None:
        self.outcome_factory = outcome_factory
        self.error_code = error_code
        self.hello_calls = 0
        self.process_calls = 0
        self.process_paths: list[str] = []
        self.batch_sizes: list[int] = []
        self.hello_payloads: list[dict[str, object]] = []

    @staticmethod
    def _response(request: ProtocolEnvelopeV1, method: str, payload: dict[str, object]) -> ProtocolEnvelopeV1:
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
        if self.error_code is not None:
            return self._response(request, "error", {"code": self.error_code})
        if request.method == "hello":
            self.hello_calls += 1
            self.hello_payloads.append(dict(request.payload))
            payload: dict[str, object] = {
                "schemaVersion": 1,
                "payloadType": "ocr_hello_result",
                "ready": True,
                "executable": r"C:\ocr-paddle\python.exe",
                "pythonVersion": "3.11.15",
                "modelSessionLoads": 1,
                "resourceFingerprint": request.payload["resourceFingerprint"],
            }
            if "expectedRuntimeId" in request.payload:
                runtime_id = request.payload["expectedRuntimeId"]
                observed = "cuda" if runtime_id == "ocr-paddle-gpu" else "cpu"
                payload.update({
                    "requestedDevice": request.payload["requestedDevice"],
                    "observedDevice": observed,
                    "runtimeId": runtime_id,
                    "runtimeFingerprint": request.payload["expectedRuntimeFingerprint"],
                    "paddleVersion": "3.2.2",
                    "compiledWithCuda": observed == "cuda",
                    "cudaVersion": "12.6" if observed == "cuda" else None,
                    "gpuName": "Test GPU" if observed == "cuda" else None,
                    "totalVramBytes": 24 * 1024 ** 3 if observed == "cuda" else None,
                })
            return self._response(
                request,
                "hello",
                payload,
            )
        if request.method == "process_batch":
            self.process_calls += 1
            items = request.payload["items"]
            assert isinstance(items, list)
            self.batch_sizes.append(len(items))
            self.process_paths.extend(str(item["relativeImagePath"]) for item in items if isinstance(item, dict))
            return self._response(
                request,
                "result",
                {
                    "schemaVersion": 1,
                    "payloadType": "ocr_process_result",
                    "items": [self.outcome_factory(dict(item)) for item in items if isinstance(item, dict)],
                },
            )
        raise AssertionError(f"unexpected OCR worker method: {request.method}")


class OcrFixture:
    def __init__(
        self,
        root: Path,
        *,
        sample_count: int = 1,
        enabled: bool = True,
        threshold: float = 0.5,
        force_reprocess: bool = False,
        resource_fingerprint: str = OCR_RESOURCE_FINGERPRINT,
        schema_version: int = 5,
        ocr_device: str = "auto",
    ) -> None:
        self.root = root
        self.dataset = root / "dataset"
        self.dataset.mkdir()
        self.image_bytes: dict[int, bytes] = {}
        self.relative_paths: dict[int, str] = {}
        for sample_id in range(1, sample_count + 1):
            relative = f"cats\\image-{sample_id}.jpg"
            source = self.dataset / "cats" / f"image-{sample_id}.jpg"
            source.parent.mkdir(parents=True, exist_ok=True)
            image_bytes = f"image-{sample_id}-bytes".encode("ascii")
            source.write_bytes(image_bytes)
            (source.with_suffix(".json")).write_text('{"nl":"baseline"}', encoding="utf-8")
            self.image_bytes[sample_id] = image_bytes
            self.relative_paths[sample_id] = relative

        self.layout = OverlayLayout.create(self.dataset, "job-ocr-runner")
        self.database = StateDatabase.open(root / "state.db")
        self.config = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot=str(self.dataset),
            schemaVersion=schema_version,
        )
        self.config.nl["promptVersion"] = "nl-default-prompt-v3"
        self.config.ocr.update(
            {
                "enabled": enabled,
                "llmMinConfidence": threshold,
                "forceReprocess": force_reprocess,
                "resourceId": OCR_RESOURCE_ID,
                "resourceManifestRelativePath": OCR_RESOURCE_MANIFEST,
                "resourceFingerprint": resource_fingerprint,
                "device": ocr_device,
            }
        )
        if schema_version >= 6:
            self.config.tokenBudget["enabled"] = False
        frozen = self.config.to_dict()
        self.config_hash = sha256_json(frozen)
        self.database.insert_job(
            {
                "job_id": "job-ocr-runner",
                "config_schema_version": schema_version,
                "config_json": json.dumps(frozen),
                "config_hash": self.config_hash,
                "profile": "e621",
                "work_mode": "in_place",
                "overwrite_mode": "incremental",
                "source_root": str(self.dataset),
                "output_root": None,
                "dataset_root": str(self.dataset),
                "dataset_root_key": windows_key(self.dataset),
                "manifest_schema_version": 1,
                "recursive": 0,
                "sample_count": sample_count,
                "manifest_generated_at": "2026-08-02T00:00:00Z",
                "status": "running",
                "current_module_id": "ocr",
                "last_event_id": 0,
                "pinned": 0,
                "api_budget_extra": 0,
                "api_budget_revision": 0,
                "overlay_root": str(self.layout.root),
                "commit_journal_path": None,
                "resume_status": None,
                "created_at": "2026-08-02T00:00:00Z",
                "started_at": None,
                "cancel_requested_at": None,
                "finished_at": None,
            }
        )
        self.database.insert_samples(
            "job-ocr-runner",
            [
                {
                    "sample_id": sample_id,
                    "relative_image_path": self.relative_paths[sample_id],
                    "annotation_key": f"cats\\image-{sample_id}",
                    "source": "e621",
                    "in_processing_scope": True,
                    "image_format": "jpeg",
                    "image_frame_count": 1,
                    "original_txt_state": "missing_or_blank",
                    "original_json_state": "nonblank",
                    "image_file_id": f"volume:{sample_id}",
                    "image_size": len(self.image_bytes[sample_id]),
                    "image_mtime_ns": sample_id,
                    "original_txt_sha256": None,
                    "original_json_sha256": hashlib.sha256(
                        (self.dataset / "cats" / f"image-{sample_id}.json").read_bytes()
                    ).hexdigest(),
                }
                for sample_id in range(1, sample_count + 1)
            ],
        )
        self.database.initialize_module_summary("job-ocr-runner", "ocr", total=sample_count)
        self.database.set_module_summary("job-ocr-runner", "ocr", status="running")
        self.database.reset_next_module_page("job-ocr-runner", "ocr", limit=sample_count)
        lease_counter = iter(range(1, 10_000))
        self.scheduler = BoundedScheduler(
            self.database,
            lease_id_factory=lambda: f"lease-ocr-{next(lease_counter)}",
        )
        self.view = OcrWorkingSidecarView(self.dataset, self.layout)

    def source_path(self, sample_id: int) -> Path:
        return self.dataset / "cats" / f"image-{sample_id}.jpg"

    def runner(self, module: object, transport: FakeOcrTransport):
        return module.OcrRunner(
            self.database,
            self.scheduler,
            transport,
            self.view,
            job_id="job-ocr-runner",
            worker_instance_id="ocr-worker-1",
            resource_manifest_relative_path=OCR_RESOURCE_MANIFEST,
            resource_fingerprint=str(self.config.ocr["resourceFingerprint"]),
        )

    def close(self) -> None:
        self.database.close()
        if self.layout.root.exists():
            self.layout.discard()


class OcrRunnerTests(unittest.TestCase):
    @staticmethod
    def _api():
        spec = importlib.util.find_spec("anima_core.ocr_runner")
        if spec is None:
            raise AssertionError("OCR runner module is missing")
        return importlib.import_module("anima_core.ocr_runner")

    def test_disabled_ocr_neither_starts_worker_nor_hashes_images(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), enabled=False)
            try:
                transport = FakeOcrTransport()
                with patch.object(module, "stream_sha256", side_effect=AssertionError("disabled OCR hashed an image")):
                    self.assertEqual("skipped", fixture.runner(module, transport).run().status)
                self.assertEqual((0, 0), (transport.hello_calls, transport.process_calls))
            finally:
                fixture.close()

    def test_v9_ocr_configuration_is_accepted_by_the_runner(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), schema_version=9)
            try:
                write_execution_request(
                    fixture.layout.resource_path("ocr-execution-request-v1.json"),
                    normalize_ocr_execution(None),
                )
                transport = FakeOcrTransport()
                report = module.OcrRunner(
                    fixture.database,
                    fixture.scheduler,
                    transport,
                    fixture.view,
                    job_id="job-ocr-runner",
                    worker_instance_id="ocr-worker-1",
                    resource_manifest_relative_path=OCR_RESOURCE_MANIFEST,
                    resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
                    runtime_id="ocr-paddle",
                    runtime_fingerprint="a" * 64,
                    binding_path=fixture.layout.resource_path("ocr-runtime-binding-v1.json"),
                ).run()
                self.assertEqual("completed", report.status)
                self.assertEqual((1, 1), (transport.hello_calls, transport.process_calls))
            finally:
                fixture.close()

    def test_v7_binds_runtime_before_the_first_single_sample_lease(self) -> None:
        module = self._api()
        required = {"runtime_id", "runtime_fingerprint", "binding_path"}
        supported = required <= set(inspect.signature(module.OcrRunner).parameters)
        self.assertTrue(supported, "Task 3.4 runner must receive its immutable runtime-binding boundary")
        if not supported:
            return
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), schema_version=7)
            try:
                binding_path = fixture.layout.resource_path("ocr-runtime-binding-v1.json")
                write_execution_request(
                    fixture.layout.resource_path("ocr-execution-request-v1.json"),
                    normalize_ocr_execution(None),
                )
                claimed_after_binding: list[bool] = []
                original_claim = fixture.scheduler.claim_batch

                def claim(*args: object, **kwargs: object):
                    claimed_after_binding.append(binding_path.is_file())
                    return original_claim(*args, **kwargs)

                fixture.scheduler.claim_batch = claim  # type: ignore[method-assign]
                runner = module.OcrRunner(
                    fixture.database,
                    fixture.scheduler,
                    FakeOcrTransport(),
                    fixture.view,
                    job_id="job-ocr-runner",
                    worker_instance_id="ocr-worker-1",
                    resource_manifest_relative_path=OCR_RESOURCE_MANIFEST,
                    resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
                    runtime_id="ocr-paddle",
                    runtime_fingerprint="a" * 64,
                    binding_path=binding_path,
                )
                self.assertEqual("completed", runner.run().status)
                self.assertTrue(claimed_after_binding)
                self.assertTrue(all(claimed_after_binding))
            finally:
                fixture.close()

    def test_v7_sends_frozen_effective_tuning_with_the_first_hello(self) -> None:
        module = self._api()
        required = {"runtime_id", "runtime_fingerprint", "binding_path", "total_vram_bytes"}
        self.assertTrue(required <= set(inspect.signature(module.OcrRunner).parameters))
        if not required <= set(inspect.signature(module.OcrRunner).parameters):
            return
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), schema_version=7, ocr_device="cuda")
            try:
                binding_path = fixture.layout.resource_path("ocr-runtime-binding-v1.json")
                write_execution_request(
                    fixture.layout.resource_path("ocr-execution-request-v1.json"),
                    normalize_ocr_execution(None),
                )
                transport = FakeOcrTransport()
                report = module.OcrRunner(
                    fixture.database,
                    fixture.scheduler,
                    transport,
                    fixture.view,
                    job_id="job-ocr-runner",
                    worker_instance_id="ocr-worker-1",
                    resource_manifest_relative_path=OCR_RESOURCE_MANIFEST,
                    resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
                    runtime_id="ocr-paddle-gpu",
                    runtime_fingerprint="a" * 64,
                    binding_path=binding_path,
                    total_vram_bytes=24 * 1024 ** 3,
                ).run()
                self.assertEqual("completed", report.status)
                self.assertEqual(
                    {"textDetLimitSideLen": 2560, "textBatchSize": 4},
                    transport.hello_payloads[0]["executionTuning"],
                )
                evidence = fixture.database.get_runtime_evidence("job-ocr-runner")
                self.assertEqual("cuda", evidence["ocr"]["observedDevice"])
                self.assertEqual("ocr-paddle-gpu", evidence["ocr"]["runtimeId"])
            finally:
                fixture.close()

    def test_v8_reuses_the_v7_device_aware_runtime_binding(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), schema_version=8, ocr_device="cuda")
            try:
                binding_path = fixture.layout.resource_path("ocr-runtime-binding-v1.json")
                write_execution_request(
                    fixture.layout.resource_path("ocr-execution-request-v1.json"),
                    normalize_ocr_execution(None),
                )
                transport = FakeOcrTransport()
                report = module.OcrRunner(
                    fixture.database,
                    fixture.scheduler,
                    transport,
                    fixture.view,
                    job_id="job-ocr-runner",
                    worker_instance_id="ocr-worker-1",
                    resource_manifest_relative_path=OCR_RESOURCE_MANIFEST,
                    resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
                    runtime_id="ocr-paddle-gpu",
                    runtime_fingerprint="a" * 64,
                    binding_path=binding_path,
                    total_vram_bytes=24 * 1024 ** 3,
                ).run()
                self.assertEqual("completed", report.status)
                self.assertEqual(
                    {"textDetLimitSideLen": 2560, "textBatchSize": 4},
                    transport.hello_payloads[0]["executionTuning"],
                )
            finally:
                fixture.close()

    def test_effective_detection_limit_participates_in_sidecar_reuse_without_runtime_or_batch_fields(self) -> None:
        tuned = {**INFERENCE, "textDetLimitSideLen": 2560}
        image_bytes = b"sidecar-tuning"
        try:
            sidecar = parse_ocr_sidecar(
                _success_sidecar("image.jpg", image_bytes, inference=tuned),
                expected_relative_image_path="image.jpg",
            )
        except OcrSidecarError as exc:
            self.fail(f"Task 3.4 sidecars must accept the frozen effective detection limit: {exc}")
        self.assertTrue(is_reusable(
            sidecar,
            image_size=len(image_bytes),
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
            inference_settings=tuned,
        ))
        self.assertFalse(is_reusable(
            sidecar,
            image_size=len(image_bytes),
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
            inference_settings=INFERENCE,
        ))
        self.assertEqual(set(INFERENCE), set(sidecar.settings.inference))

    def test_ocr_module_is_startable_by_profile_availability(self) -> None:
        self.assertEqual("pending", module_availability("e621", "ocr", enabled=True))
        self.assertEqual("skipped", module_availability("e621", "ocr", enabled=False))

    def test_disabled_ocr_dispatch_does_not_select_a_resource_or_spawn_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), enabled=False)
            try:
                dispatch = object.__new__(PipelineDispatchMixin)
                dispatch._selected_resource = lambda *_: (_ for _ in ()).throw(AssertionError("disabled OCR selected a resource"))
                dispatch._spawn_transport = lambda *_: (_ for _ in ()).throw(AssertionError("disabled OCR spawned a worker"))
                config = json.loads(str(fixture.database.get_job("job-ocr-runner")["config_json"]))

                self.assertEqual(
                    "skipped",
                    dispatch._run_active_module(
                        fixture.database,
                        fixture.scheduler,
                        "job-ocr-runner",
                        "ocr",
                        config,
                    ),
                )
            finally:
                fixture.close()

    def test_reuses_legal_formal_sidecar_and_rewrites_only_llm_threshold_in_task_overlay(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), threshold=0.95)
            try:
                relative = fixture.relative_paths[1]
                target = fixture.dataset / "ocr_annotations" / "cats" / "image-1.jpg.ocr.json"
                target.parent.mkdir(parents=True)
                target.write_bytes(_success_sidecar(relative, fixture.image_bytes[1], threshold=0.5, confidence=0.9))
                transport = FakeOcrTransport()

                report = fixture.runner(module, transport).run()
                rewritten = parse_ocr_sidecar(
                    fixture.layout.ocr_sidecar_path(relative).read_bytes(),
                    expected_relative_image_path=relative,
                )

                self.assertEqual("completed", report.status)
                self.assertEqual((0, 0), (transport.hello_calls, transport.process_calls))
                self.assertEqual((0.95, False), (rewritten.settings.llmMinConfidence, rewritten.items[0].includedForLlm))
                self.assertEqual("completed", fixture.database.get_sample_state("job-ocr-runner", 1)["status"])
            finally:
                fixture.close()

    def test_corrupt_failed_changed_resource_changed_image_and_force_sidecars_reinfer(self) -> None:
        module = self._api()
        cases = ("corrupt", "failed", "resource", "image", "force")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = OcrFixture(
                    Path(temporary),
                    force_reprocess=case == "force",
                    resource_fingerprint="c" * 64 if case == "resource" else OCR_RESOURCE_FINGERPRINT,
                )
                try:
                    relative = fixture.relative_paths[1]
                    if case == "corrupt":
                        fixture.layout.write_ocr_sidecar(relative, b"{not-json")
                    elif case == "failed":
                        fixture.layout.write_ocr_sidecar(relative, _failed_sidecar(relative, fixture.image_bytes[1]))
                    else:
                        fixture.layout.write_ocr_sidecar(
                            relative,
                            _success_sidecar(
                                relative,
                                fixture.image_bytes[1],
                                resource_fingerprint=OCR_RESOURCE_FINGERPRINT,
                            ),
                        )
                    if case == "image":
                        fixture.source_path(1).write_bytes(b"changed-image-bytes")
                    transport = FakeOcrTransport()

                    self.assertEqual("completed", fixture.runner(module, transport).run().status)
                    self.assertEqual(1, transport.process_calls)
                    if case == "image":
                        sidecar = parse_ocr_sidecar(
                            fixture.layout.ocr_sidecar_path(relative).read_bytes(),
                            expected_relative_image_path=relative,
                        )
                        self.assertEqual(
                            (len(b"changed-image-bytes"), hashlib.sha256(b"changed-image-bytes").hexdigest()),
                            (sidecar.image.sizeBytes, sidecar.image.sha256),
                        )
                finally:
                    fixture.close()

    def test_success_keeps_worker_order_duplicate_text_and_business_annotations_unchanged(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), sample_count=2)
            before = _hash_tree(fixture.dataset)
            try:
                transport = FakeOcrTransport(lambda item: _success_outcome(item, duplicate=True))
                report = fixture.runner(module, transport).run()

                self.assertEqual(("completed", 1), (report.status, report.maxResidentLeases))
                self.assertEqual((1, 2), (transport.hello_calls, transport.process_calls))
                self.assertEqual([1, 1], transport.batch_sizes)
                self.assertEqual([fixture.relative_paths[1], fixture.relative_paths[2]], transport.process_paths)
                for sample_id in (1, 2):
                    sidecar = parse_ocr_sidecar(
                        fixture.layout.ocr_sidecar_path(fixture.relative_paths[sample_id]).read_bytes(),
                        expected_relative_image_path=fixture.relative_paths[sample_id],
                    )
                    self.assertEqual(["Hello", "Hello"], [item.text for item in sidecar.items])
                    self.assertEqual([0, 1], [item.index for item in sidecar.items])
                self.assertEqual(before, _hash_tree(fixture.dataset))
            finally:
                fixture.close()

    def test_no_text_is_a_normal_completed_result(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary))
            try:
                report = fixture.runner(module, FakeOcrTransport(_no_text_outcome)).run()
                sidecar = parse_ocr_sidecar(
                    fixture.layout.ocr_sidecar_path(fixture.relative_paths[1]).read_bytes()
                )
                self.assertEqual(("completed", "no_text", 0), (report.status, sidecar.status, report.issueCount))
            finally:
                fixture.close()

    def test_success_sidecar_is_staged_before_completion_and_prepared_file_is_consumed(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary))
            try:
                with patch.object(
                    fixture.database,
                    "stage_prepared_artifact",
                    wraps=fixture.database.stage_prepared_artifact,
                ) as stage_prepared:
                    self.assertEqual("completed", fixture.runner(module, FakeOcrTransport()).run().status)
                self.assertEqual(1, stage_prepared.call_count)
                self.assertEqual((), tuple((fixture.layout.root / "prepared" / "ocr").glob("*")))
            finally:
                fixture.close()

    def test_normal_inference_failure_replaces_stale_text_and_settles_nonblocking_retriable_issue(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary), force_reprocess=True)
            try:
                relative = fixture.relative_paths[1]
                fixture.layout.write_ocr_sidecar(relative, _success_sidecar(relative, fixture.image_bytes[1], text="stale"))
                report = fixture.runner(module, FakeOcrTransport(_failed_outcome)).run()
                sidecar = parse_ocr_sidecar(fixture.layout.ocr_sidecar_path(relative).read_bytes())
                issue = fixture.database.page_issues("job-ocr-runner", limit=1)[0]
                summary = fixture.database.module_summary("job-ocr-runner", "ocr")

                self.assertEqual(("completed_with_issues", "failed"), (report.status, sidecar.status))
                self.assertEqual((0, "ocr_inference_failed", "warning", 0, 1, "ocr"), (
                    int(summary["failed"]),
                    issue["code"],
                    issue["severity"],
                    int(issue["blocking"]),
                    int(issue["retriable"]),
                    issue["repair_start_module"],
                ))
                self.assertEqual("completed", fixture.database.get_sample_state("job-ocr-runner", 1)["status"])
                self.assertEqual((), sidecar.items)
            finally:
                fixture.close()

    def test_image_too_large_settles_nonblocking_nonretriable_issue_with_strict_details(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = OcrFixture(Path(temporary))
            try:
                report = fixture.runner(module, FakeOcrTransport(lambda item: _failed_outcome(item, oversize=True))).run()
                sidecar = parse_ocr_sidecar(fixture.layout.ocr_sidecar_path(fixture.relative_paths[1]).read_bytes())
                issue = fixture.database.page_issues("job-ocr-runner", limit=1)[0]

                self.assertEqual(("completed_with_issues", "ocr_image_too_large"), (report.status, sidecar.error.code))
                self.assertEqual((40_020_000, 40_000_000, 16_384), (
                    sidecar.error.details.actualPixels,
                    sidecar.error.details.maxPixels,
                    sidecar.error.details.maxSide,
                ))
                self.assertEqual((0, 0), (int(issue["blocking"]), int(issue["retriable"])))
                self.assertEqual("completed", fixture.database.get_sample_state("job-ocr-runner", 1)["status"])
            finally:
                fixture.close()

    def test_worker_protocol_or_runtime_failure_blocks_the_module(self) -> None:
        module = self._api()
        for error_code in ("ocr_initialization_failed", "ocr_protocol_violation"):
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as temporary:
                fixture = OcrFixture(Path(temporary))
                try:
                    with self.assertRaises(module.OcrRunnerFatalError):
                        fixture.runner(module, FakeOcrTransport(error_code=error_code)).run()
                    self.assertEqual("failed", fixture.database.get_job("job-ocr-runner")["status"])
                    self.assertEqual("failed", fixture.database.module_summary("job-ocr-runner", "ocr")["status"])
                finally:
                    fixture.close()

    def test_streaming_hash_and_database_schema_do_not_persist_image_digest(self) -> None:
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "image.jpg"
            target.write_bytes(b"abcdefghij")
            self.assertEqual(
                hashlib.sha256(b"abcdefghij").hexdigest(),
                module.stream_sha256(target, chunk_size=3),
            )
            database = StateDatabase.open(Path(temporary) / "state.db")
            try:
                columns = {
                    row["name"]
                    for table in ("samples", "sample_state")
                    for row in database.connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertNotIn("image_sha256", columns)
                self.assertNotIn("ocr_image_sha256", columns)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
