from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .runtime_manifest import RuntimeBundleManifestV1, RuntimeManifestError


class WorkerLaunchError(RuntimeError):
    pass


def _clean_environment(dll_directories: tuple[Path, ...], inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if inherited is None else inherited
    retained: dict[str, str] = {}
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"}
    for key, value in source.items():
        upper = key.upper()
        if upper in allowed:
            retained[key] = value
    system_root = retained.get("SystemRoot") or retained.get("SYSTEMROOT") or retained.get("WINDIR")
    path_entries = [str(directory) for directory in dll_directories]
    if system_root:
        path_entries.extend([str(Path(system_root) / "System32"), system_root])
    retained["PATH"] = os.pathsep.join(path_entries)
    # Explicitly set only the process settings that affect Python isolation;
    # inherited PYTHON*/PIP*/CONDA*/UV* keys are intentionally absent.
    retained["PYTHONNOUSERSITE"] = "1"
    return retained


@dataclass(frozen=True)
class ResolvedWorkerLaunch:
    manifest: RuntimeBundleManifestV1
    interpreter: Path
    command: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class WorkerLauncher:
    install_root: Path
    manifests_root: Path
    resource_root: Path | None = None

    @classmethod
    def from_install_root(
        cls,
        install_root: str | Path,
        *,
        resource_root: str | Path | None = None,
    ) -> "WorkerLauncher":
        root = Path(install_root)
        return cls(root, root / "manifests" / "runtimes", Path(resource_root) if resource_root is not None else None)

    def manifest_path(self, runtime_id: str) -> Path:
        if not runtime_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in runtime_id):
            raise WorkerLaunchError("runtime id is invalid")
        return self.manifests_root / f"{runtime_id}.json"

    def resolve(self, runtime_id: str, *, expected_owner: str, extra_arguments: Sequence[str] = (), verify_interpreter: bool = True) -> ResolvedWorkerLaunch:
        try:
            manifest = RuntimeBundleManifestV1.load(self.manifest_path(runtime_id))
            if manifest.runtime.runtimeId != runtime_id:
                raise WorkerLaunchError("runtime manifest id does not match file name")
            if manifest.runtime.owner != expected_owner:
                raise WorkerLaunchError("runtime manifest owner does not match requested worker")
            interpreter = manifest.verify_interpreter(self.install_root) if verify_interpreter else manifest.verify_files(self.install_root)
            dll_directories = manifest.resolve_dll_directories(self.install_root)
        except RuntimeManifestError as exc:
            raise WorkerLaunchError(str(exc)) from exc
        command = (str(interpreter), *manifest.launch.arguments, manifest.launch.entryModule, *tuple(extra_arguments))
        environment = _clean_environment(dll_directories)
        if self.resource_root is not None:
            resource_root = self.resource_root.resolve(strict=True)
            if not resource_root.is_dir():
                raise WorkerLaunchError("resource library root is not a directory")
            environment["ANIMA_RESOURCE_ROOT"] = str(resource_root)
        return ResolvedWorkerLaunch(manifest, interpreter, command, environment)

    def spawn(self, runtime_id: str, *, expected_owner: str, extra_arguments: Sequence[str] = ()) -> subprocess.Popen[bytes]:
        launch = self.resolve(runtime_id, expected_owner=expected_owner, extra_arguments=extra_arguments)
        try:
            return subprocess.Popen(
                launch.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=launch.environment,
                cwd=str(self.install_root),
            )
        except OSError as exc:
            raise WorkerLaunchError(f"unable to launch embedded worker {runtime_id}") from exc


def clean_environment_for_test(dll_directories: tuple[Path, ...], inherited: Mapping[str, str]) -> dict[str, str]:
    """Exposed for protocol tests; production uses ``WorkerLauncher``."""
    return _clean_environment(dll_directories, inherited)
