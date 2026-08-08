from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Container, Iterable, Protocol

from .caption_protocol import (
    CaptionFormatPolicyV1,
    CaptionHelloRequestV1,
    CaptionHelloResultV1,
    CaptionProtocolError,
    CaptionResultV1,
    CaptionThresholdPolicyV1,
    CaptionWorkItemV1,
    ImageDecodePolicyV1,
    parse_caption_outcome,
    validate_outcome_for_item,
)
from .contracts import (
    ProgressEvent,
    SampleIssue,
    WorkLease,
    job_config_supports_caption_input_txt_mode,
    sha256_json,
)
from .db import StateDatabase
from .path_safety import PathSafetyError, ensure_within, safe_relative_path
from .raw_e621 import RawE621Annotation, RawE621JsonError
from .scheduler import BoundedScheduler, SchedulerError
from .worker_protocol import ProtocolEnvelopeV1, ProtocolError


CAPTION_RUNTIME_ID = "caption-e621"
CAPTION_OWNER = "caption"
CLASSIFY_RESOURCE_MANIFEST_RELATIVE_PATH = (
    "classification-indexes\\e621-classify-20260724-v1\\resource.json"
)
CAPTION_PROGRESS_INTERVAL_SECONDS = 0.25
CAPTION_FATAL_WORKER_CODES = frozenset(
    {
        "caption_resource_invalid",
        "caption_metadata_mismatch",
        "caption_model_load_failed",
        "caption_profile_mismatch",
        "caption_protocol_violation",
        "caption_source_fingerprint_mismatch",
    }
)


class CaptionTransport(Protocol):
    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1: ...


CaptionResultConsumer = Callable[[CaptionWorkItemV1, CaptionResultV1], None]
CaptionProgressConsumer = Callable[[ProgressEvent], None]
RawE621Reader = Callable[[str], RawE621Annotation | None]


class CaptionRunnerFatalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CaptionRunReport:
    status: str
    total: int
    completed: int
    failed: int
    skipped: int
    issueCount: int
    maxResidentLeases: int
    helloRequests: int
    processRequests: int


@dataclass(frozen=True)
class _CaptionExecutionPolicy:
    overwrite_mode: str
    overwrite_txt: bool
    input_txt_mode: str | None
    tagger_fallback_on_missing_txt: bool | None


def stable_caption_issue_id(job_id: str, sample_id: int, code: str) -> str:
    value = f"{job_id}\0{sample_id}\0caption\0{code}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def missing_dictionary_tags(tag_names: Iterable[str], dictionary_entries: Container[str]) -> tuple[str, ...]:
    """Tagger labels the E621 classification dictionary cannot resolve at all."""
    return tuple(name for name in tag_names if name not in dictionary_entries)


def _resource_entrypoint(
    install_root: Path,
    manifest_relative_path: str,
    owner: str,
    role: str,
    legacy_name: str,
) -> Path:
    try:
        relative = Path(safe_relative_path(manifest_relative_path).replace("\\", os.sep))
        manifest = ensure_within(install_root, install_root / relative)
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schemaVersion") == 1 and isinstance(value.get("entrypoints"), dict):
            entrypoint = value["entrypoints"].get(role)
            if not isinstance(entrypoint, str):
                raise ValueError(f"{role} entrypoint is missing")
            package_root = ensure_within(install_root, manifest.parent)
            return ensure_within(
                package_root,
                package_root / Path(safe_relative_path(entrypoint).replace("\\", os.sep)),
            )
        root_relative = value.get("rootRelativePath") if isinstance(value, dict) else None
        if not isinstance(root_relative, str):
            raise ValueError("rootRelativePath is missing")
        legacy_root = ensure_within(
            install_root,
            install_root / Path(safe_relative_path(root_relative).replace("\\", os.sep)),
        )
        return ensure_within(legacy_root, legacy_root / legacy_name)
    except (PathSafetyError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CaptionRunnerFatalError(
            "caption_resource_invalid",
            f"{owner} resource manifest is unusable: {exc}",
        ) from exc


def _resource_json(path: Path, field: str, owner: str) -> object:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaptionRunnerFatalError("caption_resource_invalid", f"{owner} {path.name} is unreadable") from exc
    member = value.get(field) if isinstance(value, dict) else None
    if not isinstance(member, (list, dict)) or not member:
        raise CaptionRunnerFatalError("caption_resource_invalid", f"{owner} {path.name} has no {field}")
    return member


def verify_tagger_dictionary_coverage(
    install_root: str | Path,
    caption_manifest_relative_path: str,
    classify_manifest_relative_path: str = CLASSIFY_RESOURCE_MANIFEST_RELATIVE_PATH,
) -> None:
    """Fail the batch when a shipped tagger label has no classification entry."""
    root = Path(install_root)
    tags_path = _resource_entrypoint(root, caption_manifest_relative_path, "caption", "tags", "tags.json")
    dictionary_path = _resource_entrypoint(
        root, classify_manifest_relative_path, "classify", "dictionary", "e621_tag_dictionary.json"
    )
    tag_names = _resource_json(tags_path, "tag_names", "caption")
    entries = _resource_json(dictionary_path, "entries", "classify")
    missing = missing_dictionary_tags([str(name) for name in tag_names], entries)  # type: ignore[arg-type]
    if missing:
        raise CaptionRunnerFatalError(
            "caption_resource_invalid",
            f"{len(missing)} tagger labels are absent from the E621 classification dictionary: "
            + ", ".join(missing[:16]),
        )


class _CaptionProgressPublisher:
    def __init__(
        self,
        database: StateDatabase,
        job_id: str,
        config_hash: str,
        *,
        consumer: CaptionProgressConsumer | None,
        clock: Callable[[], float],
    ) -> None:
        self.database = database
        self.job_id = job_id
        self.config_hash = config_hash
        self.consumer = consumer
        self.clock = clock
        self.last_published_at: float | None = None
        self.last_signature: tuple[object, ...] | None = None

    def publish(self, status: str, *, attempt: int = 0, force: bool = False) -> ProgressEvent | None:
        summary = self.database.module_summary(self.job_id, "caption")
        completed = int(summary["completed"])
        failed = int(summary["failed"])
        skipped = int(summary["skipped"])
        total = int(summary["total"])
        settled = completed + failed + skipped
        if settled > total:
            raise CaptionRunnerFatalError(
                "caption_protocol_violation",
                "caption module counts exceed the immutable manifest total",
            )
        signature = (status, settled, total, failed, skipped)
        if signature == self.last_signature:
            return None
        now = self.clock()
        if (
            not force
            and self.last_published_at is not None
            and now - self.last_published_at < CAPTION_PROGRESS_INTERVAL_SECONDS
        ):
            return None
        job = self.database.get_job(self.job_id)
        event = ProgressEvent(
            jobId=self.job_id,
            eventId=int(job["last_event_id"]) + 1,
            moduleId="caption",
            status=status,  # type: ignore[arg-type]
            completed=settled,
            total=total,
            configHash=self.config_hash,
            attempt=attempt,
        )
        self.database.append_event(event)
        self.last_published_at = now
        self.last_signature = signature
        if self.consumer is not None:
            self.consumer(event)
        return event


class CaptionRunner:
    """Bounded core-owned orchestration for one enabled caption module.

    ``result_consumer`` must durably handle exactly one validated result before
    returning. The runner never accumulates caption text or structured tags.
    """

    def __init__(
        self,
        database: StateDatabase,
        scheduler: BoundedScheduler,
        transport: CaptionTransport,
        *,
        job_id: str,
        worker_instance_id: str,
        resource_manifest_relative_path: str,
        resource_fingerprint: str,
        result_consumer: CaptionResultConsumer,
        classify_resource_manifest_relative_path: str = CLASSIFY_RESOURCE_MANIFEST_RELATIVE_PATH,
        progress_consumer: CaptionProgressConsumer | None = None,
        install_root: str | Path | None = None,
        raw_e621_reader: RawE621Reader | None = None,
        clock: Callable[[], float] = time.monotonic,
        message_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not worker_instance_id or len(worker_instance_id) > 128:
            raise ValueError("caption worker instance id must be a non-empty bounded string")
        self.database = database
        self.scheduler = scheduler
        self.transport = transport
        self.job_id = job_id
        self.worker_instance_id = worker_instance_id
        self.resource_manifest_relative_path = resource_manifest_relative_path
        self.resource_fingerprint = resource_fingerprint
        self.classify_resource_manifest_relative_path = classify_resource_manifest_relative_path
        self.result_consumer = result_consumer
        self.progress_consumer = progress_consumer
        self.install_root = install_root
        self.raw_e621_reader = raw_e621_reader
        self.clock = clock
        self.message_id_factory = message_id_factory or (lambda: f"caption-{uuid.uuid4().hex}")
        self.max_resident_leases = 0
        self.hello_requests = 0
        self.process_requests = 0
        self._started = False
        self._hello_result: CaptionHelloResultV1 | None = None

    def _build_hello(self) -> tuple[CaptionHelloRequestV1, _CaptionExecutionPolicy]:
        job = self.database.get_job(self.job_id)
        profile = job["profile"]
        if profile not in {"e621", "danbooru"}:
            raise CaptionRunnerFatalError("caption_profile_mismatch", "caption runner profile is unsupported")
        try:
            config = json.loads(str(job["config_json"]))
        except json.JSONDecodeError as exc:
            raise CaptionRunnerFatalError("caption_protocol_violation", "frozen JobConfig is not valid JSON") from exc
        if not isinstance(config, dict) or sha256_json(config) != job["config_hash"]:
            raise CaptionRunnerFatalError(
                "caption_protocol_violation",
                "frozen JobConfig does not match its persisted configHash",
            )
        config_schema_version = config.get("schemaVersion")
        supported_versions = {2, 3, 4, 8} if profile == "e621" else {4, 8}
        if (
            config.get("profile") != profile
            or type(config_schema_version) is not int
            or config_schema_version not in supported_versions
            or config_schema_version != int(job["config_schema_version"])
        ):
            raise CaptionRunnerFatalError(
                "caption_profile_mismatch",
                "frozen JobConfig profile or schema version is unsupported",
            )
        caption = config.get("caption")
        if not isinstance(caption, dict) or caption.get("enabled") is not True:
            raise CaptionRunnerFatalError("caption_protocol_violation", "caption runner cannot execute a disabled module")
        image_decode = config.get("imageDecode")
        if not isinstance(image_decode, dict):
            raise CaptionRunnerFatalError("caption_protocol_violation", "frozen image decode policy is invalid")
        overwrite_mode = config.get("overwriteMode")
        overwrite_txt = caption.get("overwriteTxt")
        if (
            overwrite_mode not in {"incremental", "rebuild"}
            or overwrite_mode != job["overwrite_mode"]
            or type(overwrite_txt) is not bool
        ):
            raise CaptionRunnerFatalError("caption_protocol_violation", "frozen caption overwrite policy is invalid")
        input_txt_mode: str | None = None
        tagger_fallback_on_missing_txt: bool | None = None
        if job_config_supports_caption_input_txt_mode(config_schema_version):
            input_txt_mode = caption.get("inputTxtMode")
            tagger_fallback_on_missing_txt = caption.get("taggerFallbackOnMissingTxt")
            if input_txt_mode not in {"tag", "nl"} or type(tagger_fallback_on_missing_txt) is not bool:
                raise CaptionRunnerFatalError("caption_protocol_violation", "frozen caption TXT input mode is invalid")
        threshold_value: dict[str, object] = {"mode": caption.get("thresholdMode")}
        if "uniformThreshold" in caption:
            threshold_value["uniformThreshold"] = caption["uniformThreshold"]
        if "categoryThresholds" in caption:
            threshold_value["categoryThresholds"] = caption["categoryThresholds"]
        try:
            value = {
                "schemaVersion": 1,
                "payloadType": "caption_hello_request",
                "jobId": self.job_id,
                "configHash": str(job["config_hash"]),
                "profile": profile,
                "datasetRoot": str(job["dataset_root"]),
                "resourceManifestRelativePath": self.resource_manifest_relative_path,
                "resourceFingerprint": self.resource_fingerprint,
                "thresholdPolicy": CaptionThresholdPolicyV1.from_dict(threshold_value).to_dict(),
                "captionFormat": CaptionFormatPolicyV1.from_dict(config.get("captionFormat")).to_dict(),
                "imageDecode": ImageDecodePolicyV1.from_dict({
                    key: image_decode.get(key)
                    for key in ("extensions", "rejectMultiFrame", "applyExifTranspose", "alphaBackground")
                }).to_dict(),
            }
            return (
                CaptionHelloRequestV1.from_dict(value),
                _CaptionExecutionPolicy(
                    str(overwrite_mode),
                    overwrite_txt,
                    input_txt_mode,
                    tagger_fallback_on_missing_txt,
                ),
            )
        except CaptionProtocolError as exc:
            raise CaptionRunnerFatalError("caption_protocol_violation", f"frozen caption configuration is invalid: {exc}") from exc

    def _exchange(self, method: str, payload: dict[str, object]) -> ProtocolEnvelopeV1:
        job = self.database.get_job(self.job_id)
        message_id = self.message_id_factory()
        request = ProtocolEnvelopeV1(
            protocolVersion="1.0",
            kind="request",
            messageId=message_id,
            runtimeId=CAPTION_RUNTIME_ID,
            owner=CAPTION_OWNER,
            method=method,
            payload=payload,
            jobId=self.job_id,
            configHash=str(job["config_hash"]),
        )
        try:
            request = ProtocolEnvelopeV1.from_dict(
                request.to_dict(),
                runtime_id=CAPTION_RUNTIME_ID,
                owner=CAPTION_OWNER,
            )
        except ProtocolError as exc:
            raise CaptionRunnerFatalError("caption_protocol_violation", f"caption request identity is invalid: {exc}") from exc
        response = self.transport.exchange(request)
        if not isinstance(response, ProtocolEnvelopeV1):
            raise CaptionRunnerFatalError("caption_protocol_violation", "caption transport returned no protocol envelope")
        if (
            response.protocolVersion != "1.0"
            or response.kind != "response"
            or response.replyTo != request.messageId
            or response.runtimeId != CAPTION_RUNTIME_ID
            or response.owner != CAPTION_OWNER
            or response.jobId != self.job_id
            or response.configHash != job["config_hash"]
        ):
            raise CaptionRunnerFatalError("caption_protocol_violation", "caption response envelope identity mismatch")
        if response.method == "error":
            code = response.payload.get("code")
            if not isinstance(code, str) or code not in CAPTION_FATAL_WORKER_CODES:
                code = "caption_protocol_violation"
            raise CaptionRunnerFatalError(code, f"caption worker returned fatal error: {code}")
        expected_method = "hello" if method == "hello" else "result"
        if response.method != expected_method:
            raise CaptionRunnerFatalError("caption_protocol_violation", "caption response method mismatch")
        return response

    def _initialize_worker(self, hello: CaptionHelloRequestV1) -> None:
        self.hello_requests += 1
        response = self._exchange("hello", hello.to_dict())
        try:
            result = CaptionHelloResultV1.from_dict(response.payload)
        except CaptionProtocolError as exc:
            raise CaptionRunnerFatalError("caption_protocol_violation", f"caption hello result is invalid: {exc}") from exc
        if result.resourceFingerprint != hello.resourceFingerprint:
            raise CaptionRunnerFatalError(
                "caption_resource_invalid",
                "caption hello resource fingerprint does not match the frozen resource",
            )
        self._hello_result = result

    def _leased_row(self, lease: WorkLease) -> sqlite3.Row:
        if not lease.leaseId or not lease.workerInstanceId:
            raise CaptionRunnerFatalError("caption_protocol_violation", "caption lease identity is incomplete")
        try:
            return self.database.get_leased_sample(
                self.job_id,
                "caption",
                lease.sampleId,
                lease_id=lease.leaseId,
                worker_instance_id=lease.workerInstanceId,
            )
        except ValueError as exc:
            raise CaptionRunnerFatalError("caption_protocol_violation", str(exc)) from exc

    @staticmethod
    def _requires_inference(row: sqlite3.Row, policy: _CaptionExecutionPolicy) -> bool:
        original_state = row["original_txt_state"]
        if original_state not in {"missing_or_blank", "nonblank"}:
            raise CaptionRunnerFatalError("caption_protocol_violation", "manifest TXT state is invalid")
        if policy.input_txt_mode == "nl":
            return True
        if policy.input_txt_mode == "tag":
            return original_state == "missing_or_blank" and policy.tagger_fallback_on_missing_txt is True
        return policy.overwrite_mode == "rebuild" or original_state == "missing_or_blank" or policy.overwrite_txt

    def _work_item(self, lease: WorkLease, row: sqlite3.Row) -> CaptionWorkItemV1:
        try:
            value: dict[str, object] = {
                "schemaVersion": 1,
                "sampleId": int(row["sample_id"]),
                "leaseId": str(row["lease_id"]),
                "source": str(row["source"]),
                "relativeImagePath": str(row["relative_image_path"]),
                "annotationKey": str(row["annotation_key"]),
                "imageFormat": str(row["image_format"]),
                "imageFrameCount": int(row["image_frame_count"]),
                "imageSize": row["image_size"],
                "imageMtimeNs": row["image_mtime_ns"],
            }
            if row["image_file_id"] is not None:
                value["imageFileId"] = str(row["image_file_id"])
            return CaptionWorkItemV1.from_dict(value)
        except (CaptionProtocolError, TypeError, ValueError) as exc:
            raise CaptionRunnerFatalError(
                "caption_source_fingerprint_mismatch",
                f"leased sample does not match its immutable manifest metadata: {exc}",
            ) from exc

    def _raw_e621_annotation(self, row: sqlite3.Row) -> RawE621Annotation | None:
        if self.raw_e621_reader is None or row["source"] != "e621":
            return None
        return self.raw_e621_reader(str(row["annotation_key"]))

    def _raw_e621_issue(self, lease: WorkLease, row: sqlite3.Row, error: RawE621JsonError) -> None:
        self.scheduler.fail_with_issue(
            lease,
            SampleIssue(
                issueId=stable_caption_issue_id(self.job_id, lease.sampleId, "e621_raw_json_invalid"),
                jobId=self.job_id,
                sampleId=lease.sampleId,
                relativeImagePath=str(row["relative_image_path"]),
                moduleId="caption",
                code="e621_raw_json_invalid",
                severity="error",
                blocking=True,
                retriable=False,
                message=str(error)[:1024],
                attempt=lease.attempt,
            ),
        )

    def _missing_txt_without_tagger_fallback_issue(self, lease: WorkLease, row: sqlite3.Row) -> None:
        self.scheduler.fail_with_issue(
            lease,
            SampleIssue(
                issueId=stable_caption_issue_id(self.job_id, lease.sampleId, "caption_missing_txt_without_tagger_fallback"),
                jobId=self.job_id,
                sampleId=lease.sampleId,
                relativeImagePath=str(row["relative_image_path"]),
                moduleId="caption",
                code="caption_missing_txt_without_tagger_fallback",
                severity="warning",
                blocking=False,
                retriable=False,
                message="source TXT is missing or blank; correct it and create a new task before running again",
                attempt=lease.attempt,
            ),
        )

    def _process(self, lease: WorkLease, item: CaptionWorkItemV1) -> None:
        self.process_requests += 1
        response = self._exchange(
            "process_batch",
            {"schemaVersion": 1, "payloadType": "caption_process_request", "items": [item.to_dict()]},
        )
        try:
            outcome = parse_caption_outcome(response.payload)
            validate_outcome_for_item(outcome, item)
        except CaptionProtocolError as exc:
            raise CaptionRunnerFatalError("caption_protocol_violation", f"caption outcome is invalid: {exc}") from exc
        if isinstance(outcome, CaptionResultV1):
            if self._hello_result is None or outcome.provider != self._hello_result.provider:
                raise CaptionRunnerFatalError("caption_protocol_violation", "caption result provider changed after hello")
            if len(outcome.tags) > self._hello_result.tagCount:
                raise CaptionRunnerFatalError(
                    "caption_protocol_violation",
                    "caption result contains more tags than the initialized model",
                )
            try:
                self.result_consumer(item, outcome)
            except Exception as exc:
                raise CaptionRunnerFatalError(
                    "caption_result_persistence_failed",
                    "caption result consumer failed before lease completion",
                ) from exc
            self.scheduler.complete(lease, txt_provenance="module1_written")
            return
        self.scheduler.fail_with_issue(
            lease,
            SampleIssue(
                issueId=stable_caption_issue_id(self.job_id, item.sampleId, outcome.code),
                jobId=self.job_id,
                sampleId=item.sampleId,
                relativeImagePath=item.relativeImagePath,
                moduleId="caption",
                code=outcome.code,
                severity="error",
                blocking=True,
                retriable=outcome.retriable,
                message=outcome.message,
                attempt=lease.attempt,
                repairStartModule=outcome.repairStartModule,
            ),
        )

    def _release(self, leases: list[WorkLease]) -> None:
        for lease in leases:
            state = self.database.get_sample_state(lease.jobId, lease.sampleId)
            if state["status"] == "leased" and state["lease_id"] == lease.leaseId:
                self.scheduler.release_unstarted(lease)

    def _fail_module(self, active: list[WorkLease], publisher: _CaptionProgressPublisher) -> None:
        self._release(active)
        summary = self.database.module_summary(self.job_id, "caption")
        if summary["status"] == "running":
            self.database.set_module_summary(self.job_id, "caption", status="failed", finished=True)
        self.database.set_job_status(self.job_id, "failed", current_module_id="caption")
        publisher.publish("failed", force=True)

    def _report(self, status: str) -> CaptionRunReport:
        summary = self.database.module_summary(self.job_id, "caption")
        return CaptionRunReport(
            status=status,
            total=int(summary["total"]),
            completed=int(summary["completed"]),
            failed=int(summary["failed"]),
            skipped=int(summary["skipped"]),
            issueCount=int(summary["issue_count"]),
            maxResidentLeases=self.max_resident_leases,
            helloRequests=self.hello_requests,
            processRequests=self.process_requests,
        )

    def run(self) -> CaptionRunReport:
        if self._started:
            raise RuntimeError("a caption runner instance can execute only once")
        self._started = True
        initial_job = self.database.get_job(self.job_id)
        publisher = _CaptionProgressPublisher(
            self.database,
            self.job_id,
            str(initial_job["config_hash"]),
            consumer=self.progress_consumer,
            clock=self.clock,
        )
        active: list[WorkLease] = []
        try:
            job = self.database.get_job(self.job_id)
            if job["status"] in {"cancelling", "paused"}:
                return self._report(str(job["status"]))
            if job["status"] != "running" or job["current_module_id"] != "caption":
                raise CaptionRunnerFatalError(
                    "caption_protocol_violation",
                    "caption runner requires the active caption module",
                )
            hello, execution_policy = self._build_hello()
            if self.install_root is not None and hello.profile == "e621":
                verify_tagger_dictionary_coverage(
                    self.install_root,
                    self.resource_manifest_relative_path,
                    self.classify_resource_manifest_relative_path,
                )
            publisher.publish("running")
            while True:
                job = self.database.get_job(self.job_id)
                if job["status"] in {"cancelling", "paused"}:
                    return self._report(str(job["status"]))
                if job["status"] != "running" or job["current_module_id"] != "caption":
                    raise CaptionRunnerFatalError(
                        "caption_protocol_violation",
                        "caption job identity changed while the module was running",
                    )
                batch = self.scheduler.claim_batch(
                    self.job_id,
                    "caption",
                    self.worker_instance_id,
                    hello.configHash,
                )
                active = batch
                self.max_resident_leases = max(self.max_resident_leases, len(batch))
                if not batch:
                    if self.database.count_module_unsettled(self.job_id, "caption"):
                        raise CaptionRunnerFatalError(
                            "caption_protocol_violation",
                            "caption scheduler has unsettled work but no claimable bounded lease",
                        )
                    summary = self.database.module_summary(self.job_id, "caption")
                    status = self.scheduler.finish_module(
                        self.job_id,
                        "caption",
                        with_issues=int(summary["issue_count"]) > 0,
                    )
                    publisher.publish(status, force=True)
                    return self._report(status)
                for index, lease in enumerate(batch):
                    job = self.database.get_job(self.job_id)
                    if job["status"] in {"cancelling", "paused"}:
                        remaining = batch[index:]
                        self._release(remaining)
                        active = []
                        return self._report(str(job["status"]))
                    if job["status"] != "running" or job["current_module_id"] != "caption":
                        raise CaptionRunnerFatalError(
                            "caption_protocol_violation",
                            "caption job identity changed before a worker request",
                        )
                    remaining_ids = [item.leaseId for item in batch[index:] if item.leaseId]
                    self.scheduler.heartbeat(self.job_id, self.worker_instance_id, remaining_ids)
                    row = self._leased_row(lease)
                    try:
                        raw_e621 = self._raw_e621_annotation(row)
                    except RawE621JsonError as exc:
                        self._raw_e621_issue(lease, row, exc)
                        active = batch[index + 1 :]
                        publisher.publish("running", attempt=lease.attempt)
                        continue
                    if raw_e621 is not None:
                        self.scheduler.skip_caption(lease)
                        self.database.increment_module_diagnostic(
                            self.job_id,
                            "caption",
                            "e621_raw_json_converted",
                            severity="info",
                            amount=1,
                        )
                        active = batch[index + 1 :]
                        publisher.publish("running", attempt=lease.attempt)
                        continue
                    if not self._requires_inference(row, execution_policy):
                        if (
                            execution_policy.input_txt_mode == "tag"
                            and row["original_txt_state"] == "missing_or_blank"
                            and execution_policy.tagger_fallback_on_missing_txt is False
                        ):
                            self._missing_txt_without_tagger_fallback_issue(lease, row)
                        else:
                            self.scheduler.skip_caption(lease)
                        active = batch[index + 1 :]
                        publisher.publish("running", attempt=lease.attempt)
                        continue
                    if self._hello_result is None:
                        self._initialize_worker(hello)
                    item = self._work_item(lease, row)
                    self._process(lease, item)
                    active = batch[index + 1 :]
                    publisher.publish("running", attempt=lease.attempt)
                active = []
        except CaptionRunnerFatalError as exc:
            self._fail_module(active, publisher)
            raise
        except (ProtocolError, SchedulerError) as exc:
            fatal = CaptionRunnerFatalError("caption_protocol_violation", str(exc))
            self._fail_module(active, publisher)
            raise fatal from exc
        except Exception:
            self._release(active)
            raise
