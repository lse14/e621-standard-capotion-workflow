from __future__ import annotations

from .contracts import CURRENT_JOB_CONFIG_SCHEMA_VERSION
from .count_review_overlay import CountReviewOverlayWriter
from .count_review_protocol import FINAL_COUNT_VALUES
from .count_review_service import CountReviewError, CountReviewService
from .db import StateDatabase
from .scheduler import BoundedScheduler


class CountReviewRunner:
    """Core-only review initialization and bounded final-count application."""

    def __init__(
        self,
        database: StateDatabase,
        scheduler: BoundedScheduler,
        writer: CountReviewOverlayWriter,
        *,
        job_id: str,
        worker_instance_id: str,
    ) -> None:
        self.database = database
        self.scheduler = scheduler
        self.writer = writer
        self.job_id = job_id
        self.worker_instance_id = worker_instance_id

    def run(self) -> str:
        job = self.database.get_job(self.job_id)
        if int(job["config_schema_version"]) != CURRENT_JOB_CONFIG_SCHEMA_VERSION or job["current_module_id"] != "count_review":
            raise CountReviewError("count review runner requires an active v10 module")
        if job["status"] in {"cancelling", "paused"}:
            return str(job["status"])
        initialized = CountReviewService(self.database, self.job_id).initialize()
        job = self.database.get_job(self.job_id)
        if job["status"] == "reviewing":
            return "reviewing"
        if job["status"] in {"cancelling", "paused"}:
            return str(job["status"])
        if job["status"] != "running":
            raise CountReviewError("count review task is not runnable")
        if initialized.pending:
            self.database.set_job_status(
                self.job_id, "reviewing", current_module_id="count_review"
            )
            return "reviewing"

        config_hash = str(job["config_hash"])
        while True:
            job = self.database.get_job(self.job_id)
            if job["status"] in {"cancelling", "paused"}:
                return str(job["status"])
            if job["status"] != "running" or job["current_module_id"] != "count_review":
                raise CountReviewError("count review application is no longer active")
            leases = self.scheduler.claim_batch(
                self.job_id,
                "count_review",
                self.worker_instance_id,
                config_hash,
            )
            if not leases:
                if self.database.count_module_unsettled(self.job_id, "count_review"):
                    raise CountReviewError("count review has unsettled but unclaimable work")
                return self.scheduler.finish_module(self.job_id, "count_review")
            for lease in leases:
                decision = self.database.get_count_review_decision(
                    self.job_id, lease.sampleId
                )
                if (
                    decision["status"] not in {"auto_resolved", "manual_resolved"}
                    or decision["final_count"] not in FINAL_COUNT_VALUES
                    or decision["applied_at"] is not None
                ):
                    raise CountReviewError("count review lease has no unapplied resolved decision")
                sample = self.database.get_leased_sample(
                    self.job_id,
                    "count_review",
                    lease.sampleId,
                    lease_id=str(lease.leaseId),
                    worker_instance_id=self.worker_instance_id,
                )
                self.writer.write(
                    lease,
                    annotation_key=str(sample["annotation_key"]),
                    final_count=str(decision["final_count"]),
                )
                self.scheduler.complete(lease)
