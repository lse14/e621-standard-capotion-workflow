from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .db import StateDatabase
from .overlay import OverlayLayout
from .state_machine import can_discard, require_discard_confirmation


class JobLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscardResult:
    jobId: str
    overlayDeleted: bool


class JobLifecycle:
    """Explicit user-facing lifecycle operations outside worker business logic."""

    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    def ensure_delete_allowed(self, job_id: str) -> None:
        if self.database.has_repair_children(job_id):
            raise JobLifecycleError("请先删除修复子任务")

    def discard(self, job_id: str, *, confirmed: bool) -> DiscardResult:
        require_discard_confirmation(confirmed)
        job = self.database.get_job(job_id)
        overlay_deleted = False
        overlay_value = job["overlay_root"]
        journal_state: str | None = None
        layout: OverlayLayout | None = None
        if overlay_value:
            root = Path(str(overlay_value))
            if root.exists():
                try:
                    layout = OverlayLayout.open_existing(root, job_id)
                    journal_state = layout.journal_state()
                except Exception as exc:
                    raise JobLifecycleError("overlay cannot be safely inspected for discard") from exc
        elif job["commit_journal_path"]:
            raise JobLifecycleError("job has a journal pointer without a recoverable overlay")
        with self.database.transaction(immediate=True):
            self.ensure_delete_allowed(job_id)
            current = self.database.get_job(job_id)
            if not can_discard(str(current["status"]), journal_state=journal_state):
                raise JobLifecycleError("job cannot be discarded before recovery and journal resolution")
            if layout is not None:
                layout.discard()
                overlay_deleted = True
            self.database.set_job_status(job_id, "discarded")
            # A discarded job can never run again, so it must stop claiming its dataset.
            self.database.release_dataset_claim(job_id)
        return DiscardResult(job_id, overlay_deleted)
