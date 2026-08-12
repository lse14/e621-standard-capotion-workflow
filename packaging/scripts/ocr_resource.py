"""Preview-first OCR resource import, reset, and cache cleanup tooling.

The default CLI path is intentionally read-only.  The explicit ``--apply``
transaction keeps every formal target outside the cache untouched until the
lock, wheel closure, runtime, resource package, and offline probe succeed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = REPOSITORY_ROOT / "core" / "src"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
if str(CORE_SOURCE) not in sys.path:
    sys.path.insert(0, str(CORE_SOURCE))

from anima_core.resource_catalog_package import ResourcePackage
from anima_core.resource_catalog_validation import OCR_MODEL_IDENTITIES
from ocr_compatibility import MODEL_ARTIFACTS as _COMPATIBILITY_MODEL_ARTIFACTS


RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
RUNTIME_ID = "ocr-paddle"
RUNTIME_FORMAT = "ppocrv5-server-paddle-v1"
DIRECT_REQUIREMENTS = (
    "paddlepaddle==3.2.2",
    "paddleocr==3.7.0",
    "paddlex[ocr-core]==3.7.2",
)
MODEL_ARTIFACTS = _COMPATIBILITY_MODEL_ARTIFACTS
MODEL_SOURCE_URL = "https://www.paddleocr.ai/latest/en/version3.x/model_list.html"
MODEL_DOWNLOAD_GUIDE = "OCR_MODEL_DOWNLOAD.md"
ENTRYPOINT_DIRECTORIES = {
    "detection": "detection",
    "recognition": "recognition",
    "textlineOrientation": "textline-orientation",
}
INFERENCE_SETTINGS = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}
CACHE_ROOT_RELATIVE = Path(".runtime-build") / "ocr-import" / "v1"
MANUAL_MODEL_ROOT_RELATIVE = Path("ocr-model-archives")
MODEL_ONLY_STAGING_RELATIVE = Path(".runtime-build") / "ocr-model-bootstrap" / "staging"
CLEANABLE_CACHE_RELATIVES = (
    str(CACHE_ROOT_RELATIVE / "build-environment"),
    str(CACHE_ROOT_RELATIVE / "downloads"),
    str(CACHE_ROOT_RELATIVE / "staging"),
)
SHA256_HEX = frozenset("0123456789abcdef")


class OcrResourceError(RuntimeError):
    """Raised when an OCR import operation cannot safely proceed."""


@dataclass(frozen=True)
class OcrProjectLayout:
    project_root: Path
    requirements_input: Path
    requirements_target: Path
    wheelhouse_target: Path
    runtime_target: Path
    runtime_manifest_target: Path
    resource_root: Path
    resource_target: Path
    cache_root: Path
    build_environment: Path
    downloads: Path
    manual_model_downloads: Path
    evidence: Path
    staging: Path
    toolchain_python: Path
    toolchain_root: Path


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except OSError as exc:
        raise OcrResourceError("path cannot be inspected") from exc
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate))))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _assert_existing_chain(root: Path, candidate: Path) -> None:
    if _is_reparse(root):
        raise OcrResourceError("reparse point is not allowed")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise OcrResourceError("path escapes project root") from exc
    current = root
    for component in relative.parts:
        current = current / component
        if not current.exists():
            break
        if _is_reparse(current):
            raise OcrResourceError("reparse point is not allowed")


def assert_project_path(project_root: str | Path, candidate: str | Path) -> Path:
    """Return a project-contained path after rejecting traversals and reparse points."""
    root = _absolute(project_root)
    if not root.is_dir():
        raise OcrResourceError("project root is unavailable")
    target = _absolute(candidate)
    if not _is_within(root, target):
        raise OcrResourceError("path escapes project root")
    _assert_existing_chain(root, target)
    return target


def _assert_tree_safe(path: Path) -> None:
    if not path.exists():
        raise OcrResourceError("required path is missing")
    if _is_reparse(path):
        raise OcrResourceError("reparse point is not allowed")
    if path.is_file():
        return
    if not path.is_dir():
        raise OcrResourceError("path type is unsupported")
    pending = [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse(child):
                    raise OcrResourceError("reparse point is not allowed")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
                elif not entry.is_file(follow_symlinks=False):
                    raise OcrResourceError("path tree contains an unsupported entry")


def _ensure_parent(root: Path, path: Path) -> None:
    assert_project_path(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_project_path(root, path.parent)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise OcrResourceError("file cannot be read") from exc
    return digest.hexdigest()


def _valid_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - SHA256_HEX:
        raise OcrResourceError("artifact SHA-256 is invalid")
    return value


def _https_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048:
        raise OcrResourceError("HTTPS URL is invalid")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise OcrResourceError("HTTPS URL is invalid")
    return value


def _artifact_records(artifacts: Iterable[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    names: set[str] = set()
    for raw in artifacts:
        name = raw.get("name")
        size = raw.get("size")
        if not isinstance(name, str) or not name or name in names or len(name.encode("utf-8")) > 128:
            raise OcrResourceError("model artifact identity is invalid")
        if type(size) is not int or size < 1:
            raise OcrResourceError("model artifact size is invalid")
        url = _https_url(raw.get("url"))
        filename = Path(urllib.parse.urlparse(url).path).name
        if not filename or filename in {".", ".."} or any(character in filename for character in "\\/:"):
            raise OcrResourceError("model artifact URL filename is invalid")
        records.append({"name": name, "url": url, "size": size, "sha256": _valid_sha256(raw.get("sha256")), "filename": filename})
        names.add(name)
    expected = set(OCR_MODEL_IDENTITIES.values())
    if {str(record["name"]) for record in records} != expected:
        raise OcrResourceError("model artifacts do not match the frozen OCR model set")
    return tuple(records)


def project_layout(project_root: str | Path) -> OcrProjectLayout:
    root = _absolute(project_root)
    assert_project_path(root, root)
    cache_root = assert_project_path(root, root / CACHE_ROOT_RELATIVE)
    layout = OcrProjectLayout(
        project_root=root,
        requirements_input=assert_project_path(root, root / "packaging" / "requirements" / "ocr-paddle.in"),
        requirements_target=assert_project_path(root, root / "packaging" / "requirements" / "ocr-paddle.lock"),
        wheelhouse_target=assert_project_path(root, root / "packaging" / "wheelhouse" / RUNTIME_ID),
        runtime_target=assert_project_path(root, root / ".runtime-build" / "runtimes" / RUNTIME_ID),
        runtime_manifest_target=assert_project_path(root, root / ".runtime-build" / "manifests" / "runtimes" / f"{RUNTIME_ID}.json"),
        resource_root=assert_project_path(root, root / "resource-library"),
        resource_target=assert_project_path(root, root / "resource-library" / "ocr-models" / RESOURCE_ID),
        cache_root=cache_root,
        build_environment=assert_project_path(root, cache_root / "build-environment"),
        downloads=assert_project_path(root, cache_root / "downloads"),
        manual_model_downloads=assert_project_path(root, root / MANUAL_MODEL_ROOT_RELATIVE),
        evidence=assert_project_path(root, cache_root / "evidence"),
        staging=assert_project_path(root, cache_root / "staging"),
        toolchain_python=assert_project_path(root, root / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64" / "python.exe"),
        toolchain_root=assert_project_path(root, root / ".toolchains" / "Python-3.11.15"),
    )
    return layout


def _read_requirements(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OcrResourceError("OCR requirements input is unreadable") from exc
    if lines != list(DIRECT_REQUIREMENTS):
        raise OcrResourceError("OCR requirements input must contain exactly the frozen direct dependencies")
    return lines


def _cache_file(layout: OcrProjectLayout, artifact: Mapping[str, object]) -> Path:
    filename = artifact["filename"]
    assert isinstance(filename, str)
    return assert_project_path(layout.project_root, layout.manual_model_downloads / filename)


def _complete_artifact(path: Path, artifact: Mapping[str, object]) -> bool:
    if not path.exists():
        return False
    if not path.is_file() or _is_reparse(path):
        raise OcrResourceError("OCR cache artifact is unsafe")
    return path.stat().st_size == artifact["size"] and _sha256(path) == artifact["sha256"]


def plan_import(
    project_root: str | Path,
    *,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> dict[str, object]:
    """Build a fully read-only import preview."""
    layout = project_layout(project_root)
    requirements = _read_requirements(layout.requirements_input)
    records = _artifact_records(artifacts)
    models = []
    for artifact in records:
        cache_path = _cache_file(layout, artifact)
        models.append({
            "name": artifact["name"],
            "url": artifact["url"],
            "sizeBytes": artifact["size"],
            "sha256": artifact["sha256"],
            "cachePath": str(cache_path),
            "cacheHit": _complete_artifact(cache_path, artifact),
        })
    return {
        "schemaVersion": 1,
        "action": "ImportOcrResource",
        "mode": "preview",
        "requirements": requirements,
        "dependencyBytes": "unresolved",
        "modelBytes": sum(int(record["size"]) for record in records),
        "models": models,
        "license": {"url": "unresolved", "evidence": "unresolved"},
        "distribution": {
            "mode": "local-only",
            "sourceUrl": MODEL_SOURCE_URL,
            "licenseStatus": "unverified",
            "reason": "model artifact redistribution permission has not been verified",
        },
        "cache": {
            "root": str(layout.cache_root),
            "buildEnvironment": str(layout.build_environment),
            "downloads": str(layout.downloads),
            "manualModels": str(layout.manual_model_downloads),
            "evidence": str(layout.evidence),
            "staging": str(layout.staging),
        },
        "targets": {
            "runtime": str(layout.runtime_target),
            "runtimeManifest": str(layout.runtime_manifest_target),
            "lock": str(layout.requirements_target),
            "wheelhouse": str(layout.wheelhouse_target),
            "resource": str(layout.resource_target),
        },
        "applyChanges": [
            "create disposable OCR cache build environment",
            "resolve and hash-verify the complete ocr-paddle wheel closure in staging",
            "verify user-downloaded model archives against frozen size and SHA-256",
            "assemble and offline-probe the staged CPU runtime and OCR resource",
            "atomically publish the lock, wheelhouse, runtime, manifest, and local-only resource",
        ],
    }


def _copy_safe_tree(source: Path, destination: Path) -> None:
    _assert_tree_safe(source)
    if not source.is_dir() or destination.exists():
        raise OcrResourceError("model staging layout is invalid")
    destination.mkdir(parents=True)
    pending = [(source, destination)]
    while pending:
        original, copied = pending.pop()
        with os.scandir(original) as entries:
            for entry in entries:
                input_path = Path(entry.path)
                output_path = copied / entry.name
                if _is_reparse(input_path):
                    raise OcrResourceError("model source contains a reparse point")
                if entry.is_dir(follow_symlinks=False):
                    output_path.mkdir()
                    pending.append((input_path, output_path))
                elif entry.is_file(follow_symlinks=False):
                    shutil.copy2(input_path, output_path)
                else:
                    raise OcrResourceError("model source contains an unsupported entry")


def _package_files(package_root: Path) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for path in sorted(package_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.name == "resource.json":
            continue
        relative = str(path.relative_to(package_root)).replace("/", "\\")
        values[relative] = {"sizeBytes": path.stat().st_size, "sha256": _sha256(path)}
    if not values:
        raise OcrResourceError("OCR model package has no files")
    return values


def _write_json(path: Path, value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare_resource_package(
    stage_library: str | Path,
    model_roots: Mapping[str, Path],
    *,
    source_url: str,
    license_status: str,
) -> Path:
    """Create and fully validate an OCR package beneath a private staging library."""
    source_url = _https_url(source_url)
    if license_status != "unverified":
        raise OcrResourceError("OCR model license status is invalid")
    library = _absolute(stage_library)
    if library.exists() and _is_reparse(library):
        raise OcrResourceError("staging library is unsafe")
    library.mkdir(parents=True, exist_ok=True)
    _assert_tree_safe(library)
    if set(model_roots) != set(ENTRYPOINT_DIRECTORIES):
        raise OcrResourceError("OCR model staging roots are invalid")
    package = library / "ocr-models" / RESOURCE_ID
    if package.exists():
        raise OcrResourceError("OCR resource staging target already exists")
    for role, directory in ENTRYPOINT_DIRECTORIES.items():
        source = _absolute(model_roots[role])
        if not source.is_dir() or not (source / "inference.json").is_file():
            raise OcrResourceError("OCR model source is incomplete")
        _copy_safe_tree(source, package / directory)
    files = _package_files(package)
    entrypoints = {role: f"{directory}\\inference.json" for role, directory in ENTRYPOINT_DIRECTORIES.items()}
    if not set(entrypoints.values()) <= set(files):
        raise OcrResourceError("OCR model inference entrypoint is missing")
    manifest = {
        "schemaVersion": 2,
        "kind": "ocr-model",
        "resourceId": RESOURCE_ID,
        "resourceVersion": RUNTIME_FORMAT,
        "profile": "shared",
        "displayName": {"zh-CN": "PaddleOCR PP-OCRv5 Server", "en": "PaddleOCR PP-OCRv5 Server"},
        "description": {"zh-CN": "本地 PaddleOCR CPU 三模型资源。", "en": "Local PaddleOCR CPU three-model resource."},
        "runtimeFormat": RUNTIME_FORMAT,
        "distribution": {
            "mode": "local-only",
            "sourceUrl": source_url,
            "licenseStatus": license_status,
        },
        "entrypoints": entrypoints,
        "files": files,
        "metadata": {"models": dict(OCR_MODEL_IDENTITIES), "inference": dict(INFERENCE_SETTINGS)},
        "documentation": [],
    }
    _write_json(package / "resource.json", manifest)
    validated = ResourcePackage.load(library, package / "resource.json", "ocr-model")
    validated.verify_files(verify_hashes=True)
    return package


def _load_staged_package(package: Path) -> ResourcePackage:
    try:
        library = package.parents[1]
    except IndexError as exc:
        raise OcrResourceError("staged OCR package location is invalid") from exc
    value = ResourcePackage.load(library, package / "resource.json", "ocr-model")
    value.verify_files(verify_hashes=True)
    return value


def _remove_safe_tree(root: Path, target: Path) -> None:
    assert_project_path(root, target)
    if not target.exists():
        return
    _assert_tree_safe(target)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _publish_paths(
    project_root: Path,
    pairs: Iterable[tuple[Path, Path]],
    *,
    replacer: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> None:
    moved: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for staged, target in pairs:
            assert_project_path(project_root, staged)
            assert_project_path(project_root, target)
            _assert_tree_safe(staged)
            _ensure_parent(project_root, target)
            if target.exists():
                _assert_tree_safe(target)
                backup = target.with_name("." + target.name + ".ocr-backup-" + uuid.uuid4().hex)
                assert_project_path(project_root, backup)
                os.replace(target, backup)
                backups.append((target, backup))
            replacer(staged, target)
            moved.append((staged, target))
    except Exception as exc:
        for staged, target in reversed(moved):
            try:
                if target.exists():
                    _ensure_parent(project_root, staged)
                    os.replace(target, staged)
            except OSError:
                pass
        for target, backup in reversed(backups):
            try:
                if backup.exists():
                    os.replace(backup, target)
            except OSError:
                pass
        raise OcrResourceError("OCR installation transaction was rolled back") from exc
    for _, backup in backups:
        _remove_safe_tree(project_root, backup)


def install_resource_package(project_root: str | Path, staged_resource: str | Path) -> str:
    layout = project_layout(project_root)
    staged = assert_project_path(layout.project_root, staged_resource)
    staged_package = _load_staged_package(staged)
    if layout.resource_target.exists():
        existing = ResourcePackage.load(layout.resource_root, layout.resource_target / "resource.json", "ocr-model")
        existing.verify_files(verify_hashes=True)
        if existing.fingerprint == staged_package.fingerprint:
            return "idempotent"
        raise OcrResourceError("existing OCR resource fingerprint conflicts with the staged package")
    _publish_paths(layout.project_root, ((staged, layout.resource_target),))
    return "installed"


def publish_install_transaction(
    project_root: str | Path,
    *,
    staged_resource: str | Path,
    staged_runtime: str | Path,
    staged_lock: str | Path,
    staged_wheelhouse: str | Path,
    staged_manifest: str | Path,
    replacer: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> dict[str, str]:
    """Publish all formal OCR artifacts together, restoring every target on failure."""
    layout = project_layout(project_root)
    resource = assert_project_path(layout.project_root, staged_resource)
    staged_package = _load_staged_package(resource)
    resource_action = "installed"
    resource_pair: tuple[Path, Path] | None = (resource, layout.resource_target)
    if layout.resource_target.exists():
        existing = ResourcePackage.load(layout.resource_root, layout.resource_target / "resource.json", "ocr-model")
        existing.verify_files(verify_hashes=True)
        if existing.fingerprint != staged_package.fingerprint:
            raise OcrResourceError("existing OCR resource fingerprint conflicts with the staged package")
        resource_pair = None
        resource_action = "idempotent"
    manifest = assert_project_path(layout.project_root, staged_manifest)
    manifest_lock = assert_project_path(
        layout.project_root,
        manifest.parent.parent / "requirements" / f"{RUNTIME_ID}.lock",
    )
    formal_manifest_lock = assert_project_path(
        layout.project_root,
        layout.runtime_manifest_target.parent.parent / "requirements" / f"{RUNTIME_ID}.lock",
    )
    pairs = [
        (assert_project_path(layout.project_root, staged_lock), layout.requirements_target),
        (assert_project_path(layout.project_root, staged_wheelhouse), layout.wheelhouse_target),
        (assert_project_path(layout.project_root, staged_runtime), layout.runtime_target),
        (manifest_lock, formal_manifest_lock),
        (manifest, layout.runtime_manifest_target),
    ]
    if resource_pair is not None:
        pairs.append(resource_pair)
    _publish_paths(layout.project_root, pairs, replacer=replacer)
    return {"resource": resource_action, "runtime": "installed", "lock": "installed", "wheelhouse": "installed", "manifestLock": "installed", "manifest": "installed"}


def reset_ocr_runtime(project_root: str | Path, *, apply: bool = False) -> dict[str, object]:
    layout = project_layout(project_root)
    result = {
        "schemaVersion": 1,
        "action": "ResetOcrRuntime",
        "mode": "apply" if apply else "preview",
        "targets": [str(layout.runtime_target)],
        "exists": layout.runtime_target.exists(),
    }
    if apply:
        _remove_safe_tree(layout.project_root, layout.runtime_target)
    return result


def clean_ocr_artifacts(project_root: str | Path, *, apply: bool = False) -> dict[str, object]:
    layout = project_layout(project_root)
    targets = [assert_project_path(layout.project_root, layout.project_root / Path(relative)) for relative in CLEANABLE_CACHE_RELATIVES]
    records: list[dict[str, object]] = []
    for target in targets:
        if target.exists():
            try:
                _assert_tree_safe(target)
                size = sum(path.stat().st_size for path in target.rglob("*") if path.is_file()) if target.is_dir() else target.stat().st_size
            except (OcrResourceError, OSError) as exc:
                if apply:
                    raise OcrResourceError("OCR cache cannot be safely inspected") from exc
                records.append({"path": str(target), "exists": True, "bytes": "unavailable", "inspection": "uninspectable"})
                continue
            records.append({"path": str(target), "exists": True, "bytes": size})
        else:
            records.append({"path": str(target), "exists": False, "bytes": 0})
    if apply:
        for target in targets:
            _remove_safe_tree(layout.project_root, target)
    return {"schemaVersion": 1, "action": "CleanOcrArtifacts", "mode": "apply" if apply else "preview", "targets": records}


def resolve_manual_model_archives(
    project_root: str | Path,
    *,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> dict[str, Path]:
    """Resolve complete user-downloaded archives without performing network access."""
    layout = project_layout(project_root)
    records = _artifact_records(artifacts)
    archives: dict[str, Path] = {}
    problems: list[str] = []
    for artifact in records:
        name = str(artifact["name"])
        path = _cache_file(layout, artifact)
        if not path.exists():
            problems.append(f"missing:{name}")
        elif not _complete_artifact(path, artifact):
            problems.append(f"invalid:{name}")
        else:
            archives[name] = path
    if problems:
        raise OcrResourceError(
            "manual OCR model archives are missing or invalid; "
            f"see {MODEL_DOWNLOAD_GUIDE}: {','.join(problems)}"
        )
    return archives


def _model_root(path: str | Path) -> Path:
    root = _absolute(path)
    if not root.is_dir() or _is_reparse(root):
        raise OcrResourceError("ocr_models_required: model archive directory is missing or unsafe")
    _assert_tree_safe(root)
    return root


def resolve_model_archives(
    model_root: Path,
    *,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> dict[str, Path]:
    """Resolve exact local archives from an explicit, caller-owned directory."""
    root = _model_root(model_root)
    records = _artifact_records(artifacts)
    archives: dict[str, Path] = {}
    problems: list[str] = []
    for artifact in records:
        name = str(artifact["name"])
        filename = str(artifact["filename"])
        archive = root / filename
        if not _is_within(root, _absolute(archive)):
            raise OcrResourceError("ocr_models_required: model archive path escapes its root")
        if not archive.exists():
            problems.append(f"missing:{name}")
        elif not _complete_artifact(archive, artifact):
            problems.append(f"invalid:{name}")
        else:
            archives[name] = archive
    if problems:
        raise OcrResourceError("ocr_models_required: " + ",".join(problems))
    return archives


def has_complete_model_archives(
    project_root: str | Path,
    *,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> bool:
    """Return true only when every expected manual archive filename is present."""
    layout = project_layout(project_root)
    root = layout.manual_model_downloads
    if not root.exists():
        return False
    root = _model_root(root)
    return all((root / str(record["filename"])).is_file() for record in _artifact_records(artifacts))


def _manual_model_input_state(
    project_root: str | Path,
    *,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> str:
    layout = project_layout(project_root)
    if layout.resource_target.exists():
        package = ResourcePackage.load(
            layout.resource_root,
            layout.resource_target / "resource.json",
            "ocr-model",
        )
        package.verify_files(verify_hashes=True)
        return "idempotent"
    root = layout.manual_model_downloads
    if not root.exists():
        return "missing"
    root = _model_root(root)
    if not any(root.iterdir()):
        return "missing"
    if not has_complete_model_archives(layout.project_root, artifacts=artifacts):
        raise OcrResourceError(
            "manual OCR model archives are incomplete or invalid; "
            f"see {MODEL_DOWNLOAD_GUIDE} and place all exact archives in {MANUAL_MODEL_ROOT_RELATIVE}"
        )
    return "ready"


def stage_model_resource(
    model_root: Path,
    stage_library: Path,
    *,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> Path:
    """Build one verified local OCR resource package from frozen local archives."""
    archives = resolve_model_archives(model_root, artifacts=artifacts)
    library = _absolute(stage_library)
    if library.exists():
        if _is_reparse(library):
            raise OcrResourceError("OCR model staging library is unsafe")
        _assert_tree_safe(library)
    library.mkdir(parents=True, exist_ok=True)
    scratch = library / (".extract-" + uuid.uuid4().hex)
    try:
        roots = {
            "detection": _safe_extract_tar(archives["PP-OCRv5_server_det"], scratch / "detection"),
            "recognition": _safe_extract_tar(archives["PP-OCRv5_server_rec"], scratch / "recognition"),
            "textlineOrientation": _safe_extract_tar(
                archives["PP-LCNet_x1_0_textline_ori"], scratch / "textline-orientation",
            ),
        }
        return prepare_resource_package(
            library,
            roots,
            source_url=MODEL_SOURCE_URL,
            license_status="unverified",
        )
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


def _run(command: list[str], *, label: str, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        raise OcrResourceError(f"{label} could not start") from exc
    if completed.returncode != 0:
        raise OcrResourceError(f"{label} failed with exit code {completed.returncode}")
    return completed.stdout


def _build_environment(layout: OcrProjectLayout) -> Path:
    expected = "3.11.15"
    rebuild = not layout.build_environment.exists()
    build_python = layout.build_environment / "Scripts" / "python.exe"
    if not rebuild and build_python.is_file():
        try:
            version = _run([str(build_python), "-B", "-I", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"], label="OCR build environment verification").strip()
            rebuild = version != expected
        except OcrResourceError:
            rebuild = True
    else:
        rebuild = True
    if rebuild:
        _remove_safe_tree(layout.project_root, layout.build_environment)
        if not layout.toolchain_python.is_file():
            raise OcrResourceError("project-local CPython build interpreter is unavailable")
        _ensure_parent(layout.project_root, layout.build_environment)
        _run([str(layout.toolchain_python), "-B", "-I", "-m", "venv", str(layout.build_environment)], label="OCR build environment creation")
    if not build_python.is_file():
        raise OcrResourceError("OCR build environment interpreter is unavailable")
    _run([str(build_python), "-B", "-I", "-m", "pip", "--version"], label="OCR build environment ensurepip verification")
    return build_python


def _safe_extract_tar(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                relative = Path(member.name)
                target = _absolute(root / relative)
                if relative.is_absolute() or ".." in relative.parts or not _is_within(root, target) or member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise OcrResourceError("OCR model archive contains an unsafe member")
                archive.extract(member, root)
    except OcrResourceError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise OcrResourceError("OCR model archive cannot be extracted") from exc
    _assert_tree_safe(root)
    candidates = [path.parent for path in root.rglob("inference.json") if path.is_file()]
    if len(candidates) != 1:
        raise OcrResourceError("OCR model archive must contain one inference.json root")
    return candidates[0]


def _resolve_and_stage_runtime(layout: OcrProjectLayout, stage: Path, build_python: Path) -> tuple[Path, Path, Path]:
    stage_requirements = stage / "requirements"
    stage_wheelhouse = stage / "wheelhouse"
    source_wheelhouse = assert_project_path(layout.project_root, layout.manual_model_downloads.parent / "wheels")
    if not source_wheelhouse.is_dir():
        raise OcrResourceError("project-local OCR wheel cache is unavailable")
    _assert_tree_safe(source_wheelhouse)
    stage_requirements.mkdir(parents=True)
    shutil.copy2(layout.requirements_input, stage_requirements / "ocr-paddle.in")
    _run([
        str(build_python), "-B", "-I", str(REPOSITORY_ROOT / "packaging" / "scripts" / "resolve_wheels.py"), RUNTIME_ID,
        "--requirements-root", str(stage_requirements), "--wheelhouse-root", str(stage_wheelhouse),
        "--source-wheelhouse", str(source_wheelhouse), "--python", str(build_python),
    ], label="OCR dependency resolution")
    _run([
        str(build_python), "-B", "-I", str(REPOSITORY_ROOT / "packaging" / "scripts" / "verify_locks.py"),
        "--requirements-root", str(stage_requirements), "--wheelhouse-root", str(stage_wheelhouse), RUNTIME_ID,
    ], label="OCR dependency lock verification")
    install_root = stage / "install"
    _run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPOSITORY_ROOT / "packaging" / "scripts" / "build_cpython311_runtime.ps1"),
        "-PythonSourceRoot", str(layout.toolchain_root), "-OutputRoot", str(install_root), "-ReuseExistingBuild",
    ], label="OCR base runtime assembly")
    _run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPOSITORY_ROOT / "packaging" / "scripts" / "assemble_runtime.ps1"),
        "-BaseRuntime", str(install_root / "runtimes" / "_base"), "-DestinationRuntime", str(install_root / "runtimes" / RUNTIME_ID),
        "-RequirementsLock", str(stage_requirements / "ocr-paddle.lock"), "-Wheelhouse", str(stage_wheelhouse / RUNTIME_ID),
        "-OwnerSource", str(REPOSITORY_ROOT / "workers" / "ocr" / "src" / "anima_ocr_worker"), "-BuildPython", str(build_python), "-KeepSetuptools",
    ], label="OCR runtime assembly")
    _remove_safe_tree(layout.project_root, install_root / "runtimes" / "_base")
    _run([
        str(build_python), "-B", "-I", str(REPOSITORY_ROOT / "packaging" / "scripts" / "generate_runtime_manifests.py"),
        "--install-root", str(install_root), "--requirements-root", str(stage_requirements),
        "--include-ocr-paddle", "--runtime-id", RUNTIME_ID,
    ], label="OCR runtime manifest generation")
    return install_root / "runtimes" / RUNTIME_ID, stage_requirements / "ocr-paddle.lock", stage_wheelhouse / RUNTIME_ID


_OFFLINE_PROBE = """\
import json
import os
import socket
import sys
from pathlib import Path

def _blocked(*args, **kwargs):
    raise RuntimeError('network is blocked during OCR probe')

socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'

import paddle
import paddleocr
import paddlex
from PIL import Image
from anima_ocr_worker.model import create_paddle_engine
from anima_ocr_worker.resource import load_ocr_resource

root = Path(sys.argv[1])
fingerprint = sys.argv[2]
resource = load_ocr_resource(root, 'ocr-models\\\\ocr-ppocrv5-server-paddle-v1\\\\resource.json', fingerprint)
engine = create_paddle_engine(resource)
result = engine.predict(Image.new('RGB', (64, 64), 'white'))
if not isinstance(result, list) or len(result) != 1:
    raise RuntimeError('OCR functional sample returned an invalid result')
value = {'paddle': paddle.__version__, 'paddleocr': paddleocr.__version__, 'paddlex': paddlex.__version__, 'cuda': paddle.device.is_compiled_with_cuda(), 'device': paddle.get_device()}
if value != {'paddle': '3.2.2', 'paddleocr': '3.7.0', 'paddlex': '3.7.2', 'cuda': False, 'device': 'cpu'}:
    raise RuntimeError('OCR runtime identity is not frozen')
print(json.dumps(value, sort_keys=True))
"""


def _offline_probe(runtime: Path, library: Path, package: Path) -> dict[str, object]:
    staged = _load_staged_package(package)
    output = _run([
        str(runtime / "python.exe"), "-B", "-I", "-c", _OFFLINE_PROBE, str(library), staged.fingerprint,
    ], label="offline OCR runtime probe", cwd=runtime.parent.parent)
    try:
        value = json.loads(output.strip())
    except json.JSONDecodeError as exc:
        raise OcrResourceError("offline OCR runtime probe returned invalid JSON") from exc
    expected = {"paddle": "3.2.2", "paddleocr": "3.7.0", "paddlex": "3.7.2", "cuda": False, "device": "cpu"}
    if value != expected:
        raise OcrResourceError("offline OCR runtime probe identity is invalid")
    return value


def import_local_model_resource(
    project_root: str | Path,
    *,
    model_root: str | Path,
    staging_root: str | Path,
    runtime: str | Path,
    artifacts: Iterable[Mapping[str, object]] = MODEL_ARTIFACTS,
) -> dict[str, object]:
    """Import local OCR model archives using an already-published OCR CPU runtime."""
    layout = project_layout(project_root)
    root = layout.project_root
    model_directory = assert_project_path(root, model_root)
    stage_parent = assert_project_path(root, staging_root)
    runtime_root = assert_project_path(root, runtime)
    if not (runtime_root / "python.exe").is_file():
        raise OcrResourceError("published OCR CPU runtime is unavailable for model verification")
    resolve_model_archives(model_directory, artifacts=artifacts)
    if stage_parent.exists():
        _assert_tree_safe(stage_parent)
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="import-", dir=stage_parent))
    try:
        library = stage / "resource-library"
        resource = stage_model_resource(model_directory, library, artifacts=artifacts)
        probe = _offline_probe(runtime_root, library, resource)
        published = install_resource_package(root, resource)
        return {"resource": published, "probe": probe}
    finally:
        if stage.exists():
            _remove_safe_tree(root, stage)
        if stage_parent.exists() and not any(stage_parent.iterdir()):
            _remove_safe_tree(root, stage_parent)


def import_available_local_model_resource(project_root: str | Path) -> dict[str, object] | None:
    """Import supplied archives, or report an already verified package without rebuilding runtime."""
    layout = project_layout(project_root)
    state = _manual_model_input_state(layout.project_root)
    if state == "missing":
        return None
    if state == "idempotent":
        return {"resource": "idempotent"}
    return import_local_model_resource(
        layout.project_root,
        model_root=layout.manual_model_downloads,
        staging_root=assert_project_path(
            layout.project_root,
            layout.project_root / MODEL_ONLY_STAGING_RELATIVE,
        ),
        runtime=layout.runtime_target,
    )


def import_ocr_resource(project_root: str | Path, *, apply: bool = False) -> dict[str, object]:
    preview = plan_import(project_root)
    if not apply:
        return preview
    layout = project_layout(project_root)
    records = _artifact_records(MODEL_ARTIFACTS)
    archives = resolve_manual_model_archives(project_root, artifacts=records)
    layout.cache_root.mkdir(parents=True, exist_ok=True)
    assert_project_path(layout.project_root, layout.cache_root)
    layout.staging.mkdir(parents=True, exist_ok=True)
    assert_project_path(layout.project_root, layout.staging)
    stage = Path(tempfile.mkdtemp(prefix="import-", dir=layout.staging))
    try:
        build_python = _build_environment(layout)
        extracted_root = stage / "models"
        model_roots = {
            "detection": _safe_extract_tar(archives["PP-OCRv5_server_det"], extracted_root / "detection"),
            "recognition": _safe_extract_tar(archives["PP-OCRv5_server_rec"], extracted_root / "recognition"),
            "textlineOrientation": _safe_extract_tar(archives["PP-LCNet_x1_0_textline_ori"], extracted_root / "textline-orientation"),
        }
        resource = prepare_resource_package(
            stage / "resource-library",
            model_roots,
            source_url=MODEL_SOURCE_URL,
            license_status="unverified",
        )
        runtime, lock, wheelhouse = _resolve_and_stage_runtime(layout, stage, build_python)
        manifest = stage / "install" / "manifests" / "runtimes" / f"{RUNTIME_ID}.json"
        probe = _offline_probe(runtime, stage / "resource-library", resource)
        published = publish_install_transaction(
            layout.project_root,
            staged_resource=resource,
            staged_runtime=runtime,
            staged_lock=lock,
            staged_wheelhouse=wheelhouse,
            staged_manifest=manifest,
        )
    except Exception:
        # Staging remains cache-only for diagnosis; no formal OCR target has been published on failure.
        raise
    return {**preview, "mode": "apply", "probe": probe, "published": published}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    importer = subcommands.add_parser("import")
    importer.add_argument("--project-root", type=Path, required=True)
    importer.add_argument("--apply", action="store_true")
    reset = subcommands.add_parser("reset")
    reset.add_argument("--project-root", type=Path, required=True)
    reset.add_argument("--apply", action="store_true")
    cleanup = subcommands.add_parser("clean")
    cleanup.add_argument("--project-root", type=Path, required=True)
    cleanup.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "import":
            result = import_ocr_resource(arguments.project_root, apply=arguments.apply)
        elif arguments.command == "reset":
            result = reset_ocr_runtime(arguments.project_root, apply=arguments.apply)
        else:
            result = clean_ocr_artifacts(arguments.project_root, apply=arguments.apply)
    except OcrResourceError as exc:
        manual_models_unavailable = MODEL_DOWNLOAD_GUIDE in str(exc)
        error = {
            "error": "ocr_manual_models_unavailable" if manual_models_unavailable else "ocr_resource_error",
            "message": str(exc),
        }
        if manual_models_unavailable:
            error["guide"] = MODEL_DOWNLOAD_GUIDE
        sys.stderr.write(json.dumps(error, ensure_ascii=True, sort_keys=True) + "\n")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
