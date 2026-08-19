from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .classify_overlay import (
    ClassifyJsonError,
    ClassifyOverlayWriter,
    compose_classify_json,
    original_count,
    parse_annotation_json,
    serialize_annotation_json,
)
from .classify_protocol import (
    ClassifyHelloRequestV1,
    ClassifyHelloResultV1,
    ClassifyIssueResultV1,
    ClassifyProtocolError,
    ClassifyResultV1,
    ClassifyWorkItemV1,
    parse_classify_outcome,
    validate_outcome_for_item,
)
from .classify_resource import ClassifyResourceError, load_classify_resource_from_install
from .contracts import (
    ProgressEvent,
    SampleIssue,
    WorkLease,
    job_config_supports_caption_input_txt_mode,
    sha256_json,
)
from .db import StateDatabase
from .overlay import WorkingAnnotationView
from .raw_e621 import RawE621Annotation, RawE621JsonError
from .scheduler import BoundedScheduler, SchedulerError
from .worker_protocol import ProtocolEnvelopeV1, ProtocolError


CLASSIFY_RUNTIME_ID = "classify-e621"
CLASSIFY_OWNER = "classify"
CLASSIFY_FATAL_WORKER_CODES = frozenset({"classify_resource_invalid", "classify_protocol_violation"})
MAX_INPUT_NL_BYTES = 16_384


class ClassifyTransport(Protocol):
    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1: ...


RawE621Reader = Callable[[str], RawE621Annotation | None]


class ClassifyRunnerFatalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def stable_classify_issue_id(job_id: str, sample_id: int, code: str) -> str:
    return hashlib.sha256(f"{job_id}\0{sample_id}\0classify\0{code}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClassifyRunReport:
    status: str
    total: int
    completed: int
    failed: int
    skipped: int
    issueCount: int
    maxResidentLeases: int
    helloRequests: int
    processRequests: int


class ClassifyRunner:
    """Bounded core-owned classify orchestration; worker input/output stays per-sample."""

    def __init__(
        self,
        database: StateDatabase,
        scheduler: BoundedScheduler,
        transport: ClassifyTransport,
        annotation_view: WorkingAnnotationView,
        overlay_writer: ClassifyOverlayWriter,
        *,
        job_id: str,
        worker_instance_id: str,
        install_root: Path,
        resource_manifest_relative_path: str,
        resource_fingerprint: str,
        progress_consumer: Callable[[ProgressEvent], None] | None = None,
        raw_e621_reader: RawE621Reader | None = None,
        clock: Callable[[], float] = time.monotonic,
        message_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not worker_instance_id or len(worker_instance_id) > 128:
            raise ValueError("classify worker instance id must be a non-empty bounded string")
        self.database = database
        self.scheduler = scheduler
        self.transport = transport
        self.annotation_view = annotation_view
        self.overlay_writer = overlay_writer
        self.job_id = job_id
        self.worker_instance_id = worker_instance_id
        self.install_root = install_root
        self.resource_manifest_relative_path = resource_manifest_relative_path
        self.resource_fingerprint = resource_fingerprint
        self.progress_consumer = progress_consumer
        self.raw_e621_reader = raw_e621_reader
        self.clock = clock
        self.message_id_factory = message_id_factory or (lambda: f"classify-{uuid.uuid4().hex}")
        self.max_resident_leases = 0
        self.hello_requests = 0
        self.process_requests = 0
        self._started = False
        self._expected_entry_count: int | None = None
        self._expected_wiki_data_source_id: str | None = None

    def _fatal(self, code: str, message: str) -> ClassifyRunnerFatalError:
        return ClassifyRunnerFatalError(code, message)

    def _hello(self) -> tuple[ClassifyHelloRequestV1, str, bool, str | None]:
        job = self.database.get_job(self.job_id)
        try:
            config = json.loads(str(job["config_json"]))
        except json.JSONDecodeError as exc:
            raise self._fatal("classify_protocol_violation", "frozen JobConfig is invalid JSON") from exc
        if not isinstance(config, dict) or sha256_json(config) != job["config_hash"]:
            raise self._fatal("classify_protocol_violation", "frozen JobConfig does not match configHash")
        classify = config.get("classify")
        caption = config.get("caption")
        config_schema_version = config.get("schemaVersion")
        required_classify = {
            "enabled", "indexMode", "overwriteJson", "overwriteCount", "wikiDataSourceId",
            "dictionaryEntryCount", "resourceProfile", "resourceId",
            "resourceManifestRelativePath", "resourceFingerprint",
        }
        allowed_classify = required_classify | {
            "customResourcePath", "customResourceContentSha256",
        }
        if (
            config_schema_version != 9
            or "profile" in config
            or config_schema_version != int(job["config_schema_version"])
            or not isinstance(classify, dict) or not required_classify.issubset(classify)
            or set(classify) - allowed_classify
            or classify.get("enabled") is not True or type(classify.get("overwriteJson")) is not bool
            or type(classify.get("overwriteCount")) is not bool
            or not isinstance(classify.get("wikiDataSourceId"), str) or not classify.get("wikiDataSourceId")
            or config.get("overwriteMode") not in {"incremental", "rebuild"} or config.get("overwriteMode") != job["overwrite_mode"]
        ):
            raise self._fatal("classify_protocol_violation", "frozen classify configuration is invalid")
        input_txt_mode: str | None = None
        if job_config_supports_caption_input_txt_mode(config_schema_version):
            if (
                not isinstance(caption, dict)
                or caption.get("inputTxtMode") not in {"tag", "nl"}
                or type(caption.get("taggerFallbackOnMissingTxt")) is not bool
            ):
                raise self._fatal("classify_protocol_violation", "frozen caption TXT input mode is invalid")
            input_txt_mode = caption["inputTxtMode"]
        try:
            manifest, _ = load_classify_resource_from_install(
                self.install_root,
                self.resource_manifest_relative_path,
                self.resource_fingerprint,
            )
        except ClassifyResourceError as exc:
            raise self._fatal("classify_resource_invalid", f"core resource validation failed: {exc}") from exc
        profile = manifest.profile
        if (
            classify.get("resourceId") != manifest.resourceId
            or classify.get("resourceManifestRelativePath") != self.resource_manifest_relative_path
            or classify.get("resourceFingerprint") != self.resource_fingerprint
            or classify.get("resourceProfile") != profile
            or classify.get("dictionaryEntryCount") != manifest.dictionaryEntryCount
            or manifest.wikiDataSourceId != classify["wikiDataSourceId"]
        ):
            raise self._fatal("classify_resource_invalid", "resource Wiki data source does not match JobConfig")
        self._expected_entry_count = manifest.dictionaryEntryCount
        self._expected_wiki_data_source_id = manifest.wikiDataSourceId
        try:
            hello = ClassifyHelloRequestV1.from_dict({
                "schemaVersion": 1, "payloadType": "classify_hello_request", "jobId": self.job_id,
                "configHash": str(job["config_hash"]), "profile": profile,
                "resourceManifestRelativePath": self.resource_manifest_relative_path,
                "resourceFingerprint": self.resource_fingerprint,
                "wikiDataSourceId": classify["wikiDataSourceId"], "overwriteCount": classify["overwriteCount"],
                "captionFormat": config.get("captionFormat"),
            })
        except ClassifyProtocolError as exc:
            raise self._fatal("classify_protocol_violation", f"classify hello is invalid: {exc}") from exc
        return hello, str(config["overwriteMode"]), bool(classify["overwriteJson"]), input_txt_mode

    def _exchange(self, method: str, payload: dict[str, object]) -> ProtocolEnvelopeV1:
        job = self.database.get_job(self.job_id)
        request = ProtocolEnvelopeV1(
            protocolVersion="1.0", kind="request", messageId=self.message_id_factory(), runtimeId=CLASSIFY_RUNTIME_ID,
            owner=CLASSIFY_OWNER, method=method, payload=payload, jobId=self.job_id, configHash=str(job["config_hash"]),
        )
        try:
            request = ProtocolEnvelopeV1.from_dict(request.to_dict(), runtime_id=CLASSIFY_RUNTIME_ID, owner=CLASSIFY_OWNER)
        except ProtocolError as exc:
            raise self._fatal("classify_protocol_violation", str(exc)) from exc
        response = self.transport.exchange(request)
        if not isinstance(response, ProtocolEnvelopeV1) or (
            response.protocolVersion != "1.0" or response.kind != "response" or response.replyTo != request.messageId
            or response.runtimeId != CLASSIFY_RUNTIME_ID or response.owner != CLASSIFY_OWNER
            or response.jobId != self.job_id or response.configHash != job["config_hash"]
        ):
            raise self._fatal("classify_protocol_violation", "classify response envelope identity mismatch")
        if response.method == "error":
            code = response.payload.get("code")
            raise self._fatal(code if code in CLASSIFY_FATAL_WORKER_CODES else "classify_protocol_violation", "classify worker returned fatal error")
        if response.method != ("hello" if method == "hello" else "result"):
            raise self._fatal("classify_protocol_violation", "classify response method mismatch")
        return response

    def _initialize(self, hello: ClassifyHelloRequestV1) -> None:
        self.hello_requests += 1
        try:
            result = ClassifyHelloResultV1.from_dict(self._exchange("hello", hello.to_dict()).payload)
        except ClassifyProtocolError as exc:
            raise self._fatal("classify_protocol_violation", f"classify hello result is invalid: {exc}") from exc
        if (
            result.resourceFingerprint != hello.resourceFingerprint
            or result.entryCount != self._expected_entry_count
            or result.wikiDataSourceId != self._expected_wiki_data_source_id
        ):
            raise self._fatal("classify_resource_invalid", "classify resource fingerprint changed after launch")

    def _issue(self, lease: WorkLease, row: object, code: str, message: str, *, retriable: bool) -> None:
        self.scheduler.fail_with_issue(lease, SampleIssue(
            issueId=stable_classify_issue_id(self.job_id, lease.sampleId, code), jobId=self.job_id, sampleId=lease.sampleId,
            relativeImagePath=str(row["relative_image_path"]), moduleId="classify", code=code, severity="error",
            blocking=True, retriable=retriable, message=message[:1024], attempt=lease.attempt,
            repairStartModule="classify" if retriable else None,
        ))

    def _record_count_diagnostics(self, result: ClassifyResultV1) -> None:
        """Persist the worker's non-blocking count findings; warnings keep only their code prefix."""
        decision = result.countDecision
        codes = dict.fromkeys((
            *decision.issueCodes,
            *(warning.split(":", 1)[0] for warning in decision.warnings),
        ))
        for code in codes:
            self.database.increment_module_diagnostic(self.job_id, "classify", code, severity="warning", amount=1)

    def _baseline_input_nl(self, annotation_key: str) -> str:
        raw = self.annotation_view.baseline.read(annotation_key, ".txt")
        if raw is None:
            return ""
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClassifyJsonError("input TXT is not strict UTF-8") from exc
        if "\x00" in value:
            raise ClassifyJsonError("input TXT contains NUL")
        if len(value.encode("utf-8")) > MAX_INPUT_NL_BYTES:
            raise ClassifyJsonError("input TXT exceeds 16,384 UTF-8 bytes")
        return value if value.strip() else ""

    def _item(
        self,
        lease: WorkLease,
        row: object,
        input_txt_mode: str | None,
    ) -> tuple[ClassifyWorkItemV1, dict[str, object] | None, RawE621Annotation | None, str | None]:
        try:
            annotation_key = str(row["annotation_key"])
            raw_e621 = (
                self.raw_e621_reader(annotation_key)
                if self.raw_e621_reader is not None and row["source"] == "e621"
                else None
            )
            if raw_e621 is not None:
                return ClassifyWorkItemV1.from_dict({
                    "schemaVersion": 1, "sampleId": lease.sampleId, "leaseId": lease.leaseId, "source": row["source"],
                    "relativeImagePath": row["relative_image_path"], "annotationKey": annotation_key,
                    "txtText": ", ".join(raw_e621.classify_tags), "txtProvenance": "original_preserved", "originalCount": None,
                }), None, raw_e621, None
            raw_txt = self.annotation_view.read(str(row["annotation_key"]), ".txt")
            if raw_txt is None:
                raise ClassifyJsonError("caption TXT is missing or blank")
            txt = raw_txt.decode("utf-8-sig")
            if not txt.strip():
                raise ClassifyJsonError("caption TXT is missing or blank")
            existing = parse_annotation_json(self.annotation_view.read(str(row["annotation_key"]), ".json"))
            nl_override = self._baseline_input_nl(annotation_key) if input_txt_mode == "nl" else None
            return ClassifyWorkItemV1.from_dict({
                "schemaVersion": 1, "sampleId": lease.sampleId, "leaseId": lease.leaseId, "source": row["source"],
                "relativeImagePath": row["relative_image_path"], "annotationKey": row["annotation_key"], "txtText": txt,
                "txtProvenance": row["txt_provenance"], "originalCount": original_count(existing),
            }), existing, None, nl_override
        except UnicodeDecodeError as exc:
            raise ClassifyJsonError("caption TXT is not strict UTF-8") from exc

    def _publish(self, status: str, attempt: int = 0) -> None:
        summary = self.database.module_summary(self.job_id, "classify")
        settled = int(summary["completed"] + summary["failed"] + summary["skipped"])
        event = ProgressEvent(self.job_id, int(self.database.get_job(self.job_id)["last_event_id"]) + 1, "classify", status, settled, int(summary["total"]), str(self.database.get_job(self.job_id)["config_hash"]), attempt)
        self.database.append_event(event)
        if self.progress_consumer is not None:
            self.progress_consumer(event)

    def _report(self, status: str) -> ClassifyRunReport:
        summary = self.database.module_summary(self.job_id, "classify")
        return ClassifyRunReport(status, int(summary["total"]), int(summary["completed"]), int(summary["failed"]), int(summary["skipped"]), int(summary["issue_count"]), self.max_resident_leases, self.hello_requests, self.process_requests)

    def run(self) -> ClassifyRunReport:
        if self._started:
            raise RuntimeError("a classify runner instance can execute only once")
        self._started = True
        active: list[WorkLease] = []
        initialized = False
        try:
            hello, overwrite_mode, overwrite_json, input_txt_mode = self._hello()
            self._publish("running")
            while True:
                job = self.database.get_job(self.job_id)
                if job["status"] in {"cancelling", "paused"}:
                    return self._report(str(job["status"]))
                if job["status"] != "running" or job["current_module_id"] != "classify":
                    raise self._fatal("classify_protocol_violation", "classify job state changed")
                active = self.scheduler.claim_batch(self.job_id, "classify", self.worker_instance_id, hello.configHash)
                self.max_resident_leases = max(self.max_resident_leases, len(active))
                if not active:
                    if self.database.count_module_unsettled(self.job_id, "classify"):
                        raise self._fatal("classify_protocol_violation", "classify scheduler has no claimable lease")
                    summary = self.database.module_summary(self.job_id, "classify")
                    status = self.scheduler.finish_module(self.job_id, "classify", with_issues=int(summary["issue_count"]) > 0)
                    self._publish(status)
                    return self._report(status)
                for lease in active[:]:
                    job = self.database.get_job(self.job_id)
                    if job["status"] in {"cancelling", "paused"}:
                        for pending in active:
                            self.scheduler.release_unstarted(pending)
                        active = []
                        return self._report(str(job["status"]))
                    self.scheduler.heartbeat(
                        self.job_id,
                        self.worker_instance_id,
                        [str(pending.leaseId) for pending in active if pending.leaseId],
                    )
                    row = self.database.get_leased_sample(self.job_id, "classify", lease.sampleId, lease_id=str(lease.leaseId), worker_instance_id=self.worker_instance_id)
                    try:
                        item, existing, raw_e621, nl_override = self._item(lease, row, input_txt_mode)
                    except (ClassifyJsonError, ClassifyProtocolError, RawE621JsonError) as exc:
                        self._issue(lease, row, "classify_input_invalid", str(exc), retriable=False)
                        active.remove(lease)
                        self._publish("running", lease.attempt)
                        continue
                    if not initialized:
                        self._initialize(hello)
                        initialized = True
                    self.process_requests += 1
                    try:
                        outcome = parse_classify_outcome(self._exchange("process_batch", {"schemaVersion": 1, "payloadType": "classify_process_request", "items": [item.to_dict()]}).payload)
                        validate_outcome_for_item(outcome, item)
                    except ClassifyProtocolError as exc:
                        raise self._fatal("classify_protocol_violation", str(exc)) from exc
                    if isinstance(outcome, ClassifyIssueResultV1):
                        self._issue(lease, row, outcome.code, outcome.message, retriable=outcome.retriable)
                    else:
                        try:
                            self.overlay_writer.write(
                                item,
                                serialize_annotation_json(compose_classify_json(
                                    existing,
                                    outcome,
                                    overwrite_mode=overwrite_mode,
                                    overwrite_json=overwrite_json,
                                    raw_e621=raw_e621,
                                    nl_override=nl_override,
                                )),
                                count_decision=outcome.countDecision.to_dict(),
                            )
                            self.scheduler.complete(lease)
                        except Exception as exc:
                            raise self._fatal("classify_result_persistence_failed", "classify overlay persistence failed") from exc
                        self._record_count_diagnostics(outcome)
                    active.remove(lease)
                    self._publish("running", lease.attempt)
        except ClassifyRunnerFatalError:
            for lease in active:
                self.scheduler.release_unstarted(lease)
            self.database.set_module_summary(self.job_id, "classify", status="failed", finished=True)
            self.database.set_job_status(self.job_id, "failed", current_module_id="classify")
            self._publish("failed")
            raise
        except (SchedulerError, ProtocolError) as exc:
            raise self._fatal("classify_protocol_violation", str(exc)) from exc
