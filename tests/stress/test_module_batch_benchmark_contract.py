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
    _failure_details,
    _finalize_state,
    _json_digest,
    _module_baseline_rows,
    _ocr_benchmark_runtime,
    _ocr_hello,
    _windows_process_memory_bytes,
    candidate_batches_for,
    select_stable_recommendation,
    validate_report,
)


def _valid_run(batch_size: int, *, warmup: bool, speed: float = 1.0, digest: str = "0" * 64) -> dict[str, object]:
    return {
        "batchSize": batch_size,
        "warmup": warmup,
        "totalSeconds": 1.0,
        "samplesPerSecond": speed,
        "cpuPercent": 0.0,
        "peakMemoryBytes": 0,
        "gpuUtilizationPercent": 0.0,
        "peakVramBytes": 0,
        "failures": 0,
        "timeouts": 0,
        "oom": 0,
        "crashed": 0,
        "outputDigest": digest,
        "failureDetails": [],
    }


def _valid_report() -> dict[str, object]:
    digest = "0" * 64
    modules: dict[str, object] = {}
    for module in BENCHMARK_MODULES:
        candidates: dict[str, object] = {}
        runs: list[dict[str, object]] = []
        batches = candidate_batches_for(module)
        for batch_size in batches:
            candidate_runs = [
                _valid_run(batch_size, warmup=True, digest=digest),
                *[_valid_run(batch_size, warmup=False, digest=digest) for _ in range(3)],
            ]
            candidates[str(batch_size)] = {"runs": candidate_runs}
            runs.extend(candidate_runs)
        modules[module] = {
            "batch1OutputDigest": digest,
            "runs": runs,
            "recommendation": 1,
            "recommendationReason": "fixture",
            "stableBatchSizes": list(batches),
            "workerEvidence": {},
            "candidates": candidates,
        }
    return {
        "schemaVersion": 1,
        "benchmarkVersion": "module-batching-v1",
        "status": "validated",
        "dataset": {
            "before": {"fileCount": 0, "totalBytes": 0, "treeSha256": digest},
            "after": {"fileCount": 0, "totalBytes": 0, "treeSha256": digest},
        },
        "nlRequests": 0,
        "modules": modules,
    }


class ModuleBatchBenchmarkContractTests(unittest.TestCase):
    def test_state_artifact_records_validated_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            before = {"fileCount": 0, "totalBytes": 0, "treeSha256": "0" * 64}
            state = {"status": "running", "modules": {}, "overlayRoots": []}

            _finalize_state(
                state_path,
                state,
                dataset_root=Path(temporary) / "dataset",
                before=before,
                after=before,
                report_path=Path(temporary) / "report.json",
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("validated", persisted["status"])
            self.assertEqual(str(Path(temporary) / "report.json"), persisted["reportPath"])
            self.assertEqual(before, persisted["dataset"]["before"])
            self.assertEqual(before, persisted["dataset"]["after"])
            self.assertEqual(list(BENCHMARK_MODULES), persisted["completedModules"])

    def test_candidate_batches_are_limited_by_each_module_protocol(self) -> None:
        self.assertEqual((1, 2, 4, 8, 16, 32, 64), candidate_batches_for("caption"))
        self.assertEqual((1, 2, 4, 8, 16), candidate_batches_for("dropout"))
        self.assertEqual(CANDIDATE_BATCHES, candidate_batches_for("classify"))
        self.assertEqual((*CANDIDATE_BATCHES, 1024), candidate_batches_for("ocr"))
        with self.assertRaisesRegex(ValueError, "unsupported benchmark module"):
            candidate_batches_for("nl")

    def test_report_requires_validated_status(self) -> None:
        report = _valid_report()
        report["status"] = "not_validated"
        with self.assertRaisesRegex(ValueError, "status"):
            validate_report(report)

    def test_report_requires_the_complete_candidate_grid(self) -> None:
        report = _valid_report()
        candidates = report["modules"]["caption"]["candidates"]  # type: ignore[index]
        candidates.pop("2")
        with self.assertRaisesRegex(ValueError, "candidate grid"):
            validate_report(report)

        report = _valid_report()
        candidates = report["modules"]["caption"]["candidates"]  # type: ignore[index]
        candidates["999"] = candidates["1"]
        with self.assertRaisesRegex(ValueError, "candidate grid"):
            validate_report(report)

    def test_report_requires_exactly_three_formal_runs_per_candidate(self) -> None:
        report = _valid_report()
        candidate = report["modules"]["caption"]["candidates"]["1"]  # type: ignore[index]
        candidate["runs"].append(_valid_run(1, warmup=False))  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "exactly three formal"):
            validate_report(report)

    def test_report_requires_recommendation_in_candidate_and_stable_sets(self) -> None:
        report = _valid_report()
        report["modules"]["caption"]["recommendation"] = 999999  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "recommendation"):
            validate_report(report)

        report = _valid_report()
        report["modules"]["caption"]["stableBatchSizes"] = [2]  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "recommendation"):
            validate_report(report)

    def test_report_recomputes_recommendation_and_stable_candidates(self) -> None:
        report = _valid_report()
        candidate = report["modules"]["caption"]["candidates"]["2"]  # type: ignore[index]
        for run in candidate["runs"]:  # type: ignore[index]
            if not run["warmup"]:
                run["samplesPerSecond"] = 2.0
        with self.assertRaisesRegex(ValueError, "recomputed recommendation"):
            validate_report(report)

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
        report = _valid_report()
        validate_report(report)

    def test_report_rejects_nonzero_nl_requests_and_mismatched_dataset_snapshots(self) -> None:
        report = {
            "schemaVersion": 1,
            "benchmarkVersion": "module-batching-v1",
            "status": "validated",
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

    def test_baseline_rows_recognize_ocr_cuda_runtime_evidence(self) -> None:
        report = {
            "device": {
                "cpuPhysicalCores": 6,
                "cpuLogicalCores": 12,
                "gpu": {
                    "available": True,
                    "totalVramBytes": 100,
                    "freeVramBytes": 80,
                },
            },
            "modules": {
                module: {
                    "recommendation": 1,
                    "workerEvidence": {},
                }
                for module in BENCHMARK_MODULES
            },
        }
        report["modules"]["ocr"]["workerEvidence"] = {
            "observedDevice": "cuda",
            "runtimeId": "ocr-paddle-gpu",
        }

        rows = {row["module"]: row for row in _module_baseline_rows(report)}

        self.assertTrue(rows["ocr"]["gpuRequired"])
        self.assertEqual(100, rows["ocr"]["minTotalVramBytes"])
        self.assertEqual(80, rows["ocr"]["minFreeVramBytes"])
        self.assertFalse(rows["classify"]["gpuRequired"])

    def test_recommendation_uses_average_stable_throughput_and_three_percent_tie_break(self) -> None:
        digest = "a" * 64

        def result(batch: int, speeds: tuple[float, float, float]) -> dict[str, object]:
            return {
                "runs": [
                    {"batchSize": batch, "warmup": False, "totalSeconds": 1.0, "samplesPerSecond": speed,
                     "cpuPercent": 0.0, "peakMemoryBytes": 0, "gpuUtilizationPercent": 0.0,
                     "peakVramBytes": 0, "failures": 0, "timeouts": 0, "oom": 0, "crashed": 0,
                     "outputDigest": digest, "failureDetails": []}
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
                "failureDetails": [{
                    "sampleId": 1, "relativePath": "sample.png", "category": "deterministic_fixture",
                    "code": "fixture_failure", "reason": "fixture", "status": "issue",
                }] * failures,
            }] * 3}

        recommendation, stable, _ = select_stable_recommendation(
            {"1": result(1, 2, 10.0), "4": result(4, 2, 20.0)},
            baseline_digest=digest,
        )
        self.assertEqual(4, recommendation)
        self.assertEqual([1, 4], stable)

    def test_recommendation_rejects_a_different_failure_set_with_the_same_count(self) -> None:
        digest = "a" * 64

        def run(batch: int, sample_id: int, speed: float) -> dict[str, object]:
            return {
                "batchSize": batch,
                "warmup": False,
                "totalSeconds": 1.0,
                "samplesPerSecond": speed,
                "cpuPercent": 0.0,
                "peakMemoryBytes": 0,
                "gpuUtilizationPercent": 0.0,
                "peakVramBytes": 0,
                "failures": 1,
                "timeouts": 0,
                "oom": 0,
                "crashed": 0,
                "outputDigest": digest,
                "failureDetails": [{
                    "sampleId": sample_id,
                    "relativePath": f"sample-{sample_id}.png",
                    "category": "deterministic_fixture",
                    "code": "fixture_failure",
                    "reason": "fixture",
                    "status": "issue",
                }],
            }

        recommendation, stable, _ = select_stable_recommendation(
            {"1": {"runs": [run(1, 1, 10.0)] * 3}, "4": {"runs": [run(4, 2, 20.0)] * 3}},
            baseline_digest=digest,
        )
        self.assertEqual(1, recommendation)
        self.assertEqual([1], stable)

    def test_digest_normalizes_only_cuda_float_tail_noise(self) -> None:
        self.assertEqual(_json_digest({"score": 0.1234561}), _json_digest({"score": 0.1234564}))
        self.assertNotEqual(_json_digest({"score": 0.1234561}), _json_digest({"score": 0.12356}))

    def test_report_keeps_batch_one_baseline_when_recommendation_is_larger(self) -> None:
        report = _valid_report()
        for module in BENCHMARK_MODULES:
            result = report["modules"][module]  # type: ignore[index]
            for candidate_run in result["candidates"]["2"]["runs"]:  # type: ignore[index]
                if not candidate_run["warmup"]:
                    candidate_run["samplesPerSecond"] = 2.0
            result["recommendation"] = 2
        validate_report(report)

    def test_failure_details_preserve_sample_identity_and_overflow_category(self) -> None:
        samples = (
            SimpleNamespace(sample_id=7, relative_path="nested/seven.png"),
            SimpleNamespace(sample_id=8, relative_path="nested/eight.png"),
        )
        details = _failure_details(
            "tokenBudget",
            [{"sampleId": 7, "status": "overflow"}, {"sampleId": 8, "status": "failed", "code": "bad_input"}],
            samples,
        )
        self.assertEqual(2, len(details))
        self.assertEqual(7, details[0]["sampleId"])
        self.assertEqual("nested/seven.png", details[0]["relativePath"])
        self.assertEqual("overflow", details[0]["category"])
        self.assertEqual("token_budget_overflow", details[0]["code"])
        self.assertEqual("deterministic_fixture", details[1]["category"])

    def test_report_requires_failure_details_and_three_formal_runs_for_each_candidate(self) -> None:
        report = _valid_report()
        report["modules"]["caption"]["candidates"]["1"]["runs"] = [  # type: ignore[index]
            _valid_run(1, warmup=False),
            _valid_run(1, warmup=False),
        ]
        with self.assertRaisesRegex(ValueError, "three formal"):
            validate_report(report)

        report = _valid_report()
        invalid_run = {**_valid_run(1, warmup=False), "failureDetails": [{
            "sampleId": 1, "relativePath": "sample.png", "category": "deterministic_fixture",
            "code": "fixture_failure", "reason": "fixture", "status": "issue",
        }]}
        report["modules"]["caption"]["runs"][1] = invalid_run  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "failureDetails"):
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
