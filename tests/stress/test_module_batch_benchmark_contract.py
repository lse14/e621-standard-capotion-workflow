from __future__ import annotations

import unittest
import tempfile
import hashlib
import json
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

from tests.stress.module_batch_benchmark import (
    BENCHMARK_MODULES,
    CANDIDATE_BATCHES,
    _WorkerSession,
    _create_overlay,
    _json_digest,
    _ocr_benchmark_runtime,
    _ocr_hello,
    _windows_process_memory_bytes,
    candidate_batches_for,
    select_stable_recommendation,
    validate_report,
)


class ModuleBatchBenchmarkContractTests(unittest.TestCase):
    def test_candidate_batches_are_limited_by_each_module_protocol(self) -> None:
        self.assertEqual((1, 2, 4, 8, 16, 32, 64), candidate_batches_for("caption"))
        self.assertEqual((1, 2, 4, 8, 16), candidate_batches_for("dropout"))
        self.assertEqual(CANDIDATE_BATCHES, candidate_batches_for("classify"))
        self.assertEqual((*CANDIDATE_BATCHES, 1024), candidate_batches_for("ocr"))
        with self.assertRaisesRegex(ValueError, "unsupported benchmark module"):
            candidate_batches_for("nl")

    def test_report_contract_requires_all_non_nl_modules_and_three_formal_runs(self) -> None:
        self.assertEqual(
            (
                "caption",
                "classify",
                "replace",
                "ocr",
                "countReview",
                "dropout",
                "tokenBudget",
                "export",
            ),
            BENCHMARK_MODULES,
        )
        self.assertEqual((1, 2, 4, 8, 16, 32, 64, 128, 256, 500), CANDIDATE_BATCHES)
        report = {
            "schemaVersion": 1,
            "benchmarkVersion": "module-batching-v1",
            "dataset": {
                "before": {"fileCount": 0, "totalBytes": 0, "treeSha256": "0" * 64},
                "after": {"fileCount": 0, "totalBytes": 0, "treeSha256": "0" * 64},
            },
            "nlRequests": 0,
            "modules": {
                module: {
                    "batch1OutputDigest": "0" * 64,
                    "runs": [
                        {
                            "batchSize": 1,
                            "warmup": False,
                            "totalSeconds": 1.0,
                            "samplesPerSecond": 1.0,
                            "cpuPercent": 0.0,
                            "peakMemoryBytes": 0,
                            "gpuUtilizationPercent": 0.0,
                            "peakVramBytes": 0,
                            "failures": 0,
                            "timeouts": 0,
                            "oom": 0,
                            "crashed": 0,
                            "outputDigest": "0" * 64,
                        }
                    ] * 3,
                    "recommendation": 1,
                }
                for module in BENCHMARK_MODULES
            },
        }
        validate_report(report)

    def test_report_rejects_nonzero_nl_requests_and_mismatched_dataset_snapshots(self) -> None:
        report = {
            "schemaVersion": 1,
            "benchmarkVersion": "module-batching-v1",
            "dataset": {
                "before": {"fileCount": 1, "totalBytes": 1, "treeSha256": "0" * 64},
                "after": {"fileCount": 1, "totalBytes": 1, "treeSha256": "1" * 64},
            },
            "nlRequests": 1,
            "modules": {},
        }
        with self.assertRaisesRegex(ValueError, "dataset|NL"):
            validate_report(report)

    def test_snapshot_records_sorted_file_identity_and_digest(self) -> None:
        from tests.stress.module_batch_benchmark import snapshot_dataset

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b.txt").write_bytes(b"two")
            (root / "a.txt").write_bytes(b"one")
            snapshot = snapshot_dataset(root)
            self.assertEqual(["a.txt", "b.txt"], [item["relativePath"] for item in snapshot["files"]])
            self.assertEqual(snapshot["treeSha256"], snapshot_dataset(root)["treeSha256"])
            self.assertEqual(2, snapshot["fileCount"])
            self.assertTrue(all(type(item["mtimeNs"]) is int for item in snapshot["files"]))

    def test_overlay_uses_the_same_job_identity_as_the_candidate(self) -> None:
        dataset = Path(r"E:\Desktop\10_uiokv")
        fake_root = Path(r"E:\Desktop\.10_uiokv.anima-overlay-bench-dropout-4")
        with patch("anima_core.overlay.OverlayLayout.create", return_value=SimpleNamespace(root=fake_root)) as create:
            self.assertEqual(fake_root, _create_overlay(dataset, "dropout", 4))
        create.assert_called_once_with(dataset, "bench-dropout-4")

    def test_worker_shutdown_request_is_sent_before_session_is_closed(self) -> None:
        session = object.__new__(_WorkerSession)
        session._closed = False
        session.process = MagicMock()
        session.process.poll.return_value = None
        session.request = MagicMock(return_value={})
        session._terminate = MagicMock()
        session.sampler = MagicMock()
        session._stderr_thread = MagicMock()
        session.process.stdin = MagicMock()
        session.process.stdout = MagicMock()
        session.process.stderr = MagicMock()

        session.close()

        session.request.assert_called_once_with("shutdown", {})
        session._terminate.assert_not_called()
        self.assertTrue(session._closed)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows process counters are platform-specific")
    def test_windows_process_memory_counter_reports_current_process(self) -> None:
        value = _windows_process_memory_bytes(__import__("os").getpid())
        self.assertGreater(value, 0)

    def test_ocr_benchmark_binds_gpu_runtime_when_gpu_is_available(self) -> None:
        install = Path(tempfile.mkdtemp())
        try:
            manifest = install / "manifests" / "runtimes" / "ocr-paddle-gpu.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_bytes(b"gpu manifest")
            with patch("tests.stress.module_batch_benchmark._device_snapshot", return_value={"gpu": {"available": True}}):
                runtime_id, fingerprint = _ocr_benchmark_runtime(install)
            self.assertEqual("ocr-paddle-gpu", runtime_id)
            self.assertEqual(hashlib.sha256(b"gpu manifest").hexdigest(), fingerprint)
            hello = _ocr_hello("bench-ocr-1", "a" * 64, runtime_id, fingerprint)
            self.assertEqual("cuda", hello["requestedDevice"])
            self.assertEqual(runtime_id, hello["expectedRuntimeId"])
            self.assertEqual(fingerprint, hello["expectedRuntimeFingerprint"])
        finally:
            shutil.rmtree(install, ignore_errors=True)

    def test_recommendation_uses_average_stable_throughput_and_three_percent_tie_break(self) -> None:
        digest = "a" * 64

        def result(batch: int, speeds: tuple[float, float, float]) -> dict[str, object]:
            return {
                "runs": [
                    {"batchSize": batch, "warmup": False, "totalSeconds": 1.0, "samplesPerSecond": speed,
                     "cpuPercent": 0.0, "peakMemoryBytes": 0, "gpuUtilizationPercent": 0.0,
                     "peakVramBytes": 0, "failures": 0, "timeouts": 0, "oom": 0, "crashed": 0,
                     "outputDigest": digest}
                    for speed in speeds
                ]
            }

        recommendation, stable, reason = select_stable_recommendation(
            {"1": result(1, (100.0, 100.0, 100.0)), "2": result(2, (108.0, 108.0, 108.0)),
             "4": result(4, (110.0, 110.0, 110.0))},
            baseline_digest=digest,
        )
        self.assertEqual(2, recommendation)
        self.assertEqual([1, 2, 4], stable)
        self.assertIn("3%", reason)

    def test_recommendation_allows_existing_batch_one_failures_without_new_failures(self) -> None:
        digest = "a" * 64

        def result(batch: int, failures: int, speed: float) -> dict[str, object]:
            return {"runs": [{
                "batchSize": batch, "warmup": False, "totalSeconds": 1.0,
                "samplesPerSecond": speed, "cpuPercent": 0.0, "peakMemoryBytes": 0,
                "gpuUtilizationPercent": 0.0, "peakVramBytes": 0, "failures": failures,
                "timeouts": 0, "oom": 0, "crashed": 0, "outputDigest": digest,
            }] * 3}

        recommendation, stable, _ = select_stable_recommendation(
            {"1": result(1, 2, 10.0), "4": result(4, 2, 20.0)},
            baseline_digest=digest,
        )
        self.assertEqual(4, recommendation)
        self.assertEqual([1, 4], stable)

    def test_digest_normalizes_only_cuda_float_tail_noise(self) -> None:
        self.assertEqual(_json_digest({"score": 0.1234561}), _json_digest({"score": 0.1234564}))
        self.assertNotEqual(_json_digest({"score": 0.1234561}), _json_digest({"score": 0.12356}))

    def test_report_keeps_batch_one_baseline_when_recommendation_is_larger(self) -> None:
        digest = "0" * 64
        run = {
            "batchSize": 1,
            "warmup": False,
            "totalSeconds": 1.0,
            "samplesPerSecond": 1.0,
            "cpuPercent": 0.0,
            "peakMemoryBytes": 0,
            "gpuUtilizationPercent": 0.0,
            "peakVramBytes": 0,
            "failures": 0,
            "timeouts": 0,
            "oom": 0,
            "crashed": 0,
            "outputDigest": digest,
        }
        candidate = {"runs": [run] * 3, "batch1OutputDigest": digest, "recommendation": 2}
        report = {
            "schemaVersion": 1,
            "benchmarkVersion": "module-batching-v1",
            "dataset": {
                "before": {"fileCount": 0, "totalBytes": 0, "treeSha256": digest},
                "after": {"fileCount": 0, "totalBytes": 0, "treeSha256": digest},
            },
            "nlRequests": 0,
            "modules": {
                module: {
                    "batch1OutputDigest": digest,
                    "runs": [run] * 3 + [{**run, "batchSize": 2}],
                    "recommendation": 2,
                    "candidates": {"1": candidate, "2": candidate},
                }
                for module in BENCHMARK_MODULES
            },
        }
        validate_report(report)

    def test_count_review_candidate_uses_real_sqlite_runner_and_cleans_lifecycle(self) -> None:
        """Count Review measurements must exercise the persisted application path."""
        from tests.stress.module_batch_benchmark import DatasetSample, _run_count_review_candidate

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            image = dataset / "sample-1.png"
            image.write_bytes(b"immutable-image")
            (dataset / "sample-1.json").write_text(
                json.dumps({"count": "solo", "quality": [], "character": "", "series": "", "artist": "", "appearance": [], "tags": [], "environment": [], "nl": ""}),
                encoding="utf-8",
            )
            stat = image.stat()
            sample = DatasetSample(
                sample_id=1,
                relative_path="sample-1.png",
                annotation_key="sample-1",
                image_path=image,
                json_path=dataset / "sample-1.json",
                annotation={"count": "solo"},
                txt_text="solo",
                image_format="png",
                image_size=stat.st_size,
                image_mtime_ns=stat.st_mtime_ns,
                image_file_id="test:1",
                image_sha256="0" * 64,
            )
            result = _run_count_review_candidate(
                (sample,),
                1,
                3,
                dataset_root=dataset,
                temp_root=root / "state",
            )

        self.assertEqual("count-review-runner", result["workerEvidence"]["implementation"])
        self.assertTrue(result["workerEvidence"]["sqliteLifecycle"])
        self.assertEqual(1, result["runs"][-1]["outputCount"])
        self.assertEqual(0, result["runs"][-1]["failures"])


if __name__ == "__main__":
    unittest.main()
