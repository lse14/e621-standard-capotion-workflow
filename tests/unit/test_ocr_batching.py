from __future__ import annotations

import hashlib
import sys
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "ocr" / "src"))

from anima_ocr_worker.protocol import parse_process
from anima_ocr_worker.worker import OcrWorker
import anima_ocr_worker.worker as worker_module


def _raw_empty() -> dict[str, object]:
    return {"rec_texts": [], "rec_scores": [], "rec_polys": [], "rec_boxes": []}


class _BatchModel:
    def __init__(self) -> None:
        self.batch_calls: list[int] = []
        self.single_calls = 0

    def predict(self, _image: object) -> list[object]:
        self.single_calls += 1
        return [_raw_empty()]

    def predict_batch(self, images: list[object]) -> list[object]:
        self.batch_calls.append(len(images))
        return [_raw_empty() for _ in images]


class OcrBatchingTests(unittest.TestCase):
    def test_worker_uses_native_multi_image_model_entrypoint_for_a_request_batch(self) -> None:
        model = _BatchModel()
        worker = OcrWorker()
        worker.hello = type("Hello", (), {"expected_runtime_id": None})()
        worker.model = model  # type: ignore[assignment]
        with mock.patch.object(worker_module, "verify_source_fingerprint", side_effect=lambda item: Path(item.image_path)):
            with mock.patch.object(worker_module, "decode_and_verify", side_effect=lambda item: worker_module.DecodedImage(Image.new("RGB", (2, 2)), 2, 2, item.image_size, item.image_sha256)):
                items_payload = []
                for sample_id in range(1, 4):
                    items_payload.append({
                        "schemaVersion": 1,
                        "sampleId": sample_id,
                        "leaseId": f"lease-{sample_id}",
                        "relativeImagePath": f"{sample_id}.png",
                        "imagePath": f"C:\\dataset\\{sample_id}.png",
                        "imageSize": 1,
                        "imageSha256": hashlib.sha256(b"x").hexdigest(),
                    })
                request = {"schemaVersion": 1, "payloadType": "ocr_process_request", "items": items_payload}
                parsed = parse_process(request)
                result = worker.process(request)
        self.assertEqual([3], model.batch_calls)
        self.assertEqual(0, model.single_calls)
        self.assertEqual(["no_text"] * 3, [item.get("status") for item in result["items"]])


if __name__ == "__main__":
    unittest.main()
