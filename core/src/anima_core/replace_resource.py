from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import canonical_json
from .path_safety import PathSafetyError, canonicalize, ensure_within, safe_relative_path, sha256_file
from .resource_catalog import ResourceCatalogError, ResourcePackage


RESOURCE_ID = "replace-e621-20260726-v2"
CSV_NAME = "e621_tag_replacement_index.csv"
CSV_ROW_COUNT = 86_922
ACTION_COUNTS = {"keep": 56_426, "replace": 11_600, "drop": 18_896}
PIPE_REPLACEMENT_COUNT = 2_151
LITERAL_KEEP_PIPE_COUNT = 1
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReplaceResourceError(ValueError):
    pass


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ReplaceResourceError(f"{field} must be a lowercase SHA-256")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ReplaceResourceError(f"{field} must be a positive integer")
    return value


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ReplaceResourceError(f"{field} must be a relative path")
    try:
        return safe_relative_path(value)
    except PathSafetyError as exc:
        raise ReplaceResourceError(f"{field} is unsafe: {exc}") from exc


@dataclass(frozen=True)
class ReplaceResourceManifestV1:
    resourceVersion: str
    rootRelativePath: str
    csvSizeBytes: int
    csvSha256: str
    fingerprint: str

    @classmethod
    def load(cls, path: str | Path) -> "ReplaceResourceManifestV1":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReplaceResourceError("replace resource manifest is not strict UTF-8 JSON") from exc
        required = {
            "schemaVersion", "resourceId", "owner", "profile", "resourceVersion", "rootRelativePath", "csvRowCount",
            "actionCounts", "pipeReplacementCount", "literalKeepPipeCount", "files", "fingerprint",
        }
        if not isinstance(value, dict) or set(value) != required or (
            value["schemaVersion"] != 1 or value["resourceId"] != RESOURCE_ID or value["owner"] != "replace"
            or value["profile"] != "e621" or value["csvRowCount"] != CSV_ROW_COUNT
            or value["actionCounts"] != ACTION_COUNTS or value["pipeReplacementCount"] != PIPE_REPLACEMENT_COUNT
            or value["literalKeepPipeCount"] != LITERAL_KEEP_PIPE_COUNT
        ):
            raise ReplaceResourceError("replace resource manifest identity is invalid")
        files = value["files"]
        if not isinstance(files, dict) or set(files) != {CSV_NAME} or not isinstance(files[CSV_NAME], dict):
            raise ReplaceResourceError("replace resource file set is invalid")
        record = files[CSV_NAME]
        if set(record) != {"sizeBytes", "sha256"}:
            raise ReplaceResourceError("replace resource CSV record is invalid")
        unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
        fingerprint = _sha256(value["fingerprint"], "fingerprint")
        if hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest() != fingerprint:
            raise ReplaceResourceError("replace resource manifest fingerprint mismatch")
        version = value["resourceVersion"]
        if not isinstance(version, str) or not version or len(version.encode("utf-8")) > 256:
            raise ReplaceResourceError("resourceVersion is invalid")
        return cls(version, _relative(value["rootRelativePath"], "rootRelativePath"), _positive(record["sizeBytes"], "files.sizeBytes"), _sha256(record["sha256"], "files.sha256"), fingerprint)

    def verify_files(self, install_root: str | Path) -> Path:
        try:
            install = canonicalize(install_root, must_exist=True, directory=True).value
            root = canonicalize(ensure_within(install, install / Path(self.rootRelativePath.replace("\\", os.sep))), must_exist=True, directory=True).value
            entries = list(os.scandir(root))
        except (OSError, PathSafetyError) as exc:
            raise ReplaceResourceError("replace resource root is unsafe or missing") from exc
        if len(entries) != 1 or not entries[0].is_file(follow_symlinks=False) or entries[0].name != CSV_NAME:
            raise ReplaceResourceError("replace resource root must contain exactly the pinned CSV")
        csv_path = canonicalize(ensure_within(root, root / CSV_NAME), must_exist=True, directory=False).value
        if csv_path.stat().st_size != self.csvSizeBytes or sha256_file(csv_path) != self.csvSha256:
            raise ReplaceResourceError("replace resource CSV digest mismatch")
        return csv_path


def load_replace_resource_from_install(install_root: str | Path, manifest_relative_path: str, expected_fingerprint: str) -> ReplaceResourceManifestV1:
    try:
        install = canonicalize(install_root, must_exist=True, directory=True).value
        relative = _relative(manifest_relative_path, "resource manifest path")
        manifest_path = ensure_within(install, install / Path(relative.replace("\\", os.sep)))
    except PathSafetyError as exc:
        raise ReplaceResourceError(f"replace resource manifest path is unsafe: {exc}") from exc
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaceResourceError("replace resource manifest is unreadable") from exc
    if isinstance(raw, dict) and raw.get("kind") == "replacement-index":
        try:
            package = ResourcePackage.load(install, manifest_path, "replacement-index")
            if package.fingerprint != _sha256(expected_fingerprint, "expected resource fingerprint"):
                raise ReplaceResourceError("selected replace resource fingerprint does not match the manifest")
            package.verify_files(verify_hashes=True)
        except ResourceCatalogError as exc:
            raise ReplaceResourceError(str(exc)) from exc
        record = package.files[package.entrypoints["index"]]
        return ReplaceResourceManifestV1(
            package.resource_version,
            str(package.package_root.relative_to(install)).replace("/", "\\"),
            record.size_bytes,
            record.sha256,
            package.fingerprint,
        )
    manifest = ReplaceResourceManifestV1.load(manifest_path)
    if manifest.fingerprint != _sha256(expected_fingerprint, "expected resource fingerprint"):
        raise ReplaceResourceError("selected replace resource fingerprint does not match the manifest")
    manifest.verify_files(install)
    return manifest
