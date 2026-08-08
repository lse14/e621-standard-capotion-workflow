from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.classify_protocol import ClassifyCountDecisionV1
from anima_core.count_review_protocol import (
    CountEvidenceV1,
    CountObservationV1,
    CountReviewProtocolError,
    classify_review_warning_codes,
    initial_count_review_decision,
)


def _decision() -> ClassifyCountDecisionV1:
    return ClassifyCountDecisionV1(
        value="duo",
        baseValue="solo",
        selectedSource="wiki_tags",
        originalRaw="solo",
        originalNormalized="solo",
        wikiValue="duo",
        matchedTags=("duo",),
        conflict=True,
        issueCodes=("count_character_lower_bound", "ordinary_diagnostic", "count_source_conflict"),
        warnings=("wiki_missing:e621:duo", "count_source_conflict:duplicate", "unrelated:detail"),
        appliedLowerBounds=("character",),
    )


class CountReviewProtocolTests(unittest.TestCase):
    def test_evidence_reparses_decision_and_keeps_only_ordered_review_warnings(self) -> None:
        evidence = CountEvidenceV1.from_decision(_decision())
        self.assertEqual(
            ("count_source_conflict", "count_character_lower_bound", "wiki_missing"),
            evidence.reviewWarningCodes,
        )
        self.assertEqual(evidence, CountEvidenceV1.from_dict(evidence.to_dict()))
        self.assertEqual(evidence.reviewWarningCodes, classify_review_warning_codes(evidence.decision))

    def test_evidence_rejects_missing_extra_or_forged_warning_fields(self) -> None:
        payload = CountEvidenceV1.from_decision(_decision()).to_dict()
        invalid = [
            {key: value for key, value in payload.items() if key != "value"},
            {**payload, "extra": True},
            {**payload, "value": "solo"},
            {**payload, "reviewWarningCodes": ["ordinary_diagnostic"]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CountReviewProtocolError):
                CountEvidenceV1.from_dict(value)

    def test_initial_decision_matrix_is_deterministic_and_ordered(self) -> None:
        def evidence(value: str, warnings: tuple[str, ...] = ()) -> CountEvidenceV1:
            return CountEvidenceV1(value, _decision(), warnings)

        def observation(status: str, count: str | None = None) -> CountObservationV1:
            if status == "not_requested":
                return CountObservationV1.not_requested("nl_disabled")
            return CountObservationV1.from_dict({
                "schemaVersion": 1,
                "status": status,
                "countValue": count,
                "layoutValue": "multi_view" if status == "observed" else None,
                "sameCharacterRepeated": True if status == "observed" else None,
                "warningCodes": ["count_observation_unknown"] if count == "unknown" else (["count_observation_invalid"] if status == "invalid" else []),
                "notRequestedReason": None,
            })

        cases = (
            (evidence("solo"), observation("observed", "solo"), ("auto_resolved", "solo", "consensus", ())),
            (evidence("solo"), observation("observed", "duo"), ("pending", None, None, ("count_observation_mismatch",))),
            (evidence("solo"), observation("observed", "unknown"), ("pending", None, None, ("count_observation_unknown",))),
            (evidence("solo"), observation("invalid"), ("pending", None, None, ("count_observation_invalid",))),
            (evidence("solo"), observation("not_requested"), ("auto_resolved", "solo", "classify", ())),
            (evidence(""), observation("observed", "duo"), ("auto_resolved", "duo", "vlm", ())),
            (evidence(""), observation("not_requested"), ("pending", None, None, ("count_observation_unknown",))),
            (evidence("solo", ("wiki_missing",)), observation("observed", "solo"), ("pending", None, None, ("wiki_missing",))),
            (
                evidence("solo", ("count_source_conflict", "wiki_missing")),
                observation("observed", "duo"),
                ("pending", None, None, ("count_source_conflict", "wiki_missing", "count_observation_mismatch")),
            ),
        )
        for count_evidence, count_observation, expected in cases:
            with self.subTest(expected=expected):
                decision = initial_count_review_decision(count_evidence, count_observation)
                self.assertEqual(expected, (decision.status, decision.finalCount, decision.selectedSource, decision.reviewReasons))
                self.assertEqual(decision, type(decision).from_dict(decision.to_dict()))


if __name__ == "__main__":
    unittest.main()
