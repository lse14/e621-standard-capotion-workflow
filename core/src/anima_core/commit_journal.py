"""Strict module-6 commit journal schema and crash-recovery classification."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .overlay import OverlayLayout, OverlayError
from .path_safety import canonicalize, windows_paths_equal

JournalState = Literal["prepared", "backup_verified", "rollback_created", "committed", "rollback_required", "rolled_back"]
STATES = frozenset({"prepared", "backup_verified", "rollback_created", "committed", "rollback_required", "rolled_back"})

class CommitJournalError(RuntimeError): pass

@dataclass(frozen=True)
class CommitJournal:
    job_id: str; state: JournalState; dataset_root: Path; staging_root: Path; rollback_root: Path; backup_zip: Path
    @classmethod
    def from_value(cls, value: object, *, job_id: str) -> "CommitJournal":
        if not isinstance(value,dict) or set(value)!={"schemaVersion","jobId","state","datasetRoot","stagingRoot","rollbackRoot","backupZip"}: raise CommitJournalError("commit journal fields are invalid")
        if value["schemaVersion"]!=1 or value["jobId"]!=job_id or value["state"] not in STATES: raise CommitJournalError("commit journal identity is invalid")
        try:
            dataset=canonicalize(value["datasetRoot"],must_exist=False,directory=True).value; staging=canonicalize(value["stagingRoot"],must_exist=False,directory=True).value; rollback=canonicalize(value["rollbackRoot"],must_exist=False,directory=True).value; backup=canonicalize(value["backupZip"],must_exist=False,directory=False).value
        except Exception as exc: raise CommitJournalError("commit journal paths are invalid") from exc
        expected_backup_directory = dataset.parent / f".{dataset.name}.anima-backups"
        if (
            staging.parent != dataset.parent or rollback.parent != dataset.parent
            or backup.parent != expected_backup_directory
            or windows_paths_equal(dataset, staging) or windows_paths_equal(dataset, rollback)
        ):
            raise CommitJournalError("commit journal paths are not sibling-safe")
        return cls(job_id,value["state"],dataset,staging,rollback,backup)
    def to_value(self) -> dict[str,object]:
        return {"schemaVersion":1,"jobId":self.job_id,"state":self.state,"datasetRoot":str(self.dataset_root),"stagingRoot":str(self.staging_root),"rollbackRoot":str(self.rollback_root),"backupZip":str(self.backup_zip)}

def load(layout: OverlayLayout) -> CommitJournal | None:
    path=layout.commit_journal_path()
    if not path.exists(): return None
    try:
        import json; value=json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc: raise CommitJournalError("commit journal is unreadable") from exc
    return CommitJournal.from_value(value,job_id=layout.job_id)

def write(layout: OverlayLayout,journal: CommitJournal) -> Path:
    if journal.job_id!=layout.job_id or not windows_paths_equal(journal.dataset_root,layout.dataset_root): raise CommitJournalError("commit journal does not match overlay")
    try: return layout.write_journal(journal.to_value())
    except OverlayError as exc: raise CommitJournalError("commit journal cannot be persisted") from exc

def recovery_action(journal: CommitJournal) -> Literal["finish_commit","restore_rollback","verify_committed"]:
    if journal.state in {"prepared","backup_verified","rollback_required"}: return "restore_rollback"
    if journal.state=="rollback_created": return "finish_commit"
    return "verify_committed"
