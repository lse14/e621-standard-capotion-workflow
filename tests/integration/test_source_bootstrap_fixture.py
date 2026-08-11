from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER_ROOT = ROOT / "packaging" / "installer"


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


if __name__ == "__main__":
    unittest.main()
