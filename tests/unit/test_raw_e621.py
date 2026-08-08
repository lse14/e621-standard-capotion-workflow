from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.raw_e621 import RawE621JsonError, parse_raw_e621_annotation


RAW_GROUPS = (
    "artist", "character", "contributor", "copyright", "general",
    "invalid", "lore", "meta", "species",
)


def _raw_json(**overrides: object) -> bytes:
    value: dict[str, object] = {group: [] for group in RAW_GROUPS}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class RawE621AnnotationTests(unittest.TestCase):
    def test_parses_exact_groups_and_builds_classify_tags(self) -> None:
        annotation = parse_raw_e621_annotation(_raw_json(
            artist=["kannos"],
            character=["wolf_character"],
            contributor=["uploader"],
            copyright=["example_series"],
            general=["solo", "blue_fur", "solo"],
            invalid=["bad"],
            lore=["lore_tag"],
            meta=["cool_colors"],
            species=["wolf"],
        ))

        assert annotation is not None
        self.assertEqual("kannos", annotation.artist)
        self.assertEqual("wolf_character", annotation.character)
        self.assertEqual(
            ("example_series", "solo", "blue_fur", "cool_colors", "wolf", "wolf_character"),
            annotation.classify_tags,
        )

    def test_preserves_raw_artist_and_character_order(self) -> None:
        annotation = parse_raw_e621_annotation(_raw_json(
            artist=["first_artist", "second_artist"],
            character=["first_character", "second_character"],
        ))

        assert annotation is not None
        self.assertEqual("first_artist, second_artist", annotation.artist)
        self.assertEqual("first_character, second_character", annotation.character)

    def test_non_candidate_standard_json_returns_none(self) -> None:
        self.assertIsNone(parse_raw_e621_annotation(b'{"artist":"kannos","count":"solo"}'))

    def test_rejects_raw_candidate_with_wrong_group_shape(self) -> None:
        with self.assertRaisesRegex(RawE621JsonError, "artist"):
            parse_raw_e621_annotation(_raw_json(artist="kannos"))

        with self.assertRaisesRegex(RawE621JsonError, "unexpected"):
            parse_raw_e621_annotation(_raw_json(unexpected=[]))

    def test_rejects_duplicate_keys_and_unsafe_tags(self) -> None:
        duplicate = b'{"artist":[],"artist":[],"character":[],"contributor":[],"copyright":[],"general":[],"invalid":[],"lore":[],"meta":[],"species":[]}'
        with self.assertRaisesRegex(RawE621JsonError, "duplicate"):
            parse_raw_e621_annotation(duplicate)

        with self.assertRaisesRegex(RawE621JsonError, "general"):
            parse_raw_e621_annotation(_raw_json(general=["tag,break"]))


if __name__ == "__main__":
    unittest.main()
