"""Validation and state inspection for optional OCR distribution components."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = REPOSITORY_ROOT / "core" / "src"
if str(SCRIPT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_ROOT))
if str(CORE_SOURCE) not in os.sys.path:
    os.sys.path.insert(0, str(CORE_SOURCE))

from anima_core.resource_catalog_package import ResourcePackage
from anima_core.runtime_manifest import RuntimeBundleManifestV1


RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RuntimeState = Literal["absent", "complete", "partial"]
OcrInstallationState = Literal["none", "cpu", "gpu"]


class OcrComponentError(RuntimeError):
    """Raised when an optional OCR component or installed state is unsafe."""


@dataclass(frozen=True)
class ComponentFileV1:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ComponentManifestV1:
    component_id: Literal["ocr-cpu", "ocr-gpu"]
    runtime_ids: tuple[str, ...]
    requires_resource_id: str
    files: tuple[ComponentFileV1, ...]


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_reparse(path: Path) -> bool:
    information = os.lstat(path)
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _within(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate))))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _safe_root(path: str | Path, *, description: str) -> Path:
    root = _absolute(path)
    if not root.is_dir():
        raise OcrComponentError(f"{description} is unavailable")
    if _is_reparse(root):
        raise OcrComponentError("reparse point is not allowed")
    return root


def _assert_existing_chain(root: Path, candidate: Path) -> None:
    if not _within(root, candidate):
        raise OcrComponentError("path escapes its root")
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        if _is_reparse(current):
            raise OcrComponentError("reparse point is not allowed")


def _normal_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OcrComponentError("component file path is invalid")
    normalized = value.replace("/", "\\")
    if normalized.startswith("\\") or ":" in normalized:
        raise OcrComponentError("component file path is invalid")
    parts = normalized.split("\\")
    if any(part in {"", ".", ".."} for part in parts):
        raise OcrComponentError("component file path is invalid")
    return "\\".join(parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise OcrComponentError("component file cannot be read") from exc
    return digest.hexdigest()


def _payload_files(payload: Path) -> dict[str, Path]:
    if not payload.is_dir() or _is_reparse(payload):
        raise OcrComponentError("component payload is unavailable")
    values: dict[str, Path] = {}
    pending = [payload]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise OcrComponentError("component payload cannot be inspected") from exc
        for entry in entries:
            child = Path(entry.path)
            if _is_reparse(child):
                raise OcrComponentError("reparse point is not allowed")
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)
            elif entry.is_file(follow_symlinks=False):
                relative = _normal_relative(str(child.relative_to(payload)))
                key = relative.casefold()
                if key in values:
                    raise OcrComponentError("component payload has a case collision")
                values[key] = child
            else:
                raise OcrComponentError("component payload contains an unsupported entry")
    return values


def _component_file(value: object) -> ComponentFileV1:
    if not isinstance(value, dict) or set(value) != {"path", "sizeBytes", "sha256"}:
        raise OcrComponentError("component file record is invalid")
    path = _normal_relative(value["path"])
    size = value["sizeBytes"]
    digest = value["sha256"]
    if type(size) is not int or size < 0:
        raise OcrComponentError("component file size is invalid")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise OcrComponentError("component file SHA-256 is invalid")
    return ComponentFileV1(path, size, digest)


def load_component_manifest(component_root: Path) -> ComponentManifestV1:
    """Load and fully verify one OCR component payload without importing Paddle."""
    root = _safe_root(component_root, description="component root")
    manifest_path = root / "component.json"
    _assert_existing_chain(root, manifest_path)
    if not manifest_path.is_file() or _is_reparse(manifest_path):
        raise OcrComponentError("component manifest is missing")
    try:
        raw = manifest_path.read_bytes()
        if not raw or len(raw) > MAX_MANIFEST_BYTES:
            raise OcrComponentError("component manifest is invalid")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OcrComponentError("component manifest is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion", "componentId", "runtimeIds", "requiresResourceId", "files",
    }:
        raise OcrComponentError("component manifest fields are invalid")
    if value["schemaVersion"] != 1:
        raise OcrComponentError("component manifest schema is invalid")
    component_id = value["componentId"]
    runtime_ids = value["runtimeIds"]
    expected = {
        "ocr-cpu": ("ocr-paddle",),
        "ocr-gpu": ("ocr-paddle-gpu",),
    }
    if component_id not in expected or runtime_ids != list(expected[component_id]):
        raise OcrComponentError("component runtime identity is invalid")
    if value["requiresResourceId"] != RESOURCE_ID:
        raise OcrComponentError("component resource identity is invalid")
    raw_files = value["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise OcrComponentError("component files are invalid")
    files = tuple(_component_file(record) for record in raw_files)
    paths = [record.path for record in files]
    keys = [path.casefold() for path in paths]
    if len(set(keys)) != len(keys):
        raise OcrComponentError("component file paths are duplicate or case-colliding")
    if paths != sorted(paths, key=str.casefold):
        raise OcrComponentError("component file paths are not sorted")
    payload = root / "payload"
    _assert_existing_chain(root, payload)
    actual = _payload_files(payload)
    expected_keys = set(keys)
    extra = set(actual) - expected_keys
    missing = expected_keys - set(actual)
    if extra:
        raise OcrComponentError("component payload contains extra files")
    if missing:
        raise OcrComponentError("component payload has missing files")
    for record in files:
        target = actual[record.path.casefold()]
        if target.stat().st_size != record.size_bytes:
            raise OcrComponentError("component file size mismatch")
        if _sha256(target) != record.sha256:
            raise OcrComponentError("component file SHA-256 mismatch")
    return ComponentManifestV1(component_id, expected[component_id], RESOURCE_ID, files)


def resolve_runtime_root(app_root: Path) -> Path:
    """Resolve the standard or development runtime root for an application."""
    root = _safe_root(app_root, description="application root")
    standard_core = root / "runtimes" / "core" / "python.exe"
    _assert_existing_chain(root, standard_core)
    if standard_core.is_file():
        return root
    development = root / ".runtime-build"
    _assert_existing_chain(root, development)
    development_core = development / "runtimes" / "core" / "python.exe"
    if development_core.is_file():
        return development
    raise OcrComponentError("application core runtime is missing")


def _runtime_state(runtime_root: Path, runtime_id: str) -> RuntimeState:
    targets = (
        runtime_root / "runtimes" / runtime_id,
        runtime_root / "manifests" / "runtimes" / f"{runtime_id}.json",
        runtime_root / "manifests" / "requirements" / f"{runtime_id}.lock",
    )
    for target in targets:
        _assert_existing_chain(runtime_root, target)
    exists = tuple(target.exists() for target in targets)
    if not any(exists):
        return "absent"
    if not all(exists) or not targets[0].is_dir() or not targets[1].is_file() or not targets[2].is_file():
        return "partial"
    try:
        manifest = RuntimeBundleManifestV1.load(targets[1])
        if manifest.runtime.runtimeId != runtime_id or manifest.runtime.owner != "ocr":
            return "partial"
        manifest.verify_files(runtime_root)
    except Exception:
        return "partial"
    return "complete"


def _resource_state(app_root: Path) -> RuntimeState:
    library = app_root / "resource-library"
    package = library / "ocr-models" / RESOURCE_ID
    manifest = package / "resource.json"
    _assert_existing_chain(app_root, package)
    if not package.exists():
        return "absent"
    if not package.is_dir() or not manifest.is_file():
        return "partial"
    try:
        resource = ResourcePackage.load(library, manifest, "ocr-model")
        if resource.resource_id != RESOURCE_ID:
            return "partial"
        resource.verify_files(verify_hashes=True)
    except Exception:
        return "partial"
    return "complete"


def inspect_ocr_installation(app_root: Path) -> OcrInstallationState:
    """Return the verified formal OCR state, rejecting every partial combination."""
    root = _safe_root(app_root, description="application root")
    runtime_root = resolve_runtime_root(root)
    cpu = _runtime_state(runtime_root, "ocr-paddle")
    gpu = _runtime_state(runtime_root, "ocr-paddle-gpu")
    resource = _resource_state(root)
    if gpu != "absent" and cpu == "absent":
        raise OcrComponentError("GPU OCR installation requires complete CPU fallback")
    if cpu == gpu == resource == "absent":
        return "none"
    if cpu == "complete" and resource == "complete" and gpu == "absent":
        return "cpu"
    if cpu == "complete" and resource == "complete" and gpu == "complete":
        return "gpu"
    raise OcrComponentError("partial OCR installation is not usable")


def _application_path(root: Path, candidate: Path, *, label: str) -> Path:
    target = _absolute(candidate)
    if not _within(root, target):
        raise OcrComponentError(f"{label} must remain inside the application root")
    _assert_existing_chain(root, target)
    return target


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or _is_reparse(source) or destination.exists():
        raise OcrComponentError("component payload input is invalid")
    _payload_files(source)
    shutil.copytree(source, destination)


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or _is_reparse(source):
        raise OcrComponentError("component payload input is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _stage_component(component_root: Path, stage_root: Path, runtime_id: str) -> None:
    load_component_manifest(component_root)
    payload = component_root / "payload"
    lock_input = payload / "packaging" / "requirements" / f"{runtime_id}.lock"
    lock_mirror = payload / "manifests" / "requirements" / f"{runtime_id}.lock"
    if not lock_input.is_file() or not lock_mirror.is_file() or lock_input.read_bytes() != lock_mirror.read_bytes():
        raise OcrComponentError("component lock input differs from the lock mirror")
    _copy_tree(payload / "runtimes" / runtime_id, stage_root / "runtimes" / runtime_id)
    _copy_file(
        payload / "manifests" / "runtimes" / f"{runtime_id}.json",
        stage_root / "manifests" / "runtimes" / f"{runtime_id}.json",
    )
    _copy_file(lock_mirror, stage_root / "manifests" / "requirements" / f"{runtime_id}.lock")
    try:
        manifest = RuntimeBundleManifestV1.load(
            stage_root / "manifests" / "runtimes" / f"{runtime_id}.json"
        )
        if manifest.runtime.runtimeId != runtime_id or manifest.runtime.owner != "ocr":
            raise OcrComponentError("component runtime manifest identity is invalid")
        manifest.verify_files(stage_root)
    except OcrComponentError:
        raise
    except Exception as exc:
        raise OcrComponentError("component runtime manifest verification failed") from exc


def _default_probe(runtime_id: str, runtime: Path, library: Path) -> dict[str, object]:
    package = ResourcePackage.load(
        library,
        library / "ocr-models" / RESOURCE_ID / "resource.json",
        "ocr-model",
    )
    package.verify_files(verify_hashes=True)
    probe_device = "cpu" if runtime_id == "ocr-paddle" else "cuda"
    script = """
import json
import os
import socket
from pathlib import Path

def blocked(*args, **kwargs):
    raise RuntimeError('network is blocked during OCR probe')

socket.socket.connect = blocked
socket.socket.connect_ex = blocked
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'

import paddle
import paddleocr
import paddlex
from PIL import Image
from anima_ocr_worker.model import create_paddle_engine
from anima_ocr_worker.resource import load_ocr_resource

library = Path(__import__('sys').argv[1])
fingerprint = __import__('sys').argv[2]
engine = create_paddle_engine(load_ocr_resource(
    library, r'ocr-models\\ocr-ppocrv5-server-paddle-v1\\resource.json', fingerprint,
), device=__ANIMA_PROBE_DEVICE__)
result = engine.predict(Image.new('RGB', (64, 64), 'white'))
if not isinstance(result, list) or len(result) != 1:
    raise RuntimeError('OCR functional sample returned an invalid result')
print(json.dumps({
    'paddle': paddle.__version__, 'paddleocr': paddleocr.__version__,
    'paddlex': paddlex.__version__, 'cuda': paddle.device.is_compiled_with_cuda(),
    'device': paddle.get_device(),
}, sort_keys=True))
""".replace("__ANIMA_PROBE_DEVICE__", repr(probe_device))
    environment = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(name, None)
    try:
        completed = subprocess.run(
            [str(runtime / "python.exe"), "-B", "-I", "-c", script, str(library), package.fingerprint],
            cwd=runtime.parent.parent,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=900,
        )
        if completed.returncode != 0:
            raise OcrComponentError(f"offline {runtime_id} probe failed")
        value = json.loads(completed.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise OcrComponentError(f"offline {runtime_id} probe could not be verified") from exc
    if not isinstance(value, dict):
        raise OcrComponentError(f"offline {runtime_id} probe failed")
    common = {"paddle": "3.2.2", "paddleocr": "3.7.0", "paddlex": "3.7.2"}
    if any(value.get(key) != expected for key, expected in common.items()):
        raise OcrComponentError(f"offline {runtime_id} probe identity is invalid")
    if runtime_id == "ocr-paddle":
        if value.get("cuda") is not False or value.get("device") != "cpu":
            raise OcrComponentError("offline CPU probe device identity is invalid")
    elif value.get("cuda") is not True or not str(value.get("device", "")).lower().startswith(("gpu", "cuda")):
        raise OcrComponentError("offline GPU probe device identity is invalid")
    return value


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if _is_reparse(path):
        raise OcrComponentError("reparse point is not allowed")
    if path.is_dir():
        _payload_files(path)
        shutil.rmtree(path)
    else:
        path.unlink()


def _publish_pairs(
    pairs: list[tuple[Path, Path]],
    *,
    replacer: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None],
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    moved: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for source, target in pairs:
            if not source.exists() or _is_reparse(source):
                raise OcrComponentError("staged OCR target is unavailable")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if _is_reparse(target):
                    raise OcrComponentError("reparse point is not allowed")
                backup = target.with_name("." + target.name + ".ocr-backup-" + uuid.uuid4().hex)
                os.replace(target, backup)
                backups.append((target, backup))
            replacer(source, target)
            moved.append((source, target))
    except Exception as exc:
        _rollback_pairs(moved, backups)
        if isinstance(exc, OcrComponentError):
            raise
        raise OcrComponentError("OCR installation transaction was rolled back") from exc
    return moved, backups


def _rollback_pairs(moved: list[tuple[Path, Path]], backups: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(moved):
        try:
            if target.exists() and not source.exists():
                os.replace(target, source)
        except OSError:
            pass
    for target, backup in reversed(backups):
        try:
            if backup.exists():
                os.replace(backup, target)
        except OSError:
            pass


def install_optional_ocr(
    app_root: Path,
    *,
    mode: Literal["none", "cpu", "gpu"],
    model_root: Path,
    model_stager: Callable[[Path, Path], Path] | None = None,
    probe: Callable[[str, Path, Path], object] = _default_probe,
    replacer: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> dict[str, object]:
    """Install a verified optional OCR state without changing formal targets on failure."""
    root = _safe_root(app_root, description="application root")
    if mode not in {"none", "cpu", "gpu"}:
        raise OcrComponentError("OCR mode is invalid")
    current = inspect_ocr_installation(root)
    if mode == "none":
        return {"status": "unchanged", "runtimeIds": [] if current == "none" else ["ocr-paddle"] + (["ocr-paddle-gpu"] if current == "gpu" else [])}
    archives = _application_path(root, model_root, label="model archive directory")
    if not archives.is_dir() or _is_reparse(archives):
        raise OcrComponentError("ocr_models_required: model archive directory is missing or unsafe")
    desired = mode
    if current == desired or (current == "gpu" and desired == "cpu"):
        return {"status": "ready", "runtimeIds": ["ocr-paddle"] + (["ocr-paddle-gpu"] if current == "gpu" else [])}
    runtime_root = resolve_runtime_root(root)
    component_root = _application_path(root, root / "optional-components", label="optional component directory")
    required_runtime_ids = ("ocr-paddle", "ocr-paddle-gpu") if current == "none" and mode == "gpu" else (
        ("ocr-paddle",) if current == "none" else ("ocr-paddle-gpu",)
    )
    component_paths = {
        "ocr-paddle": component_root / "ocr-cpu",
        "ocr-paddle-gpu": component_root / "ocr-gpu",
    }
    for runtime_id in required_runtime_ids:
        path = component_paths[runtime_id]
        _assert_existing_chain(root, path)
        load_component_manifest(path)
    stage = Path(tempfile.mkdtemp(prefix=".ocr-install-", dir=root))
    moved: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    try:
        staged_install = stage / "install"
        staged_install.mkdir()
        for runtime_id in required_runtime_ids:
            _stage_component(component_paths[runtime_id], staged_install, runtime_id)
        resource_pair: tuple[Path, Path] | None = None
        if current == "none":
            if model_stager is None:
                from ocr_resource import stage_model_resource

                model_stager = stage_model_resource
            staged_library = stage / "resource-library"
            try:
                staged_resource = model_stager(archives, staged_library)
                resource = ResourcePackage.load(staged_library, staged_resource / "resource.json", "ocr-model")
                resource.verify_files(verify_hashes=True)
            except Exception as exc:
                message = str(exc)
                if "ocr_models_required" in message:
                    raise OcrComponentError(message) from exc
                raise OcrComponentError("ocr_models_required: local model archives could not be staged") from exc
            resource_pair = (
                staged_resource,
                root / "resource-library" / "ocr-models" / RESOURCE_ID,
            )
            probe_library = staged_library
        else:
            probe_library = root / "resource-library"
        for runtime_id in required_runtime_ids:
            probe(runtime_id, staged_install / "runtimes" / runtime_id, probe_library)
        pairs = [
            (staged_install / "runtimes" / runtime_id, runtime_root / "runtimes" / runtime_id)
            for runtime_id in required_runtime_ids
        ]
        pairs.extend(
            (staged_install / "manifests" / "runtimes" / f"{runtime_id}.json", runtime_root / "manifests" / "runtimes" / f"{runtime_id}.json")
            for runtime_id in required_runtime_ids
        )
        pairs.extend(
            (staged_install / "manifests" / "requirements" / f"{runtime_id}.lock", runtime_root / "manifests" / "requirements" / f"{runtime_id}.lock")
            for runtime_id in required_runtime_ids
        )
        if resource_pair is not None:
            pairs.append(resource_pair)
        moved, backups = _publish_pairs(pairs, replacer=replacer)
        verified = inspect_ocr_installation(root)
        if verified != desired:
            raise OcrComponentError("installed OCR state does not match the requested mode")
    except Exception as exc:
        _rollback_pairs(moved, backups)
        if isinstance(exc, OcrComponentError):
            raise
        raise OcrComponentError("OCR installation transaction was rolled back") from exc
    else:
        for _, backup in backups:
            _remove_tree(backup)
        runtime_ids = ["ocr-paddle"] + (["ocr-paddle-gpu"] if desired == "gpu" else [])
        return {"status": "ready", "runtimeIds": runtime_ids}
    finally:
        if stage.exists():
            _remove_tree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--app-root", type=Path, required=True)
    install.add_argument("--mode", choices=("cpu", "gpu"), required=True)
    install.add_argument("--model-root", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = install_optional_ocr(
            arguments.app_root, mode=arguments.mode, model_root=arguments.model_root,
        )
    except OcrComponentError as exc:
        error = "ocr_models_required" if "ocr_models_required" in str(exc) else "ocr_component_error"
        sys.stderr.write(json.dumps({"error": error, "message": str(exc)}, ensure_ascii=True, sort_keys=True) + "\n")
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
