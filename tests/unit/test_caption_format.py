from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "anima_caption_format"))

import anima_caption_format
from anima_caption_format import flat_txt_sha256, normalize_annotation, serialize_flat_txt
from anima_caption_format.flat_txt import flat_txt_sha256 as flat_txt_module_sha256
from anima_caption_format.normalizer import CaptionDisplayPolicy, normalize_annotation as normalizer_annotation


POLICY = CaptionDisplayPolicy(True, True, True, ("anima_style",))
RAW = {
    "quality": [],
    "count": "duo",
    "character": "初音_ミク, 博丽_灵梦",
    "series": "東方_Project_\\(series\\)",
    "artist": "artist_(name)",
    "appearance": [],
    "tags": ["笑顔", "long_hair"],
    "environment": [],
    "nl": "初音ミク smiles, happily.",
}


class CaptionFormatTests(unittest.TestCase):
    def test_public_api_is_declared_at_the_shared_module_owners(self) -> None:
        self.assertEqual(
            ["flat_txt_sha256", "normalize_annotation", "serialize_flat_txt"],
            anima_caption_format.__all__,
        )
        self.assertIs(normalize_annotation, normalizer_annotation)
        self.assertIs(flat_txt_sha256, flat_txt_module_sha256)

    def test_shared_public_api_preserves_frozen_json_and_flat_txt_bytes(self) -> None:
        result = normalize_annotation(json.dumps(RAW, ensure_ascii=False).encode("utf-8"), POLICY)

        self.assertTrue(result.valid)
        self.assertEqual(286, len(result.json_bytes))
        self.assertEqual(
            "7ee3dffc5b94d10686de2923507ab9614f0aff969f9bb031843cd403a10771fd",
            hashlib.sha256(result.json_bytes).hexdigest(),
        )
        txt = serialize_flat_txt(result.payload, POLICY)
        self.assertIsInstance(txt, bytes)
        self.assertEqual(150, len(txt))
        self.assertEqual(
            "856230f539c598f803112649eb2021f941cedf4944416d78fe18eb791d001848",
            flat_txt_sha256(result.payload, POLICY),
        )
        self.assertEqual(
            "anima style, duo, \n\n初音 ミク, 博丽 灵梦, \n\n東方 Project \\(series\\), \n\nartist \\(name\\), \n\n笑顔, long hair, \n\n初音ミク smiles, happily.",
            txt.decode("utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
