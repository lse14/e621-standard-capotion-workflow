from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.commit_journal import CommitJournal
from anima_core.export_summary import build_export_summary, commit_status


class ExportSummaryTests(unittest.TestCase):
    def _journal(self, state: str) -> CommitJournal:
        root = Path(tempfile.gettempdir()) / "anima-export-summary"
        return CommitJournal("job", state, root / "dataset", root / ".dataset.stage", root / ".dataset.rollback", root / ".dataset.anima-backups" / "job.zip")  # type: ignore[arg-type]

    def test_every_persisted_journal_state_has_one_frozen_ui_status(self) -> None:
        expected = {
            "prepared": "staged", "backup_verified": "backup_verified", "rollback_created": "committing",
            "committed": "committed", "rolled_back": "rolled_back", "rollback_required": "rollback_required",
        }
        for state, status in expected.items():
            self.assertEqual(status, commit_status(job_status="committing", module_status="completed", journal=self._journal(state)))

    def test_summary_never_claims_export_before_directory_commit(self) -> None:
        module = {"status": "completed", "completed": 12, "failed": 3, "skipped": 4, "issue_count": 3}
        staging = build_export_summary(job_id="job", format_value="both", job_status="exporting", module_summary=module, journal=None)
        self.assertEqual(("staging", 19, 12, 3, 0, 4, 3), (staging.commitStatus, staging.scanned, staging.valid, staging.invalid, staging.exported, staging.skipped, staging.issueCount))
        committed = build_export_summary(job_id="job", format_value="both", job_status="succeeded", module_summary=module, journal=self._journal("committed"))
        self.assertEqual(("committed", 12), (committed.commitStatus, committed.exported))

    def test_safe_conversions_are_reported_by_type_and_by_sample(self) -> None:
        # F32: ROADMAP.md:988 — aggregated counts only, never a per-sample log.
        module = {"status": "completed", "completed": 12, "failed": 0, "skipped": 0, "issue_count": 0}
        diagnostics = [
            {"code": "samples_converted", "count": 7},
            {"code": "array_duplicate_removed", "count": 9},
            {"code": "array_string_split", "count": 4},
            {"code": "some_unrelated_diagnostic", "count": 3},
        ]
        summary = build_export_summary(
            job_id="job", format_value="both", job_status="exporting",
            module_summary=module, journal=None, diagnostics=diagnostics,
        ).to_dict()
        self.assertEqual(7, summary["convertedSamples"])
        # Frozen vocabulary order, unrelated diagnostics excluded.
        self.assertEqual(["array_string_split", "array_duplicate_removed"], list(summary["conversions"]))
        self.assertEqual({"array_string_split": 4, "array_duplicate_removed": 9}, summary["conversions"])
        empty = build_export_summary(job_id="job", format_value="json", job_status="exporting", module_summary=module, journal=None).to_dict()
        self.assertEqual((0, {}), (empty["convertedSamples"], empty["conversions"]))

