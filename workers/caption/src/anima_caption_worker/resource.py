from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse


MAX_MANIFEST_BYTES = 1_048_576
REQUIRED_FILES = frozenset({"model.onnx", "model.onnx.data", "tags.json", "thresholds.json", "preprocess.json"})
CATEGORIES = ["general", "character", "species", "rating"]
V2_LAYOUTS: dict[str, frozenset[str]] = {
    "cl-tagger-v2-onnx-v1": frozenset({"model", "modelData", "metadata", "vocabulary", "thresholds"}),
    "wd-eva02-large-tagger-v3-onnx-v1": frozenset({"model", "selectedTags", "preprocess", "thresholds"}),
}
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
CATEGORY_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CaptionResourceError(ValueError):
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
        raise CaptionResourceError(f"{field} is invalid")
    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or any(part in {"", ".", ".."} for part in path.parts):
        raise CaptionResourceError(f"{field} is unsafe")
    return normalized


def _is_reparse(path: Path) -> bool:
    info = os.lstat(path)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _resolve_within(root: Path, relative: str, *, directory: bool) -> Path:
    install = Path(os.path.abspath(root))
    if not install.is_dir() or _is_reparse(install):
        raise CaptionResourceError("install root is missing or is a reparse point")
    target = install / Path(relative.replace("\\", os.sep))
    absolute = Path(os.path.abspath(target))
    try:
        if os.path.commonpath((str(install), str(absolute))) != str(install):
            raise CaptionResourceError("resource path escapes the install root")
    except ValueError as exc:
        raise CaptionResourceError("resource path is on another volume") from exc
    current = install
    for part in absolute.relative_to(install).parts:
        current = current / part
        if not current.exists():
            raise CaptionResourceError(f"resource path is missing: {relative}")
        if _is_reparse(current):
            raise CaptionResourceError(f"resource path contains a reparse point: {relative}")
    if directory and not absolute.is_dir():
        raise CaptionResourceError(f"resource directory is missing: {relative}")
    if not directory and not absolute.is_file():
        raise CaptionResourceError(f"resource file is missing: {relative}")
    return absolute


def _validate_resource_directory(root: Path, expected: frozenset[str] = REQUIRED_FILES) -> None:
    pending = [root]
    found: set[str] = set()
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _is_reparse(path):
                        raise CaptionResourceError(f"resource root contains an unsafe entry: {entry.name}")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        raise CaptionResourceError(f"resource root contains an unsafe entry: {entry.name}")
                    found.add(str(path.relative_to(root)).replace("/", "\\"))
    except OSError as exc:
        raise CaptionResourceError("unable to enumerate the caption resource root") from exc
    if found != expected:
        raise CaptionResourceError("resource root file set is invalid")


def _text(value: object, field: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise CaptionResourceError(f"{field} is invalid")
    if len(value.encode("utf-8")) > max_bytes:
        raise CaptionResourceError(f"{field} is too long")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1 or value > 1_000_000:
        raise CaptionResourceError(f"{field} is invalid")
    return value


def _category_list(value: object, field: str, *, lowercase: bool) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise CaptionResourceError(f"{field} is invalid")
    result = [_text(item, field, max_bytes=64) for item in value]
    if lowercase and any(not CATEGORY_ID.fullmatch(item) for item in result):
        raise CaptionResourceError(f"{field} contains an invalid category")
    if len(set(result)) != len(result):
        raise CaptionResourceError(f"{field} contains duplicate categories")
    return result


def _validate_v2_metadata(value: object) -> dict[str, object]:
    required = {
        "tagCount", "modelCategories", "adjustableCategories", "excludedCategories",
        "vocabularyFingerprint",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CaptionResourceError("catalog tagging model metadata is invalid")
    model_categories = _category_list(value["modelCategories"], "metadata.modelCategories", lowercase=False)
    adjustable = _category_list(value["adjustableCategories"], "metadata.adjustableCategories", lowercase=True)
    excluded = _category_list(value["excludedCategories"], "metadata.excludedCategories", lowercase=True)
    normalized_model = [item.lower() for item in model_categories]
    fingerprint = value["vocabularyFingerprint"]
    if (
        len(set(normalized_model)) != len(normalized_model)
        or set(adjustable) & set(excluded)
        or set(adjustable) | set(excluded) != set(normalized_model)
        or not isinstance(fingerprint, str)
        or not SHA256.fullmatch(fingerprint)
    ):
        raise CaptionResourceError("catalog tagging model category metadata is invalid")
    return {
        "tagCount": _positive(value["tagCount"], "metadata.tagCount"),
        "modelCategories": model_categories,
        "adjustableCategories": adjustable,
        "excludedCategories": excluded,
        "vocabularyFingerprint": fingerprint,
    }


def _validate_distribution(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"mode", "sourceUrl", "licenseUrl"}:
        raise CaptionResourceError("catalog tagging model distribution is invalid")
    if value.get("mode") != "local-only":
        raise CaptionResourceError("Danbooru tagging models must be local-only")
    result = {"mode": "local-only"}
    for field in ("sourceUrl", "licenseUrl"):
        url = _text(value[field], f"distribution.{field}", max_bytes=2_048)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise CaptionResourceError(f"distribution.{field} is invalid")
        result[field] = url
    return result


@dataclass(frozen=True)
class WorkerCaptionResource:
    resource_id: str
    resource_version: str
    root: Path
    fingerprint: str
    files: dict[str, Path]
    file_records: dict[str, dict[str, object]]
    schema_version: int
    profile: str
    runtime_format: str
    metadata: dict[str, object]
    entrypoints: dict[str, Path]


def _load_catalog_resource(
    install_root: Path,
    manifest_path: Path,
    value: dict[str, object],
    expected_fingerprint: str,
    *,
    verify_external_data_hash: bool,
) -> WorkerCaptionResource:
    schema_version = value.get("schemaVersion")
    common = {
        "schemaVersion", "kind", "resourceId", "resourceVersion", "profile", "displayName",
        "description", "runtimeFormat", "entrypoints", "files", "metadata", "documentation",
    }
    required = common if schema_version == 1 else common | {"distribution"}
    if schema_version not in {1, 2} or set(value) != required or value.get("kind") != "tagging-model":
        raise CaptionResourceError("catalog tagging model identity is invalid")
    resource_id = value.get("resourceId")
    resource_version = value.get("resourceVersion")
    if (
        not isinstance(resource_id, str)
        or not RESOURCE_ID.fullmatch(resource_id)
        or not isinstance(resource_version, str)
        or not resource_version
        or resource_version != resource_version.strip()
        or len(resource_version.encode("utf-8")) > 256
    ):
        raise CaptionResourceError("catalog tagging model id or version is invalid")
    for field in ("displayName", "description"):
        localized = value[field]
        if not isinstance(localized, dict) or set(localized) != {"zh-CN", "en"}:
            raise CaptionResourceError(f"catalog tagging model {field} is invalid")
        for language in ("zh-CN", "en"):
            _text(localized[language], f"{field}.{language}")

    profile = value.get("profile")
    runtime_format = value.get("runtimeFormat")
    if schema_version == 1:
        roles = frozenset({"model", "modelData", "preprocess", "tags", "thresholds"})
        if profile != "e621" or runtime_format != "e621-eva02-onnx-v1":
            raise CaptionResourceError("catalog tagging model identity is invalid")
        metadata = value.get("metadata")
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"tagCount", "categories"}
            or metadata.get("tagCount") != 8_783
            or metadata.get("categories") != CATEGORIES
        ):
            raise CaptionResourceError("catalog tagging model metadata is invalid")
        normalized_metadata = {"tagCount": 8_783, "categories": list(CATEGORIES)}
        distribution: dict[str, str] | None = None
    else:
        if profile != "danbooru" or runtime_format not in V2_LAYOUTS:
            raise CaptionResourceError("catalog tagging model identity is invalid")
        roles = V2_LAYOUTS[str(runtime_format)]
        normalized_metadata = _validate_v2_metadata(value.get("metadata"))
        distribution = _validate_distribution(value.get("distribution"))

    entrypoints = value.get("entrypoints")
    records = value.get("files")
    if not isinstance(entrypoints, dict) or set(entrypoints) != roles or not isinstance(records, dict):
        raise CaptionResourceError("catalog tagging model entrypoints are invalid")
    normalized_entrypoints = {
        role: _relative(entrypoints[role], f"entrypoints.{role}") for role in sorted(entrypoints)
    }
    if set(normalized_entrypoints.values()) != set(records):
        raise CaptionResourceError("catalog tagging model entrypoints and files differ")
    normalized_records: dict[str, dict[str, object]] = {}
    for name, record in records.items():
        normalized = _relative(name, "resource file path")
        if (
            normalized != str(name).replace("/", "\\")
            or normalized in normalized_records
            or not isinstance(record, dict)
            or set(record) != {"sizeBytes", "sha256"}
        ):
            raise CaptionResourceError("catalog tagging model file record is invalid")
        size, digest = record["sizeBytes"], record["sha256"]
        if type(size) is not int or size < 1 or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise CaptionResourceError("catalog tagging model file size or digest is invalid")
        normalized_records[normalized] = {"sizeBytes": size, "sha256": digest}

    documentation = value.get("documentation")
    if not isinstance(documentation, list) or len(documentation) > 16:
        raise CaptionResourceError("catalog tagging model documentation is invalid")
    documentation_paths: set[str] = set()
    for index, record in enumerate(documentation):
        if not isinstance(record, dict) or set(record) != {"path", "language", "title"}:
            raise CaptionResourceError("catalog tagging model documentation is invalid")
        path = _relative(record["path"], f"documentation[{index}].path")
        _text(record["language"], f"documentation[{index}].language")
        _text(record["title"], f"documentation[{index}].title")
        if path in documentation_paths or path in normalized_records:
            raise CaptionResourceError("catalog tagging model documentation path is duplicated")
        documentation_paths.add(path)

    unsigned: dict[str, object] = {
        "schemaVersion": schema_version,
        "kind": "tagging-model",
        "resourceId": resource_id,
        "resourceVersion": resource_version,
        "profile": profile,
        "runtimeFormat": runtime_format,
        "entrypoints": normalized_entrypoints,
        "files": {name: normalized_records[name] for name in sorted(normalized_records)},
        "metadata": normalized_metadata,
    }
    if distribution is not None:
        unsigned["distribution"] = distribution
    fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if fingerprint != expected_fingerprint:
        raise CaptionResourceError("catalog tagging model fingerprint does not match hello")

    root = manifest_path.parent
    _validate_resource_directory(
        root,
        frozenset({"resource.json", *normalized_records, *documentation_paths}),
    )
    paths: dict[str, Path] = {}
    entrypoint_paths: dict[str, Path] = {}
    file_records: dict[str, dict[str, object]] = {}
    role_for_path = {relative: role for role, relative in normalized_entrypoints.items()}
    for relative, record in normalized_records.items():
        path = _resolve_within(root, relative, directory=False)
        if path.stat().st_size != record["sizeBytes"]:
            raise CaptionResourceError(f"resource file size mismatch: {relative}")
        role = role_for_path[relative]
        if role != "modelData" or verify_external_data_hash:
            if _sha256(path) != record["sha256"]:
                raise CaptionResourceError(f"resource file digest mismatch: {relative}")
        paths[relative] = path
        entrypoint_paths[role] = path
        file_records[relative] = record
    if schema_version == 2:
        vocabulary_role = "vocabulary" if runtime_format == "cl-tagger-v2-onnx-v1" else "selectedTags"
        vocabulary_record = normalized_records[normalized_entrypoints[vocabulary_role]]
        if normalized_metadata["vocabularyFingerprint"] != vocabulary_record["sha256"]:
            raise CaptionResourceError("vocabulary fingerprint does not match its resource file")
    return WorkerCaptionResource(
        resource_id,
        resource_version,
        root,
        fingerprint,
        paths,
        file_records,
        int(schema_version),
        str(profile),
        str(runtime_format),
        normalized_metadata,
        entrypoint_paths,
    )


def load_caption_resource(
    install_root: Path,
    manifest_relative_path: str,
    expected_fingerprint: str,
    *,
    verify_external_data_hash: bool = False,
) -> WorkerCaptionResource:
    manifest_relative = _relative(manifest_relative_path, "resource manifest path")
    manifest_path = _resolve_within(install_root, manifest_relative, directory=False)
    data = manifest_path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise CaptionResourceError("resource manifest exceeds 1 MiB")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CaptionResourceError("resource manifest is not UTF-8 JSON") from exc
    if isinstance(value, dict) and value.get("kind") == "tagging-model":
        return _load_catalog_resource(
            install_root,
            manifest_path,
            value,
            expected_fingerprint,
            verify_external_data_hash=verify_external_data_hash,
        )
    required = {
        "schemaVersion", "resourceId", "owner", "profile", "resourceVersion", "rootRelativePath",
        "tagCount", "categories", "files", "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CaptionResourceError("resource manifest fields are invalid")
    if (
        value["schemaVersion"] != 1
        or value["owner"] != "caption"
        or value["profile"] != "e621"
        or value["tagCount"] != 8_783
        or value["categories"] != CATEGORIES
    ):
        raise CaptionResourceError("resource manifest identity is invalid")
    fingerprint = value["fingerprint"]
    if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint) or fingerprint != expected_fingerprint:
        raise CaptionResourceError("resource fingerprint does not match hello")
    unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
    if hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest() != fingerprint:
        raise CaptionResourceError("resource manifest fingerprint is invalid")
    records = value["files"]
    if not isinstance(records, dict) or set(records) != REQUIRED_FILES:
        raise CaptionResourceError("resource manifest file set is invalid")
    root_relative = _relative(value["rootRelativePath"], "rootRelativePath")
    root = _resolve_within(install_root, root_relative, directory=True)
    _validate_resource_directory(root)
    paths: dict[str, Path] = {}
    validated_records: dict[str, dict[str, object]] = {}
    for name, record in records.items():
        relative_name = _relative(name, "resource file path")
        if "\\" in relative_name or not isinstance(record, dict) or set(record) != {"sizeBytes", "sha256"}:
            raise CaptionResourceError("resource file record is invalid")
        size = record["sizeBytes"]
        digest = record["sha256"]
        if type(size) is not int or size < 1 or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise CaptionResourceError("resource file size or digest is invalid")
        path = _resolve_within(root, relative_name, directory=False)
        if path.stat().st_size != size:
            raise CaptionResourceError(f"resource file size mismatch: {name}")
        if name != "model.onnx.data" or verify_external_data_hash:
            if _sha256(path) != digest:
                raise CaptionResourceError(f"resource file digest mismatch: {name}")
        paths[name] = path
        validated_records[name] = {"sizeBytes": size, "sha256": digest}
    resource_id = value["resourceId"]
    resource_version = value["resourceVersion"]
    if (
        not isinstance(resource_id, str)
        or not RESOURCE_ID.fullmatch(resource_id)
        or not isinstance(resource_version, str)
        or not resource_version
        or len(resource_version.encode("utf-8")) > 256
    ):
        raise CaptionResourceError("resource id or version is invalid")
    entrypoints = {
        "model": paths["model.onnx"],
        "modelData": paths["model.onnx.data"],
        "preprocess": paths["preprocess.json"],
        "tags": paths["tags.json"],
        "thresholds": paths["thresholds.json"],
    }
    return WorkerCaptionResource(
        resource_id,
        resource_version,
        root,
        fingerprint,
        paths,
        validated_records,
        1,
        "e621",
        "e621-eva02-onnx-v1",
        {"tagCount": 8_783, "categories": list(CATEGORIES)},
        entrypoints,
    )
