"""Build an audited pure-Python vendor wheel before release lock resolution.

The resulting wheel is the only artifact accepted by the release assembler.
This exists because pywebview's required proxy-tools package publishes an sdist
but no upstream wheel for the frozen Windows build target.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requirement", choices=("proxy-tools==0.1.0",))
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    arguments = parser.parse_args()
    arguments.wheelhouse.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [arguments.python, "-m", "pip", "wheel", "--wheel-dir", str(arguments.wheelhouse), arguments.requirement],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
