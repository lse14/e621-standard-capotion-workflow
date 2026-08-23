from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .contracts import ProgressEvent, SampleIssue, WorkLease, job_config_supports_ocr, job_config_supports_ocr_device, sha256_json
from .db import StateDatabase
from .db_scheduler import _complete_leased_sample_with_issue
from .ocr_overlay import OcrWorkingSidecarView
from .ocr_runtime_binding import (
    OcrExecutionError,
    OcrExecutionRequestV1,
    OcrRuntimeBindingV1,
    read_execution_request,
    read_runtime_binding,
    recommend_tuning,
    write_runtime_binding,
)
from .ocr_protocol import (
    OcrHelloRequestV1,
    OcrHelloResultV1,
    OcrOutcomeV1,
    OcrProcessRequestV1,
    OcrProtocolError,
    OcrRawItemV1,
    OcrWorkItemV1,
    parse_ocr_process_result,
    validate_hello_result,
    validate_outcome_for_item,
)
from .ocr_sidecar import (
    FIXED_OCR_INFERENCE_SETTINGS,
    OcrSidecar,
    OcrSidecarError,
    is_reusable,
    parse_ocr_sidecar,
    position_from_bbox,
    serialize_ocr_sidecar,
    with_llm_threshold,
)
from .overlay import OverlayError
from .path_safety import PathSafetyError, safe_relative_path, sha256_file
from .scheduler import BoundedScheduler, SchedulerError
from .stdio_transport import StdioJsonlTransportError
from .worker_protocol import ProtocolEnvelopeV1, ProtocolError


OCR_RUNTIME_ID = "ocr-paddle"
OCR_OWNER = "ocr"
OCR_RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
OCR_FATAL_WORKER_CODES = frozenset(
    {"ocr_initialization_failed", "ocr_resource_invalid", "ocr_protocol_violation"}
)


class OcrTransport(Protocol):
    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1: ...


class OcrRunnerFatalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OcrRunReport:
    status: str
    total: int
    completed: int
    failed: int
    skipped: int
    issueCount: int
    maxResidentLeases: int
    helloRequests: int
    processRequests: int


def stable_ocr_issue_id(job_id: str, sample_id: int, code: str) -> str:
    return hashlib.sha256(f"{job_id}\0{sample_id}\0ocr\0{code}".encode("utf-8")).hexdigest()


def stream_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    if type(chunk_size) is not int or chunk_size <= 0 or chunk_size > 16 * 1024 * 1024:
        raise ValueError("chunk_size is invalid")
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_bbox(bbox: tuple[float, float, float, float], *, width: int, height: int) -> tuple[float, float, float, float]:
    return (bbox[0] / width, bbox[1] / height, bbox[2] / width, bbox[3] / height)


def _normalized_polygon(
    polygon: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]],
    *,
    width: int,
    height: int,
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]:
    return tuple((x / width, y / height) for x, y in polygon)  # type: ignore[return-value]


def _sidecar_from_outcome(
    outcome: OcrOutcomeV1,
    *,
    threshold: float,
    resource_fingerprint: str,
    inference_settings: dict[str, object],
) -> OcrSidecar:
    image = outcome.image.to_dict()
    items: list[dict[str, object]] = []
    width = outcome.image.width
    height = outcome.image.height
    for index, raw in enumerate(outcome.items):
        bbox = _normalized_bbox(raw.bboxPixels, width=width, height=height)
        polygon = _normalized_polygon(raw.polygonPixels, width=width, height=height)
        items.append(
            {
                "index": index,
                "text": raw.text,
                "confidence": raw.confidence,
                "polygonPixels": [list(point) for point in raw.polygonPixels],
                "polygon": [list(point) for point in polygon],
                "bboxPixels": list(raw.bboxPixels),
                "bbox": list(bbox),
                "position": position_from_bbox(bbox),
                "textlineOrientationDegrees": raw.textlineOrientationDegrees,
                "includedForLlm": raw.confidence >= threshold,
            }
        )
    value: dict[str, object] = {
        "schemaVersion": 1,
        "relativeImagePath": outcome.relativeImagePath,
        "image": image,
        "status": outcome.status,
        "engine": {
            "backend": "paddle",
            "resourceId": OCR_RESOURCE_ID,
            "resourceFingerprint": resource_fingerprint,
        },
        "settings": {"llmMinConfidence": threshold, "inference": dict(inference_settings)},
        "items": items,
        "error": None,
    }
    if outcome.error is not None:
        value["error"] = outcome.error.to_dict()
    return parse_ocr_sidecar(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class OcrRunner:
    """Core-owned OCR orchestration; the worker returns raw OCR and Core writes sidecars."""

    def __init__(
        self,
        database: StateDatabase,
        scheduler: BoundedScheduler,
        transport: OcrTransport,
        sidecar_view: OcrWorkingSidecarView,
        *,
        job_id: str,
        worker_instance_id: str,
        resource_manifest_relative_path: str,
        resource_fingerprint: str,
        runtime_id: str = OCR_RUNTIME_ID,
        runtime_fingerprint: str | None = None,
        binding_path: str | Path | None = None,
        total_vram_bytes: int | None = None,
        startup_reason: str | None = None,
        progress_consumer: Callable[[ProgressEvent], None] | None = None,
        message_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not worker_instance_id or len(worker_instance_id) > 128:
            raise ValueError("OCR worker instance id must be a non-empty bounded string")
        if runtime_id not in {"ocr-paddle", "ocr-paddle-gpu"}:
            raise ValueError("OCR runtime id is invalid")
        self.database = database
        self.scheduler = scheduler
        self.transport = transport
        self.sidecar_view = sidecar_view
        self.job_id = job_id
        self.worker_instance_id = worker_instance_id
        self.resource_manifest_relative_path = resource_manifest_relative_path
        self.resource_fingerprint = resource_fingerprint
        self.runtime_id = runtime_id
        self.runtime_fingerprint = runtime_fingerprint
        self.binding_path = Path(binding_path) if binding_path is not None else None
        self.total_vram_bytes = total_vram_bytes
        self.startup_reason = startup_reason
        self.inference_settings = dict(FIXED_OCR_INFERENCE_SETTINGS)
        self._execution_tuning: tuple[int, int] | None = None
        self._selection_recommendation = None
        self.progress_consumer = progress_consumer
        self.message_id_factory = message_id_factory or (lambda: f"ocr-{uuid.uuid4().hex}")
        self.max_resident_leases = 0
        self.hello_requests = 0
        self.process_requests = 0
        self._started = False

    def _fatal(self, code: str, message: str) -> OcrRunnerFatalError:
        return OcrRunnerFatalError(code, message)

    def _config(self) -> tuple[str, dict[str, object], float, int]:
        job = self.database.get_job(self.job_id)
        try:
            config = json.loads(str(job["config_json"]))
        except json.JSONDecodeError as exc:
            raise self._fatal("ocr_protocol_violation", "frozen JobConfig is invalid JSON") from exc
        if (
            not isinstance(config, dict)
            or sha256_json(config) != job["config_hash"]
            or not job_config_supports_ocr(config.get("schemaVersion"))
            or not job_config_supports_ocr(int(job["config_schema_version"]))
            or not isinstance(config.get("ocr"), dict)
        ):
            raise self._fatal("ocr_protocol_violation", "frozen OCR configuration is invalid")
        ocr = config["ocr"]
        threshold = ocr.get("llmMinConfidence")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= float(threshold) <= 1:
            raise self._fatal("ocr_protocol_violation", "frozen OCR threshold is invalid")
        return str(job["config_hash"]), ocr, float(threshold), int(job["config_schema_version"])

    def _exchange(self, method: str, payload: dict[str, object], config_hash: str) -> dict[str, object]:
        request = ProtocolEnvelopeV1(
            protocolVersion="1.0",
            kind="request",
            messageId=getattr(self, "message_id_factory", lambda: f"ocr-{uuid.uuid4().hex}")(),
            runtimeId=self.runtime_id,
            owner=OCR_OWNER,
            method=method,
            payload=payload,
            jobId=self.job_id,
            configHash=config_hash,
        )
        try:
            request = ProtocolEnvelopeV1.from_dict(request.to_dict(), runtime_id=self.runtime_id, owner=OCR_OWNER)
        except ProtocolError as exc:
            raise self._fatal("ocr_protocol_violation", str(exc)) from exc
        try:
            response = self.transport.exchange(request)
        except StdioJsonlTransportError:
            raise
        except Exception as exc:
            raise self._fatal("ocr_protocol_violation", "OCR transport failed") from exc
        if not isinstance(response, ProtocolEnvelopeV1) or (
            response.protocolVersion != "1.0"
            or response.kind != "response"
            or response.replyTo != request.messageId
            or response.runtimeId != self.runtime_id
            or response.owner != OCR_OWNER
            or response.jobId != self.job_id
            or response.configHash != config_hash
        ):
            raise self._fatal("ocr_protocol_violation", "OCR response envelope identity mismatch")
        if response.method == "error":
            code = response.payload.get("code")
            raise self._fatal(
                code if code in OCR_FATAL_WORKER_CODES else "ocr_protocol_violation",
                "OCR worker returned fatal error",
            )
        if response.method != ("hello" if method == "hello" else "result"):
            raise self._fatal("ocr_protocol_violation", "OCR response method mismatch")
        return response.payload

    def _hello_request(
        self,
        config_hash: str,
        *,
        requested_device: str | None = None,
        execution_tuning: tuple[int, int] | None = None,
    ) -> OcrHelloRequestV1:
        value: dict[str, object] = {
            "schemaVersion": 1,
            "payloadType": "ocr_hello_request",
            "jobId": self.job_id,
            "configHash": config_hash,
            "resourceId": OCR_RESOURCE_ID,
            "resourceManifestRelativePath": self.resource_manifest_relative_path,
            "resourceFingerprint": self.resource_fingerprint,
            "inference": dict(self.inference_settings),
        }
        if requested_device is not None:
            if self.runtime_fingerprint is None:
                raise self._fatal("ocr_protocol_violation", "OCR runtime fingerprint is unavailable")
            value.update({
                "requestedDevice": requested_device,
                "expectedRuntimeId": self.runtime_id,
                "expectedRuntimeFingerprint": self.runtime_fingerprint,
            })
        if execution_tuning is not None:
            value["executionTuning"] = {
                "textDetLimitSideLen": execution_tuning[0],
                "textBatchSize": execution_tuning[1],
            }
        return OcrHelloRequestV1.from_dict(value)

    def _initialize_worker(self, hello: OcrHelloRequestV1, config_hash: str) -> OcrHelloResultV1:
        self.hello_requests += 1
        try:
            result = OcrHelloResultV1.from_dict(self._exchange("hello", hello.to_dict(), config_hash))
            validate_hello_result(result, hello)
        except OcrProtocolError as exc:
            raise self._fatal("ocr_protocol_violation", f"OCR hello result is invalid: {exc}") from exc
        return result

    def _v7_binding(
        self,
        *,
        ocr: dict[str, object],
        hello_result: OcrHelloResultV1 | None,
    ) -> OcrRuntimeBindingV1:
        if self.binding_path is None:
            raise self._fatal("ocr_protocol_violation", "OCR runtime binding path is unavailable")
        try:
            if self.binding_path.exists():
                binding = read_runtime_binding(self.binding_path)
                if (
                    binding.runtimeId != self.runtime_id
                    or binding.runtimeFingerprint != self.runtime_fingerprint
                    or binding.resourceFingerprint != self.resource_fingerprint
                ):
                    raise OcrExecutionError("OCR runtime binding does not match the selected runtime")
                if hello_result is not None and (
                    hello_result.runtimeId != binding.runtimeId
                    or hello_result.runtimeFingerprint != binding.runtimeFingerprint
                    or hello_result.observedDevice != binding.observedDevice
                    or hello_result.paddleVersion != binding.paddleVersion
                    or hello_result.compiledWithCuda != binding.compiledWithCuda
                    or hello_result.cudaVersion != binding.cudaVersion
                    or hello_result.gpuName != binding.gpuName
                    or hello_result.totalVramBytes != binding.totalVramBytes
                ):
                    raise OcrExecutionError("OCR worker runtime evidence does not match the binding")
                self.inference_settings["textDetLimitSideLen"] = binding.effectiveTextDetLimitSideLen
                self._execution_tuning = (
                    binding.effectiveTextDetLimitSideLen,
                    binding.effectiveTextBatchSize,
                )
                return binding
            request = read_execution_request(self.binding_path.with_name("ocr-execution-request-v1.json"))
            requested_device = ocr.get("device")
            if requested_device not in {"auto", "cuda", "cpu"}:
                raise OcrExecutionError("frozen OCR device is invalid")
            if hello_result is None or hello_result.observedDevice is None or hello_result.runtimeId is None:
                raise OcrExecutionError("OCR worker runtime evidence is unavailable")
            recommendation = recommend_tuning(
                device=hello_result.observedDevice,
                total_vram_bytes=hello_result.totalVramBytes,
            )
            if self._selection_recommendation is not None and recommendation != self._selection_recommendation:
                raise OcrExecutionError("OCR worker VRAM evidence does not match the selected tuning")
            effective_limit = (
                request.textDetLimitSideLen.value
                if request.textDetLimitSideLen.mode == "manual"
                else recommendation.textDetLimitSideLen
            )
            effective_batch = (
                request.textBatchSize.value
                if request.textBatchSize.mode == "manual"
                else recommendation.textBatchSize
            )
            binding = OcrRuntimeBindingV1.from_dict({
                "schemaVersion": 1,
                "requested": {"device": requested_device, **request.to_dict()},
                "recommended": {
                    "source": recommendation.source,
                    "totalVramBytes": hello_result.totalVramBytes,
                    "textDetLimitSideLen": recommendation.textDetLimitSideLen,
                    "textBatchSize": recommendation.textBatchSize,
                },
                "effective": {"textDetLimitSideLen": effective_limit, "textBatchSize": effective_batch},
                "runtime": {
                    "runtimeId": hello_result.runtimeId,
                    "runtimeFingerprint": hello_result.runtimeFingerprint,
                    "observedDevice": hello_result.observedDevice,
                    "paddleVersion": hello_result.paddleVersion,
                    "compiledWithCuda": hello_result.compiledWithCuda,
                    "cudaVersion": hello_result.cudaVersion,
                    "gpuName": hello_result.gpuName,
                },
                "resourceFingerprint": self.resource_fingerprint,
                "startupReason": self.startup_reason,
            })
            write_runtime_binding(self.binding_path, binding)
            self.inference_settings["textDetLimitSideLen"] = binding.effectiveTextDetLimitSideLen
            self._execution_tuning = (
                binding.effectiveTextDetLimitSideLen,
                binding.effectiveTextBatchSize,
            )
            return binding
        except OcrExecutionError as exc:
            raise self._fatal("ocr_protocol_violation", "OCR runtime binding is unavailable or invalid") from exc

    def _publish(self, status: str, attempt: int = 0) -> None:
        summary = self.database.module_summary(self.job_id, "ocr")
        settled = int(summary["completed"] + summary["failed"] + summary["skipped"])
        event = ProgressEvent(
            self.job_id,
            int(self.database.get_job(self.job_id)["last_event_id"]) + 1,
            "ocr",
            status,  # type: ignore[arg-type]
            settled,
            int(summary["total"]),
            str(self.database.get_job(self.job_id)["config_hash"]),
            attempt,
        )
        self.database.append_event(event)
        if self.progress_consumer is not None:
            self.progress_consumer(event)

    def _report(self, status: str) -> OcrRunReport:
        summary = self.database.module_summary(self.job_id, "ocr")
        return OcrRunReport(
            status,
            int(summary["total"]),
            int(summary["completed"]),
            int(summary["failed"]),
            int(summary["skipped"]),
            int(summary["issue_count"]),
            self.max_resident_leases,
            self.hello_requests,
            self.process_requests,
        )

    def _maybe_reuse(self, relative_path: str, *, image_size: int, image_sha256: str, threshold: float) -> bytes | None:
        raw = self.sidecar_view.read_bytes(relative_path)
        if raw is None:
            return None
        try:
            sidecar = parse_ocr_sidecar(raw, expected_relative_image_path=relative_path)
        except OcrSidecarError:
            return None
        if not is_reusable(
            sidecar,
            image_size=image_size,
            image_sha256=image_sha256,
            resource_fingerprint=self.resource_fingerprint,
            inference_settings=self.inference_settings,
        ) or sidecar.engine.resourceId != OCR_RESOURCE_ID:
            return None
        rewritten = with_llm_threshold(sidecar, threshold)
        return serialize_ocr_sidecar(rewritten)

    def _work_item(
        self,
        lease: WorkLease,
        row: object,
        *,
        image_size: int,
        image_sha256: str,
    ) -> OcrWorkItemV1:
        relative_path = str(row["relative_image_path"])
        dataset_root = Path(str(self.database.get_job(self.job_id)["dataset_root"]))
        image_path = dataset_root / Path(relative_path.replace("\\", os.sep))
        return OcrWorkItemV1.from_dict(
            {
                "schemaVersion": 1,
                "sampleId": lease.sampleId,
                "leaseId": lease.leaseId,
                "relativeImagePath": relative_path,
                "imagePath": str(image_path.resolve()),
                "imageSize": image_size,
                "imageSha256": image_sha256,
            }
        )

    def _issue(self, lease: WorkLease, row: object, sidecar: OcrSidecar) -> None:
        if sidecar.error is None:
            raise self._fatal("ocr_protocol_violation", "OCR failed sidecar has no error")
        issue = SampleIssue(
            issueId=stable_ocr_issue_id(self.job_id, lease.sampleId, sidecar.error.code),
            jobId=self.job_id,
            sampleId=lease.sampleId,
            relativeImagePath=str(row["relative_image_path"]),
            moduleId="ocr",
            code=sidecar.error.code,
            severity="warning",
            blocking=False,
            retriable=sidecar.error.retriable,
            repairStartModule="ocr",
            message=sidecar.error.message[:1024],
            attempt=lease.attempt,
        )
        if not lease.leaseId:
            raise self._fatal("ocr_protocol_violation", "OCR completion requires a lease")
        try:
            _complete_leased_sample_with_issue(
                self.database,
                self.job_id,
                "ocr",
                lease.sampleId,
                lease_id=lease.leaseId,
                issue=issue,
                allowed_statuses=("prepared",),
            )
        except ValueError as exc:
            raise self._fatal("ocr_protocol_violation", "OCR completion with issue does not belong to an active lease") from exc

    def _persist_sidecar(self, lease: WorkLease, relative_path: str, payload: bytes) -> None:
        if not lease.leaseId:
            raise self._fatal("ocr_protocol_violation", "OCR result requires a lease")
        prepared, digest = self.sidecar_view.layout.write_ocr_prepared(lease.leaseId, payload)
        relative = os.path.relpath(prepared, self.sidecar_view.layout.root).replace("/", "\\")
        self.scheduler.stage_prepared(lease, relative_path=relative, sha256=digest)
        self.sidecar_view.layout.commit_ocr_prepared(relative, digest, relative_path)

    def _recover_prepared_sidecars(self) -> None:
        """Commit and settle OCR outputs persisted before an in-process worker restart."""
        cursor: int | None = None
        while True:
            page = self.database.recovery_state_page(self.job_id, after_sample_id=cursor, limit=500)
            if not page:
                return
            for row in page:
                if row["current_module_id"] != "ocr" or row["status"] != "prepared":
                    continue
                lease_id = row["lease_id"]
                relative = row["prepared_artifact_relative_path"]
                digest = row["prepared_artifact_sha256"]
                if not isinstance(lease_id, str) or not isinstance(relative, str) or not isinstance(digest, str):
                    raise self._fatal("ocr_protocol_violation", "OCR prepared state is incomplete")
                try:
                    valid_path = safe_relative_path(relative) == f"prepared\\ocr\\{lease_id}.json"
                except PathSafetyError as exc:
                    raise self._fatal("ocr_protocol_violation", "OCR prepared state path is invalid") from exc
                if not valid_path:
                    raise self._fatal("ocr_protocol_violation", "OCR prepared state identity is invalid")
                target = self.sidecar_view.layout.ocr_sidecar_path(str(row["relative_image_path"]))
                prepared = self.sidecar_view.layout.resolve_prepared(relative)
                if target.is_file():
                    if sha256_file(target) != digest:
                        raise self._fatal("ocr_protocol_violation", "OCR committed sidecar digest is invalid")
                    payload = target.read_bytes()
                    if prepared.is_file() and sha256_file(prepared) == digest:
                        prepared.unlink()
                else:
                    if not prepared.is_file() or sha256_file(prepared) != digest:
                        raise self._fatal("ocr_protocol_violation", "OCR prepared sidecar digest is invalid")
                    payload = prepared.read_bytes()
                    committed = self.sidecar_view.layout.commit_ocr_prepared(
                        relative, digest, str(row["relative_image_path"]),
                    )
                    if sha256_file(committed) != digest:
                        raise self._fatal("ocr_protocol_violation", "OCR prepared sidecar commit digest is invalid")
                try:
                    sidecar = parse_ocr_sidecar(
                        payload, expected_relative_image_path=str(row["relative_image_path"]),
                    )
                except OcrSidecarError as exc:
                    raise self._fatal("ocr_protocol_violation", "OCR prepared sidecar is invalid") from exc
                if sidecar.image.sizeBytes != int(row["image_size"]):
                    raise self._fatal("ocr_protocol_violation", "OCR prepared sidecar image identity is invalid")
                lease = WorkLease(
                    jobId=self.job_id,
                    moduleId="ocr",
                    sampleId=int(row["sample_id"]),
                    status="leased",
                    attempt=int(row["attempt"]),
                    configHash=str(self.database.get_job(self.job_id)["config_hash"]),
                    leaseId=lease_id,
                    workerInstanceId=str(row["worker_instance_id"]),
                    leaseExpiresAt=str(row["lease_expires_at"]),
                )
                if sidecar.status == "failed":
                    self._issue(lease, row, sidecar)
                else:
                    self.scheduler.complete(lease)
            cursor = int(page[-1]["sample_id"])

    def _process_with_worker(
        self,
        lease: WorkLease,
        row: object,
        item: OcrWorkItemV1,
        *,
        hello: OcrHelloRequestV1,
        config_hash: str,
        threshold: float,
    ) -> OcrSidecar:
        self.process_requests += 1
        try:
            request = OcrProcessRequestV1.from_dict(
                {"schemaVersion": 1, "payloadType": "ocr_process_request", "items": [item.to_dict()]}
            )
            outcomes = parse_ocr_process_result(
                self._exchange("process_batch", request.to_dict(), config_hash)
            )
            if len(outcomes) != 1:
                raise OcrProtocolError("OCR worker returned an unexpected number of outcomes")
            outcome = outcomes[0]
            validate_outcome_for_item(outcome, item)
            return _sidecar_from_outcome(
                outcome,
                threshold=threshold,
                resource_fingerprint=self.resource_fingerprint,
                inference_settings=self.inference_settings,
            )
        except OcrProtocolError as exc:
            raise self._fatal("ocr_protocol_violation", str(exc)) from exc

    def run(self) -> OcrRunReport:
        if self._started:
            raise RuntimeError("an OCR runner instance can execute only once")
        self._started = True
        active: list[WorkLease] = []
        initialized = False
        try:
            config_hash, ocr, threshold, schema_version = self._config()
            if type(ocr.get("enabled")) is not bool:
                raise self._fatal("ocr_protocol_violation", "frozen OCR enabled flag is invalid")
            if type(ocr.get("forceReprocess")) is not bool:
                raise self._fatal("ocr_protocol_violation", "frozen OCR reprocess flag is invalid")
            if ocr.get("enabled") is not True:
                total = int(self.database.module_summary(self.job_id, "ocr")["total"])
                self.database.set_module_summary(self.job_id, "ocr", status="skipped", skipped=total, finished=True)
                return self._report("skipped")
            if (
                ocr.get("resourceId") != OCR_RESOURCE_ID
                or ocr.get("resourceManifestRelativePath") != self.resource_manifest_relative_path
                or ocr.get("resourceFingerprint") != self.resource_fingerprint
            ):
                raise self._fatal("ocr_resource_invalid", "frozen OCR resource no longer matches")
            force_reprocess = ocr.get("forceReprocess") is True
            self._recover_prepared_sidecars()
            hello: OcrHelloRequestV1
            if job_config_supports_ocr_device(schema_version):
                if self.binding_path and self.binding_path.exists():
                    self._v7_binding(ocr=ocr, hello_result=None)
                else:
                    request = read_execution_request(
                        self.binding_path.with_name("ocr-execution-request-v1.json")
                    ) if self.binding_path is not None else None
                    if request is None:
                        raise self._fatal("ocr_protocol_violation", "OCR execution request is unavailable")
                    selected_device = "cuda" if self.runtime_id == "ocr-paddle-gpu" else "cpu"
                    recommendation = recommend_tuning(
                        device=selected_device,
                        total_vram_bytes=self.total_vram_bytes,
                    )
                    self._selection_recommendation = recommendation
                    effective_limit = (
                        request.textDetLimitSideLen.value
                        if request.textDetLimitSideLen.mode == "manual"
                        else recommendation.textDetLimitSideLen
                    )
                    effective_batch = (
                        request.textBatchSize.value
                        if request.textBatchSize.mode == "manual"
                        else recommendation.textBatchSize
                    )
                    assert effective_limit is not None and effective_batch is not None
                    self.inference_settings["textDetLimitSideLen"] = effective_limit
                    self._execution_tuning = (effective_limit, effective_batch)
                requested_device = ocr.get("device")
                if requested_device not in {"auto", "cuda", "cpu"}:
                    raise self._fatal("ocr_protocol_violation", "frozen OCR device is invalid")
                hello = self._hello_request(
                    config_hash,
                    requested_device=requested_device,
                    execution_tuning=self._execution_tuning,
                )
                hello_result = self._initialize_worker(hello, config_hash)
                self._v7_binding(ocr=ocr, hello_result=hello_result)
                initialized = True
            else:
                hello = self._hello_request(config_hash)
            self._publish("running")
            while True:
                job = self.database.get_job(self.job_id)
                if job["status"] in {"cancelling", "paused"}:
                    return self._report(str(job["status"]))
                if job["status"] != "running" or job["current_module_id"] != "ocr":
                    raise self._fatal("ocr_protocol_violation", "OCR job state changed")
                active = self.scheduler.claim_batch(self.job_id, "ocr", self.worker_instance_id, config_hash, limit=1)
                self.max_resident_leases = max(self.max_resident_leases, len(active))
                if not active:
                    if self.database.count_module_unsettled(self.job_id, "ocr"):
                        raise self._fatal("ocr_protocol_violation", "OCR scheduler has no claimable lease")
                    summary = self.database.module_summary(self.job_id, "ocr")
                    status = self.scheduler.finish_module(self.job_id, "ocr", with_issues=int(summary["issue_count"]) > 0)
                    self._publish(status)
                    return self._report(status)
                for lease in active[:]:
                    row = self.database.get_leased_sample(
                        self.job_id,
                        "ocr",
                        lease.sampleId,
                        lease_id=str(lease.leaseId),
                        worker_instance_id=self.worker_instance_id,
                    )
                    relative_path = str(row["relative_image_path"])
                    image_path = Path(str(self.database.get_job(self.job_id)["dataset_root"])) / Path(relative_path.replace("\\", os.sep))
                    image_size = image_path.stat().st_size
                    image_sha256 = stream_sha256(image_path)
                    if not force_reprocess:
                        reused = self._maybe_reuse(
                            relative_path,
                            image_size=image_size,
                            image_sha256=image_sha256,
                            threshold=threshold,
                        )
                        if reused is not None:
                            self._persist_sidecar(lease, relative_path, reused)
                            self.scheduler.complete(lease)
                            active.remove(lease)
                            self._publish("running", lease.attempt)
                            continue
                    if not initialized:
                        self._initialize_worker(hello, config_hash)
                        initialized = True
                    item = self._work_item(
                        lease,
                        row,
                        image_size=image_size,
                        image_sha256=image_sha256,
                    )
                    sidecar = self._process_with_worker(
                        lease,
                        row,
                        item,
                        hello=hello,
                        config_hash=config_hash,
                        threshold=threshold,
                    )
                    self._persist_sidecar(lease, relative_path, serialize_ocr_sidecar(sidecar))
                    if sidecar.status == "failed":
                        self._issue(lease, row, sidecar)
                    else:
                        self.scheduler.complete(lease)
                    active.remove(lease)
                    self._publish("running", lease.attempt)
        except OcrRunnerFatalError:
            for lease in active:
                self.scheduler.release_unstarted(lease)
            self.database.set_module_summary(self.job_id, "ocr", status="failed", finished=True)
            self.database.set_job_status(self.job_id, "failed", current_module_id="ocr")
            self._publish("failed")
            raise
        except (SchedulerError, ProtocolError, OcrSidecarError, OverlayError) as exc:
            fatal = self._fatal("ocr_protocol_violation", str(exc))
            self.database.set_module_summary(self.job_id, "ocr", status="failed", finished=True)
            self.database.set_job_status(self.job_id, "failed", current_module_id="ocr")
            self._publish("failed")
            raise fatal from exc
