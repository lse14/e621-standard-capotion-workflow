from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))


class NlLengthTests(unittest.TestCase):
    def _module(self):
        try:
            return importlib.import_module("anima_core.nl_length")
        except ModuleNotFoundError as exc:
            self.fail(f"Task 2.1 pure routing module is missing: {exc}")

    def test_stable_length_tier_normalizes_path_separators(self) -> None:
        module = self._module()
        self.assertEqual(
            module.stable_length_tier(
                seed="anima-nl-length-v1",
                relative_image_path="12_role\\scene\\a.png",
                distribution={"short": 33, "medium": 34, "long": 33},
            ),
            module.stable_length_tier(
                seed="anima-nl-length-v1",
                relative_image_path="12_role/scene/a.png",
                distribution={"short": 33, "medium": 34, "long": 33},
            ),
        )

    def test_stable_length_tier_honors_zero_and_full_distribution_buckets(self) -> None:
        module = self._module()
        self.assertEqual(
            "long",
            module.stable_length_tier(
                seed="seed",
                relative_image_path="12_role/a.png",
                distribution={"short": 0, "medium": 0, "long": 100},
            ),
        )
        self.assertEqual(
            "short",
            module.stable_length_tier(
                seed="seed",
                relative_image_path="12_role/a.png",
                distribution={"short": 100, "medium": 0, "long": 0},
            ),
        )

    def test_character_name_uses_only_the_first_directory_component(self) -> None:
        module = self._module()
        self.assertEqual("角色", module.character_name("12_角色\\scene\\a.png"))

    def test_character_name_rejects_nonconforming_first_directory(self) -> None:
        module = self._module()
        for value in ("role/a.png", "000265_1ad34fe21652dc3c14829e3110757447.jpg", "12_/a.png", "12_   /a.png", "12-role/a.png"):
            with self.subTest(relative_image_path=value):
                with self.assertRaisesRegex(ValueError, "first-level directories"):
                    module.character_name(value)


if __name__ == "__main__":
    unittest.main()
