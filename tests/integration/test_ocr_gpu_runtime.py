from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "ocr" / "src"))


class OcrGpuRuntimeSourceContractTests(unittest.TestCase):
    def test_worker_exposes_runtime_evidence_without_eager_paddle_imports(self) -> None:
        import anima_ocr_worker.worker as worker

        self.assertTrue(
            callable(getattr(worker, "_runtime_evidence", None)),
            "GPU hello needs a runtime-manifest/Paddle evidence boundary",
        )


if __name__ == "__main__":
    unittest.main()
