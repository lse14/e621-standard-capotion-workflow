from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.classify_overlay import (
    ClassifyJsonError,
    compose_classify_json,
    original_count,
    parse_annotation_json,
    serialize_annotation_json,
)
from anima_core.classify_protocol import (
    ClassifyCountDecisionV1,
    ClassifyProjectionV1,
    ClassifyResultV1,
)


def _result(count: str = "duo") -> ClassifyResultV1:
    return ClassifyResultV1(
        sampleId=1,
        leaseId="lease-1",
        relativeImagePath="nested\\sample.png",
        projection=ClassifyProjectionV1(
            quality=(), count=count, character="named_character", series="", artist="",
            appearance=("blue_fur",), tags=("duo",), environment=("forest",), nl="",
        ),
        countDecision=ClassifyCountDecisionV1(
            value=count, baseValue=count, selectedSource="wiki_tags", originalRaw=None,
            originalNormalized=None, wikiValue=count, matchedTags=(count,), conflict=False,
            issueCodes=(), warnings=(), appliedLowerBounds=(),
        ),
        inputTagCount=4,
        outputTagCount=4,
        droppedTagCount=0,
    )


class ClassifyOverlayTests(unittest.TestCase):
    def test_strict_json_parsing_and_legacy_flattening(self) -> None:
        self.assertIsNone(parse_annotation_json(None))
        self.assertIsNone(parse_annotation_json(b"\xef\xbb\xbf \t\r\n"))
        with self.assertRaises(ClassifyJsonError):
            parse_annotation_json(b'{"count":"solo","count":"duo"}')
        with self.assertRaises(ClassifyJsonError):
            parse_annotation_json(b'{"count":NaN}')
        with self.assertRaises(ClassifyJsonError):
            parse_annotation_json(b'{"appearance":"not-a-list"}')
        legacy = b'{"tags":{"quality":[],"count":"solo","character":"","series":"","artist":"","appearance":[],"tags":[],"environment":[],"nl":""}}'
        self.assertEqual("solo", original_count(parse_annotation_json(legacy)))
        with self.assertRaises(ClassifyJsonError):
            parse_annotation_json(b'{"tags":{"count":"solo"},"unknown":true}')

    def test_json_write_matrix_preserves_only_incremental_existing_non_count_values(self) -> None:
        result = _result()
        existing = {
            "quality": ["old_quality"], "count": "solo", "character": "old_character", "series": "old_series",
            "artist": "old_artist", "appearance": ["old_appearance"], "tags": ["old_tag"],
            "environment": ["old_environment"], "nl": "old NL", "extra": {"preserved": True},
        }
        incremental = compose_classify_json(existing, result, overwrite_mode="incremental", overwrite_json=False)
        self.assertEqual("duo", incremental["count"])
        for field in ("quality", "character", "series", "artist", "appearance", "tags", "environment", "nl", "extra"):
            self.assertEqual(existing[field], incremental[field], field)
        for overwrite_mode, overwrite_json in (("incremental", True), ("rebuild", False)):
            with self.subTest(overwrite_mode=overwrite_mode):
                expected = result.projection.to_dict()
                expected["character"] = "old_character"
                self.assertEqual(expected, compose_classify_json(
                    existing, result, overwrite_mode=overwrite_mode, overwrite_json=overwrite_json,
                ))
        self.assertEqual(result.projection.to_dict(), compose_classify_json(
            None, result, overwrite_mode="incremental", overwrite_json=False,
        ))

    def test_json_overwrite_and_rebuild_preserve_existing_character(self) -> None:
        existing = {"character": "old_character"}
        for overwrite_mode, overwrite_json in (("incremental", True), ("rebuild", False)):
            with self.subTest(overwrite_mode=overwrite_mode, overwrite_json=overwrite_json):
                output = compose_classify_json(
                    existing, _result(), overwrite_mode=overwrite_mode, overwrite_json=overwrite_json,
                )
                self.assertEqual("old_character", output["character"])
                self.assertEqual(("blue_fur",), tuple(output["appearance"]))

    def test_incremental_preservation_uses_the_fixed_nine_field_order(self) -> None:
        # T5: the preserved branch used to echo the existing key order, so the same payload
        # could serialize to different bytes.
        scrambled = {
            "extra": {"preserved": True}, "nl": "old NL", "tags": ["old_tag"], "artist": "old_artist",
            "another": 1, "character": "old_character",
        }
        preserved = compose_classify_json(scrambled, _result(), overwrite_mode="incremental", overwrite_json=False)
        self.assertEqual(
            ["count", "character", "artist", "tags", "nl", "extra", "another"], list(preserved),
        )
        self.assertEqual("duo", preserved["count"])

    def test_serialization_is_deterministic_utf8_with_a_single_lf(self) -> None:
        value = _result("solo").projection.to_dict()
        data = serialize_annotation_json(value)
        self.assertTrue(data.endswith(b"\n"))
        self.assertFalse(data.endswith(b"\n\n"))
        self.assertNotIn(b"\r", data)
        self.assertEqual(value, parse_annotation_json(data))


if __name__ == "__main__":
    unittest.main()
