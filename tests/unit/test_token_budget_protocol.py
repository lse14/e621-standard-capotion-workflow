from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE_SOURCE = ROOT / "core" / "src"
SHARED_SOURCE = ROOT / "shared" / "anima_caption_format"
PROTOCOL_SOURCE = CORE_SOURCE / "anima_core" / "token_budget_protocol.py"
sys.path.insert(0, str(SHARED_SOURCE))
sys.path.insert(0, str(CORE_SOURCE))

from anima_caption_format import flat_txt_sha256, serialize_flat_txt
from anima_caption_format.normalizer import CaptionDisplayPolicy


CAPTION_FORMAT = {
    "replaceUnderscoresWithSpaces": True,
    "preserveEscapes": True,
    "triggersEnabled": False,
    "triggerTerms": [],
}


def annotation(*, tags: list[str] | None = None, character: str = "Hero") -> dict[str, object]:
    return {
        "quality": [],
        "count": "solo",
        "character": character,
        "series": "Series",
        "artist": "Artist",
        "appearance": [],
        "tags": ["first_tag", "tail_tag"] if tags is None else tags,
        "environment": [],
        "nl": "A caption.",
    }


class TokenBudgetProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(PROTOCOL_SOURCE.is_file(), "Token Budget Core protocol source is missing")
        from anima_core.token_budget_protocol import TokenBudgetProtocolError, validate_token_budget_outcome

        self.error = TokenBudgetProtocolError
        self.validate = validate_token_budget_outcome
        self.policy = CaptionDisplayPolicy.from_mapping(CAPTION_FORMAT)
        self.original = annotation()
        self.final = annotation(tags=["first_tag"])

    def _trimmed(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "payloadType": "token_budget_outcome",
            "sampleId": 7,
            "leaseId": "lease-7",
            "status": "trimmed",
            "originalTokens": 11,
            "finalTokens": 7,
            "removed": {"quality": [], "environment": [], "tags": ["tail_tag"], "appearance": []},
            "annotation": copy.deepcopy(self.final),
            "flatTextSha256": flat_txt_sha256(self.final, self.policy),
        }

    def _validate(self, value: object, *, max_tokens: int = 7) -> object:
        return self.validate(
            value,
            expected_sample_id=7,
            expected_lease_id="lease-7",
            original_annotation=self.original,
            caption_format=CAPTION_FORMAT,
            max_tokens=max_tokens,
        )

    def test_accepts_an_exact_trimmed_outcome_and_recomputes_the_flat_hash(self) -> None:
        parsed = self._validate(self._trimmed())

        self.assertEqual("trimmed", parsed.status)
        self.assertEqual(self.final, parsed.annotation)
        self.assertEqual(
            hashlib.sha256(serialize_flat_txt(self.final, self.policy)).hexdigest(),
            parsed.flat_text_sha256,
        )

    def test_rejects_untrusted_identity_hash_and_tail_audit_changes(self) -> None:
        for label, changed in (
            ("sample", {"sampleId": 8}),
            ("lease", {"leaseId": "lease-8"}),
            ("hash", {"flatTextSha256": "0" * 64}),
            ("audit", {"removed": {"quality": [], "environment": [], "tags": ["first_tag"], "appearance": []}}),
            ("protected", {"annotation": annotation(tags=["first_tag"], character="Changed")}),
        ):
            with self.subTest(label=label):
                candidate = self._trimmed()
                candidate.update(changed)
                with self.assertRaises(self.error):
                    self._validate(candidate)

    def test_rejects_bool_nan_and_oversize_frames(self) -> None:
        cases = []
        bool_tokens = self._trimmed()
        bool_tokens["finalTokens"] = True
        cases.append(bool_tokens)
        nan_tokens = self._trimmed()
        nan_tokens["originalTokens"] = float("nan")
        cases.append(nan_tokens)
        oversize = self._trimmed()
        oversize["annotation"] = annotation(tags=["first_tag"])
        oversize["annotation"]["nl"] = "x" * 1_048_576
        cases.append(oversize)

        for candidate in cases:
            with self.subTest(candidate=candidate.get("finalTokens", candidate.get("originalTokens", "frame"))):
                with self.assertRaises(self.error):
                    self._validate(candidate)

    def test_overflow_has_no_committable_annotation_or_hash_and_reports_all_tail_removals(self) -> None:
        value = {
            "schemaVersion": 1,
            "payloadType": "token_budget_outcome",
            "sampleId": 7,
            "leaseId": "lease-7",
            "status": "overflow",
            "originalTokens": 11,
            "finalTokens": 8,
            "removed": {"quality": [], "environment": [], "tags": ["first_tag", "tail_tag"], "appearance": []},
        }

        parsed = self._validate(value)

        self.assertEqual("overflow", parsed.status)
        self.assertIsNone(parsed.annotation)
        self.assertIsNone(parsed.flat_text_sha256)

    def test_runtime_manifest_declares_the_isolated_token_budget_owner(self) -> None:
        from anima_core.runtime_manifest import RUNTIME_OWNERS, runtime_lifecycle

        self.assertIn("token-budget", RUNTIME_OWNERS)
        self.assertEqual("assembled", runtime_lifecycle("token-budget"))


if __name__ == "__main__":
    unittest.main()
