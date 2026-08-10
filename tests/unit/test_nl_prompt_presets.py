from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.nl_prompt_presets import (
    BUILTIN_PRESET_IDS,
    BUILTIN_V4_BASE_PRESET_ID,
    MAX_CUSTOM_PRESETS,
    MAX_STORE_BYTES,
    NlPromptPresetStore,
    PromptPresetConflictError,
    PromptPresetNotFoundError,
    PromptPresetValidationError,
)


def _custom_id(index: int) -> str:
    return f"custom:{index:032x}"


class NlPromptPresetStoreTests(unittest.TestCase):
    def test_v1_preset_library_exposes_three_typed_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = NlPromptPresetStore(Path(temporary) / "presets.json")
            summaries = store.list_summaries()
            self.assertEqual(tuple(BUILTIN_PRESET_IDS), tuple(item["presetId"] for item in summaries))
            self.assertEqual(
                ("general", "style", "character"),
                tuple(item["type"] for item in summaries),
            )
            for preset_id in BUILTIN_PRESET_IDS:
                detail = store.get(preset_id)
                self.assertTrue(detail.builtIn)
                self.assertTrue(detail.promptText.strip())

    def test_builtin_can_be_overridden_and_reset_and_custom_type_is_editable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = NlPromptPresetStore(Path(temporary) / "presets.json")
            general_id = BUILTIN_PRESET_IDS[0]
            updated = store.update(general_id, name="General", preset_type="general", prompt_text="Local override")
            self.assertEqual(("General", "general", "Local override"), (updated.name, updated.type, updated.promptText))
            store.reset(general_id)
            self.assertNotEqual("Local override", store.get(general_id).promptText)
            custom = store.create(name="Custom", preset_type="style", prompt_text="Style prompt")
            self.assertEqual("style", custom.type)
            changed = store.update(custom.presetId, name="Character custom", preset_type="character", prompt_text="Character prompt")
            self.assertEqual(("Character custom", "character", "Character prompt"), (changed.name, changed.type, changed.promptText))

    def test_builtin_preset_is_resource_backed_and_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = NlPromptPresetStore(Path(temporary) / "presets.json")
            built_in = store.get(BUILTIN_PRESET_IDS[0])
            self.assertTrue(built_in.builtIn)
            self.assertEqual(
                hashlib.sha256(built_in.basePrompt.encode("utf-8")).hexdigest(),
                built_in.sha256,
            )
            self.assertEqual(
                (BUILTIN_PRESET_IDS[0], BUILTIN_PRESET_IDS[1], BUILTIN_PRESET_IDS[2]),
                tuple(item["presetId"] for item in store.list_summaries()),
            )

    def test_custom_preset_create_update_and_delete_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = NlPromptPresetStore(Path(temporary) / "presets.json")
            created = store.create(name="Custom A", base_prompt="Base A")
            updated = store.update(created.presetId, name="Renamed", base_prompt="Base B")
            self.assertEqual(
                (created.presetId, "Renamed", "Base B"),
                (updated.presetId, updated.name, updated.basePrompt),
            )
            store.delete(created.presetId)
            with self.assertRaises(PromptPresetNotFoundError):
                store.get(created.presetId)

    def test_builtin_can_be_updated_and_reset_but_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = NlPromptPresetStore(Path(temporary) / "presets.json")
            preset_id = BUILTIN_PRESET_IDS[0]
            store.update(preset_id, name="General", preset_type="general", prompt_text="Changed")
            self.assertEqual("Changed", store.get(preset_id).promptText)
            store.reset(preset_id)
            self.assertNotEqual("Changed", store.get(preset_id).promptText)
            with self.assertRaises(PromptPresetConflictError):
                store.delete(preset_id)

    def test_malformed_store_values_fail_closed(self) -> None:
        invalid_stores = (
            b"not json",
            json.dumps({"schemaVersion": 1, "presets": [], "extra": True}).encode("utf-8"),
            json.dumps({"schemaVersion": 2, "presets": []}).encode("utf-8"),
            json.dumps({"schemaVersion": 1, "presets": [{"presetId": _custom_id(1), "name": "A", "basePrompt": "B"}, {"presetId": _custom_id(1), "name": "C", "basePrompt": "D"}]}).encode("utf-8"),
            json.dumps({"schemaVersion": 1, "presets": [{"presetId": _custom_id(2), "name": "A", "basePrompt": "B", "extra": True}]}).encode("utf-8"),
            json.dumps({"schemaVersion": 2, "builtInOverrides": [{"presetId": BUILTIN_PRESET_IDS[0], "promptText": "x"}], "customPresets": [{"presetId": _custom_id(2), "name": "A", "type": "other", "promptText": "B"}]}).encode("utf-8"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            store = NlPromptPresetStore(path)
            for raw in invalid_stores:
                with self.subTest(raw=raw[:32]):
                    path.write_bytes(raw)
                    with self.assertRaises(PromptPresetValidationError):
                        store.list_summaries()

    def test_name_and_prompt_byte_limits_and_nul_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = NlPromptPresetStore(Path(temporary) / "presets.json")
            accepted = store.create(name="n" * 256, base_prompt="p" * 65_536)
            self.assertEqual(256, len(accepted.name.encode("utf-8")))
            self.assertEqual(65_536, accepted.sizeBytes)
            for name, prompt in (
                ("", "Base"),
                ("   ", "Base"),
                ("n" * 257, "Base"),
                ("A\x00B", "Base"),
                ("Name", ""),
                ("Name", "   "),
                ("Name", "p" * 65_537),
                ("Name", "A\x00B"),
            ):
                with self.subTest(name=name[:8], prompt_bytes=len(prompt.encode("utf-8"))):
                    with self.assertRaises(PromptPresetValidationError):
                        store.create(name=name, base_prompt=prompt)

    def test_record_and_store_byte_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            records = [
                {"presetId": _custom_id(index), "name": f"Name {index}", "basePrompt": "Base"}
                for index in range(MAX_CUSTOM_PRESETS)
            ]
            path.write_text(json.dumps({"schemaVersion": 1, "presets": records}), encoding="utf-8")
            store = NlPromptPresetStore(path)
            self.assertEqual(MAX_CUSTOM_PRESETS + len(BUILTIN_PRESET_IDS), len(store.list_summaries()))
            with self.assertRaises(PromptPresetValidationError):
                store.create(name="Overflow", base_prompt="Base")
            path.write_bytes(b" " * (MAX_STORE_BYTES + 1))
            with self.assertRaises(PromptPresetValidationError):
                store.list_summaries()

    def test_store_contains_only_sorted_custom_records_and_no_success_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            store = NlPromptPresetStore(path)
            with patch("anima_core.nl_prompt_presets.uuid.uuid4", side_effect=(type("Uuid", (), {"hex": "f" * 32})(), type("Uuid", (), {"hex": "0" * 32})())):
                store.create(name="Last", base_prompt="Last prompt")
                store.create(name="First", base_prompt="First prompt")
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([_custom_id(0), _custom_id(int("f" * 32, 16))], [item["presetId"] for item in stored["customPresets"]])
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())

    def test_replace_failure_preserves_existing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            store = NlPromptPresetStore(path)
            created = store.create(name="Stable", base_prompt="Original")
            previous = path.read_bytes()
            with patch("anima_core.nl_prompt_presets.os.replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    store.update(created.presetId, name="Changed", base_prompt="Changed")
            self.assertEqual(previous, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
