from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .replacement import ReplacementError, ReplacementRule, rule_from_csv


RESOURCE_ID = "replace-e621-20260726-v2"
RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
CSV_NAME = "e621_tag_replacement_index.csv"
HEADER = ("source_tag", "canonical_e621_tag", "action", "replacement_tags")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReplaceResourceError(ValueError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReplaceResourceError(f"{field} is invalid")
    result = value.replace("/", "\\")
    path = PureWindowsPath(result)
    if path.is_absolute() or path.drive or path.root or any(part in {"", ".", ".."} for part in path.parts):
        raise ReplaceResourceError(f"{field} is unsafe")
    return result


def _resolve(root: Path, relative: str, *, directory: bool) -> Path:
    install = Path(os.path.abspath(root))
    target = Path(os.path.abspath(install / Path(relative.replace("\\", os.sep))))
    if not install.is_dir() or os.path.commonpath((str(install), str(target))) != str(install):
        raise ReplaceResourceError("resource path escapes the install root")
    if (directory and not target.is_dir()) or (not directory and not target.is_file()):
        raise ReplaceResourceError("resource path is missing")
    return target


@dataclass(frozen=True)
class ReplaceResource:
    fingerprint: str
    rules: dict[str, ReplacementRule]
    keep_non_canonical: int = 0
    canonical_direction_conflicts: int = 0


def _canonical_audit(canonical: dict[str, str], keep_outputs: dict[str, str]) -> tuple[int, int]:
    """M3-07: the only runtime reader of canonical_e621_tag.

    A keep row should emit the canonical alias target, and a canonical target that is itself
    indexed must be its own canonical; an index written in the wrong direction breaks both.
    """
    non_canonical = sum(1 for tag, output in keep_outputs.items() if canonical.get(tag) and output != canonical[tag])
    conflicts = sum(1 for target in canonical.values() if target in canonical and canonical[target] != target)
    return non_canonical, conflicts


def load_replace_resource(install_root: Path, manifest_relative_path: str, expected_fingerprint: str) -> ReplaceResource:
    manifest_path = _resolve(install_root, _relative(manifest_relative_path, "manifest path"), directory=False)
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaceResourceError("replace resource manifest is invalid") from exc
    catalog_manifest = isinstance(value, dict) and value.get("kind") == "replacement-index"
    if catalog_manifest:
        required = {
            "schemaVersion", "kind", "resourceId", "resourceVersion", "profile", "displayName",
            "description", "runtimeFormat", "entrypoints", "files", "metadata", "documentation",
        }
        if set(value) != required or (
            value.get("schemaVersion") != 1 or value.get("profile") != "e621"
            or value.get("runtimeFormat") != "e621-replacement-csv-v1"
            or not isinstance(value.get("resourceId"), str) or not RESOURCE_ID_PATTERN.fullmatch(value["resourceId"])
        ):
            raise ReplaceResourceError("catalog replacement resource identity is invalid")
        entrypoints, files, metadata = value.get("entrypoints"), value.get("files"), value.get("metadata")
        if (
            not isinstance(entrypoints, dict) or set(entrypoints) != {"index"}
            or not isinstance(files, dict) or set(entrypoints.values()) != set(files)
            or not isinstance(metadata, dict) or set(metadata) != {
                "ruleCount", "actionCounts", "pipeReplacementCount", "literalKeepPipeCount"
            }
        ):
            raise ReplaceResourceError("catalog replacement metadata is invalid")
        csv_relative = _relative(entrypoints["index"], "entrypoints.index")
        normalized_files = {_relative(name, "resource file path"): record for name, record in files.items()}
        unsigned = {
            "schemaVersion": 1, "kind": "replacement-index", "resourceId": value["resourceId"],
            "resourceVersion": value["resourceVersion"], "profile": "e621",
            "runtimeFormat": "e621-replacement-csv-v1", "entrypoints": {"index": csv_relative},
            "files": {name: normalized_files[name] for name in sorted(normalized_files)}, "metadata": metadata,
        }
        fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
        if fingerprint != expected_fingerprint:
            raise ReplaceResourceError("catalog replacement fingerprint does not match hello")
        root = manifest_path.parent
        record = normalized_files.get(csv_relative)
        csv_row_count = metadata.get("ruleCount")
        expected_actions = metadata.get("actionCounts")
        expected_pipe_replacements = metadata.get("pipeReplacementCount")
        expected_literal_keep_pipe = metadata.get("literalKeepPipeCount")
    else:
        required = {
            "schemaVersion", "resourceId", "owner", "profile", "resourceVersion", "rootRelativePath", "csvRowCount",
            "actionCounts", "pipeReplacementCount", "literalKeepPipeCount", "files", "fingerprint",
        }
        if not isinstance(value, dict) or set(value) != required or value.get("schemaVersion") != 1 or value.get("resourceId") != RESOURCE_ID or value.get("owner") != "replace" or value.get("profile") != "e621":
            raise ReplaceResourceError("replace resource manifest identity is invalid")
        unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
        fingerprint = value["fingerprint"]
        if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint) or fingerprint != expected_fingerprint or hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest() != fingerprint:
            raise ReplaceResourceError("replace resource manifest fingerprint is invalid")
        files = value["files"]
        if not isinstance(files, dict) or set(files) != {CSV_NAME}:
            raise ReplaceResourceError("replace resource file set is invalid")
        record = files[CSV_NAME]
        root = _resolve(install_root, _relative(value["rootRelativePath"], "resource root"), directory=True)
        csv_relative = CSV_NAME
        csv_row_count = value["csvRowCount"]
        expected_actions = value["actionCounts"]
        expected_pipe_replacements = value["pipeReplacementCount"]
        expected_literal_keep_pipe = value["literalKeepPipeCount"]
    if not isinstance(record, dict) or set(record) != {"sizeBytes", "sha256"} or type(record["sizeBytes"]) is not int or not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"]):
        raise ReplaceResourceError("replace resource CSV record is invalid")
    csv_path = _resolve(root, csv_relative, directory=False)
    if csv_path.stat().st_size != record["sizeBytes"] or _sha256(csv_path) != record["sha256"]:
        raise ReplaceResourceError("replace resource CSV digest is invalid")
    rules: dict[str, ReplacementRule] = {}
    canonical: dict[str, str] = {}
    keep_outputs: dict[str, str] = {}
    actions = {"keep": 0, "replace": 0, "drop": 0}
    pipe_replacements = literal_keep_pipe = 0
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != HEADER:
                raise ReplaceResourceError("replace resource CSV header is invalid")
            for row in reader:
                if set(row) != set(HEADER) or any(row[name] is None for name in HEADER):
                    raise ReplaceResourceError("replace resource CSV row is invalid")
                tag = row["source_tag"]
                if not tag or tag in rules:
                    raise ReplaceResourceError("replace resource CSV has duplicate source tags")
                rule = rule_from_csv(row["action"], row["replacement_tags"])
                rules[tag] = rule
                canonical[tag] = row["canonical_e621_tag"]
                if rule.action == "keep":
                    keep_outputs[tag] = rule.replacement_tags[0]
                actions[rule.action] += 1
                pipe_replacements += int(rule.action == "replace" and "|" in row["replacement_tags"])
                literal_keep_pipe += int(tag == ":|" and rule.action == "keep" and rule.replacement_tags == (":|",))
    except (OSError, UnicodeError, csv.Error, ReplacementError) as exc:
        raise ReplaceResourceError("replace resource CSV cannot be loaded") from exc
    if (
        len(rules) != csv_row_count or actions != expected_actions
        or pipe_replacements != expected_pipe_replacements or literal_keep_pipe != expected_literal_keep_pipe
    ):
        raise ReplaceResourceError("replace resource CSV audited statistics are invalid")
    return ReplaceResource(fingerprint, rules, *_canonical_audit(canonical, keep_outputs))


def load_custom_replace_resource(overlay_root: Path, custom_index_path: str, expected_sha256: str, expected_rule_count: int) -> ReplaceResource:
    if not isinstance(custom_index_path, str) or not SHA256.fullmatch(expected_sha256) or type(expected_rule_count) is not int or not 1 <= expected_rule_count <= 250_000:
        raise ReplaceResourceError("custom replace resource identity is invalid")
    try:
        root = Path(os.path.abspath(overlay_root))
        csv_path = Path(os.path.abspath(custom_index_path))
        expected = root / "resources" / "replace" / "custom-index.csv"
        if not root.is_dir() or os.path.commonpath((str(root), str(csv_path))) != str(root) or csv_path != expected or not csv_path.is_file():
            raise ReplaceResourceError("custom replace resource path is invalid")
        if csv_path.stat().st_size > 64 * 1024 * 1024 or _sha256(csv_path) != expected_sha256:
            raise ReplaceResourceError("custom replace resource digest is invalid")
    except OSError as exc:
        raise ReplaceResourceError("custom replace resource is unreadable") from exc
    rules: dict[str, ReplacementRule] = {}
    canonical: dict[str, str] = {}
    keep_outputs: dict[str, str] = {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != HEADER:
                raise ReplaceResourceError("custom replace resource CSV header is invalid")
            for row in reader:
                if len(rules) >= 250_000 or set(row) != set(HEADER) or any(row[name] is None for name in HEADER):
                    raise ReplaceResourceError("custom replace resource CSV row is invalid")
                tag = row["source_tag"]
                if not tag or tag in rules:
                    raise ReplaceResourceError("custom replace resource has duplicate source tags")
                rule = rule_from_csv(row["action"], row["replacement_tags"])
                rules[tag] = rule
                canonical[tag] = row["canonical_e621_tag"]
                if rule.action == "keep":
                    keep_outputs[tag] = rule.replacement_tags[0]
    except (OSError, UnicodeError, csv.Error, ReplacementError) as exc:
        raise ReplaceResourceError("custom replace resource CSV cannot be loaded") from exc
    if len(rules) != expected_rule_count:
        raise ReplaceResourceError("custom replace resource rule count is invalid")
    return ReplaceResource(expected_sha256, rules, *_canonical_audit(canonical, keep_outputs))
