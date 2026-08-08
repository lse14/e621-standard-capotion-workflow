from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import PROTOCOL_VERSION
from .contracts import ModuleId, SampleIssue, SampleRunState, WorkLease, pipeline_module_ids
from .db import MAX_PAGE_SIZE, StateDatabase
from .profiles import module_availability, require_available
from .state_machine import transition_module


FINAL_MODULE_STATUSES = frozenset({"completed", "completed_with_issues", "skipped", "skipped_not_available"})


@dataclass(frozen=True)
class ModuleQueueLimits:
    lease_batch_size: int
    max_resident_pages: int
    max_prefetch: int | None = None


MODULE_QUEUE_LIMITS: dict[str, ModuleQueueLimits] = {
    "caption": ModuleQueueLimits(lease_batch_size=64, max_resident_pages=1, max_prefetch=64),
    "classify": ModuleQueueLimits(lease_batch_size=500, max_resident_pages=2),
    "replace": ModuleQueueLimits(lease_batch_size=500, max_resident_pages=2),
    "ocr": ModuleQueueLimits(lease_batch_size=1, max_resident_pages=1),
    "nl": ModuleQueueLimits(lease_batch_size=32, max_resident_pages=1),
    "count_review": ModuleQueueLimits(lease_batch_size=500, max_resident_pages=1),
    "dropout": ModuleQueueLimits(lease_batch_size=16, max_resident_pages=1, max_prefetch=16),
    "token_budget": ModuleQueueLimits(lease_batch_size=32, max_resident_pages=1),
    "export": ModuleQueueLimits(lease_batch_size=500, max_resident_pages=2),
}


@dataclass(frozen=True)
class RecoveryReport:
    returnedLeases: int
    committedPrepared: int
    repeatedPrepared: int
    pendingApiDecisions: int


class SchedulerError(RuntimeError):
    pass


def _expires_at(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class BoundedScheduler:
    """Control-plane scheduler; it leases IDs only and never holds business payloads."""

    def __init__(self, database: StateDatabase, *, lease_seconds: int = 60, lease_id_factory: Callable[[], str] | None = None) -> None:
        if lease_seconds < 5 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        self.database = database
        self.lease_seconds = lease_seconds
        self.lease_id_factory = lease_id_factory or (lambda: uuid.uuid4().hex)

    def nl_queue_limit(self, concurrency: int) -> int:
        if not 1 <= concurrency <= 16:
            raise ValueError("NL concurrency must be between 1 and 16")
        return min(2 * concurrency, 32)

    @staticmethod
    def _nl_concurrency(job: object) -> int:
        try:
            config = json.loads(job["config_json"])  # type: ignore[index]
            policy = config["nl"].get("apiPolicy") or {}
            concurrency = policy.get("concurrency", 3)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SchedulerError("frozen NL configuration is invalid") from exc
        if type(concurrency) is not int or not 1 <= concurrency <= 16:
            raise SchedulerError("frozen NL concurrency must be between 1 and 16")
        return concurrency

    @staticmethod
    def _dropout_batch_size(job: object) -> int:
        try:
            config = json.loads(job["config_json"])  # type: ignore[index]
            batch_size = config["dropout"]["quality"]["batchSize"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SchedulerError("frozen policy configuration is invalid") from exc
        if type(batch_size) is not int or not 1 <= batch_size <= 16:
            raise SchedulerError("frozen policy batch size must be between 1 and 16")
        return batch_size

    def _assert_module_order(self, job_id: str, module_id: ModuleId) -> None:
        job = self.database.get_job(job_id)
        try:
            module_order = pipeline_module_ids(int(job["config_schema_version"]))
            index = module_order.index(module_id)
        except ValueError as exc:
            raise SchedulerError(f"module {module_id} is not valid for this task schema") from exc
        statuses = {row["module_id"]: row["status"] for row in self.database.module_summaries(job_id)}
        for predecessor in module_order[:index]:
            if statuses.get(predecessor) not in FINAL_MODULE_STATUSES:
                raise SchedulerError(f"{module_id} cannot start before {predecessor} reaches a final state")
        for successor in module_order[index + 1:]:
            if successor in statuses:
                raise SchedulerError(f"{module_id} cannot start after {successor} has been initialized")

    def start_module(self, job_id: str, module_id: ModuleId, *, enabled: bool, profile: str) -> str:
        job = self.database.get_job(job_id)
        if profile != job["profile"]:
            raise SchedulerError("requested profile does not match the immutable job profile")
        require_available(profile)
        allowed_job_states = {"ready", "running", "paused", "reviewing", "exporting"}
        if module_id == "caption":
            allowed_job_states.add("preparing_workspace")
        if job["status"] not in allowed_job_states:
            raise SchedulerError(f"job state {job['status']} cannot start a module")
        self._assert_module_order(job_id, module_id)
        availability = module_availability(profile, module_id, enabled=enabled)
        total = self.database.count_module_samples(job_id, module_id)
        self.database.initialize_module_summary(job_id, module_id, total=total)
        summary = self.database.module_summary(job_id, module_id)
        if summary["status"] == "running":
            expected_job_status = "exporting" if module_id == "export" else "running"
            if job["current_module_id"] == module_id and job["status"] == expected_job_status:
                # Recovery must retain already completed samples rather than reset the module queue.
                return "running"
            raise SchedulerError("only the persisted active module can be resumed")
        if availability == "skipped_not_available":
            transition_module(summary["status"], availability, module_id=module_id)
            self.database.set_module_summary(job_id, module_id, status=availability, skipped=total, finished=True)
            if job["status"] == "preparing_workspace":
                self.database.set_job_status(job_id, "running", current_module_id=module_id)
            return availability
        if availability == "skipped":
            transition_module(summary["status"], availability, module_id=module_id)
            if module_id == "caption":
                cursor: int | None = None
                while True:
                    page = self.database.skip_disabled_caption_page(
                        job_id,
                        after_sample_id=cursor,
                        limit=MAX_PAGE_SIZE,
                    )
                    if not page:
                        break
                    cursor = page[-1]
            elif module_id == "nl":
                cursor = None
                while True:
                    page = self.database.record_nl_disabled_observations_page(
                        job_id, after_sample_id=cursor, limit=500
                    )
                    if not page:
                        break
                    cursor = page[-1]
            self.database.set_module_summary(job_id, module_id, status=availability, skipped=total, finished=True)
            if job["status"] == "preparing_workspace":
                self.database.set_job_status(job_id, "running", current_module_id=module_id)
            return availability
        transition_module(summary["status"], "running", module_id=module_id)
        if module_id == "export":
            # Samples that failed before Export never reach it on their own.
            # Record them as explicit export skips so the module can complete and
            # the commit projection stops expecting artifacts they cannot have.
            cursor: int | None = None
            skipped_failures = 0
            while True:
                page = self.database.skip_failed_samples_for_export_page(
                    job_id, after_sample_id=cursor, limit=MAX_PAGE_SIZE
                )
                if not page:
                    break
                skipped_failures += len(page)
                cursor = page[-1]
            if skipped_failures:
                self.database.increment_module_counts(job_id, "export", skipped=skipped_failures)
        # A page-at-a-time reset is required for all modules after their
        # predecessor. Caption starts from the initial NULL module state.
        if module_id != "caption":
            cursor: int | None = None
            while True:
                page = self.database.reset_next_module_page(job_id, module_id, after_sample_id=cursor, limit=MAX_PAGE_SIZE)
                if not page:
                    break
                cursor = page[-1]
        self.database.set_module_summary(job_id, module_id, status="running")
        self.database.set_job_status(
            job_id, "exporting" if module_id == "export" else "running", current_module_id=module_id
        )
        return "running"

    def reclaim_expired_leases(self, job_id: str) -> int:
        """Return leases whose holder stopped heart-beating before a new claim."""
        return self.database.return_expired_leases(job_id, _expires_at(0))

    def claim_batch(self, job_id: str, module_id: ModuleId, worker_instance_id: str, config_hash: str, *, limit: int | None = None) -> list[WorkLease]:
        job = self.database.get_job(job_id)
        require_available(job["profile"])
        expected_job_status = "exporting" if module_id == "export" else "running"
        if job["status"] != expected_job_status or job["current_module_id"] != module_id:
            return []
        # Every claim loop passes through here, so expired leases are recovered
        # before capacity and single-worker limits are measured.
        self.reclaim_expired_leases(job_id)
        if job["config_hash"] != config_hash:
            raise SchedulerError("config hash does not match immutable job configuration")
        maximum = MODULE_QUEUE_LIMITS[module_id].lease_batch_size
        if module_id == "dropout":
            maximum = self._dropout_batch_size(job)
        actual_limit = maximum if limit is None else limit
        if not 1 <= actual_limit <= maximum:
            raise SchedulerError(f"{module_id} lease limit must be between 1 and {maximum}")
        max_in_flight = maximum * MODULE_QUEUE_LIMITS[module_id].max_resident_pages
        if module_id == "nl":
            max_in_flight = self.nl_queue_limit(self._nl_concurrency(job))
        try:
            rows = self.database.claim_leases(
                job_id,
                module_id,
                worker_instance_id,
                config_hash,
                limit=actual_limit,
                max_in_flight=max_in_flight,
                single_worker=module_id in {"caption", "ocr", "count_review", "dropout"},
                lease_id_factory=self.lease_id_factory,
                expires_at=_expires_at(self.lease_seconds),
            )
        except ValueError as exc:
            raise SchedulerError(str(exc)) from exc
        return [
            WorkLease(
                jobId=job_id,
                moduleId=module_id,
                sampleId=int(row["sample_id"]),
                status="leased",
                attempt=int(row["attempt"]),
                configHash=config_hash,
                leaseId=str(row["lease_id"]),
                workerInstanceId=str(row["worker_instance_id"]),
                leaseExpiresAt=str(row["lease_expires_at"]),
            )
            for row in rows
        ]

    def heartbeat(self, job_id: str, worker_instance_id: str, lease_ids: list[str]) -> int:
        return self.database.heartbeat(job_id, worker_instance_id, _expires_at(self.lease_seconds), lease_ids=lease_ids)

    def stage_prepared(self, lease: WorkLease, *, relative_path: str, sha256: str) -> None:
        if not lease.leaseId:
            raise SchedulerError("prepared artifacts require a lease id")
        self.database.stage_prepared_artifact(
            lease.jobId, lease.sampleId, lease_id=lease.leaseId, relative_path=relative_path, sha256=sha256
        )

    def complete(self, lease: WorkLease, *, txt_provenance: str | None = None) -> None:
        if not lease.leaseId:
            raise SchedulerError("completion requires a lease id")
        self.database.complete_leased_sample_and_count(
            lease.jobId,
            lease.moduleId,
            lease.sampleId,
            lease_id=lease.leaseId,
            txt_provenance=txt_provenance,
        )

    def skip_caption(self, lease: WorkLease) -> None:
        if lease.moduleId != "caption" or not lease.leaseId:
            raise SchedulerError("caption skip requires a caption lease id")
        self.database.skip_leased_caption_sample(
            lease.jobId,
            lease.sampleId,
            lease_id=lease.leaseId,
        )

    def fail_with_issue(self, lease: WorkLease, issue: SampleIssue, *, allowed_statuses: tuple[str, ...] = ("leased",)) -> None:
        if not lease.leaseId:
            raise SchedulerError("failure requires a lease id")
        self.database.fail_leased_sample_with_issue(
            lease.jobId,
            lease.moduleId,
            lease.sampleId,
            lease_id=lease.leaseId,
            issue=issue,
            allowed_statuses=allowed_statuses,
        )

    def release_unstarted(self, lease: WorkLease) -> None:
        if not lease.leaseId:
            raise SchedulerError("returning a lease requires a lease id")
        self.database.return_lease_to_pending(
            lease.jobId,
            lease.sampleId,
            lease_id=lease.leaseId,
        )

    def fail(self, lease: WorkLease, *, retriable: bool) -> None:
        if not lease.leaseId:
            raise SchedulerError("failure requires a lease id")
        if retriable:
            self.database.return_lease_to_pending(lease.jobId, lease.sampleId, lease_id=lease.leaseId)
        else:
            state = self.database.get_sample_state(lease.jobId, lease.sampleId)
            self.database.set_sample_state(
                lease.jobId,
                lease.sampleId,
                SampleRunState(
                    sampleId=lease.sampleId,
                    txtProvenance=state["txt_provenance"],
                    currentModuleId=lease.moduleId,
                    status="failed",
                    attempt=int(state["attempt"]),
                    leaseId=None,
                    workerInstanceId=None,
                    leaseExpiresAt=None,
                ),
            )
            self.database.increment_module_counts(lease.jobId, lease.moduleId, failed=1)

    def finish_module(self, job_id: str, module_id: ModuleId, *, with_issues: bool = False) -> str:
        job = self.database.get_job(job_id)
        if job["current_module_id"] != module_id:
            raise SchedulerError("only the current module can be finished")
        if self.database.count_module_unsettled(job_id, module_id):
            raise SchedulerError("module still has pending or in-flight samples")
        summary = self.database.module_summary(job_id, module_id)
        target = "completed_with_issues" if with_issues else "completed"
        transition_module(summary["status"], target, module_id=module_id)
        self.database.set_module_summary(job_id, module_id, status=target, finished=True)
        self.database.checkpoint()
        return target

    def worker_exited(self, job_id: str, module_id: ModuleId, *, abnormal: bool) -> bool:
        """Return whether a worker may be restarted; the third crash fails the module."""
        if not abnormal:
            return False
        attempts = self.database.increment_worker_restart(job_id, module_id)
        if attempts <= 2:
            return True
        self.database.set_module_summary(job_id, module_id, status="failed", finished=True)
        self.database.set_job_status(job_id, "failed", current_module_id=module_id)
        return False

    def begin_cancellation(self, job_id: str) -> None:
        self.database.begin_cancellation(job_id)

    def settle_cancellation(self, job_id: str, *, commit_succeeded: bool = False) -> None:
        self.database.settle_cancellation(job_id, succeeded=commit_succeeded)

    def confirm_nl_api_outcome_unknown(self, job_id: str, *, confirmed: bool) -> int:
        if not confirmed:
            raise SchedulerError("explicit confirmation is required before repeating unknown API outcomes")
        job = self.database.get_job(job_id)
        if job["current_module_id"] != "nl" or job["status"] not in {"paused", "interrupted"}:
            raise SchedulerError("unknown API outcomes can be confirmed only for a paused or interrupted NL module")
        returned = 0
        while True:
            page = self.database.confirm_nl_unknown_requests(job_id)
            if not page:
                return returned
            self.database.increment_module_diagnostic(job_id, "nl", "nl_api_outcome_unknown_confirmed", severity="warning", amount=page)
            returned += page

    def recover(
        self,
        job_id: str,
        *,
        confirmed: bool,
        expected_config_hash: str,
        manifest_schema_version: int,
        protocol_version: str,
        verify_source_fingerprints: Callable[[], bool],
        commit_prepared: Callable[[str, int, str, str], bool | str],
        commit_response_staged: Callable[[str, int, str], bool] | None = None,
    ) -> RecoveryReport:
        if not confirmed:
            raise SchedulerError("manual confirmation is required before recovery")
        job = self.database.get_job(job_id)
        if job["status"] != "interrupted":
            raise SchedulerError("only interrupted jobs require manual recovery")
        if job["config_hash"] != expected_config_hash or int(job["manifest_schema_version"]) != manifest_schema_version:
            raise SchedulerError("job configuration or manifest schema no longer matches")
        if protocol_version != PROTOCOL_VERSION:
            raise SchedulerError("worker protocol version no longer matches")
        if not verify_source_fingerprints():
            raise SchedulerError("source file fingerprints no longer match the immutable manifest")
        returned = 0
        committed = 0
        repeated = 0
        api_pending = 0
        cursor: int | None = None
        while True:
            page = self.database.recovery_state_page(job_id, after_sample_id=cursor, limit=500)
            if not page:
                break
            for row in page:
                status = row["status"]
                sample_id = int(row["sample_id"])
                if status == "leased":
                    self.database.return_lease_to_pending(job_id, sample_id, lease_id=row["lease_id"])
                    returned += 1
                elif status == "prepared":
                    path = row["prepared_artifact_relative_path"]
                    digest = row["prepared_artifact_sha256"]
                    result = (
                        commit_prepared(job_id, sample_id, path, digest)
                        if isinstance(path, str) and isinstance(digest, str)
                        else False
                    )
                    if result == "settled":
                        committed += 1
                    elif result is True:
                        self.database.complete_leased_sample_and_count(
                            job_id,
                            str(row["current_module_id"]),
                            sample_id,
                            lease_id=row["lease_id"],
                            allowed_statuses=("prepared",),
                            txt_provenance="module1_written" if row["current_module_id"] == "caption" else None,
                        )
                        committed += 1
                    else:
                        self.database.return_lease_to_pending(job_id, sample_id, lease_id=row["lease_id"])
                        repeated += 1
                elif status == "request_started":
                    # The API could have charged the request. It remains for a
                    # user decision and is never blindly resent during restart.
                    api_pending += 1
                elif status == "response_staged":
                    if commit_response_staged is not None and isinstance(row["lease_id"], str) and commit_response_staged(job_id, sample_id, row["lease_id"]):
                        self.database.complete_leased_sample_and_count(
                            job_id, str(row["current_module_id"]), sample_id, lease_id=row["lease_id"],
                            allowed_statuses=("prepared", "response_staged"),
                        )
                        committed += 1
                    else:
                        api_pending += 1
            cursor = int(page[-1]["sample_id"])
        return RecoveryReport(returned, committed, repeated, api_pending)
