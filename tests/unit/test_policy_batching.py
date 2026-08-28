from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "policy" / "src"))

from anima_policy_worker.policy import CoupledProbabilities, PolicyConfig
from anima_policy_worker.protocol import PolicyHelloV1, QualityConfigV1, parse_process
from anima_policy_worker.worker import PolicyWorker


class _Scorer:
    load_count = 1
    device_name = "cpu"

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def score(self, images: list[Image.Image]) -> list[float]:
        self.batch_sizes.append(len(images))
        return [3.0] * len(images)


class PolicyBatchingTests(unittest.TestCase):
    def test_module_batch_can_exceed_quality_micro_batch_and_scores_in_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            overlay = root / "overlay"
            dataset.mkdir()
            (overlay / "annotations").mkdir(parents=True)
            (overlay / "overlay-manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "jobId": "job-policy", "datasetRoot": str(dataset)}),
                encoding="utf-8",
            )
            scorer = _Scorer()
            worker = PolicyWorker()
            worker.hello = PolicyHelloV1(
                jobId="job-policy",
                configHash="a" * 64,
                datasetRoot=str(dataset),
                overlayRoot=str(overlay),
                artistRootName="dataset",
                resourceManifestRelativePath=None,
                resourceFingerprint=None,
                quality=QualityConfigV1(True, 0.0, "cpu", 2, "test-model"),
                policy=PolicyConfig(
                    seed="test-seed",
                    artistEnabled=False,
                    artistDropoutProbability=0.0,
                    qualityEnabled=True,
                    qualityDropoutProbability=0.0,
                    appearanceNlEnabled=False,
                    solo=CoupledProbabilities(0.0, 0.0),
                    nonSolo=CoupledProbabilities(0.0, 0.0),
                    unknown=CoupledProbabilities(0.0, 0.0),
                ),
            )
            worker.dataset_root = dataset
            worker.overlay_root = overlay
            worker.scorer = scorer
            items_payload = []
            for sample_id in range(1, 5):
                relative = f"{sample_id}.png"
                image = dataset / relative
                Image.new("RGB", (2, 2), "white").save(image)
                stat = image.stat()
                (dataset / f"{sample_id}.json").write_text(
                    json.dumps({
                        "quality": [], "count": "solo", "character": "", "series": "",
                        "artist": "", "appearance": [], "tags": ["tag"],
                        "environment": [], "nl": "caption",
                    }),
                    encoding="utf-8",
                )
                items_payload.append({
                    "schemaVersion": 1,
                    "sampleId": sample_id,
                    "leaseId": f"lease-{sample_id}",
                    "relativeImagePath": relative,
                    "annotationKey": str(sample_id),
                    "imageSize": stat.st_size,
                    "imageMtimeNs": stat.st_mtime_ns,
                    "imageFileId": None,
                })
            items = parse_process({"schemaVersion": 1, "payloadType": "policy_process_request", "items": items_payload})
            result = worker.process(items)
            self.assertEqual("policy_batch_result", result["payloadType"])
            self.assertEqual([2, 2], scorer.batch_sizes)
            self.assertEqual(["prepared"] * 4, [item["status"] for item in result["outcomes"]])


if __name__ == "__main__":
    unittest.main()
