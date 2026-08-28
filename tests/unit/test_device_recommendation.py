from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

import anima_core.device_recommendation as device_recommendation
from anima_core.device_recommendation import DeviceRecommendationService, GpuFacts


def _baseline(path: Path) -> None:
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "baselineVersion": "fixture-v1",
        "rows": [
            {"module": "caption", "minPhysicalCores": 1, "minLogicalCores": 1, "gpuRequired": False, "stableBatchSize": 4, "reason": "cpu 4 validated"},
            {"module": "caption", "minPhysicalCores": 1, "minLogicalCores": 1, "gpuRequired": True, "minTotalVramBytes": 8, "minFreeVramBytes": 4, "stableBatchSize": 32, "reason": "gpu 32 validated"},
            {"module": "classify", "minPhysicalCores": 1, "minLogicalCores": 1, "gpuRequired": False, "stableBatchSize": 128, "reason": "classify 128 validated"},
        ]
    }), encoding="utf-8")


class DeviceRecommendationTests(unittest.TestCase):
    def test_missing_psutil_uses_windows_physical_core_probe(self) -> None:
        with mock.patch.dict(sys.modules, {"psutil": None}), mock.patch.object(
            device_recommendation, "_windows_physical_core_count", return_value=6
        ), mock.patch.object(device_recommendation.os, "cpu_count", return_value=12):
            self.assertEqual((6, 12), device_recommendation._default_cpu_probe())

    def test_physical_core_probe_failure_does_not_reuse_logical_count(self) -> None:
        with mock.patch.dict(sys.modules, {"psutil": None}), mock.patch.object(
            device_recommendation, "_windows_physical_core_count", return_value=None
        ), mock.patch.object(device_recommendation.os, "cpu_count", return_value=12):
            self.assertEqual((1, 12), device_recommendation._default_cpu_probe())

    def test_cpu_only_selects_cpu_rows_and_nl_respects_rpm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline = Path(temporary) / "baseline.json"
            _baseline(baseline)
            service = DeviceRecommendationService(
                baseline,
                cpu_probe=lambda: (4, 8),
                cuda_probe=lambda: None,
                nvidia_smi_probe=lambda: None,
            )
            result = service.recommend(rpm=2)
            self.assertEqual("fixture-v1", result["baselineVersion"])
            self.assertEqual((4, 8), (result["cpuPhysicalCores"], result["cpuLogicalCores"]))
            self.assertFalse(result["gpu"]["available"])
            self.assertEqual(4, result["moduleBatchSize"]["caption"])
            self.assertEqual(128, result["moduleBatchSize"]["classify"])
            self.assertEqual(2, result["moduleBatchSize"]["nl"])
            self.assertIn("fallback to 1", result["reasons"]["replace"])

    def test_high_vram_uses_larger_gpu_row_but_low_free_vram_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline = Path(temporary) / "baseline.json"
            _baseline(baseline)
            high = DeviceRecommendationService(
                baseline,
                cpu_probe=lambda: (8, 16),
                cuda_probe=lambda: GpuFacts(True, "test", 16, 8, "cuda-runtime"),
                nvidia_smi_probe=lambda: (_ for _ in ()).throw(AssertionError("must not run")),
            ).recommend()
            self.assertEqual(32, high["moduleBatchSize"]["caption"])
            self.assertEqual("cuda-runtime", high["gpu"]["probeSource"])
            low_free = DeviceRecommendationService(
                baseline,
                cpu_probe=lambda: (8, 16),
                cuda_probe=lambda: GpuFacts(True, "test", 16, 2, "cuda-runtime"),
                nvidia_smi_probe=lambda: None,
            ).recommend()
            self.assertEqual(4, low_free["moduleBatchSize"]["caption"])

    def test_cuda_probe_failure_uses_nvidia_smi_then_cpu_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline = Path(temporary) / "baseline.json"
            _baseline(baseline)
            result = DeviceRecommendationService(
                baseline,
                cpu_probe=lambda: (2, 4),
                cuda_probe=lambda: (_ for _ in ()).throw(RuntimeError("no cuda")),
                nvidia_smi_probe=lambda: GpuFacts(True, "smi", 8, 4, "nvidia-smi"),
            ).recommend()
            self.assertTrue(result["gpu"]["available"])
            self.assertEqual("nvidia-smi", result["gpu"]["probeSource"])
            self.assertEqual(32, result["moduleBatchSize"]["caption"])

    def test_missing_baseline_returns_one_without_throwing(self) -> None:
        service = DeviceRecommendationService(
            Path("does-not-exist-baseline.json"),
            cpu_probe=lambda: (1, 1), cuda_probe=lambda: None, nvidia_smi_probe=lambda: None,
        )
        result = service.recommend()
        self.assertEqual("unavailable", result["baselineVersion"])
        self.assertTrue(all(value == 1 for key, value in result["moduleBatchSize"].items() if key != "nl"))
        self.assertIn("baseline_unavailable", result["probeErrors"])


if __name__ == "__main__":
    unittest.main()
