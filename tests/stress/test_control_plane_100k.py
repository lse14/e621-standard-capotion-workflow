from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest
from itertools import islice
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "src"))

from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.path_safety import windows_key
from anima_core.scheduler import BoundedScheduler, MODULE_QUEUE_LIMITS, SchedulerError


SAMPLE_COUNT = 100_000
ISSUE_COUNT = 5_000
MAX_DATABASE_BYTES = 256 * 1024 * 1024
WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
COUNT_REVIEW_PENDING = SAMPLE_COUNT // 10
NOW = "2026-01-01T00:00:00Z"
COUNT_DECISION_JSON = json.dumps(
    {
        "value": "solo",
        "baseValue": "solo",
        "selectedSource": "wiki_tags",
        "originalRaw": None,
        "originalNormalized": None,
        "wikiValue": "solo",
        "matchedTags": ["solo"],
        "conflict": False,
        "issueCodes": [],
        "warnings": [],
        "appliedLowerBounds": [],
    },
    sort_keys=True,
    separators=(",", ":"),
)


def _job(root: Path) -> dict[str, object]:
    config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(root))
    return {
        "job_id": "stress-job", "config_schema_version": 3, "config_json": json.dumps(config.to_dict()), "config_hash": config.config_hash,
        "profile": "e621", "work_mode": "in_place", "overwrite_mode": "incremental", "source_root": str(root),
        "output_root": None, "dataset_root": str(root), "dataset_root_key": windows_key(root), "manifest_schema_version": 1,
        "recursive": 1, "sample_count": 0, "manifest_generated_at": None, "status": "reviewing", "current_module_id": "count_review",
        "last_event_id": 0, "pinned": 0, "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": None,
        "commit_journal_path": None, "resume_status": None, "created_at": "2026-01-01T00:00:00Z", "started_at": None,
        "cancel_requested_at": None, "finished_at": None,
    }


def _sample_rows():
    for sample_id in range(1, SAMPLE_COUNT + 1):
        yield {
            "sample_id": sample_id,
            "relative_image_path": f"shard-{sample_id // 1000:03d}\\{sample_id}.png",
            "annotation_key": f"shard-{sample_id // 1000:03d}\\{sample_id}",
            "source": "e621",
            "in_processing_scope": True,
            "image_format": "png",
            "image_frame_count": 1,
            "original_txt_state": "missing_or_blank",
            "original_json_state": "missing_or_blank",
        }


def _issue_rows():
    for index in range(1, ISSUE_COUNT + 1):
        yield (
            f"issue-{index:05d}", "stress-job", index, f"{index}.png", "caption", "decode_failed", "error", 0, 1,
            "caption", "bounded", "[]", 1, None, NOW,
        )


def _export_artifact_rows():
    for sample_id in range(1, SAMPLE_COUNT + 1):
        yield ("stress-job", sample_id, "json", f"prepared\\export\\lease-{sample_id}.json", "a" * 64)
        yield ("stress-job", sample_id, "txt", f"prepared\\export\\lease-{sample_id}.txt", "b" * 64)


def _count_evidence_rows():
    for sample_id in range(1, SAMPLE_COUNT + 1):
        yield ("stress-job", sample_id, 1, "solo", COUNT_DECISION_JSON, "[]", NOW, NOW)


def _count_observation_rows():
    for sample_id in range(1, SAMPLE_COUNT + 1):
        count = "duo" if sample_id % 10 == 0 else "solo"
        yield (
            "stress-job", sample_id, 1, "observed", count, "single_scene", 0, "[]", None, NOW, NOW,
        )


def _count_review_decision_rows():
    for sample_id in range(1, SAMPLE_COUNT + 1):
        if sample_id % 10 == 0:
            yield (
                "stress-job", sample_id, 1, "pending", None, None,
                '["count_observation_mismatch"]', 1, None, None, NOW, NOW,
            )
        else:
            yield (
                "stress-job", sample_id, 1, "auto_resolved", "solo", "consensus",
                "[]", 1, NOW, None, NOW, NOW,
            )


def _fake_ocr_sidecar_metadata(row: object) -> dict[str, object]:
    """Build one transient OCR outcome without decoding an image or calling Paddle."""
    sample_id = int(row["sample_id"])  # type: ignore[index]
    relative_path = str(row["relative_image_path"])  # type: ignore[index]
    if sample_id % 10 == 0:
        return {"relativeImagePath": relative_path, "status": "no_text", "items": []}
    return {
        "relativeImagePath": relative_path,
        "status": "success",
        "items": [{"index": 0, "text": f"fake-{sample_id}", "position": "center"}],
    }


def _insert_batched(
    database: StateDatabase,
    statement: str,
    rows: Iterable[tuple[object, ...]],
    *,
    batch_size: int = 500,
) -> None:
    iterator = iter(rows)
    while batch := list(islice(iterator, batch_size)):
        with database.transaction():
            database.connection.executemany(statement, batch)


class ControlPlane100kTests(unittest.TestCase):
    def test_v9_fake_ocr_metadata_uses_keyset_pages_and_one_resident_lease(self) -> None:
        self.assertEqual(
            "sample.png",
            _fake_ocr_sidecar_metadata({"sample_id": 1, "relative_image_path": "sample.png"})["relativeImagePath"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state.db"
            database = StateDatabase.open(database_path)
            try:
                config = JobConfig(
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot=str(root),
                    schemaVersion=9,
                )
                config.caption["enabled"] = config.classify["enabled"] = config.replace["enabled"] = False
                config.ocr["enabled"] = True
                config.nl["enabled"] = config.dropout["enabled"] = False
                config.countReview["enabled"] = False  # type: ignore[index]
                config.tokenBudget["enabled"] = False  # type: ignore[index]
                frozen = config.to_dict()
                job = _job(root)
                job.update({
                    "config_schema_version": config.schemaVersion,
                    "config_json": json.dumps(frozen),
                    "config_hash": config.config_hash,
                    "status": "running",
                    "current_module_id": "ocr",
                })
                database.insert_job(job)
                tracemalloc.start()
                database.insert_samples("stress-job", _sample_rows(), batch_size=500)
                database.initialize_module_summary("stress-job", "ocr", total=SAMPLE_COUNT)
                database.set_module_summary("stress-job", "ocr", status="running")
                database.connection.execute(
                    "UPDATE sample_state SET current_module_id='ocr',status='pending' WHERE job_id='stress-job'"
                )

                cursor: int | None = None
                read = success = no_text = 0
                largest_page = 0
                while True:
                    page = database.page_samples("stress-job", after_sample_id=cursor, limit=500)
                    self.assertFalse(database.connection.in_transaction)
                    if not page:
                        break
                    largest_page = max(largest_page, len(page))
                    metadata = [_fake_ocr_sidecar_metadata(row) for row in page]
                    self.assertLessEqual(len(metadata), 500)
                    self.assertTrue(all("imagePath" not in item for item in metadata))
                    success += sum(item["status"] == "success" for item in metadata)
                    no_text += sum(item["status"] == "no_text" for item in metadata)
                    read += len(metadata)
                    cursor = int(page[-1]["sample_id"])

                scheduler = BoundedScheduler(database, lease_id_factory=lambda: "stress-ocr-lease")
                maximum = MODULE_QUEUE_LIMITS["ocr"]
                self.assertEqual((1, 1), (maximum.lease_batch_size, maximum.max_resident_pages))
                with self.assertRaises(SchedulerError):
                    scheduler.claim_batch("stress-job", "ocr", "stress-ocr", config.config_hash, limit=2)
                largest_lease_batch = 0
                for _ in range(3):
                    leases = scheduler.claim_batch("stress-job", "ocr", "stress-ocr", config.config_hash)
                    largest_lease_batch = max(largest_lease_batch, len(leases))
                    self.assertEqual(1, len(leases))
                    scheduler.complete(leases[0])

                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertEqual(SAMPLE_COUNT, read)
                self.assertEqual(SAMPLE_COUNT, success + no_text)
                self.assertLessEqual(largest_page, 500)
                self.assertLessEqual(largest_lease_batch, maximum.lease_batch_size * maximum.max_resident_pages)
                self.assertLess(peak, 32 * 1024 * 1024)
                database.checkpoint(truncate=True)
                wal_path = database_path.with_name(f"{database_path.name}-wal")
                wal_size = wal_path.stat().st_size if wal_path.exists() else 0
                database_size = database_path.stat().st_size
                print(
                    "ocr-control-plane-100k metrics: "
                    f"peak_memory={peak} database={database_size} wal_after_truncate={wal_size} "
                    f"success={success} no_text={no_text}"
                )
                self.assertLessEqual(wal_size, WAL_SIZE_LIMIT_BYTES)
                self.assertLess(database_size, MAX_DATABASE_BYTES)
            finally:
                database.close()

    def test_v3_samples_count_control_plane_and_issues_are_bounded_and_keyset_paged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state.db"
            database = StateDatabase.open(database_path)
            try:
                database.insert_job(_job(root))
                tracemalloc.start()
                database.insert_samples("stress-job", _sample_rows(), batch_size=500)
                database.connection.execute(
                    "UPDATE sample_state SET current_module_id='count_review',status='pending' WHERE job_id='stress-job'"
                )
                cursor: int | None = None
                read = 0
                largest_page = 0
                while True:
                    page = database.page_samples("stress-job", after_sample_id=cursor, limit=500)
                    self.assertFalse(database.connection.in_transaction)
                    if not page:
                        break
                    largest_page = max(largest_page, len(page))
                    read += len(page)
                    cursor = int(page[-1]["sample_id"])
                _insert_batched(
                    database,
                    """INSERT INTO issues(issue_id,job_id,sample_id,relative_image_path,module_id,code,severity,blocking,retriable,
                       repair_start_module,message,field_errors_json,attempt,resolved_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    _issue_rows(),
                )
                _insert_batched(
                    database,
                    """INSERT INTO count_evidence(
                           job_id,sample_id,schema_version,value,decision_json,
                           review_warning_codes_json,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    _count_evidence_rows(),
                )
                _insert_batched(
                    database,
                    """INSERT INTO count_observations(
                           job_id,sample_id,schema_version,status,count_value,layout_value,
                           same_character_repeated,warning_codes_json,not_requested_reason,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    _count_observation_rows(),
                )
                _insert_batched(
                    database,
                    """INSERT INTO count_review_decisions(
                           job_id,sample_id,schema_version,status,final_count,selected_source,
                           review_reasons_json,version,resolved_at,applied_at,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    _count_review_decision_rows(),
                )
                self.assertEqual(SAMPLE_COUNT, database.count("samples", "stress-job"))
                self.assertEqual(SAMPLE_COUNT, database.count("sample_state", "stress-job"))
                self.assertEqual(ISSUE_COUNT, database.count("issues", "stress-job"))
                for statement in (
                    "SELECT COUNT(*) FROM count_evidence WHERE job_id=?",
                    "SELECT COUNT(*) FROM count_observations WHERE job_id=?",
                    "SELECT COUNT(*) FROM count_review_decisions WHERE job_id=?",
                ):
                    count = database.connection.execute(
                        statement, ("stress-job",)
                    ).fetchone()[0]
                    self.assertEqual(SAMPLE_COUNT, count)
                self.assertEqual(COUNT_REVIEW_PENDING, database.count_current_review_decisions("stress-job", status="pending"))
                with self.assertRaises(sqlite3.IntegrityError):
                    database.connection.execute(
                        "INSERT INTO samples SELECT * FROM samples WHERE job_id='stress-job' AND sample_id=1"
                    )
                self.assertEqual(SAMPLE_COUNT, read)
                self.assertLessEqual(largest_page, 500)
                for page_method in (
                    database.page_count_evidence,
                    database.page_count_observations,
                    database.page_count_review_decisions,
                    database.page_count_review_inputs,
                    database.page_count_review_items,
                ):
                    count_cursor: int | None = None
                    count_read = 0
                    largest_count_page = 0
                    while True:
                        page = page_method("stress-job", after_sample_id=count_cursor, limit=500)
                        self.assertFalse(database.connection.in_transaction)
                        if not page:
                            break
                        largest_count_page = max(largest_count_page, len(page))
                        count_read += len(page)
                        count_cursor = int(page[-1]["sample_id"])
                    self.assertEqual(SAMPLE_COUNT, count_read)
                    self.assertLessEqual(largest_count_page, 500)
                    with self.assertRaises(ValueError):
                        page_method("stress-job", limit=501)
                mismatch_cursor: int | None = None
                mismatch_read = 0
                while True:
                    page = database.page_count_review_items(
                        "stress-job", after_sample_id=mismatch_cursor, mismatch_only=True, limit=500,
                    )
                    self.assertFalse(database.connection.in_transaction)
                    if not page:
                        break
                    mismatch_read += len(page)
                    mismatch_cursor = int(page[-1]["sample_id"])
                self.assertEqual(COUNT_REVIEW_PENDING, mismatch_read)
                _insert_batched(
                    database,
                    """INSERT INTO export_artifacts(job_id,sample_id,kind,relative_path,sha256)
                       VALUES (?,?,?,?,?)""",
                    _export_artifact_rows(),
                    batch_size=1_000,
                )
                export_cursor: int | None = None
                export_read = 0
                largest_export_page = 0
                while True:
                    page = database.page_export_artifact_groups("stress-job", after_sample_id=export_cursor, limit=500)
                    self.assertFalse(database.connection.in_transaction)
                    if not page:
                        break
                    largest_export_page = max(largest_export_page, len(page))
                    sample_ids = {int(row["sample_id"]) for row in page}
                    self.assertEqual(2 * len(sample_ids), len(page))
                    export_read += len(sample_ids)
                    export_cursor = int(page[-1]["sample_id"])
                self.assertEqual(SAMPLE_COUNT, export_read)
                self.assertLessEqual(largest_export_page, 1_000)
                issue_cursor: int | None = None
                issues_read = 0
                while True:
                    page = database.page_issues("stress-job", after_sample_id=issue_cursor, limit=200)
                    self.assertFalse(database.connection.in_transaction)
                    if not page:
                        break
                    issues_read += len(page)
                    issue_cursor = int(page[-1]["sample_id"])
                self.assertEqual(ISSUE_COUNT, issues_read)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertLess(peak, 32 * 1024 * 1024)
                self.assertEqual("wal", database.connection.execute("PRAGMA journal_mode").fetchone()[0].lower())
                self.assertEqual(WAL_SIZE_LIMIT_BYTES, database.connection.execute("PRAGMA journal_size_limit").fetchone()[0])
                self.assertEqual(1_000, database.connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0])
                database.checkpoint(truncate=True)
                wal_path = database_path.with_name(f"{database_path.name}-wal")
                wal_size = wal_path.stat().st_size if wal_path.exists() else 0
                database_size = database_path.stat().st_size
                print(
                    "control-plane-100k metrics: "
                    f"peak_memory={peak} database={database_size} wal_after_truncate={wal_size}"
                )
                self.assertLessEqual(wal_size, WAL_SIZE_LIMIT_BYTES)
                self.assertLess(database_size, MAX_DATABASE_BYTES)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
