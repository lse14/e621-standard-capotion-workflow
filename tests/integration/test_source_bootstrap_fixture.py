from __future__ import annotations

import importlib
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER_ROOT = ROOT / "packaging" / "installer"
RELEASE_VALIDATOR = ROOT / "packaging" / "scripts" / "Validate-SourceBootstrapRelease.ps1"


def _probes_module():
    sys.path.insert(0, str(INSTALLER_ROOT))
    try:
        sys.modules.pop("probes", None)
        return importlib.import_module("probes")
    finally:
        sys.path.pop(0)


class SourceBootstrapFixtureTests(unittest.TestCase):
    def test_cpu_fixture_install_runs_offline_probes(self) -> None:
        probes = _probes_module()
        script = """
import json
import socket

try:
    socket.create_connection((\"127.0.0.1\", 9))
except RuntimeError as exc:
    print(json.dumps({\"networkBlocked\": \"network is blocked\" in str(exc), \"fixture\": \"offline\"}))
else:
    raise RuntimeError(\"fixture probe reached the network\")
"""

        evidence = probes.run_json_probe(Path(sys.executable), script, (), cwd=ROOT)

        self.assertEqual({"fixture": "offline", "networkBlocked": True}, evidence)

    def test_release_gate_accepts_the_frozen_production_assets(self) -> None:
        self.assertTrue(RELEASE_VALIDATOR.is_file(), "source-bootstrap release validator must exist")

        completed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(RELEASE_VALIDATOR), "-ProjectRoot", str(ROOT),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("Source-bootstrap release gate passed for source-bootstrap-e621-v4", completed.stdout)


if __name__ == "__main__":
    unittest.main()
