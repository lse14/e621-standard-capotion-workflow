from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import canonical_json
from .path_safety import PathSafetyError, assert_no_reparse_tree, canonicalize, ensure_within, sha256_file
from .resource_catalog_validation import (
    COMMON_FIELDS_V1,
    COMMON_FIELDS_V2,
    KIND_LAYOUT,
    MAX_MANIFEST_BYTES,
    OCR_MODEL_DIRECTORY,
    OCR_MODEL_ENTRYPOINTS,
    OCR_MODEL_RESOURCE_ID,
    OCR_MODEL_RUNTIME_FORMAT,
    RESOURCE_ID,
    TOKENIZER_DIRECTORY,
    TOKENIZER_FILE_ALLOWLIST,
    TOKENIZER_MANIFEST_FIELDS,
    TOKENIZER_REQUIRED_FILES,
    TOKENIZER_RESOURCE_IDENTITIES,
    TOKENIZER_RESOURCE_ID,
    TOKENIZER_REVISION,
    V2_CLASSIFICATION_LAYOUTS,
    V2_TAGGING_LAYOUTS,
    ResourceCatalogError,
    ResourceKind,
    _CACHE_LOCK,
    _distribution,
    _localized,
    _metadata,
    _positive,
    _relative,
    _sha256,
    _text,
)


_HASH_CACHE: dict[tuple[str, int, int, str], str] = {}


def _ocr_model_roots(entrypoints: dict[str, str]) -> tuple[tuple[str, ...], ...]:
    if set(entrypoints) != OCR_MODEL_ENTRYPOINTS:
        raise ResourceCatalogError("ocr model entrypoints are invalid")
    roots: list[tuple[str, ...]] = []
    for role in sorted(OCR_MODEL_ENTRYPOINTS):
        parts = tuple(entrypoints[role].split("\\"))
        if len(parts) < 2 or parts[-1].lower() != "inference.json":
            raise ResourceCatalogError("ocr model entrypoints are invalid")
        roots.append(parts[:-1])
    lowered = [tuple(part.casefold() for part in root) for root in roots]
    for index, root in enumerate(lowered):
        for other in lowered[index + 1:]:
            if root == other or root[:len(other)] == other or other[:len(root)] == root:
                raise ResourceCatalogError("ocr model roots must be distinct and non-overlapping")
    return tuple(roots)


def _file_is_in_exactly_one_ocr_root(relative: str, roots: tuple[tuple[str, ...], ...]) -> bool:
    parts = tuple(part.casefold() for part in relative.split("\\"))
    matches = sum(
        len(parts) > len(root) and parts[:len(root)] == tuple(part.casefold() for part in root)
        for root in roots
    )
    return matches == 1


@dataclass(frozen=True)
class ResourceFile:
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"sizeBytes": self.size_bytes, "sha256": self.sha256}


@dataclass(frozen=True)
class ResourcePackage:
    library_root: Path
    package_root: Path
    manifest_relative_path: str
    schema_version: int
    kind: ResourceKind
    resource_id: str
    resource_version: str
    profile: str
    display_name: dict[str, str]
    description: dict[str, str]
    runtime_format: str
    entrypoints: dict[str, str]
    files: dict[str, ResourceFile]
    metadata: dict[str, Any]
    distribution: dict[str, str]
    documentation: tuple[dict[str, str], ...]
    fingerprint: str
    official_model_id: str | None = None
    revision: str | None = None
    tokenizer_family: str | None = None
    context_limit: int | None = None
    root_relative_path: str | None = None

    @classmethod
    def _load_tokenizer(
        cls,
        library_root: Path,
        manifest_path: Path,
        value: dict[str, object],
    ) -> "ResourcePackage":
        if set(value) != TOKENIZER_MANIFEST_FIELDS or value.get("schemaVersion") != 3:
            raise ResourceCatalogError("tokenizer resource manifest fields are invalid")
        if value.get("kind") != "tokenizer" or value.get("owner") != "token-budget":
            raise ResourceCatalogError("tokenizer resource identity is invalid")
        if value.get("profile") != "shared":
            raise ResourceCatalogError("tokenizer resource profile must be shared")
        resource_id = value.get("resourceId")
        if not isinstance(resource_id, str) or not TOKENIZER_RESOURCE_ID.fullmatch(resource_id):
            raise ResourceCatalogError("tokenizer resourceId is invalid")
        official_model_id = value.get("officialModelId")
        if TOKENIZER_RESOURCE_IDENTITIES.get(resource_id) != official_model_id:
            raise ResourceCatalogError("tokenizer officialModelId is invalid")
        revision = value.get("revision")
        if not isinstance(revision, str) or not TOKENIZER_REVISION.fullmatch(revision):
            raise ResourceCatalogError("tokenizer revision must be an immutable 40-character commit")
        tokenizer_family = value.get("tokenizerFamily")
        if tokenizer_family != "qwen3":
            raise ResourceCatalogError("tokenizerFamily is invalid")
        context_limit = _positive(value.get("contextLimit"), "tokenizer contextLimit")
        resource_version = _text(value.get("resourceVersion"), "resourceVersion")
        root_relative_path = _relative(value.get("rootRelativePath"), "tokenizer rootRelativePath")
        expected_root = f"{TOKENIZER_DIRECTORY}\\{resource_id}"
        if root_relative_path != expected_root:
            raise ResourceCatalogError("tokenizer rootRelativePath is invalid")
        package_root = manifest_path.parent
        try:
            ensure_within(library_root / TOKENIZER_DIRECTORY, package_root)
        except PathSafetyError as exc:
            raise ResourceCatalogError("tokenizer package escaped its category") from exc
        relative_manifest = _relative(str(manifest_path.relative_to(library_root)), "manifest path")
        raw_files = value.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ResourceCatalogError("tokenizer files must be a non-empty array")
        files: dict[str, ResourceFile] = {}
        paths: list[str] = []
        for index, record in enumerate(raw_files):
            if not isinstance(record, dict) or set(record) != {"path", "sizeBytes", "sha256"}:
                raise ResourceCatalogError(f"tokenizer files[{index}] record is invalid")
            relative = _relative(record["path"], f"tokenizer files[{index}].path")
            if relative not in TOKENIZER_FILE_ALLOWLIST:
                raise ResourceCatalogError("tokenizer files must use the tokenizer allowlist")
            paths.append(relative)
            files[relative] = ResourceFile(
                _positive(record["sizeBytes"], f"tokenizer files[{index}].sizeBytes"),
                _sha256(record["sha256"], f"tokenizer files[{index}].sha256"),
            )
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ResourceCatalogError("tokenizer file paths must be sorted and unique")
        if not TOKENIZER_REQUIRED_FILES <= set(files):
            raise ResourceCatalogError("tokenizer files must include config.json and tokenizer.json")
        distribution = _distribution(value.get("distribution"), allow_unverified_license=True)
        expected_distribution = {
            "mode": "local-only",
            "sourceUrl": f"https://huggingface.co/{official_model_id}",
            "licenseStatus": "unverified",
        }
        if distribution != expected_distribution:
            raise ResourceCatalogError("tokenizer distribution must use its official model page")
        fingerprint = _sha256(value.get("fingerprint"), "tokenizer fingerprint")
        unsigned = {key: value[key] for key in sorted(TOKENIZER_MANIFEST_FIELDS - {"fingerprint"})}
        expected_fingerprint = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
        if fingerprint != expected_fingerprint:
            raise ResourceCatalogError("tokenizer fingerprint does not match the manifest")
        package = cls(
            library_root=library_root,
            package_root=package_root,
            manifest_relative_path=relative_manifest,
            schema_version=3,
            kind="tokenizer",
            resource_id=resource_id,
            resource_version=resource_version,
            profile="shared",
            display_name={},
            description={},
            runtime_format="tokenizer",
            entrypoints={},
            files=files,
            metadata={},
            distribution=distribution,
            documentation=(),
            fingerprint=fingerprint,
            official_model_id=official_model_id,
            revision=revision,
            tokenizer_family=tokenizer_family,
            context_limit=context_limit,
            root_relative_path=root_relative_path,
        )
        package.verify_files(verify_hashes=False)
        return package

    @classmethod
    def load(cls, library_root: Path, manifest_path: Path, expected_kind: ResourceKind) -> "ResourcePackage":
        try:
            data = manifest_path.read_bytes()
        except OSError as exc:
            raise ResourceCatalogError("resource.json cannot be read") from exc
        if not data or len(data) > MAX_MANIFEST_BYTES:
            raise ResourceCatalogError("resource.json is empty or exceeds 1 MiB")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceCatalogError("resource.json is not strict UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ResourceCatalogError("resource.json fields are invalid")
        if expected_kind == "tokenizer":
            return cls._load_tokenizer(library_root, manifest_path, value)
        schema_version = value.get("schemaVersion")
        expected_fields = COMMON_FIELDS_V1 if schema_version == 1 else COMMON_FIELDS_V2
        if schema_version not in {1, 2} or set(value) != expected_fields:
            raise ResourceCatalogError("resource.json fields are invalid")
        if value["kind"] != expected_kind:
            raise ResourceCatalogError("resource identity does not match its category")
        profile = value["profile"]
        if expected_kind == "ocr-model":
            if profile != "shared":
                raise ResourceCatalogError("ocr model profile must be shared")
        elif profile not in {"e621", "danbooru"}:
            raise ResourceCatalogError("resource identity does not match its category")
        resource_id = value["resourceId"]
        if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
            raise ResourceCatalogError("resourceId is invalid")
        resource_version = _text(value["resourceVersion"], "resourceVersion")
        runtime_format = value["runtimeFormat"]
        if expected_kind == "ocr-model":
            if schema_version != 2:
                raise ResourceCatalogError("ocr model must use resource manifest v2")
            if resource_id != OCR_MODEL_RESOURCE_ID:
                raise ResourceCatalogError("ocr model resourceId is unsupported")
            if runtime_format != OCR_MODEL_RUNTIME_FORMAT:
                raise ResourceCatalogError("ocr model runtime format is unsupported")
            directory = OCR_MODEL_DIRECTORY
            required_entrypoints = OCR_MODEL_ENTRYPOINTS
        else:
            directory, _, v1_runtime_format, v1_entrypoints = KIND_LAYOUT[expected_kind]
            if schema_version == 1:
                if profile != "e621" or runtime_format != v1_runtime_format:
                    raise ResourceCatalogError("resource identity does not match manifest v1")
                required_entrypoints = v1_entrypoints
            else:
                if profile != "danbooru":
                    raise ResourceCatalogError("resource identity does not match manifest v2")
                if expected_kind == "tagging-model" and runtime_format in V2_TAGGING_LAYOUTS:
                    required_entrypoints = V2_TAGGING_LAYOUTS[runtime_format]
                elif expected_kind == "classification-index" and runtime_format in V2_CLASSIFICATION_LAYOUTS:
                    required_entrypoints = V2_CLASSIFICATION_LAYOUTS[runtime_format]
                else:
                    raise ResourceCatalogError("resource identity does not match manifest v2")
        raw_entrypoints = value["entrypoints"]
        if not isinstance(raw_entrypoints, dict) or set(raw_entrypoints) != required_entrypoints:
            if expected_kind == "ocr-model":
                raise ResourceCatalogError("ocr model entrypoints are invalid")
            raise ResourceCatalogError("resource entrypoints are invalid")
        entrypoints = {name: _relative(raw_entrypoints[name], f"entrypoints.{name}") for name in sorted(raw_entrypoints)}
        ocr_roots = _ocr_model_roots(entrypoints) if expected_kind == "ocr-model" else ()
        raw_files = value["files"]
        if not isinstance(raw_files, dict) or not raw_files:
            raise ResourceCatalogError("resource files must be a non-empty object")
        files: dict[str, ResourceFile] = {}
        for raw_path, record in raw_files.items():
            relative = _relative(raw_path, "files path")
            if relative != raw_path.replace("/", "\\") or relative in files:
                raise ResourceCatalogError("resource file paths must be unique normalized relative paths")
            if not isinstance(record, dict) or set(record) != {"sizeBytes", "sha256"}:
                raise ResourceCatalogError(f"files.{raw_path} record is invalid")
            files[relative] = ResourceFile(
                _positive(record["sizeBytes"], f"files.{raw_path}.sizeBytes"),
                _sha256(record["sha256"], f"files.{raw_path}.sha256"),
            )
        if expected_kind == "ocr-model":
            if not set(entrypoints.values()) <= set(files):
                raise ResourceCatalogError("ocr model entrypoints must reference listed files")
            if any(not _file_is_in_exactly_one_ocr_root(relative, ocr_roots) for relative in files):
                raise ResourceCatalogError("ocr model files must be under entrypoint model roots")
        elif set(entrypoints.values()) != set(files):
            raise ResourceCatalogError("entrypoints must reference every runtime file exactly once")
        documentation: list[dict[str, str]] = []
        raw_docs = value["documentation"]
        if not isinstance(raw_docs, list) or len(raw_docs) > 16:
            raise ResourceCatalogError("documentation must be a bounded array")
        for index, record in enumerate(raw_docs):
            if not isinstance(record, dict) or set(record) != {"path", "language", "title"}:
                raise ResourceCatalogError(f"documentation[{index}] fields are invalid")
            documentation.append({
                "path": _relative(record["path"], f"documentation[{index}].path"),
                "language": _text(record["language"], f"documentation[{index}].language"),
                "title": _text(record["title"], f"documentation[{index}].title"),
            })
        package_root = manifest_path.parent
        expected_parent = library_root / directory
        try:
            ensure_within(expected_parent, package_root)
        except PathSafetyError as exc:
            raise ResourceCatalogError("resource package escaped its category") from exc
        relative_manifest = _relative(str(manifest_path.relative_to(library_root)), "manifest path")
        metadata = _metadata(
            expected_kind, value["metadata"], schema_version=schema_version, runtime_format=runtime_format,
        )
        if schema_version == 2 and expected_kind == "tagging-model":
            vocabulary_role = "vocabulary" if runtime_format == "cl-tagger-v2-onnx-v1" else "selectedTags"
            vocabulary_file = files[entrypoints[vocabulary_role]]
            if metadata["vocabularyFingerprint"] != vocabulary_file.sha256:
                raise ResourceCatalogError("vocabularyFingerprint must match the vocabulary entrypoint SHA-256")
        distribution = (
            {"mode": "bundled"}
            if schema_version == 1
            else _distribution(
                value["distribution"],
                allow_unverified_license=expected_kind == "ocr-model",
            )
        )
        if schema_version == 2 and expected_kind == "tagging-model" and distribution["mode"] != "local-only":
            raise ResourceCatalogError("Danbooru tagging model manifests must be local-only")
        if schema_version == 2 and expected_kind == "classification-index" and distribution["mode"] != "bundled":
            raise ResourceCatalogError("Danbooru classification manifests must be bundled")
        unsigned = {
            "schemaVersion": schema_version,
            "kind": expected_kind,
            "resourceId": resource_id,
            "resourceVersion": resource_version,
            "profile": profile,
            "runtimeFormat": runtime_format,
            "entrypoints": entrypoints,
            "files": {name: files[name].to_dict() for name in sorted(files)},
            "metadata": metadata,
        }
        if schema_version == 2:
            unsigned["distribution"] = distribution
        fingerprint = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
        package = cls(
            library_root, package_root, relative_manifest, schema_version, expected_kind, resource_id, resource_version, profile,
            _localized(value["displayName"], "displayName"), _localized(value["description"], "description"),
            runtime_format, entrypoints, files, metadata, distribution, tuple(documentation), fingerprint,
        )
        package.verify_files(verify_hashes=False)
        return package

    def resolve_relative(self, relative: str) -> Path:
        try:
            target = ensure_within(self.package_root, self.package_root / Path(relative.replace("\\", os.sep)))
            return canonicalize(target, must_exist=True, directory=False).value
        except PathSafetyError as exc:
            raise ResourceCatalogError(f"resource file is unsafe or missing: {relative}") from exc

    def entrypoint(self, role: str) -> Path:
        try:
            return self.resolve_relative(self.entrypoints[role])
        except KeyError as exc:
            raise ResourceCatalogError(f"resource entrypoint is unavailable: {role}") from exc

    def verify_files(self, *, verify_hashes: bool) -> None:
        if self.kind in {"ocr-model", "tokenizer"}:
            try:
                assert_no_reparse_tree(self.package_root)
            except PathSafetyError as exc:
                raise ResourceCatalogError(f"{self.kind} package contains a reparse point") from exc
        allowed = {"resource.json", *self.files, *(record["path"] for record in self.documentation)}
        actual = {
            str(path.relative_to(self.package_root)).replace("/", "\\")
            for path in self.package_root.rglob("*")
            if path.is_file()
        }
        if actual != allowed:
            raise ResourceCatalogError("resource package contains missing or unlisted files")
        for relative, expected in self.files.items():
            target = self.resolve_relative(relative)
            stat = target.stat()
            if stat.st_size != expected.size_bytes:
                raise ResourceCatalogError(f"resource file size mismatch: {relative}")
            if verify_hashes:
                key = (str(target), stat.st_size, stat.st_mtime_ns, expected.sha256)
                with _CACHE_LOCK:
                    actual = _HASH_CACHE.get(key)
                if actual is None:
                    actual = sha256_file(target)
                    with _CACHE_LOCK:
                        _HASH_CACHE[key] = actual
                if actual != expected.sha256:
                    raise ResourceCatalogError(f"resource file SHA-256 mismatch: {relative}")
        for record in self.documentation:
            self.resolve_relative(record["path"])

    @property
    def adjustable_categories(self) -> tuple[str, ...]:
        if self.kind != "tagging-model":
            return ()
        values = self.metadata.get("adjustableCategories", self.metadata.get("categories", ()))
        return tuple(values) if isinstance(values, list) else ()

    @property
    def excluded_categories(self) -> tuple[str, ...]:
        if self.kind != "tagging-model":
            return ()
        values = self.metadata.get("excludedCategories", ())
        return tuple(values) if isinstance(values, list) else ()

    @property
    def default_thresholds(self) -> dict[str, float]:
        if self.kind != "tagging-model":
            return {}
        path = self.entrypoint("thresholds")
        try:
            data = path.read_bytes()
            value = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceCatalogError("tagging model thresholds are unreadable or invalid") from exc
        expected = set(self.adjustable_categories)
        if not isinstance(value, dict) or set(value) != expected:
            raise ResourceCatalogError("tagging model thresholds do not match adjustable categories")
        thresholds: dict[str, float] = {}
        for category in self.adjustable_categories:
            raw = value[category]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ResourceCatalogError("tagging model threshold must be numeric")
            threshold = float(raw)
            if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise ResourceCatalogError("tagging model threshold must be between 0 and 1")
            thresholds[category] = threshold
        return thresholds

    def api_dict(
        self,
        *,
        default_for_profiles: tuple[str, ...] = (),
        compatibility: dict[str, str] | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "kind": self.kind,
            "resourceId": self.resource_id,
            "resourceVersion": self.resource_version,
            "profile": self.profile,
            "displayName": self.display_name,
            "description": self.description,
            "runtimeFormat": self.runtime_format,
            "distribution": self.distribution,
            "fingerprint": self.fingerprint,
            "metadata": self.metadata,
            "adjustableCategories": list(self.adjustable_categories),
            "excludedCategories": list(self.excluded_categories),
            "defaultThresholds": self.default_thresholds,
            "compatibility": compatibility or {"status": "not_applicable"},
            "available": True,
            "default": bool(default_for_profiles),
            "defaultForProfiles": list(default_for_profiles),
        }
        if self.kind == "tokenizer":
            if self.official_model_id is None or self.context_limit is None:
                raise ResourceCatalogError("tokenizer resource API metadata is invalid")
            result["officialModelId"] = self.official_model_id
            result["contextLimit"] = self.context_limit
        return result
