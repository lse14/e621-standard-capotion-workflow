from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import canonical_json
from .path_safety import PathSafetyError, canonicalize, ensure_within, safe_relative_path, sha256_file


MAX_RESOURCE_MANIFEST_BYTES = 1_048_576
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CAPTION_RESOURCE_FILES = frozenset({"model.onnx", "model.onnx.data", "tags.json", "thresholds.json", "preprocess.json"})
CAPTION_RESOURCE_CATEGORIES = ("general", "character", "species", "rating")


class ResourceManifestError(ValueError):
    pass


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResourceManifestError(f"{field} must be a relative path")
    try:
        return safe_relative_path(value)
    except PathSafetyError as exc:
        raise ResourceManifestError(f"{field} is unsafe: {exc}") from exc


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ResourceManifestError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_size(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ResourceManifestError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class ResourceFileV1:
    sizeBytes: int
    sha256: str

    @classmethod
    def from_dict(cls, value: object, field: str) -> "ResourceFileV1":
        if not isinstance(value, dict) or set(value) != {"sizeBytes", "sha256"}:
            raise ResourceManifestError(f"{field} must contain sizeBytes and sha256")
        return cls(_positive_size(value["sizeBytes"], f"{field}.sizeBytes"), _sha256(value["sha256"], f"{field}.sha256"))

    def to_dict(self) -> dict[str, object]:
        return {"sizeBytes": self.sizeBytes, "sha256": self.sha256}


@dataclass(frozen=True)
class ProfileResourceManifestV1:
    resourceId: str
    resourceVersion: str
    rootRelativePath: str
    files: dict[str, ResourceFileV1]
    fingerprint: str
    owner: str = "caption"
    profile: str = "e621"
    tagCount: int = 8_783
    categories: tuple[str, ...] = CAPTION_RESOURCE_CATEGORIES
    schemaVersion: int = 1

    @classmethod
    def from_dict(cls, value: object) -> "ProfileResourceManifestV1":
        required = {
            "schemaVersion", "resourceId", "owner", "profile", "resourceVersion", "rootRelativePath",
            "tagCount", "categories", "files", "fingerprint",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ResourceManifestError("resource manifest fields are invalid")
        resource_id = value["resourceId"]
        if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
            raise ResourceManifestError("resourceId is invalid")
        resource_version = value["resourceVersion"]
        if not isinstance(resource_version, str) or not resource_version or len(resource_version.encode("utf-8")) > 256:
            raise ResourceManifestError("resourceVersion is invalid")
        if value["schemaVersion"] != 1 or value["owner"] != "caption" or value["profile"] != "e621":
            raise ResourceManifestError("resource manifest identity is invalid")
        if value["tagCount"] != 8_783 or value["categories"] != list(CAPTION_RESOURCE_CATEGORIES):
            raise ResourceManifestError("resource model metadata identity is invalid")
        raw_files = value["files"]
        if not isinstance(raw_files, dict) or set(raw_files) != CAPTION_RESOURCE_FILES:
            raise ResourceManifestError("resource manifest must contain exactly five caption files")
        files: dict[str, ResourceFileV1] = {}
        for relative, record in raw_files.items():
            normalized = _safe_relative(relative, "resource file path")
            if "\\" in normalized:
                raise ResourceManifestError("caption resource files must be direct children of the resource root")
            files[normalized] = ResourceFileV1.from_dict(record, f"files.{normalized}")
        manifest = cls(
            resourceId=resource_id,
            resourceVersion=resource_version,
            rootRelativePath=_safe_relative(value["rootRelativePath"], "rootRelativePath"),
            files=files,
            fingerprint=_sha256(value["fingerprint"], "fingerprint"),
        )
        if manifest.fingerprint != manifest.calculate_fingerprint():
            raise ResourceManifestError("resource manifest fingerprint mismatch")
        return manifest

    @classmethod
    def load(cls, path: str | Path) -> "ProfileResourceManifestV1":
        target = Path(path)
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ResourceManifestError(f"unable to read resource manifest: {target}") from exc
        if len(data) > MAX_RESOURCE_MANIFEST_BYTES:
            raise ResourceManifestError("resource manifest exceeds 1 MiB")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceManifestError("resource manifest is not strict UTF-8 JSON") from exc
        return cls.from_dict(value)

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "resourceId": self.resourceId,
            "owner": self.owner,
            "profile": self.profile,
            "resourceVersion": self.resourceVersion,
            "rootRelativePath": self.rootRelativePath,
            "tagCount": self.tagCount,
            "categories": list(self.categories),
            "files": {name: self.files[name].to_dict() for name in sorted(self.files)},
        }

    def calculate_fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.unsigned_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "fingerprint": self.fingerprint}

    def resolve_root(self, install_root: str | Path) -> Path:
        try:
            install = canonicalize(install_root, must_exist=True, directory=True).value
            resource = ensure_within(install, install / Path(self.rootRelativePath.replace("\\", os.sep)))
            return canonicalize(resource, must_exist=True, directory=True).value
        except PathSafetyError as exc:
            raise ResourceManifestError(f"resource root is unsafe: {exc}") from exc

    def verify_files(self, install_root: str | Path, *, verify_hashes: bool = True) -> dict[str, Path]:
        root = self.resolve_root(install_root)
        try:
            with os.scandir(root) as entries:
                actual = {
                    entry.name
                    for entry in entries
                    if entry.is_file(follow_symlinks=False)
                }
            if actual != CAPTION_RESOURCE_FILES or len(list(root.iterdir())) != len(CAPTION_RESOURCE_FILES):
                raise ResourceManifestError("resource root must contain exactly the five pinned caption files")
        except OSError as exc:
            raise ResourceManifestError("unable to enumerate the caption resource root") from exc
        resolved: dict[str, Path] = {}
        for relative, expected in self.files.items():
            try:
                target = ensure_within(root, root / Path(relative.replace("\\", os.sep)))
                target = canonicalize(target, must_exist=True, directory=False).value
            except PathSafetyError as exc:
                raise ResourceManifestError(f"resource file is unsafe: {relative}") from exc
            if target.stat().st_size != expected.sizeBytes:
                raise ResourceManifestError(f"resource file size mismatch: {relative}")
            if verify_hashes and sha256_file(target) != expected.sha256:
                raise ResourceManifestError(f"resource file digest mismatch: {relative}")
            resolved[relative] = target
        return resolved


def load_caption_resource_from_install(
    install_root: str | Path,
    manifest_relative_path: str,
    expected_fingerprint: str,
    *,
    verify_hashes: bool = True,
) -> tuple[ProfileResourceManifestV1, dict[str, Path]]:
    try:
        install = canonicalize(install_root, must_exist=True, directory=True).value
        relative = _safe_relative(manifest_relative_path, "resource manifest path")
        manifest_path = ensure_within(install, install / Path(relative.replace("\\", os.sep)))
    except PathSafetyError as exc:
        raise ResourceManifestError(f"resource manifest path is unsafe: {exc}") from exc
    manifest = ProfileResourceManifestV1.load(manifest_path)
    if manifest.fingerprint != _sha256(expected_fingerprint, "expected resource fingerprint"):
        raise ResourceManifestError("selected resource fingerprint does not match the manifest")
    return manifest, manifest.verify_files(install, verify_hashes=verify_hashes)
