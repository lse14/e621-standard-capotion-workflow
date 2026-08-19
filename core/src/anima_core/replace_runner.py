from __future__ import annotations

import hashlib
import json
import uuid
from typing import Protocol

from .classify_overlay import ClassifyJsonError, parse_annotation_json
from .contracts import SampleIssue, WorkLease, sha256_json
from .db import StateDatabase
from .overlay import WorkingAnnotationView
from .replace_overlay import ReplaceOverlayWriter
from .replace_provenance import DatasetReplaceProvenance, ReplaceProvenanceError
from .replace_protocol import ReplaceProtocolError, validate_replace_outcome
from .replace_resource import ReplaceResourceError, load_replace_resource_from_install
from .custom_replace_index import CustomReplaceIndexError, verify_frozen_custom_replace_index
from .scheduler import BoundedScheduler, SchedulerError
from .stdio_transport import StdioJsonlTransportError
from .worker_protocol import ProtocolEnvelopeV1, ProtocolError


RUNTIME_ID = "replace-e621"
OWNER = "replace"


class ReplaceTransport(Protocol):
    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1: ...


class ReplaceRunnerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReplaceRunner:
    """Bounded core scheduling for Replace; only the overlay writer persists JSON."""

    def __init__(
        self, database: StateDatabase, scheduler: BoundedScheduler, transport: ReplaceTransport,
        view: WorkingAnnotationView, writer: ReplaceOverlayWriter, *, job_id: str, worker_instance_id: str,
        install_root: str, resource_manifest_relative_path: str | None = None, resource_fingerprint: str | None = None,
        custom_index_path: str | None = None, custom_index_overlay_root: str | None = None,
        custom_index_sha256: str | None = None, custom_index_rule_count: int | None = None,
    ) -> None:
        self.database = database
        self.scheduler = scheduler
        self.transport = transport
        self.view = view
        self.writer = writer
        self.job_id = job_id
        self.worker_instance_id = worker_instance_id
        self.install_root = install_root
        self.resource_manifest_relative_path = resource_manifest_relative_path
        self.resource_fingerprint = resource_fingerprint
        self.custom_index_path = custom_index_path
        self.custom_index_overlay_root = custom_index_overlay_root
        self.custom_index_sha256 = custom_index_sha256
        self.custom_index_rule_count = custom_index_rule_count
        self._hello_done = False

    def _fatal(self, code: str, message: str) -> ReplaceRunnerError:
        return ReplaceRunnerError(code, message)

    def _diagnostic(self, code: str, amount: int, *, severity: str = "info") -> None:
        while amount > 0:
            step = min(amount, 1_000)
            self.database.increment_module_diagnostic(self.job_id, "replace", code, severity=severity, amount=step)
            amount -= step

    def _config(self) -> tuple[str, str, dict[str, object]]:
        job = self.database.get_job(self.job_id)
        try:
            config = json.loads(str(job["config_json"]))
        except json.JSONDecodeError as exc:
            raise self._fatal("replace_protocol_violation", "frozen JobConfig is invalid JSON") from exc
        if (
            not isinstance(config, dict) or sha256_json(config) != job["config_hash"]
            or config.get("schemaVersion") != 9 or "profile" in config
            or not isinstance(config.get("replace"), dict)
        ):
            raise self._fatal("replace_protocol_violation", "frozen replace configuration is invalid")
        replace = config["replace"]
        if replace.get("enabled") is not True or replace.get("indexMode") not in {"bundled", "custom"}:
            raise self._fatal("replace_protocol_violation", "frozen replace configuration is invalid")
        return str(job["config_hash"]), str(job["dataset_root"]), replace

    def _exchange(self, method: str, payload: dict[str, object], config_hash: str) -> dict[str, object]:
        request = ProtocolEnvelopeV1(
            protocolVersion="1.0", kind="request", messageId=f"replace-{uuid.uuid4().hex}", runtimeId=RUNTIME_ID,
            owner=OWNER, method=method, payload=payload, jobId=self.job_id, configHash=config_hash,
        )
        try:
            response = self.transport.exchange(request)
        except StdioJsonlTransportError:
            raise
        except Exception as exc:
            raise self._fatal("replace_protocol_violation", "replace transport failed") from exc
        if not isinstance(response, ProtocolEnvelopeV1) or (
            response.kind != "response" or response.replyTo != request.messageId or response.runtimeId != RUNTIME_ID
            or response.owner != OWNER or response.jobId != self.job_id or response.configHash != config_hash
        ):
            raise self._fatal("replace_protocol_violation", "replace response envelope identity mismatch")
        if response.method == "error":
            code = response.payload.get("code")
            raise self._fatal(code if code == "replace_resource_invalid" else "replace_protocol_violation", "replace worker failed")
        return response.payload

    def _hello(self, config_hash: str, replace: dict[str, object]) -> None:
        if replace["indexMode"] == "bundled":
            if not isinstance(self.resource_manifest_relative_path, str) or not isinstance(self.resource_fingerprint, str):
                raise self._fatal("replace_resource_invalid", "bundled replace resource is missing")
            request = {"schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": self.job_id, "configHash": config_hash, "resourceManifestRelativePath": self.resource_manifest_relative_path, "resourceFingerprint": self.resource_fingerprint}
            expected_fingerprint, expected_rules = self.resource_fingerprint, None
        else:
            if not all(isinstance(value, str) for value in (self.custom_index_path, self.custom_index_overlay_root, self.custom_index_sha256)) or type(self.custom_index_rule_count) is not int:
                raise self._fatal("replace_resource_invalid", "custom replace resource is missing")
            request = {"schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": self.job_id, "configHash": config_hash, "customIndexOverlayRoot": self.custom_index_overlay_root, "customIndexPath": self.custom_index_path, "customIndexSha256": self.custom_index_sha256, "customIndexRuleCount": self.custom_index_rule_count}
            expected_fingerprint, expected_rules = self.custom_index_sha256, self.custom_index_rule_count
        payload = self._exchange("hello", request, config_hash)
        rule_count = payload.get("ruleCount")
        if (
            payload.get("payloadType") != "replace_hello_result" or payload.get("ready") is not True
            or payload.get("indexLoads") != 1 or type(rule_count) is not int or rule_count < 1
            or (expected_rules is not None and rule_count != expected_rules)
            or payload.get("resourceFingerprint") != expected_fingerprint
        ):
            raise self._fatal("replace_protocol_violation", "replace hello result is invalid")
        # M3-07: the load time audit derived from canonical_e621_tag is reported once per job.
        for key, code, severity in (
            ("keepNonCanonical", "replace_keep_non_canonical", "info"),
            ("canonicalDirectionConflict", "replace_canonical_direction_conflict", "warning"),
        ):
            amount = payload.get(key)
            if type(amount) is not int or amount < 0:
                raise self._fatal("replace_protocol_violation", "replace hello index audit is invalid")
            self._diagnostic(code, amount, severity=severity)
        self._hello_done = True

    def _validate_resource(self, replace: dict[str, object]) -> None:
        try:
            if replace["indexMode"] == "bundled":
                if not isinstance(self.resource_manifest_relative_path, str) or not isinstance(self.resource_fingerprint, str):
                    raise ReplaceResourceError("bundled replace resource is missing")
                load_replace_resource_from_install(self.install_root, self.resource_manifest_relative_path, self.resource_fingerprint)
            else:
                if not isinstance(self.custom_index_path, str) or not isinstance(self.custom_index_sha256, str) or type(self.custom_index_rule_count) is not int:
                    raise CustomReplaceIndexError("custom replace resource is missing")
                verify_frozen_custom_replace_index(self.custom_index_path, self.custom_index_sha256, self.custom_index_rule_count)
        except (ReplaceResourceError, CustomReplaceIndexError) as exc:
            raise self._fatal("replace_resource_invalid", str(exc)) from exc

    def _fail_input(self, lease: WorkLease, row: object, message: str) -> None:
        self.scheduler.fail_with_issue(lease, SampleIssue(
            issueId=uuid.uuid5(uuid.NAMESPACE_URL, f"{self.job_id}/replace/{lease.sampleId}/replace_json_invalid").hex,
            jobId=self.job_id, sampleId=lease.sampleId, relativeImagePath=str(row["relative_image_path"]), moduleId="replace",
            code="replace_json_invalid", severity="error", blocking=True, retriable=False, message=message[:1024], attempt=lease.attempt,
        ))

    def run(self) -> str:
        config_hash, dataset_root, replace = self._config()
        active: list[WorkLease] = []
        dataset_provenance: DatasetReplaceProvenance | None = None
        try:
            self._validate_resource(replace)
            dataset_provenance = DatasetReplaceProvenance.open(dataset_root)
            fingerprint = str(self.resource_fingerprint if replace["indexMode"] == "bundled" else self.custom_index_sha256)
            while True:
                job = self.database.get_job(self.job_id)
                if job["status"] in {"cancelling", "paused"}:
                    return str(job["status"])
                if job["status"] != "running" or job["current_module_id"] != "replace":
                    raise self._fatal("replace_protocol_violation", "replace module is not active")
                active = self.scheduler.claim_batch(self.job_id, "replace", self.worker_instance_id, config_hash)
                if not active:
                    if self.database.count_module_unsettled(self.job_id, "replace"):
                        raise self._fatal("replace_protocol_violation", "replace has unsettled but unclaimable work")
                    summary = self.database.module_summary(self.job_id, "replace")
                    return self.scheduler.finish_module(self.job_id, "replace", with_issues=int(summary["issue_count"]) > 0)
                for lease in active[:]:
                    job = self.database.get_job(self.job_id)
                    if job["status"] in {"cancelling", "paused"}:
                        for pending in active:
                            self.scheduler.release_unstarted(pending)
                        active = []
                        return str(job["status"])
                    self.scheduler.heartbeat(
                        self.job_id, self.worker_instance_id,
                        [str(pending.leaseId) for pending in active if pending.leaseId],
                    )
                    row = self.database.get_leased_sample(self.job_id, "replace", lease.sampleId, lease_id=str(lease.leaseId), worker_instance_id=self.worker_instance_id)
                    if self.writer.provenance(str(row["annotation_key"])) == fingerprint:
                        self._diagnostic("replace_already_applied", 1)
                        self.scheduler.complete(lease)
                        active.remove(lease)
                        continue
                    try:
                        raw_json = self.view.read(str(row["annotation_key"]), ".json")
                        projection = parse_annotation_json(raw_json)
                        if projection is None:
                            raise ClassifyJsonError("working JSON is missing or blank")
                    except ClassifyJsonError as exc:
                        self._fail_input(lease, row, str(exc))
                        active.remove(lease)
                        continue
                    assert raw_json is not None
                    if dataset_provenance.matches(
                        str(row["annotation_key"]), fingerprint, hashlib.sha256(raw_json).hexdigest(),
                    ):
                        try:
                            self.writer.mark_provenance(str(row["annotation_key"]), fingerprint)
                        except Exception as exc:
                            raise self._fatal("replace_result_persistence_failed", "replace provenance persistence failed") from exc
                        self._diagnostic("replace_already_applied", 1)
                        self.scheduler.complete(lease)
                        active.remove(lease)
                        continue
                    if not self._hello_done:
                        self._hello(config_hash, replace)
                    payload = self._exchange("process_batch", {
                        "schemaVersion": 1, "payloadType": "replace_process_request", "items": [{
                            "schemaVersion": 1, "sampleId": lease.sampleId, "leaseId": lease.leaseId, "source": "e621",
                            "relativeImagePath": row["relative_image_path"], "projection": projection,
                        }],
                    }, config_hash)
                    outcome, projection, counts, message = validate_replace_outcome(
                        payload, sample_id=lease.sampleId, lease_id=str(lease.leaseId),
                        relative_image_path=str(row["relative_image_path"]), original_projection=projection,
                    )
                    if outcome == "issue":
                        self._fail_input(lease, row, str(message))
                    else:
                        try:
                            self.writer.write(sample_id=lease.sampleId, lease_id=str(lease.leaseId), annotation_key=str(row["annotation_key"]), projection=projection or {}, provenance=fingerprint)
                            for key, code in (
                                ("passthrough", "replace_passthrough"), ("replaced", "replace_replaced"),
                                ("dropped", "replace_dropped"), ("keepRewritten", "replace_keep_rewritten"),
                            ):
                                self._diagnostic(code, counts[key])
                            self.scheduler.complete(lease)
                        except Exception as exc:
                            raise self._fatal("replace_result_persistence_failed", "replace overlay persistence failed") from exc
                    active.remove(lease)
        except (ReplaceRunnerError, ReplaceProtocolError, ReplaceProvenanceError, SchedulerError, ProtocolError) as exc:
            for lease in active:
                self.scheduler.release_unstarted(lease)
            self.database.set_module_summary(self.job_id, "replace", status="failed", finished=True)
            self.database.set_job_status(self.job_id, "failed", current_module_id="replace")
            if isinstance(exc, ReplaceProtocolError):
                raise self._fatal("replace_protocol_violation", str(exc)) from exc
            if isinstance(exc, ReplaceProvenanceError):
                raise self._fatal("replace_provenance_invalid", str(exc)) from exc
            raise
        finally:
            if dataset_provenance is not None:
                dataset_provenance.close()
