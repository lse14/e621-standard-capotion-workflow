"""Verify release locks and wheelhouses without importing application code."""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
PIN = re.compile(r"^[a-z0-9][a-z0-9_.-]*==[^\s]+")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(lock: Path, wheelhouse: Path) -> None:
    hashes: set[str] = set()
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not PIN.match(line):
            raise ValueError(f"lock entry is not an exact pin: {lock}: {line}")
        match = HASH.search(line)
        if not match:
            raise ValueError(f"lock entry has no SHA-256: {lock}: {line}")
        hashes.add(match.group(1))
    wheels = list(wheelhouse.glob("*.whl")) if wheelhouse.exists() else []
    actual = {file_hash(wheel) for wheel in wheels}
    if hashes != actual:
        raise ValueError(f"lock/wheelhouse digest mismatch for {lock.stem}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements-root", type=Path, required=True)
    parser.add_argument("--wheelhouse-root", type=Path, required=True)
    parser.add_argument("runtimes", nargs="+")
    arguments = parser.parse_args()
    for runtime in arguments.runtimes:
        verify(arguments.requirements_root / f"{runtime}.lock", arguments.wheelhouse_root / runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
