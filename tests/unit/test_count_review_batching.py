from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.count_review_runner import CountReviewRunner
from anima_core.contracts import WorkLease
from anima_core.count_review_service import CountReviewError


class _Database:
    def __init__(self, schema_version: int = 10) -> None:
        self.job = {
            "config_schema_version": schema_version,
            "current_module_id": "count_review",
            "status": "running",
            "config_hash": "hash",
            "config_json": json.dumps({"schemaVersion": schema_version}),
        }

    def get_job(self, _job_id: str) -> dict[str, object]:
        return self.job

    def get_count_review_decision(self, _job_id: str, _sample_id: int) -> dict[str, object]:
        return {"status": "auto_resolved", "final_count": "solo", "applied_at": None}

    def get_leased_sample(self, _job_id: str, _module: str, sample_id: int, **_kwargs: object) -> dict[str, object]:
        return {"annotation_key": f"sample-{sample_id}"}

    def count_module_unsettled(self, _job_id: str, _module: str) -> int:
        return 0


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._batches = [
            [
                WorkLease("job", "count_review", 1, "leased", 1, "hash", "lease-1", "worker"),
                WorkLease("job", "count_review", 2, "leased", 1, "hash", "lease-2", "worker"),
            ],
            [],
        ]

    def claim_batch(self, *args: object, **kwargs: object) -> list[WorkLease]:
        self.calls.append({"args": args, **kwargs})
        return self._batches.pop(0)

    def complete(self, _lease: WorkLease) -> None:
        return None

    def finish_module(self, _job_id: str, _module: str) -> str:
        return "completed"


class _Writer:
    def __init__(self) -> None:
        self.writes: list[int] = []

    def write(self, lease: WorkLease, **_kwargs: object) -> None:
        self.writes.append(lease.sampleId)


class CountReviewBatchingTests(unittest.TestCase):
    def test_runner_rejects_legacy_schema_before_initialization(self) -> None:
        database = _Database(schema_version=9)
        with self.assertRaisesRegex(CountReviewError, "v10"):
            CountReviewRunner(
                database, _Scheduler(), _Writer(), job_id="job", worker_instance_id="worker"
            ).run()

    def test_runner_processes_the_scheduler_batch_without_serializing_a_single_item(self) -> None:
        database = _Database()
        scheduler = _Scheduler()
        writer = _Writer()
        with patch("anima_core.count_review_runner.CountReviewService") as service_type:
            service_type.return_value.initialize.return_value = type("Result", (), {"pending": 0})()
            result = CountReviewRunner(
                database, scheduler, writer, job_id="job", worker_instance_id="worker"
            ).run()
        self.assertEqual("completed", result)
        self.assertEqual([1, 2], writer.writes)
        self.assertEqual([None, None], [call.get("limit") for call in scheduler.calls])


if __name__ == "__main__":
    unittest.main()
