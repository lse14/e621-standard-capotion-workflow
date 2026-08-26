"""Generate signed-by-hash runtime manifests from an assembled install tree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Mapping


PROTOCOL_VERSION = "1.0"
PYTHON_VERSION = "3.11.15"
RUNTIMES = {
    "core": ("core", "anima_core.__main__", "core", ()),
    "caption-e621": (
        "caption",
        "anima_caption_worker.entry",
        "caption-e621",
        (
            "Lib/site-packages/onnxruntime/capi",
            "Lib/site-packages/nvidia/cublas/bin",
            "Lib/site-packages/nvidia/cuda_runtime/bin",
            "Lib/site-packages/nvidia/cudnn/bin",
            "Lib/site-packages/nvidia/cufft/bin",
            "Lib/site-packages/nvidia/curand/bin",
            "Lib/site-packages/nvidia/cusolver/bin",
            "Lib/site-packages/nvidia/cusparse/bin",
            "Lib/site-packages/nvidia/nvjitlink/bin",
        ),
    ),
    "classify-e621": ("classify", "anima_classify_worker.entry", "classify-e621", ()),
    "replace-e621": ("replace", "anima_replace_worker.entry", "replace-e621", ()),
    "nl": ("nl", "anima_nl_worker.entry", "nl", ()),
    "policy": ("policy", "anima_policy_worker.entry", "policy", ("Lib/site-packages/torch/lib",)),
    "export": ("export", "anima_export_worker.entry", "export", ()),
    "token-budget": ("token-budget", "anima_token_budget_worker.entry", "token-budget", ()),
}
# Task 4 promotes this identity only inside a fully staged installation.
ASSEMBLED_OCR_RUNTIME = ("ocr", "anima_ocr_worker.entry", "ocr-paddle", ())
ASSEMBLED_OCR_GPU_RUNTIME = (
    "ocr",
    "anima_ocr_worker.entry",
    "ocr-paddle-gpu",
    (
        "Lib/site-packages/nvidia/cublas/bin",
        "Lib/site-packages/nvidia/cuda_runtime/bin",
        "Lib/site-packages/nvidia/cudnn/bin",
        "Lib/site-packages/nvidia/cufft/bin",
        "Lib/site-packages/nvidia/curand/bin",
        "Lib/site-packages/nvidia/cusolver/bin",
        "Lib/site-packages/nvidia/cusparse/bin",
        "Lib/site-packages/nvidia/nvjitlink/bin",
        "Lib/site-packages/paddle/libs",
    ),
)
RUNTIME_SHARED_PACKAGES = {
    "core": ("anima_caption_format",),
    "export": ("anima_caption_format",),
    "token-budget": ("anima_caption_format",),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("/", "\\")


def runtime_specs(
    *,
    include_ocr_paddle: bool = False,
    include_ocr_paddle_gpu: bool = False,
    lock_names: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, str, str, tuple[str, ...]]]:
    values = dict(RUNTIMES)
    if include_ocr_paddle:
        values["ocr-paddle"] = ASSEMBLED_OCR_RUNTIME
    if include_ocr_paddle_gpu:
        values["ocr-paddle-gpu"] = ASSEMBLED_OCR_GPU_RUNTIME
    for runtime_id, lock_name in (lock_names or {}).items():
        if runtime_id not in values or not isinstance(lock_name, str) or not lock_name:
            raise ValueError("runtime lock selection is invalid")
        owner, entry_module, _current_lock, dll_relative = values[runtime_id]
        values[runtime_id] = (owner, entry_module, lock_name, dll_relative)
    return values


def require_complete_gpu_inputs(root: Path, requirements_root: Path) -> None:
    runtime = root / "runtimes" / "ocr-paddle-gpu"
    required_files = (
        runtime / "python.exe",
        runtime / "python311.dll",
        runtime / "python311._pth",
        runtime / "Lib" / "site-packages" / "anima_ocr_worker" / "entry.py",
        requirements_root / "ocr-paddle-gpu.lock",
    )
    if not runtime.is_dir() or not all(path.is_file() for path in required_files):
        raise ValueError("GPU runtime inputs are incomplete")


def manifest(
    root: Path,
    requirements_root: Path,
    runtime_id: str,
    runtimes: dict[str, tuple[str, str, str, tuple[str, ...]]],
) -> dict[str, object]:
    owner, entry_module, lock_name, dll_relative = runtimes[runtime_id]
    runtime = root / "runtimes" / runtime_id
    lock = requirements_root / f"{lock_name}.lock"
    if not runtime.is_dir() or not lock.is_file():
        raise ValueError(f"runtime or lock is missing for {runtime_id}")
    critical = [runtime / "python.exe", runtime / "python311.dll", runtime / "python311._pth"]
    package = entry_module.split(".", 1)[0]
    critical.extend(sorted((runtime / "Lib" / "site-packages" / package).glob("**/*.py")))
    for shared_package in RUNTIME_SHARED_PACKAGES.get(runtime_id, ()):
        critical.extend(sorted((runtime / "Lib" / "site-packages" / shared_package).glob("**/*.py")))
    values: dict[str, str] = {}
    for path in critical:
        if not path.is_file():
            raise ValueError(f"critical runtime file is missing: {path}")
        values[relative(path, root)] = sha256(path)
    dll_paths = [f"runtimes\\{runtime_id}\\" + item.replace("/", "\\") for item in dll_relative]
    return {
        "schemaVersion": 1,
        "runtime": {
            "runtimeId": runtime_id,
            "owner": owner,
            "pythonVersion": PYTHON_VERSION,
            "interpreterRelativePath": f"runtimes\\{runtime_id}\\python.exe",
            "dependencyLockSha256": sha256(lock),
            "protocolVersion": PROTOCOL_VERSION,
            "criticalFilesSha256": dict(sorted(values.items())),
        },
        "launch": {
            "entryModule": entry_module,
            "arguments": ["-B", "-I", "-u", "-m"],
            "protocolTransport": "stdio-jsonl",
            "maxFrameBytes": 1048576,
            "dllDirectoriesRelative": dll_paths,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--requirements-root", type=Path, required=True)
    parser.add_argument("--include-ocr-paddle", action="store_true")
    parser.add_argument("--include-ocr-paddle-gpu", action="store_true")
    parser.add_argument("--runtime-id", action="append", default=[])
    arguments = parser.parse_args()
    root = arguments.install_root.resolve()
    requirements_root = arguments.requirements_root.resolve()
    runtimes = runtime_specs(
        include_ocr_paddle=arguments.include_ocr_paddle,
        include_ocr_paddle_gpu=arguments.include_ocr_paddle_gpu,
    )
    requested = arguments.runtime_id
    if requested:
        unknown = sorted(set(requested) - set(runtimes))
        if unknown or len(set(requested)) != len(requested):
            parser.error("runtime selection is invalid")
        runtime_ids = requested
    else:
        runtime_ids = list(runtimes)
    if "ocr-paddle-gpu" in runtime_ids:
        try:
            require_complete_gpu_inputs(root, requirements_root)
        except ValueError as exc:
            parser.error(str(exc))
    output = root / "manifests" / "runtimes"
    output.mkdir(parents=True, exist_ok=True)
    lock_output = root / "manifests" / "requirements"
    lock_output.mkdir(parents=True, exist_ok=True)
    for runtime_id in runtime_ids:
        value = manifest(root, requirements_root, runtime_id, runtimes)
        (output / f"{runtime_id}.json").write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        source_lock = requirements_root / f"{runtimes[runtime_id][2]}.lock"
        (lock_output / source_lock.name).write_bytes(source_lock.read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
