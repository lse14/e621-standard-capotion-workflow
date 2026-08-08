from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.credentials import CredentialStoreError, DpapiCredentialStore


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is required")
class CredentialStoreTests(unittest.TestCase):
    def test_dpapi_round_trip_never_persists_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DpapiCredentialStore(Path(temporary))
            store.save("nl-api-default", "super-secret-api-key")
            blob = (Path(temporary) / "nl-api-default.dpapi").read_bytes()
            self.assertNotIn(b"super-secret-api-key", blob)
            self.assertEqual("super-secret-api-key", store.load("nl-api-default"))
            store.delete("nl-api-default")
            self.assertFalse((Path(temporary) / "nl-api-default.dpapi").exists())

    def test_reference_and_secret_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DpapiCredentialStore(Path(temporary))
            with self.assertRaises(CredentialStoreError):
                store.save("../escape", "secret")
            with self.assertRaises(CredentialStoreError):
                store.save("valid", "")


if __name__ == "__main__":
    unittest.main()
