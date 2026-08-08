"""Core-owned Token Budget orchestration for an isolated tokenizer worker."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Mapping, Protocol

from .contracts import SampleIssue, WorkLease, job_config_supports_token_budget, profile_supports_job_config_schema, sha256_json
from .db import StateDatabase
from .overlay import WorkingAnnotationView
from .scheduler import BoundedScheduler, SchedulerError
from .stdio_transport import StdioJsonlTransportError
from .token_budget_overlay import TokenBudgetOverlayError, TokenBudgetOverlayWriter
from .token_budget_protocol import TokenBudgetProtocolError, validate_token_budget_outcome
from .worker_protocol import ProtocolEnvelopeV1, ProtocolError


RUNTIME_ID = "token-budget"
OWNER = "token-budget"


class TokenBudgetTransport(Protocol):
    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1: ...


class TokenBudgetRunnerError(RuntimeError):
    pass


class TokenBudgetRunner:
    """Validate every worker result before a batch makes any overlay change."""

    def __init__(self, database: StateDatabase, scheduler: BoundedScheduler, transport: TokenBudgetTransport, view: WorkingAnnotationView, *, job_id: str, worker_instance_id: str) -> None:
        self.database = database
        self.scheduler = scheduler
        self.transport = transport
        self.view = view
        self.job_id = job_id
        self.worker_instance_id = worker_instance_id
        self._hello_done = False

    def _config(self) -> tuple[str, dict[str, object], dict[str, object]]:
        job = self.database.get_job(self.job_id)
        try:
            config = json.loads(str(job["config_json"]))
        except json.JSONDecodeError as exc:
            raise TokenBudgetRunnerError("frozen Token Budget configuration is invalid") from exc
        if not isinstance(config, dict) or not job_config_supports_token_budget(config.get("schemaVersion")) or sha256_json(config) != job["config_hash"] or config.get("profile") != job["profile"] or not profile_supports_job_config_schema(str(job["profile"]), config.get("schemaVersion")):
            raise TokenBudgetRunnerError("frozen Token Budget configuration is invalid")
        section = config.get("tokenBudget")
        caption_format = config.get("captionFormat")
        if not isinstance(section, dict) or section.get("enabled") is not True or not isinstance(caption_format, dict):
            raise TokenBudgetRunnerError("Token Budget runner cannot execute a disabled module")
        required = {"enabled", "maxTokens", "resourceId", "resourceManifestRelativePath", "resourceFingerprint", "contextLimit"}
        if set(section) != required or type(section["maxTokens"]) is not int or type(section["contextLimit"]) is not int or not 1 <= section["maxTokens"] <= section["contextLimit"]:
            raise TokenBudgetRunnerError("frozen Token Budget configuration is invalid")
        if not all(isinstance(section[name], str) and section[name] for name in ("resourceId", "resourceManifestRelativePath", "resourceFingerprint")):
            raise TokenBudgetRunnerError("frozen Token Budget resource reference is invalid")
        return str(job["config_hash"]), section, caption_format

    def _exchange(self, method: str, payload: dict[str, object], config_hash: str) -> dict[str, object]:
        request = ProtocolEnvelopeV1("1.0", "request", f"token-budget-{uuid.uuid4().hex}", RUNTIME_ID, OWNER, method, payload, jobId=self.job_id, configHash=config_hash)
        try:
            response = self.transport.exchange(request)
        except StdioJsonlTransportError:
            raise
        except Exception as exc:
            raise TokenBudgetRunnerError("Token Budget transport failed") from exc
        if not isinstance(response, ProtocolEnvelopeV1) or response.kind != "response" or response.replyTo != request.messageId or response.runtimeId != RUNTIME_ID or response.owner != OWNER or response.jobId != self.job_id or response.configHash != config_hash:
            raise TokenBudgetRunnerError("Token Budget response envelope identity mismatch")
        if response.method == "error":
            raise TokenBudgetRunnerError("Token Budget worker rejected the request")
        if response.method != ("hello" if method == "hello" else "result"):
            raise TokenBudgetRunnerError("Token Budget response method is invalid")
        return response.payload

    def _hello(self, config_hash: str, section: Mapping[str, object]) -> None:
        payload = self._exchange("hello", {
            "schemaVersion": 1, "payloadType": "token_budget_hello_request", "jobId": self.job_id,
            "configHash": config_hash, "resourceId": section["resourceId"],
            "resourceManifestRelativePath": section["resourceManifestRelativePath"],
            "resourceFingerprint": section["resourceFingerprint"], "contextLimit": section["contextLimit"],
            "maxTokens": section["maxTokens"],
        }, config_hash)
        if payload != {"schemaVersion": 1, "payloadType": "token_budget_hello_result", "ready": True, "resourceFingerprint": section["resourceFingerprint"], "contextLimit": section["contextLimit"]}:
            raise TokenBudgetRunnerError("Token Budget hello result is invalid")
        self._hello_done = True

    def _issue(self, lease: WorkLease, row: object, *, code: str, message: str) -> None:
        self.scheduler.fail_with_issue(lease, SampleIssue(
            issueId=hashlib.sha256(f"{self.job_id}\0{lease.sampleId}\0token_budget\0{code}".encode("utf-8")).hexdigest(),
            jobId=self.job_id, sampleId=lease.sampleId, relativeImagePath=str(row["relative_image_path"]),
            moduleId="token_budget", code=code, severity="error", blocking=True, retriable=True,
            message=message, attempt=lease.attempt, repairStartModule="token_budget",
        ))

    def _prepare_batch(self, active: list[WorkLease], rows: list[object], outcomes: object, caption_format: Mapping[str, object], max_tokens: int) -> list[tuple[WorkLease, object, object]]:
        if not isinstance(outcomes, list) or len(outcomes) != len(active):
            raise TokenBudgetRunnerError("Token Budget batch result is invalid")
        pending = {(lease.sampleId, str(lease.leaseId)): (lease, row) for lease, row in zip(active, rows, strict=True)}
        mapped: list[tuple[WorkLease, object, object]] = []
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                raise TokenBudgetRunnerError("Token Budget outcome is invalid")
            key = (outcome.get("sampleId"), outcome.get("leaseId"))
            if key not in pending:
                raise TokenBudgetRunnerError("Token Budget outcome does not map to one active lease")
            lease, row = pending.pop(key)
            mapped.append((lease, row, outcome))
        if pending:
            raise TokenBudgetRunnerError("Token Budget batch omitted an active lease")
        # This pass does not write. A malformed envelope therefore has no partial effect.
        validated: list[tuple[WorkLease, object, object]] = []
        for lease, row, outcome in mapped:
            original_bytes = self.view.read(str(row["annotation_key"]), ".json")
            if original_bytes is None:
                validated.append((lease, row, TokenBudgetProtocolError("working annotation is missing")))
                continue
            try:
                original = json.loads(original_bytes.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                validated.append((lease, row, validate_token_budget_outcome(outcome, expected_sample_id=lease.sampleId, expected_lease_id=str(lease.leaseId), original_annotation=original, caption_format=caption_format, max_tokens=max_tokens)))
            except (UnicodeError, json.JSONDecodeError, ValueError, TokenBudgetProtocolError) as exc:
                validated.append((lease, row, TokenBudgetProtocolError("working annotation is invalid")))
        return validated

    def run(self) -> str:
        config_hash, section, caption_format = self._config()
        max_tokens = int(section["maxTokens"])
        writer = TokenBudgetOverlayWriter(self.database, self.view.overlay, self.view, self.job_id)
        active: list[WorkLease] = []
        try:
            while True:
                job = self.database.get_job(self.job_id)
                if job["status"] in {"cancelling", "paused"}:
                    return str(job["status"])
                if job["status"] != "running" or job["current_module_id"] != "token_budget":
                    raise TokenBudgetRunnerError("Token Budget module is not active")
                active = self.scheduler.claim_batch(self.job_id, "token_budget", self.worker_instance_id, config_hash)
                if not active:
                    if self.database.count_module_unsettled(self.job_id, "token_budget"):
                        raise TokenBudgetRunnerError("Token Budget has unsettled work")
                    summary = self.database.module_summary(self.job_id, "token_budget")
                    return self.scheduler.finish_module(self.job_id, "token_budget", with_issues=int(summary["issue_count"]) > 0)
                rows = [self.database.get_leased_sample(self.job_id, "token_budget", lease.sampleId, lease_id=str(lease.leaseId), worker_instance_id=self.worker_instance_id) for lease in active]
                if not self._hello_done:
                    self._hello(config_hash, section)
                payload = self._exchange("process_batch", {"schemaVersion": 1, "payloadType": "token_budget_process_request", "captionFormat": dict(caption_format), "items": [{"schemaVersion": 1, "sampleId": lease.sampleId, "leaseId": lease.leaseId, "annotation": json.loads(self.view.read(str(row["annotation_key"]), ".json") or b"null")} for lease, row in zip(active, rows, strict=True)]}, config_hash)
                if payload.get("schemaVersion") != 1 or payload.get("payloadType") != "token_budget_process_result":
                    raise TokenBudgetRunnerError("Token Budget batch result is invalid")
                prepared = self._prepare_batch(active, rows, payload.get("outcomes"), caption_format, max_tokens)
                job = self.database.get_job(self.job_id)
                if job["status"] in {"cancelling", "paused"}:
                    for lease in active:
                        state = self.database.get_sample_state(self.job_id, lease.sampleId)
                        if state["status"] == "leased" and state["lease_id"] == lease.leaseId:
                            self.scheduler.release_unstarted(lease)
                    active = []
                    return str(job["status"])
                try:
                    for lease in active:
                        self.database.get_leased_sample(
                            self.job_id, "token_budget", lease.sampleId,
                            lease_id=str(lease.leaseId), worker_instance_id=self.worker_instance_id,
                        )
                except (KeyError, ValueError) as exc:
                    raise TokenBudgetRunnerError("Token Budget worker response lease is stale") from exc
                for lease, row, value in prepared:
                    if isinstance(value, TokenBudgetProtocolError):
                        self._issue(lease, row, code="token_budget_protocol_violation", message="Token Budget worker outcome failed validation")
                    elif value.status == "failed":
                        self._issue(lease, row, code="token_budget_worker_failed", message="Token Budget worker could not process the sample")
                    elif value.status == "overflow":
                        writer.write_overflow_review(sample_id=lease.sampleId, lease_id=str(lease.leaseId), original_tokens=int(value.original_tokens), final_tokens=int(value.final_tokens), removed=value.removed or {}, max_tokens=max_tokens, resource_id=str(section["resourceId"]), resource_fingerprint=str(section["resourceFingerprint"]))
                        self._issue(lease, row, code="token_budget_overflow", message="Token Budget exceeded the frozen maximum")
                    else:
                        writer.prepare_and_commit(sample_id=lease.sampleId, lease_id=str(lease.leaseId), annotation_key=str(row["annotation_key"]), outcome=value, caption_format=caption_format, max_tokens=max_tokens)
                        self.scheduler.complete(lease)
                    active.remove(lease)
        except (TokenBudgetRunnerError, TokenBudgetOverlayError, SchedulerError, ProtocolError):
            for lease in active:
                state = self.database.get_sample_state(self.job_id, lease.sampleId)
                if state["status"] == "leased" and state["lease_id"] == lease.leaseId:
                    self.scheduler.release_unstarted(lease)
            self.database.set_module_summary(self.job_id, "token_budget", status="failed", finished=True)
            self.database.set_job_status(self.job_id, "failed", current_module_id="token_budget")
            raise
