"""Validate and freeze a user supplied Replace CSV without importing worker code."""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from .path_safety import PathSafetyError, canonicalize, sha256_file


HEADER = ("source_tag", "canonical_e621_tag", "action", "replacement_tags")
MAX_BYTES = 64 * 1024 * 1024
MAX_RULES = 250_000


class CustomReplaceIndexError(ValueError):
    pass


@dataclass(frozen=True)
class CustomReplaceIndex:
    path: Path
    content: bytes
    sha256: str
    rule_count: int

    def summary(self) -> dict[str, object]:
        return {"mode": "custom", "path": str(self.path), "sha256": self.sha256, "ruleCount": self.rule_count}


def _is_writable_tag(tag: str) -> bool:
    """Same constraint the worker output must satisfy in replace_protocol.parse_replace_projection."""
    return bool(tag) and tag == tag.strip() and not any(character in tag for character in ",\r\n\x00")


def _validate_rows(content: bytes) -> int:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CustomReplaceIndexError("custom replace index must be UTF-8") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if tuple(reader.fieldnames or ()) != HEADER:
            raise CustomReplaceIndexError("custom replace index CSV header is invalid")
        tags: set[str] = set()
        for count, row in enumerate(reader, start=1):
            line = reader.line_num
            if count > MAX_RULES or set(row) != set(HEADER) or any(row[name] is None for name in HEADER):
                raise CustomReplaceIndexError(f"custom replace index CSV row is invalid (line {line})")
            source = row["source_tag"]
            action = row["action"]
            replacements = row["replacement_tags"]
            if not source or source in tags or any(character in source for character in "\r\n\x00"):
                raise CustomReplaceIndexError(f"custom replace index has an invalid or duplicate source tag (line {line})")
            tags.add(source)
            if action == "drop":
                valid = not replacements
            elif action == "keep":
                # keep writes replacement_tags out as one whole tag without splitting or stripping.
                valid = _is_writable_tag(replacements)
            elif action == "replace":
                parts = tuple(part.strip() for part in replacements.split("|"))
                valid = bool(parts) and all(_is_writable_tag(part) for part in parts)
            else:
                valid = False
            if not valid:
                kind = action if action in {"drop", "keep", "replace"} else "unknown action"
                raise CustomReplaceIndexError(f"custom replace index contains an invalid {kind} rule (line {line})")
    except csv.Error as exc:
        raise CustomReplaceIndexError("custom replace index CSV cannot be parsed") from exc
    if not tags:
        raise CustomReplaceIndexError("custom replace index must contain at least one rule")
    return len(tags)


def inspect_custom_replace_index(path: str) -> CustomReplaceIndex:
    try:
        target = canonicalize(path, must_exist=True, directory=False).value
        content = target.read_bytes()
    except (OSError, PathSafetyError) as exc:
        raise CustomReplaceIndexError("custom replace index path is unsafe or unreadable") from exc
    if not content or len(content) > MAX_BYTES:
        raise CustomReplaceIndexError("custom replace index size is invalid")
    return CustomReplaceIndex(target, content, hashlib.sha256(content).hexdigest(), _validate_rows(content))


def verify_frozen_custom_replace_index(path: str | Path, expected_sha256: str, expected_rule_count: int) -> CustomReplaceIndex:
    index = inspect_custom_replace_index(str(path))
    if index.sha256 != expected_sha256 or index.rule_count != expected_rule_count:
        raise CustomReplaceIndexError("frozen custom replace index does not match task metadata")
    return index
