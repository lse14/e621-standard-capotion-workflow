from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "src"))

from anima_core.contracts import JobConfig, sha256_json
from anima_core.db import StateDatabase
from anima_core.job_preflight import config_from_dict
from anima_core.path_safety import windows_key
from anima_core.pipeline import PipelineService
from anima_core.pipeline_dispatch import PipelineError
from anima_core.repair import RepairPreparationService
from anima_core.scheduler import BoundedScheduler, SchedulerError


def _samples(count: int) -> list[dict[str, object]]:
    return [{
        "sample_id": index,
        "relative_image_path": f"{index}.png",
        "annotation_key": str(index),
        "source": "e621",
        "in_processing_scope": True,
        "image_format": "png",
        "image_frame_count": 1,
        "original_txt_state": "missing_or_blank",
        "original_json_state": "missing_or_blank",
    } for index in range(1, count + 1)]


def _job_row(
    job_id: str,
    root: Path,
    config: JobConfig,
    *,
    sample_count: int,
    status: str = "ready",
) -> dict[str, object]:
    return {
        "job_id": job_id, "config_schema_version": config.schemaVersion,
        "config_json": json.dumps(config.to_dict(), ensure_ascii=False),
        "config_hash": config.config_hash, "profile": "e621", "work_mode": "in_place",
        "overwrite_mode": "incremental", "source_root": str(root), "output_root": None,
        "dataset_root": str(root), "dataset_root_key": windows_key(root), "manifest_schema_version": 1,
        "recursive": 0, "sample_count": sample_count, "manifest_generated_at": None, "status": status,
        "current_module_id": None, "last_event_id": 0, "pinned": 0, "api_budget_extra": 0,
        "api_budget_revision": 0, "overlay_root": None, "commit_journal_path": None,
        "resume_status": None, "created_at": "2026-08-27T00:00:00Z", "started_at": None,
        "cancel_requested_at": None, "finished_at": None,
    }


class ModuleBatchingSchedulerTests(unittest.TestCase):
    def test_claim_batch_uses_frozen_v10_module_batch_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = JobConfig(
                workMode="in_place", overwriteMode="incremental", sourceRoot=str(root),
                moduleBatchSize={
                    "caption": 3, "classify": 5, "replace": 7, "ocr": 4, "nl": 2,
                    "countReview": 6, "dropout": 4, "tokenBudget": 8, "export": 9,
                },
            )
            database = StateDatabase.open(root / "state.db")
            database.insert_job(_job_row("job-v10", root, config, sample_count=12))
            database.insert_samples("job-v10", _samples(12))
            try:
                scheduler = BoundedScheduler(database, lease_id_factory=lambda: "lease-fixed")
                scheduler.start_module("job-v10", "caption", enabled=True)
                leases = scheduler.claim_batch("job-v10", "caption", "worker", config.config_hash)
                self.assertEqual(3, len(leases))
                self.assertNotEqual(
                    config.config_hash,
                    JobConfig(
                        workMode="in_place", overwriteMode="incremental", sourceRoot=str(root),
                        moduleBatchSize={**config.moduleBatchSize, "caption": 4},
                    ).config_hash,
                )
            finally:
                database.close()

    def test_v10_ocr_claims_the_schema_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = JobConfig(
                workMode="in_place", overwriteMode="incremental", sourceRoot=str(root),
                moduleBatchSize={
                    "caption": 4, "classify": 128, "replace": 128, "ocr": 1024, "nl": 3,
                    "countReview": 100, "dropout": 4, "tokenBudget": 128, "export": 500,
                },
            )
            database = StateDatabase.open(root / "state.db")
            database.insert_job(_job_row("job-v10", root, config, sample_count=1024))
            database.insert_samples("job-v10", _samples(1024))
            try:
                scheduler = BoundedScheduler(
                    database,
                    lease_id_factory=iter(f"ocr-lease-{index}" for index in range(1, 1025)).__next__,
                )
                for module_id in ("caption", "classify", "replace"):
                    scheduler.start_module("job-v10", module_id, enabled=False)
                scheduler.start_module("job-v10", "ocr", enabled=True)

                leases = scheduler.claim_batch("job-v10", "ocr", "ocr-worker", config.config_hash)

                self.assertEqual(1024, len(leases))
            finally:
                database.close()

    def test_scheduler_rejects_legacy_configuration_before_starting_a_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = JobConfig(
                profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(root),
                schemaVersion=9,
            )
            database = StateDatabase.open(root / "state.db")
            database.insert_job(_job_row("job-v9", root, config, sample_count=1))
            database.insert_samples("job-v9", _samples(1))
            try:
                with self.assertRaisesRegex(SchedulerError, "legacy JobConfig|v10"):
                    BoundedScheduler(database).start_module("job-v9", "caption", enabled=True)
            finally:
                database.close()

    def test_scheduler_rejects_incomplete_batch_map_before_creating_a_module_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = JobConfig(workMode="in_place", overwriteMode="incremental", sourceRoot=str(root))
            frozen = config.to_dict()
            frozen["moduleBatchSize"].pop("export")
            row = _job_row("job-v10", root, config, sample_count=1)
            row["config_json"] = json.dumps(frozen, ensure_ascii=False)
            row["config_hash"] = sha256_json(frozen)
            database = StateDatabase.open(root / "state.db")
            database.insert_job(row)
            database.insert_samples("job-v10", _samples(1))
            try:
                with self.assertRaisesRegex(SchedulerError, "moduleBatchSize"):
                    BoundedScheduler(database).start_module("job-v10", "caption", enabled=True)
                self.assertEqual([], database.module_summaries("job-v10"))
            finally:
                database.close()

    def test_pipeline_runs_a_frozen_v10_config_past_the_schema_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = JobConfig(workMode="in_place", overwriteMode="incremental", sourceRoot=str(root))
            config.nl["systemPrompt"] = "Describe the visible image."
            database_path = root / "state.db"
            database = StateDatabase.open(database_path)
            database.insert_job(_job_row("job-v10", root, config, sample_count=0, status="preparing_workspace"))
            database.close()
            service = object.__new__(PipelineService)
            service.database_path = database_path

            with patch("anima_core.pipeline.BoundedScheduler") as scheduler_type:
                scheduler_type.return_value.start_module.side_effect = PipelineError("scheduler reached")
                with self.assertRaisesRegex(PipelineError, "scheduler reached"):
                    service._run("job-v10")

    def test_pipeline_rejects_legacy_config_before_creating_a_worker_thread(self) -> None:
        class NoopThread:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = JobConfig(
                profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(root),
                schemaVersion=9,
            )
            database_path = root / "state.db"
            database = StateDatabase.open(database_path)
            database.insert_job(_job_row("job-v9", root, config, sample_count=0, status="preparing_workspace"))
            database.close()
            service = object.__new__(PipelineService)
            service.database_path = database_path
            service._lock = threading.Lock()
            service._threads = {}

            with patch("anima_core.pipeline.threading.Thread", NoopThread):
                with self.assertRaisesRegex(PipelineError, "legacy JobConfig|v10"):
                    service.start("job-v9")

    def test_repair_row_copies_the_parent_frozen_v10_batch_map(self) -> None:
        root = Path(tempfile.gettempdir()) / "anima-module-batching-repair"
        config = JobConfig(
            workMode="in_place", overwriteMode="incremental", sourceRoot=str(root),
            moduleBatchSize={
                "caption": 5, "classify": 129, "replace": 130, "ocr": 8, "nl": 4,
                "countReview": 101, "dropout": 5, "tokenBudget": 129, "export": 499,
            },
        )
        parent = _job_row("parent-v10", root, config, sample_count=0)

        child = RepairPreparationService._job_row("repair-v10", parent, root)

        self.assertEqual(parent["config_hash"], child["config_hash"])
        self.assertEqual(
            config.moduleBatchSize,
            json.loads(str(child["config_json"]))["moduleBatchSize"],
        )

    def test_preflight_parser_preserves_every_frozen_v10_batch_value(self) -> None:
        config = JobConfig(
            workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\dataset",
            moduleBatchSize={
                "caption": 6, "classify": 130, "replace": 131, "ocr": 16, "nl": 5,
                "countReview": 102, "dropout": 6, "tokenBudget": 130, "export": 498,
            },
        )
        config.nl["systemPrompt"] = "Describe the visible image."

        parsed = config_from_dict(config.to_dict())

        self.assertEqual(config.moduleBatchSize, parsed.moduleBatchSize)
        self.assertEqual(config.config_hash, parsed.config_hash)


if __name__ == "__main__":
    unittest.main()
