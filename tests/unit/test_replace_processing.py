from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "replace" / "src"))

from anima_replace_worker.replacement import ReplacementRule, replace_projection, rule_from_csv


def _projection() -> dict[str, object]:
    return {
        "quality": ["keep_pipe", "old"], "count": "duo", "character": "old, drop, old",
        "series": "series", "artist": "artist", "appearance": ["pair", "keep_pipe"],
        "tags": ["drop", "pair", "unknown", "pair"], "environment": [], "nl": "unchanged",
    }


class ReplacementProcessingTests(unittest.TestCase):
    def test_keep_replace_drop_and_passthrough_are_single_round_and_ordered(self) -> None:
        # F02: this case previously asserted field scoped dedup, so ":|" appeared in both
        # quality and appearance and "left"/"right" in both appearance and tags.
        rules = {
            "keep_pipe": rule_from_csv("keep", ":|"),
            "pair": rule_from_csv("replace", "left|right"),
            "drop": rule_from_csv("drop", ""),
            "old": ReplacementRule("replace", ("pair",)),
        }
        result, summary = replace_projection(_projection(), rules)
        self.assertEqual([":|", "pair"], result["quality"])
        self.assertEqual(["left", "right"], result["appearance"])
        self.assertEqual(["unknown"], result["tags"])
        self.assertEqual("", result["character"])
        self.assertEqual((6, 2, 1, 2), (summary.replaced, summary.dropped, summary.passthrough, summary.keep_rewritten))
        self.assertEqual(("duo", "series", "artist", "unchanged"), (
            result["count"], result["series"], result["artist"], result["nl"],
        ))

    def test_cross_field_collisions_are_deduped_by_the_frozen_field_priority(self) -> None:
        # F02 regression: the real index replaces overweight -> fat and overweight_anthro ->
        # fat|furry, so field scoped dedup emitted "fat" in both tags and appearance and
        # Export blocked the whole job with cross_field_tag_collision.
        rules = {
            "overweight": rule_from_csv("replace", "fat"),
            "overweight_anthro": rule_from_csv("replace", "fat|furry"),
            "wink": rule_from_csv("replace", "one_eye_closed"),
            "one_eye_closed": rule_from_csv("keep", "one_eye_closed"),
        }
        projection = {
            "quality": ["one_eye_closed"], "count": "solo", "character": "solo_focus", "series": "", "artist": "",
            "appearance": ["overweight_anthro"], "tags": ["overweight", "wink"], "environment": ["Furry"], "nl": "",
        }
        result, summary = replace_projection(projection, rules)
        self.assertEqual(["one_eye_closed"], result["quality"])
        self.assertEqual("solo_focus", result["character"])
        self.assertEqual(["fat", "furry"], result["appearance"])
        self.assertEqual([], result["tags"])
        self.assertEqual([], result["environment"])
        emitted = [tag for field in ("quality", "appearance", "tags", "environment") for tag in result[field]]
        emitted.extend(tag.strip() for tag in str(result["character"]).split(",") if tag.strip())
        self.assertEqual(len(emitted), len({tag.casefold() for tag in emitted}))
        self.assertEqual((3, 0, 2, 0), (summary.replaced, summary.dropped, summary.passthrough, summary.keep_rewritten))

    def test_only_replace_splits_pipe_and_invalid_csv_rules_are_rejected(self) -> None:
        self.assertEqual(("a|b",), rule_from_csv("keep", "a|b").replacement_tags)
        self.assertEqual(("a", "b"), rule_from_csv("replace", "a|b").replacement_tags)
        for action, tags in (("drop", "x"), ("replace", "a||b"), ("keep", ""), ("unknown", "x")):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    rule_from_csv(action, tags)


if __name__ == "__main__":
    unittest.main()
