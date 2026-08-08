from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlparse


MAX_MANIFEST_BYTES = 1_048_576
RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
RESOURCE_VERSION = "ppocrv5-server-paddle-v1"
RUNTIME_FORMAT = "ppocrv5-server-paddle-v1"
EXPECTED_MANIFEST_RELATIVE = f"ocr-models\\{RESOURCE_ID}\\resource.json"
ENTRYPOINTS = {
    "detection": "detection\\inference.json",
    "recognition": "recognition\\inference.json",
    "textlineOrientation": "textline-orientation\\inference.json",
}
MODEL_IDENTITIES = {
    "detection": "PP-OCRv5_server_det",
    "recognition": "PP-OCRv5_server_rec",
    "textlineOrientation": "PP-LCNet_x1_0_textline_ori",
}
INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class OcrResourceError(ValueError):
    pass


@dataclass(frozen=True)
class OcrResource:
    resource_id: str
    fingerprint: str
    root: Path
    detection_root: Path
    recognition_root: Path
    textline_orientation_root: Path
    entrypoints: dict[str, Path]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OcrResourceError("OCR model file cannot be read") from exc
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except OSError as exc:
        raise OcrResourceError("OCR resource path cannot be inspected") from exc
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise OcrResourceError(f"{field} is invalid")
    if len(value.encode("utf-8")) > maximum:
        raise OcrResourceError(f"{field} is too long")
    return value


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OcrResourceError(f"{field} is invalid")
    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or normalized.startswith("\\"):
        raise OcrResourceError(f"{field} is unsafe")
    for component in normalized.split("\\"):
        if not component or component in {".", ".."} or ":" in component or component.endswith((".", " ")):
            raise OcrResourceError(f"{field} is unsafe")
        if component.split(".", 1)[0].upper() in RESERVED:
            raise OcrResourceError(f"{field} is unsafe")
    return normalized


def _resolve_within(root: Path, relative: str, *, directory: bool) -> Path:
    library = Path(os.path.abspath(root))
    if not library.is_dir() or _is_reparse(library):
        raise OcrResourceError("OCR resource library is unavailable")
    target = Path(os.path.abspath(library / Path(relative.replace("\\", os.sep))))
    try:
        if os.path.commonpath((str(library), str(target))) != str(library):
            raise OcrResourceError("OCR resource path escapes its library")
        parts = target.relative_to(library).parts
    except ValueError as exc:
        raise OcrResourceError("OCR resource path is invalid") from exc
    current = library
    for component in parts:
        current = current / component
        if not current.exists() or _is_reparse(current):
            raise OcrResourceError("OCR resource path is missing or unsafe")
    try:
        mode = os.lstat(target).st_mode
    except OSError as exc:
        raise OcrResourceError("OCR resource path cannot be inspected") from exc
    if directory:
        if not stat.S_ISDIR(mode):
            raise OcrResourceError("OCR resource directory is invalid")
    elif not stat.S_ISREG(mode):
        raise OcrResourceError("OCR resource file is invalid")
    return target


def _localized(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"zh-CN", "en"}:
        raise OcrResourceError(f"{field} is invalid")
    return {language: _text(value[language], f"{field}.{language}") for language in ("zh-CN", "en")}


def _https_url(value: object, field: str) -> str:
    result = _text(value, field, 2048)
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise OcrResourceError(f"{field} is invalid")
    return result


def _distribution(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"mode", "sourceUrl", "licenseStatus"}
        or value.get("mode") != "local-only"
        or value.get("licenseStatus") != "unverified"
    ):
        raise OcrResourceError("OCR resource distribution is invalid")
    return {
        "mode": "local-only",
        "sourceUrl": _https_url(value["sourceUrl"], "distribution.sourceUrl"),
        "licenseStatus": "unverified",
    }


def _metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"models", "inference"}:
        raise OcrResourceError("OCR resource metadata is invalid")
    models = value["models"]
    inference = value["inference"]
    if models != MODEL_IDENTITIES or not isinstance(inference, dict) or set(inference) != set(INFERENCE):
        raise OcrResourceError("OCR resource metadata is invalid")
    for name in ("useDocOrientationClassify", "useDocUnwarping", "useTextlineOrientation"):
        if type(inference[name]) is not bool:
            raise OcrResourceError("OCR resource inference metadata is invalid")
    if type(inference["textRecScoreThresh"]) is not int or type(inference["textDetLimitSideLen"]) is not int:
        raise OcrResourceError("OCR resource inference metadata is invalid")
    if inference != INFERENCE:
        raise OcrResourceError("OCR resource inference metadata is invalid")
    return {"models": dict(MODEL_IDENTITIES), "inference": dict(INFERENCE)}


def _under_root(path: str, root: str) -> bool:
    path_parts = tuple(part.casefold() for part in PureWindowsPath(path).parts)
    root_parts = tuple(part.casefold() for part in PureWindowsPath(root).parts)
    return len(path_parts) > len(root_parts) and path_parts[:len(root_parts)] == root_parts


def _directory_layout(root: Path, expected_files: set[str]) -> None:
    expected_directories: set[str] = set()
    for relative in expected_files:
        current = PureWindowsPath(relative).parent
        while str(current) not in {"", "."}:
            expected_directories.add(str(current).replace("/", "\\"))
            current = current.parent
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _is_reparse(path):
                        raise OcrResourceError("OCR resource contains a reparse point")
                    relative = str(path.relative_to(root)).replace("/", "\\")
                    if entry.is_dir(follow_symlinks=False):
                        actual_directories.add(relative)
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        actual_files.add(relative)
                    else:
                        raise OcrResourceError("OCR resource contains an unsafe entry")
    except OSError as exc:
        raise OcrResourceError("OCR resource cannot be enumerated") from exc
    if actual_files != {"resource.json", *expected_files}:
        raise OcrResourceError("OCR resource contains missing or unlisted files")
    if actual_directories != expected_directories:
        raise OcrResourceError("OCR resource directory layout is invalid")


def load_ocr_resource(
    resource_root: Path,
    manifest_relative_path: str,
    expected_fingerprint: str,
) -> OcrResource:
    manifest_relative = _relative(manifest_relative_path, "resourceManifestRelativePath")
    if manifest_relative != EXPECTED_MANIFEST_RELATIVE:
        raise OcrResourceError("OCR resource manifest path is invalid")
    if not isinstance(expected_fingerprint, str) or not SHA256.fullmatch(expected_fingerprint):
        raise OcrResourceError("OCR resource fingerprint is invalid")
    manifest_path = _resolve_within(resource_root, manifest_relative, directory=False)
    try:
        data = manifest_path.read_bytes()
    except OSError as exc:
        raise OcrResourceError("OCR resource manifest cannot be read") from exc
    if len(data) > MAX_MANIFEST_BYTES:
        raise OcrResourceError("OCR resource manifest exceeds 1 MiB")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrResourceError("OCR resource manifest is not UTF-8 JSON") from exc
    required = {
        "schemaVersion", "kind", "resourceId", "resourceVersion", "profile", "displayName", "description",
        "runtimeFormat", "distribution", "entrypoints", "files", "metadata", "documentation",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise OcrResourceError("OCR resource manifest fields are invalid")
    if (
        value["schemaVersion"] != 2
        or value["kind"] != "ocr-model"
        or value["resourceId"] != RESOURCE_ID
        or value["resourceVersion"] != RESOURCE_VERSION
        or value["profile"] != "shared"
        or value["runtimeFormat"] != RUNTIME_FORMAT
    ):
        raise OcrResourceError("OCR resource manifest identity is invalid")
    _localized(value["displayName"], "displayName")
    _localized(value["description"], "description")
    distribution = _distribution(value["distribution"])
    metadata = _metadata(value["metadata"])
    raw_entrypoints = value["entrypoints"]
    if not isinstance(raw_entrypoints, dict) or set(raw_entrypoints) != set(ENTRYPOINTS):
        raise OcrResourceError("OCR resource entrypoints are invalid")
    entrypoints = {name: _relative(raw_entrypoints[name], f"entrypoints.{name}") for name in sorted(ENTRYPOINTS)}
    if entrypoints != ENTRYPOINTS:
        raise OcrResourceError("OCR resource entrypoints are invalid")
    roots = {name: str(PureWindowsPath(path).parent).replace("/", "\\") for name, path in entrypoints.items()}
    if len(set(roots.values())) != len(roots):
        raise OcrResourceError("OCR resource model roots are duplicated")
    raw_files = value["files"]
    if not isinstance(raw_files, dict) or not raw_files:
        raise OcrResourceError("OCR resource files are invalid")
    files: dict[str, dict[str, object]] = {}
    for raw_name, raw_record in raw_files.items():
        relative = _relative(raw_name, "OCR resource file path")
        if relative != str(raw_name).replace("/", "\\") or relative in files:
            raise OcrResourceError("OCR resource file path is invalid")
        if not isinstance(raw_record, dict) or set(raw_record) != {"sizeBytes", "sha256"}:
            raise OcrResourceError("OCR resource file record is invalid")
        size = raw_record["sizeBytes"]
        digest = raw_record["sha256"]
        if type(size) is not int or size < 1 or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise OcrResourceError("OCR resource file record is invalid")
        if sum(_under_root(relative, model_root) for model_root in roots.values()) != 1:
            raise OcrResourceError("OCR resource file is outside a model root")
        files[relative] = {"sizeBytes": size, "sha256": digest}
    if not set(entrypoints.values()).issubset(files):
        raise OcrResourceError("OCR resource entrypoint is not a listed file")
    documentation = value["documentation"]
    if documentation != []:
        raise OcrResourceError("OCR resource documentation is unsupported")
    unsigned = {
        "schemaVersion": 2,
        "kind": "ocr-model",
        "resourceId": RESOURCE_ID,
        "resourceVersion": RESOURCE_VERSION,
        "profile": "shared",
        "runtimeFormat": RUNTIME_FORMAT,
        "entrypoints": {name: entrypoints[name] for name in sorted(entrypoints)},
        "files": {name: files[name] for name in sorted(files)},
        "metadata": metadata,
        "distribution": distribution,
    }
    fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if fingerprint != expected_fingerprint:
        raise OcrResourceError("OCR resource fingerprint does not match hello")
    package_root = manifest_path.parent
    _directory_layout(package_root, set(files))
    resolved_entrypoints: dict[str, Path] = {}
    for name, relative in entrypoints.items():
        resolved_entrypoints[name] = _resolve_within(package_root, relative, directory=False)
    for relative, record in files.items():
        path = _resolve_within(package_root, relative, directory=False)
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise OcrResourceError("OCR model file cannot be inspected") from exc
        if actual_size != record["sizeBytes"] or _sha256(path) != record["sha256"]:
            raise OcrResourceError("OCR model file size or SHA-256 is invalid")
    return OcrResource(
        resource_id=RESOURCE_ID,
        fingerprint=fingerprint,
        root=package_root,
        detection_root=resolved_entrypoints["detection"].parent,
        recognition_root=resolved_entrypoints["recognition"].parent,
        textline_orientation_root=resolved_entrypoints["textlineOrientation"].parent,
        entrypoints=resolved_entrypoints,
    )
