"""Build deterministic OCR component payloads outside the base release tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from ocr_component import (
    OcrComponentError,
    RESOURCE_ID,
    inspect_ocr_installation,
    load_component_manifest,
    resolve_runtime_root,
)


def _is_reparse(path: Path) -> bool:
    information = os.lstat(path)
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_safe_tree(root: Path) -> None:
    if not root.exists() or _is_reparse(root):
        raise OcrComponentError("component source is unsafe")
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse(child):
                    raise OcrComponentError("reparse point is not allowed")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
                elif not entry.is_file(follow_symlinks=False):
                    raise OcrComponentError("component source contains an unsupported entry")


def _copy_tree(source: Path, destination: Path) -> None:
    _assert_safe_tree(source)
    if not source.is_dir() or destination.exists():
        raise OcrComponentError("component payload source is invalid")
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or _is_reparse(source):
        raise OcrComponentError("component payload input is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_component(component_root: Path, source_root: Path, runtime_root: Path, runtime_id: str) -> None:
    payload = component_root / "payload"
    _copy_tree(runtime_root / "runtimes" / runtime_id, payload / "runtimes" / runtime_id)
    _copy_file(
        runtime_root / "manifests" / "runtimes" / f"{runtime_id}.json",
        payload / "manifests" / "runtimes" / f"{runtime_id}.json",
    )
    _copy_file(
        runtime_root / "manifests" / "requirements" / f"{runtime_id}.lock",
        payload / "manifests" / "requirements" / f"{runtime_id}.lock",
    )
    _copy_file(
        source_root / "packaging" / "requirements" / f"{runtime_id}.lock",
        payload / "packaging" / "requirements" / f"{runtime_id}.lock",
    )
    records = []
    for path in sorted(
        payload.rglob("*"),
        key=lambda item: str(item.relative_to(payload)).replace("/", "\\").casefold(),
    ):
        if not path.is_file():
            continue
        relative = str(path.relative_to(payload)).replace("/", "\\")
        records.append({"path": relative, "sizeBytes": path.stat().st_size, "sha256": _sha256(path)})
    component_id = "ocr-cpu" if runtime_id == "ocr-paddle" else "ocr-gpu"
    manifest = {
        "schemaVersion": 1,
        "componentId": component_id,
        "runtimeIds": [runtime_id],
        "requiresResourceId": RESOURCE_ID,
        "files": records,
    }
    (component_root / "component.json").write_text(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    load_component_manifest(component_root)


def build_components(source_root: Path, destination_root: Path, *, mode: str) -> dict[str, Path]:
    """Publish complete CPU/GPU component directories via one atomic rename."""
    source = Path(os.path.abspath(os.path.normpath(os.fspath(source_root))))
    destination = Path(os.path.abspath(os.path.normpath(os.fspath(destination_root))))
    if mode not in {"cpu", "gpu"}:
        raise OcrComponentError("component mode is invalid")
    actual_state = inspect_ocr_installation(source)
    if mode == "cpu" and actual_state not in {"cpu", "gpu"}:
        raise OcrComponentError("complete CPU OCR inputs are required")
    if mode == "gpu" and actual_state != "gpu":
        raise OcrComponentError("GPU component build requires complete CPU fallback")
    runtime_root = resolve_runtime_root(source)
    if destination.exists():
        raise OcrComponentError("optional component destination already exists")
    parent = destination.parent
    if not parent.is_dir() or _is_reparse(parent):
        raise OcrComponentError("optional component destination parent is unsafe")
    staging = parent / ("." + destination.name + ".staging-" + uuid.uuid4().hex)
    try:
        staging.mkdir()
        _write_component(staging / "ocr-cpu", source, runtime_root, "ocr-paddle")
        if mode == "gpu":
            _write_component(staging / "ocr-gpu", source, runtime_root, "ocr-paddle-gpu")
        os.replace(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {path.name: path for path in sorted(destination.iterdir(), key=lambda item: item.name)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("cpu", "gpu"), required=True)
    arguments = parser.parse_args(argv)
    try:
        components = build_components(arguments.source_root, arguments.destination_root, mode=arguments.mode)
    except OcrComponentError as exc:
        sys.stderr.write(json.dumps({"error": "ocr_component_error", "message": str(exc)}, ensure_ascii=True) + "\n")
        return 2
    print(json.dumps({"status": "built", "components": sorted(components)}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
