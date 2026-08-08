"""Run tests in the runtime that owns their dependencies.

Invoke with the distributed core interpreter.  Worker tests intentionally run
under their own embedded interpreter; core discovery only tests core-owned
logic and cross-process contracts.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from anima_core.runtime_manifest import RuntimeManifestError, inspect_optional_ocr_runtime_state


ROOT = Path(__file__).resolve().parents[1]
WORKER_TESTS = {
    "caption-e621": ("test_caption_processing.py",),
    "classify-e621": ("test_classify_processing.py",),
    "replace-e621": ("test_replace_processing.py", "test_replace_resource.py"),
    "nl": ("test_nl_worker.py",),
    "policy": ("test_policy.py", "test_policy_worker.py"),
    "export": ("test_export_worker.py",),
    "token-budget": ("test_token_budget_worker.py",),
}


@dataclass(frozen=True)
class TestCommand:
    owner: str
    interpreter: Path
    arguments: tuple[str, ...]
    kind: Literal["discover", "file"]
    start_directory: Path


def _run(command: Sequence[str], *, environment: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def _runtime_root(install_root: Path | None = None) -> Path:
    return install_root or Path(os.environ.get("ANIMA_INSTALL_ROOT", ROOT / ".runtime-build"))


def resolve_ocr_mode(requested: Literal["auto", "none", "cpu", "gpu"], install_root: Path) -> Literal["none", "cpu", "gpu"]:
    try:
        actual = inspect_optional_ocr_runtime_state(install_root)
    except RuntimeManifestError as exc:
        raise ValueError(f"optional OCR state is invalid: {exc}") from exc
    if requested != "auto" and actual != requested:
        raise ValueError(f"requested OCR mode {requested} does not match installed state {actual}")
    return actual


def _worker_commands(runtimes: Path, ocr_mode: Literal["none", "cpu", "gpu"]) -> tuple[TestCommand, ...]:
    worker_tests = dict(WORKER_TESTS)
    if ocr_mode in {"cpu", "gpu"}:
        worker_tests["ocr-paddle"] = ("test_ocr_worker.py",)
    if ocr_mode == "gpu":
        worker_tests["ocr-paddle-gpu"] = ("test_ocr_worker.py",)
    return tuple(
        TestCommand(
            runtime_id,
            runtimes / runtime_id / "python.exe",
            ("-B", "-I", str(ROOT / "tests" / "unit" / name)),
            "file",
            ROOT / "tests" / "unit" / name,
        )
        for runtime_id, names in worker_tests.items()
        for name in names
    )


def commands_for_level(
    level: Literal["fast", "full", "stress"],
    *,
    runtime_root: Path | None = None,
    ocr_mode: Literal["none", "cpu", "gpu"] = "none",
) -> tuple[TestCommand, ...]:
    install = runtime_root or _runtime_root()
    core = install / "runtimes" / "core" / "python.exe"
    core_starts = {
        "fast": (ROOT / "tests" / "unit", ROOT / "tests" / "contract"),
        "full": (ROOT / "tests" / "unit", ROOT / "tests" / "contract", ROOT / "tests" / "integration"),
        "stress": (ROOT / "tests" / "stress",),
    }[level]
    commands = [
        TestCommand(
            "core",
            core,
            ("-B", "-I", "-m", "unittest", "discover", "-s", str(start), "-t", str(ROOT), "-v"),
            "discover",
            start,
        )
        for start in core_starts
    ]
    if level != "stress":
        commands.extend(_worker_commands(install / "runtimes", ocr_mode))
    return tuple(commands)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", choices=("fast", "full", "stress"), default="full")
    parser.add_argument("--ocr-mode", choices=("auto", "none", "cpu", "gpu"), default="auto")
    parser.add_argument("--install-root", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    runtime_root = _runtime_root(arguments.install_root)
    try:
        ocr_mode = resolve_ocr_mode(arguments.ocr_mode, runtime_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    environment = dict(os.environ)
    environment.pop("ANIMA_INSTALL_ROOT", None)
    environment.pop("ANIMA_RESOURCE_ROOT", None)
    for command in commands_for_level(arguments.level, runtime_root=runtime_root, ocr_mode=ocr_mode):
        _run([str(command.interpreter), *command.arguments], environment=environment)


if __name__ == "__main__":
    main()
