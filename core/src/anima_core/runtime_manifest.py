from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import PROTOCOL_VERSION
from .path_safety import PathSafetyError, canonicalize, ensure_within, safe_relative_path, sha256_file, windows_key


MAX_MANIFEST_BYTES = 1024 * 1024
ASSEMBLED_RUNTIME_IDS = frozenset({
    "core", "caption-e621", "classify-e621", "replace-e621", "ocr-paddle", "nl", "policy", "export", "token-budget",
})
DECLARED_UNASSEMBLED_RUNTIMES: dict[str, tuple[str, str, str]] = {
    "ocr-paddle-gpu": ("ocr", "anima_ocr_worker.entry", "ocr-paddle-gpu"),
}
RUNTIME_OWNERS = frozenset({"core", "caption", "classify", "replace", "ocr", "nl", "policy", "export", "token-budget"})
RUNTIME_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_PYTHON_VERSION = "3.11.15"


class RuntimeManifestError(ValueError):
    pass


def runtime_lifecycle(runtime_id: str) -> str:
    if runtime_id in ASSEMBLED_RUNTIME_IDS:
        return "assembled"
    if runtime_id in DECLARED_UNASSEMBLED_RUNTIMES:
        return "declared-unassembled"
    raise RuntimeManifestError("runtime lifecycle is unknown")


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise RuntimeManifestError(f"{field} must be a lowercase SHA-256")
    return value


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeManifestError(f"{field} must be a relative path")
    try:
        return safe_relative_path(value)
    except PathSafetyError as exc:
        raise RuntimeManifestError(f"invalid {field}: {exc}") from exc


@dataclass(frozen=True)
class EmbeddedRuntimeManifest:
    runtimeId: str
    owner: str
    pythonVersion: str
    interpreterRelativePath: str
    dependencyLockSha256: str
    protocolVersion: str
    criticalFilesSha256: dict[str, str]

    @classmethod
    def from_dict(cls, value: object) -> "EmbeddedRuntimeManifest":
        if not isinstance(value, dict):
            raise RuntimeManifestError("runtime manifest must be an object")
        runtime_id = value.get("runtimeId")
        owner = value.get("owner")
        if not isinstance(runtime_id, str) or not RUNTIME_ID_PATTERN.fullmatch(runtime_id):
            raise RuntimeManifestError("runtimeId is invalid")
        if owner not in RUNTIME_OWNERS:
            raise RuntimeManifestError("runtime owner is invalid or unavailable")
        python_version = value.get("pythonVersion")
        if python_version != REQUIRED_PYTHON_VERSION:
            raise RuntimeManifestError(f"runtime must use Python {REQUIRED_PYTHON_VERSION}")
        protocol_version = value.get("protocolVersion")
        if protocol_version != PROTOCOL_VERSION:
            raise RuntimeManifestError("runtime protocolVersion does not match")
        critical = value.get("criticalFilesSha256")
        if not isinstance(critical, dict) or not critical:
            raise RuntimeManifestError("criticalFilesSha256 must be a non-empty object")
        normalized_critical: dict[str, str] = {}
        for path, digest in critical.items():
            normalized_critical[_relative(path, "critical file path")] = _require_sha256(digest, "critical file digest")
        return cls(
            runtimeId=runtime_id,
            owner=owner,
            pythonVersion=python_version,
            interpreterRelativePath=_relative(value.get("interpreterRelativePath"), "interpreterRelativePath"),
            dependencyLockSha256=_require_sha256(value.get("dependencyLockSha256"), "dependencyLockSha256"),
            protocolVersion=protocol_version,
            criticalFilesSha256=normalized_critical,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "runtimeId": self.runtimeId,
            "owner": self.owner,
            "pythonVersion": self.pythonVersion,
            "interpreterRelativePath": self.interpreterRelativePath,
            "dependencyLockSha256": self.dependencyLockSha256,
            "protocolVersion": self.protocolVersion,
            "criticalFilesSha256": dict(sorted(self.criticalFilesSha256.items())),
        }


@dataclass(frozen=True)
class RuntimeLaunchSpec:
    entryModule: str
    arguments: tuple[str, str, str, str]
    protocolTransport: str
    maxFrameBytes: int
    dllDirectoriesRelative: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> "RuntimeLaunchSpec":
        if not isinstance(value, dict):
            raise RuntimeManifestError("launch must be an object")
        entry = value.get("entryModule")
        arguments = value.get("arguments")
        transport = value.get("protocolTransport")
        frame_size = value.get("maxFrameBytes")
        dll_dirs = value.get("dllDirectoriesRelative", [])
        if not isinstance(entry, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", entry):
            raise RuntimeManifestError("launch entryModule is invalid")
        if arguments != ["-B", "-I", "-u", "-m"]:
            raise RuntimeManifestError("launch arguments must be exactly -B -I -u -m")
        if transport != "stdio-jsonl" or frame_size != 1_048_576:
            raise RuntimeManifestError("launch transport or frame size is invalid")
        if not isinstance(dll_dirs, list) or not all(isinstance(item, str) for item in dll_dirs):
            raise RuntimeManifestError("dllDirectoriesRelative must be a string list")
        return cls(entry, ("-B", "-I", "-u", "-m"), transport, frame_size, tuple(_relative(item, "DLL directory") for item in dll_dirs))

    def to_dict(self) -> dict[str, object]:
        return {
            "entryModule": self.entryModule,
            "arguments": list(self.arguments),
            "protocolTransport": self.protocolTransport,
            "maxFrameBytes": self.maxFrameBytes,
            "dllDirectoriesRelative": list(self.dllDirectoriesRelative),
        }


@dataclass(frozen=True)
class RuntimeBundleManifestV1:
    schemaVersion: int
    runtime: EmbeddedRuntimeManifest
    launch: RuntimeLaunchSpec

    @classmethod
    def from_dict(cls, value: object) -> "RuntimeBundleManifestV1":
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise RuntimeManifestError("runtime bundle schemaVersion must be 1")
        return cls(1, EmbeddedRuntimeManifest.from_dict(value.get("runtime")), RuntimeLaunchSpec.from_dict(value.get("launch")))

    @classmethod
    def load(cls, path: str | Path) -> "RuntimeBundleManifestV1":
        target = Path(path)
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise RuntimeManifestError(f"unable to read runtime manifest: {target}") from exc
        if len(data) > MAX_MANIFEST_BYTES:
            raise RuntimeManifestError("runtime manifest exceeds 1 MiB")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeManifestError("runtime manifest is not UTF-8 JSON") from exc
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, object]:
        return {"schemaVersion": self.schemaVersion, "runtime": self.runtime.to_dict(), "launch": self.launch.to_dict()}

    def resolve_interpreter(self, install_root: str | Path) -> Path:
        root = canonicalize(install_root, must_exist=True, directory=True).value
        candidate = root / Path(self.runtime.interpreterRelativePath.replace("\\", os.sep))
        interpreter = ensure_within(root, candidate)
        if not interpreter.is_file():
            raise RuntimeManifestError(f"embedded interpreter is missing: {interpreter}")
        return interpreter

    def resolve_dll_directories(self, install_root: str | Path) -> tuple[Path, ...]:
        root = canonicalize(install_root, must_exist=True, directory=True).value
        resolved: list[Path] = []
        for relative in self.launch.dllDirectoriesRelative:
            directory = ensure_within(root, root / Path(relative.replace("\\", os.sep)))
            if not directory.is_dir():
                raise RuntimeManifestError(f"runtime DLL directory is missing: {directory}")
            resolved.append(directory)
        return tuple(resolved)

    def verify_files(self, install_root: str | Path) -> Path:
        root = canonicalize(install_root, must_exist=True, directory=True).value
        interpreter = self.resolve_interpreter(root)
        lock_path = root / "manifests" / "requirements" / f"{self.runtime.runtimeId}.lock"
        if not lock_path.is_file() or sha256_file(lock_path) != self.runtime.dependencyLockSha256:
            raise RuntimeManifestError("runtime dependency lock is missing or has been modified")
        for relative, expected in self.runtime.criticalFilesSha256.items():
            target = ensure_within(root, root / Path(relative.replace("\\", os.sep)))
            if not target.is_file():
                raise RuntimeManifestError(f"runtime critical file is missing: {target}")
            if sha256_file(target) != expected:
                raise RuntimeManifestError(f"runtime critical file digest mismatch: {relative}")
        runtime_root = interpreter.parent
        pth_path = runtime_root / "python311._pth"
        if not pth_path.is_file():
            raise RuntimeManifestError("embedded runtime is missing python311._pth")
        try:
            pth_lines = pth_path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RuntimeManifestError("embedded runtime python311._pth is unreadable") from exc
        for line in pth_lines:
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            if entry == ".":
                continue
            if entry == "import site" or os.path.isabs(entry):
                raise RuntimeManifestError("embedded runtime python311._pth is not controlled")
            _relative(entry, "python311._pth entry")
        prohibited = ("pip", "wheel", "pytest")
        if (self.runtime.runtimeId, self.runtime.owner) not in {("ocr-paddle", "ocr"), ("ocr-paddle-gpu", "ocr")}:
            prohibited += ("setuptools",)
        for base in (runtime_root / "Lib" / "site-packages", runtime_root / "Scripts"):
            if not base.exists():
                continue
            for name in prohibited:
                if (base / name).exists() or (base / f"{name}.exe").exists() or (base / f"{name}.py").exists():
                    raise RuntimeManifestError(f"production runtime must not contain {name}")
        return interpreter

    def verify_interpreter(self, install_root: str | Path, *, timeout_seconds: float = 10.0) -> Path:
        interpreter = self.verify_files(install_root)
        try:
            completed = subprocess.run(
                [str(interpreter), "-B", "-I", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeManifestError("embedded interpreter cannot be executed") from exc
        if completed.returncode != 0 or completed.stdout.strip() != self.runtime.pythonVersion:
            raise RuntimeManifestError("embedded interpreter version does not match manifest")
        return interpreter


def sha256_path(path: str | Path) -> str:
    return sha256_file(path)


def dependency_lock_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_optional_ocr_runtime_state(install_root: str | Path) -> Literal["none", "cpu", "gpu"]:
    """Inspect only optional OCR runtime/manifests, rejecting partial publication."""
    root = canonicalize(install_root, must_exist=True, directory=True).value

    def state(runtime_id: str) -> Literal["absent", "complete", "partial"]:
        runtime = root / "runtimes" / runtime_id
        manifest_path = root / "manifests" / "runtimes" / f"{runtime_id}.json"
        lock = root / "manifests" / "requirements" / f"{runtime_id}.lock"
        targets = (runtime, manifest_path, lock)
        present = tuple(target.exists() for target in targets)
        if not any(present):
            return "absent"
        if not all(present) or not runtime.is_dir() or not manifest_path.is_file() or not lock.is_file():
            return "partial"
        try:
            manifest = RuntimeBundleManifestV1.load(manifest_path)
            if manifest.runtime.runtimeId != runtime_id or manifest.runtime.owner != "ocr":
                return "partial"
            manifest.verify_files(root)
        except RuntimeManifestError:
            return "partial"
        return "complete"

    cpu = state("ocr-paddle")
    gpu = state("ocr-paddle-gpu")
    if gpu != "absent" and cpu == "absent":
        raise RuntimeManifestError("GPU OCR runtime requires complete CPU fallback")
    if cpu == gpu == "absent":
        return "none"
    if cpu == "complete" and gpu == "absent":
        return "cpu"
    if cpu == gpu == "complete":
        return "gpu"
    raise RuntimeManifestError("optional OCR runtime publication is partial")


def validate_runtime_isolation(manifests: list[RuntimeBundleManifestV1], install_root: str | Path) -> None:
    seen_ids: set[str] = set()
    seen_interpreters: set[str] = set()
    seen_owners: set[str] = set()
    for manifest in manifests:
        if manifest.runtime.runtimeId in seen_ids:
            raise RuntimeManifestError("duplicate runtimeId")
        if manifest.runtime.owner in seen_owners:
            raise RuntimeManifestError("runtime owners must not share an environment")
        interpreter_key = windows_key(manifest.resolve_interpreter(install_root))
        if interpreter_key in seen_interpreters:
            raise RuntimeManifestError("runtimes must not share an interpreter")
        seen_ids.add(manifest.runtime.runtimeId)
        seen_owners.add(manifest.runtime.owner)
        seen_interpreters.add(interpreter_key)
