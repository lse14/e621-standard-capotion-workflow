"""Creation of isolated, bounded issue-repair tasks."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contracts import utc_now
from .db import StateDatabase
from .job_preflight import config_from_dict
from .locks import DatasetLock
from .overlay import OverlayLayout
from .path_safety import windows_key


class RepairPreparationError(ValueError):
    pass


@dataclass(frozen=True)
class RepairPreparationResult:
    repairJobId: str
    parentJobId: str
    targetCount: int
    datasetRoot: str
    overlayRoot: str


class RepairPreparationService:
    """Creates a new task state without copying the complete dataset again."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._locks: dict[str, DatasetLock] = {}

    @staticmethod
    def _job_row(repair_job_id: str, parent: object, dataset: Path) -> dict[str, object]:
        return {
            "job_id": repair_job_id,
            "config_schema_version": parent["config_schema_version"], "config_json": parent["config_json"],
            "config_hash": parent["config_hash"], "profile": parent["profile"],
            "work_mode": parent["work_mode"], "overwrite_mode": parent["overwrite_mode"],
            "source_root": str(dataset), "output_root": parent["output_root"], "dataset_root": str(dataset),
            "dataset_root_key": windows_key(dataset), "manifest_schema_version": 1, "recursive": parent["recursive"],
            "sample_count": 0, "manifest_generated_at": None, "status": "preflighting", "current_module_id": None,
            "last_event_id": 0, "pinned": 0, "api_budget_extra": 0, "api_budget_revision": 0,
            "overlay_root": None, "commit_journal_path": None, "resume_status": None,
            "created_at": utc_now(), "started_at": None, "cancel_requested_at": None,
            "finished_at": None,
        }

    @staticmethod
    def _copy_parent_manifest(database: StateDatabase, parent_job_id: str, repair_job_id: str) -> int:
        """Reuse the parent's frozen manifest instead of decoding the dataset again.

        A repair task runs against the same locked dataset, so re-running the
        full image preflight would cost two opens per image for every sample.
        """
        cursor: int | None = None
        count = 0
        while True:
            page = database.page_samples(parent_job_id, after_sample_id=cursor, limit=500)
            if not page:
                break
            cursor = int(page[-1]["sample_id"])
            count += database.insert_samples(repair_job_id, [dict(row) for row in page], batch_size=500)
        database.connection.execute(
            "UPDATE jobs SET sample_count=?,manifest_generated_at=? WHERE job_id=?",
            (count, utc_now(), repair_job_id),
        )
        return count

    def prepare(self, parent_job_id: str) -> RepairPreparationResult:
        database = StateDatabase.open(self.database_path)
        layout: OverlayLayout | None = None
        acquired: DatasetLock | None = None
        retain_database = False
        repair_job_id = uuid.uuid4().hex
        try:
            parent = database.get_job(parent_job_id)
            if parent["status"] not in {"reviewing", "failed"}:
                raise RepairPreparationError("only a reviewed or failed task can create a repair task")
            try:
                config = config_from_dict(json.loads(str(parent["config_json"])))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RepairPreparationError("parent frozen JobConfig is invalid") from exc
            if (
                config.config_hash != parent["config_hash"]
                or config.profile != parent["profile"]
                or config.schemaVersion != int(parent["config_schema_version"])
            ):
                raise RepairPreparationError("parent frozen JobConfig identity does not match")
            dataset = Path(str(parent["dataset_root"]))
            database.insert_job(self._job_row(repair_job_id, parent, dataset))
            acquired = DatasetLock.acquire(database, dataset, repair_job_id)
            try:
                self._copy_parent_manifest(database, parent_job_id, repair_job_id)
            except (OSError, ValueError) as exc:
                raise RepairPreparationError("current formal dataset cannot produce a repair manifest") from exc
            database.create_repair_link(repair_job_id, parent_job_id)
            cursor: int | None = None
            while True:
                page = database.stage_repair_target_page(repair_job_id, parent_job_id, after_sample_id=cursor, limit=500)
                if not page:
                    break
                cursor = database.repair_parent_cursor(repair_job_id)
            target_count = database.count_repair_targets(repair_job_id)
            if target_count == 0:
                raise RepairPreparationError("task has no eligible retriable sample issues")
            try:
                database.inherit_repair_count_evidence(repair_job_id, parent_job_id)
            except ValueError as exc:
                raise RepairPreparationError(str(exc)) from exc
            layout = OverlayLayout.create(dataset, repair_job_id)
            database.set_workspace_metadata(repair_job_id, dataset_root=str(dataset), dataset_root_key=windows_key(dataset), overlay_root=str(layout.root))
            database.set_job_status(repair_job_id, "preparing_workspace", current_module_id="workspace")
            self._locks[repair_job_id] = acquired
            acquired = None
            retain_database = True
            return RepairPreparationResult(repair_job_id, parent_job_id, target_count, str(dataset), str(layout.root))
        except Exception:
            if layout is not None and layout.root.exists():
                layout.discard()
            try:
                database.delete_job_control_record(repair_job_id)
            except KeyError:
                pass
            if acquired is not None:
                acquired.release(recovery_complete=True)
            raise
        finally:
            if not retain_database:
                database.close()

    def close(self) -> None:
        for lock in tuple(self._locks.values()):
            try:
                lock.release(recovery_complete=True)
            finally:
                lock.database.close()
        self._locks.clear()
