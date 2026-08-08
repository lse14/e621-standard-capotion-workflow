from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALL_ROOT = ROOT / ".runtime-build"
RESOURCE_ROOT = ROOT / "resource-library"
RESOURCE_MANIFEST = r"dropout-models\lse14-scorer-5k-v1\resource.json"
RESOURCE_FINGERPRINT = "1281c8365e0a2d9bc62b5cd8953665cf8d6f5ce32f41c4ec10a347c673b128ba"
sys.path.insert(0, str(ROOT / "core" / "src"))

from PIL import Image

from anima_core.launcher import WorkerLauncher
from anima_core.worker_protocol import ProtocolEnvelopeV1, decode_frame, encode_frame


class PolicyRuntimeIntegrationTests(unittest.TestCase):
    @unittest.skipUnless((INSTALL_ROOT / "manifests" / "runtimes" / "policy.json").is_file(), "policy runtime not assembled")
    def test_real_policy_worker_loads_once_and_preserves_appearance_or_nl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            artist = dataset / "1_Crow (Siranui)"
            artist.mkdir(parents=True)
            image_path = artist / "image.png"
            Image.new("RGB", (640, 960), (128, 96, 64)).save(image_path)
            original = {
                "quality": [],
                "count": "solo",
                "character": "amy_rose",
                "series": "",
                "artist": "",
                "appearance": ["white hair"],
                "tags": ["smile"],
                "environment": ["outdoors"],
                "nl": "A person smiles outdoors.",
            }
            json_path = artist / "image.json"
            json_path.write_text(json.dumps(original), encoding="utf-8")
            overlay = root / ".dataset.anima-overlay-job-policy-real"
            (overlay / "annotations").mkdir(parents=True)
            (overlay / "prepared").mkdir()
            (overlay / "commit").mkdir()
            (overlay / "overlay-manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "jobId": "job-policy-real", "datasetRoot": str(dataset)}),
                encoding="utf-8",
            )
            hello_payload = {
                "schemaVersion": 1,
                "payloadType": "policy_hello_request",
                "jobId": "job-policy-real",
                "configHash": "b" * 64,
                "datasetRoot": str(dataset),
                "overlayRoot": str(overlay),
                "resourceManifestRelativePath": RESOURCE_MANIFEST,
                "resourceFingerprint": RESOURCE_FINGERPRINT,
                "policy": {
                    "policyVersion": "dataset-batch-policy-v1",
                    "seed": "integration-seed",
                    "artist": {"enabled": True, "dropoutProbability": 0.0},
                    "quality": {
                        "enabled": True,
                        "dropoutProbability": 0.0,
                        "device": "auto",
                        "batchSize": 1,
                        "resourceId": "lse14-scorer-5k-v1",
                    },
                    "appearanceNl": {
                        "enabled": True,
                        "solo": {"dropNl": 1.0, "dropAppearance": 0.0},
                        "nonSolo": {"dropNl": 0.05, "dropAppearance": 0.70},
                        "unknown": {"dropNl": 0.15, "dropAppearance": 0.15},
                    },
                },
            }
            information = image_path.stat()
            file_id = f"{getattr(information, 'st_dev', 0)}:{getattr(information, 'st_ino', 0)}"
            process_payload = {
                "schemaVersion": 1,
                "payloadType": "policy_process_request",
                "items": [{
                    "schemaVersion": 1,
                    "sampleId": 1,
                    "leaseId": "lease-policy-real-1",
                    "relativeImagePath": r"1_Crow (Siranui)\image.png",
                    "annotationKey": r"1_Crow (Siranui)\image",
                    "imageSize": information.st_size,
                    "imageMtimeNs": information.st_mtime_ns,
                    "imageFileId": file_id,
                }],
            }
            requests = (
                ProtocolEnvelopeV1("1.0", "request", "hello-policy-real", "policy", "policy", "hello", hello_payload, jobId="job-policy-real", configHash="b" * 64),
                ProtocolEnvelopeV1("1.0", "request", "process-policy-real", "policy", "policy", "process_batch", process_payload, jobId="job-policy-real", configHash="b" * 64),
                ProtocolEnvelopeV1("1.0", "request", "shutdown-policy-real", "policy", "policy", "shutdown", {}),
            )
            launch = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT).resolve(
                "policy", expected_owner="policy"
            )
            completed = subprocess.run(
                launch.command,
                input=b"".join(encode_frame(request) for request in requests),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=launch.environment,
                cwd=str(INSTALL_ROOT),
                check=False,
                timeout=180,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", errors="replace"))
            responses = completed.stdout.splitlines()
            self.assertEqual(3, len(responses))
            initialized = decode_frame(responses[0], runtime_id="policy", owner="policy")
            self.assertEqual(1, initialized.payload["modelLoadCount"])
            processed = decode_frame(responses[1], runtime_id="policy", owner="policy")
            outcome = processed.payload["outcomes"][0]
            self.assertEqual("prepared", outcome["status"])
            self.assertEqual(1, processed.payload["modelLoadCount"])
            prepared_path = overlay / Path(str(outcome["preparedRelativePath"]).replace("\\", os.sep))
            result = json.loads(prepared_path.read_text(encoding="utf-8"))
            self.assertEqual(["white hair"], result["appearance"])
            self.assertEqual("", result["nl"])
            self.assertTrue(result["quality"])
            self.assertEqual("@Crow (Siranui)", result["artist"])
            self.assertEqual(original, json.loads(json_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
