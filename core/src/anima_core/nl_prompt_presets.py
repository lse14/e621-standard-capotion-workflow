from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .nl_profiles import V4_BASE_PROMPT_VERSION, load_default_system_prompt


BUILTIN_V4_BASE_PRESET_ID = "builtin:nl-default-prompt-v4-base"
MAX_CUSTOM_PRESETS = 100
MAX_STORE_BYTES = 8 * 1024 * 1024
MAX_NAME_BYTES = 256
MAX_PROMPT_BYTES = 65_536
_CUSTOM_ID = re.compile(r"^custom:[0-9a-f]{32}$")


class PromptPresetError(ValueError):
    pass


class PromptPresetValidationError(PromptPresetError):
    pass


class PromptPresetConflictError(PromptPresetError):
    pass


class PromptPresetNotFoundError(PromptPresetError):
    pass


@dataclass(frozen=True)
class NlPromptPreset:
    presetId: str
    name: str
    builtIn: bool
    basePrompt: str
    sha256: str
    sizeBytes: int


def _reject_duplicate_keys(pairs: list[tuple[object, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise PromptPresetValidationError("prompt preset store contains duplicate JSON keys")
        value[key] = item
    return value


def _validate_text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PromptPresetValidationError(f"prompt preset {field} is invalid")
    if len(value.encode("utf-8")) > limit:
        raise PromptPresetValidationError(f"prompt preset {field} exceeds its limit")
    return value


def _preset(*, preset_id: str, name: str, built_in: bool, base_prompt: str) -> NlPromptPreset:
    data = base_prompt.encode("utf-8")
    return NlPromptPreset(
        presetId=preset_id,
        name=name,
        builtIn=built_in,
        basePrompt=base_prompt,
        sha256=hashlib.sha256(data).hexdigest(),
        sizeBytes=len(data),
    )


class NlPromptPresetStore:
    """Global diagnostic-only v4 base prompts, isolated from API profiles and credentials."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else Path(os.environ["LOCALAPPDATA"]) / "AnimaDatasetTool" / "nl-prompt-presets.json"
        )

    def _built_in(self) -> NlPromptPreset:
        return _preset(
            preset_id=BUILTIN_V4_BASE_PRESET_ID,
            name=V4_BASE_PROMPT_VERSION,
            built_in=True,
            base_prompt=load_default_system_prompt(prompt_version=V4_BASE_PROMPT_VERSION),
        )

    def _load_custom_records(self) -> dict[str, tuple[str, str]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise PromptPresetValidationError("prompt preset store is unreadable") from exc
        if len(raw) > MAX_STORE_BYTES:
            raise PromptPresetValidationError("prompt preset store exceeds its limit")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PromptPresetValidationError("prompt preset store is invalid JSON") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schemaVersion", "presets"}
            or value.get("schemaVersion") != 1
            or not isinstance(value.get("presets"), list)
            or len(value["presets"]) > MAX_CUSTOM_PRESETS
        ):
            raise PromptPresetValidationError("prompt preset store shape is invalid")
        records: dict[str, tuple[str, str]] = {}
        for record in value["presets"]:
            if not isinstance(record, dict) or set(record) != {"presetId", "name", "basePrompt"}:
                raise PromptPresetValidationError("prompt preset store record fields are invalid")
            preset_id = record["presetId"]
            if not isinstance(preset_id, str) or not _CUSTOM_ID.fullmatch(preset_id) or preset_id in records:
                raise PromptPresetValidationError("prompt preset id is invalid")
            records[preset_id] = (
                _validate_text(record["name"], field="name", limit=MAX_NAME_BYTES),
                _validate_text(record["basePrompt"], field="base prompt", limit=MAX_PROMPT_BYTES),
            )
        return records

    def _write_custom_records(self, records: dict[str, tuple[str, str]]) -> None:
        if len(records) > MAX_CUSTOM_PRESETS:
            raise PromptPresetValidationError("prompt preset limit is exceeded")
        target = {
            "schemaVersion": 1,
            "presets": [
                {"presetId": preset_id, "name": records[preset_id][0], "basePrompt": records[preset_id][1]}
                for preset_id in sorted(records)
            ],
        }
        encoded = (json.dumps(target, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_STORE_BYTES:
            raise PromptPresetValidationError("prompt preset store exceeds its limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, self.path)
        except OSError:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _summary(preset: NlPromptPreset) -> dict[str, object]:
        return {
            "presetId": preset.presetId,
            "name": preset.name,
            "builtIn": preset.builtIn,
            "sha256": preset.sha256,
            "sizeBytes": preset.sizeBytes,
        }

    def list_summaries(self) -> tuple[dict[str, object], ...]:
        records = self._load_custom_records()
        summaries = [self._summary(self._built_in())]
        summaries.extend(
            self._summary(_preset(preset_id=preset_id, name=name, built_in=False, base_prompt=base_prompt))
            for preset_id, (name, base_prompt) in sorted(records.items())
        )
        return tuple(summaries)

    def get(self, preset_id: str) -> NlPromptPreset:
        if preset_id == BUILTIN_V4_BASE_PRESET_ID:
            return self._built_in()
        record = self._load_custom_records().get(preset_id)
        if record is None:
            raise PromptPresetNotFoundError("prompt preset was not found")
        return _preset(preset_id=preset_id, name=record[0], built_in=False, base_prompt=record[1])

    def create(self, *, name: str, base_prompt: str) -> NlPromptPreset:
        valid_name = _validate_text(name, field="name", limit=MAX_NAME_BYTES)
        valid_prompt = _validate_text(base_prompt, field="base prompt", limit=MAX_PROMPT_BYTES)
        records = self._load_custom_records()
        if len(records) >= MAX_CUSTOM_PRESETS:
            raise PromptPresetValidationError("prompt preset limit is exceeded")
        preset_id = f"custom:{uuid.uuid4().hex}"
        while preset_id in records:
            preset_id = f"custom:{uuid.uuid4().hex}"
        records[preset_id] = (valid_name, valid_prompt)
        self._write_custom_records(records)
        return _preset(preset_id=preset_id, name=valid_name, built_in=False, base_prompt=valid_prompt)

    def update(self, preset_id: str, *, name: str, base_prompt: str) -> NlPromptPreset:
        if preset_id == BUILTIN_V4_BASE_PRESET_ID:
            raise PromptPresetConflictError("built-in prompt preset is immutable")
        valid_name = _validate_text(name, field="name", limit=MAX_NAME_BYTES)
        valid_prompt = _validate_text(base_prompt, field="base prompt", limit=MAX_PROMPT_BYTES)
        records = self._load_custom_records()
        if preset_id not in records:
            raise PromptPresetNotFoundError("prompt preset was not found")
        records[preset_id] = (valid_name, valid_prompt)
        self._write_custom_records(records)
        return _preset(preset_id=preset_id, name=valid_name, built_in=False, base_prompt=valid_prompt)

    def delete(self, preset_id: str) -> None:
        if preset_id == BUILTIN_V4_BASE_PRESET_ID:
            raise PromptPresetConflictError("built-in prompt preset is immutable")
        records = self._load_custom_records()
        if preset_id not in records:
            raise PromptPresetNotFoundError("prompt preset was not found")
        del records[preset_id]
        self._write_custom_records(records)
