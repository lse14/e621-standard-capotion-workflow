from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "replace" / "src"))

from anima_replace_worker.resource import ReplaceResourceError, load_custom_replace_resource, load_replace_resource
from anima_replace_worker.worker import ReplaceWorker


RESOURCE_ROOT = ROOT / "resource-library"
RESOURCE_MANIFEST = r"replacement-indexes\e621-replace-20260726-v2\resource.json"
RESOURCE_FINGERPRINT = "3cabbeeffd379a893a0b53d427c3dbb26ea6c587f474ae761b21afde4ee4c47b"


class ReplaceResourceTests(unittest.TestCase):
    def test_worker_process_batch_reuses_the_loaded_index_and_isolates_item_failures(self) -> None:
        worker = ReplaceWorker()
        worker.initialize({
            "schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": "job-1", "configHash": "a" * 64,
            "resourceManifestRelativePath": RESOURCE_MANIFEST,
            "resourceFingerprint": RESOURCE_FINGERPRINT,
        }, install_root=RESOURCE_ROOT)

        outcomes = worker.process_batch([
            {
                "schemaVersion": 1, "sampleId": 1, "leaseId": "lease-1", "source": "e621", "relativeImagePath": "one.png",
                "projection": {"quality": [], "count": "", "character": "", "series": "", "artist": "", "appearance": [], "tags": ["!"], "environment": [], "nl": ""},
            },
            {
                "schemaVersion": 1, "sampleId": 2, "leaseId": "lease-2", "source": "e621", "relativeImagePath": "two.png",
                "projection": {},
            },
        ])

        self.assertEqual(
            [("replace_result", 1, "lease-1"), ("replace_issue", 2, "lease-2")],
            [(outcome["payloadType"], outcome["sampleId"], outcome["leaseId"]) for outcome in outcomes],
        )

    def test_worker_initializes_once_and_processes_without_filesystem_input(self) -> None:
        install = RESOURCE_ROOT
        worker = ReplaceWorker()
        hello = worker.initialize({
            "schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": "job-1", "configHash": "a" * 64,
            "resourceManifestRelativePath": RESOURCE_MANIFEST,
            "resourceFingerprint": RESOURCE_FINGERPRINT,
        }, install_root=install)
        self.assertEqual((True, 1, 86_922), (hello["ready"], hello["indexLoads"], hello["ruleCount"]))
        # M3-07: canonical_e621_tag now has a runtime reader; the pinned CSV has 269 keep rows
        # whose output is not the canonical tag and a fully self consistent alias direction.
        self.assertEqual((269, 0), (hello["keepNonCanonical"], hello["canonicalDirectionConflict"]))
        result = worker.process({
            "schemaVersion": 1, "sampleId": 1, "leaseId": "lease-1", "source": "e621", "relativeImagePath": "sample.png",
            "projection": {"quality": [], "count": "", "character": "", "series": "", "artist": "", "appearance": [], "tags": ["!"], "environment": [], "nl": ""},
        })
        # F34: "!" is a keep row whose output differs from its input, which Replace used to
        # report as neither replaced nor passthrough.
        self.assertEqual(("replace_result", ["exclamation_point"], 1), (result["payloadType"], result["projection"]["tags"], result["keepRewritten"]))
        with self.assertRaises(RuntimeError):
            worker.initialize({}, install_root=install)

    def test_legacy_profile_cannot_initialize_the_profileless_replace_worker(self) -> None:
        worker = ReplaceWorker()
        with self.assertRaises(RuntimeError):
            worker.initialize({
                "schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": "job-1", "configHash": "a" * 64,
                "profile": "danbooru", "resourceManifestRelativePath": RESOURCE_MANIFEST,
                "resourceFingerprint": "0" * 64,
            }, install_root=RESOURCE_ROOT)

    def test_real_pinned_csv_loads_once_with_exact_unicode_keys(self) -> None:
        install = RESOURCE_ROOT
        resource = load_replace_resource(install, RESOURCE_MANIFEST, RESOURCE_FINGERPRINT)
        self.assertEqual(86_922, len(resource.rules))
        self.assertEqual(":|", resource.rules[":|"].replacement_tags[0])
        self.assertIn("...", resource.rules)
        self.assertIn("…", resource.rules)
        with self.assertRaises(ReplaceResourceError):
            load_replace_resource(install, RESOURCE_MANIFEST, "0" * 64)

    def test_custom_frozen_csv_requires_the_expected_overlay_path_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            overlay = Path(temporary) / "overlay"
            target = overlay / "resources" / "replace"
            target.mkdir(parents=True)
            csv_path = target / "custom-index.csv"
            data = b"source_tag,canonical_e621_tag,action,replacement_tags\nold,,replace,new\n"
            csv_path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            resource = load_custom_replace_resource(overlay, str(csv_path), digest, 1)
            self.assertEqual((digest, "new"), (resource.fingerprint, resource.rules["old"].replacement_tags[0]))
            with self.assertRaises(ReplaceResourceError):
                load_custom_replace_resource(overlay, str(csv_path), "0" * 64, 1)

    def test_canonical_column_audits_a_custom_index_written_in_the_wrong_direction(self) -> None:
        # M3-07: canonical_e621_tag used to be header-only, so an index whose alias direction is
        # reversed or chained loaded silently. wink is both a canonical target and an alias here.
        with tempfile.TemporaryDirectory() as temporary:
            overlay = Path(temporary) / "overlay"
            target = overlay / "resources" / "replace"
            target.mkdir(parents=True)
            csv_path = target / "custom-index.csv"
            data = (
                b"source_tag,canonical_e621_tag,action,replacement_tags\n"
                b"one_eye_closed,wink,keep,one_eye_closed\n"
                b"wink,narrowed_eyes,keep,wink\n"
                b"narrowed_eyes,narrowed_eyes,keep,narrowed_eyes\n"
            )
            csv_path.write_bytes(data)
            resource = load_custom_replace_resource(overlay, str(csv_path), hashlib.sha256(data).hexdigest(), 3)
            self.assertEqual((2, 1), (resource.keep_non_canonical, resource.canonical_direction_conflicts))


if __name__ == "__main__":
    unittest.main()
