from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.launcher import WorkerLauncher
from anima_core.ocr_protocol import parse_ocr_process_result
from anima_core.worker_protocol import ProtocolEnvelopeV1, decode_frame, encode_frame
from tests.worker_test_support import test_config_hash, worker_hello_payload


INSTALL_ROOT = Path(os.environ.get("ANIMA_INSTALL_ROOT", ROOT / ".runtime-build"))
RESOURCE_ROOT = Path(os.environ.get("ANIMA_RESOURCE_ROOT", ROOT / "resource-library"))
OCR_RESOURCE_MANIFEST = r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json"
OCR_RESOURCE_FINGERPRINT = "368c31b8af0e96cc61239097688a457a050dfcc1205d054d4e631bd20529c9ca"


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _work_item(sample_id: int, path: Path, relative_path: str) -> dict[str, object]:
    information = path.stat()
    return {
        "schemaVersion": 1,
        "sampleId": sample_id,
        "leaseId": f"task10-lease-{sample_id}",
        "relativeImagePath": relative_path,
        "imagePath": str(path),
        "imageSize": information.st_size,
        "imageSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class OcrV1EndToEndTests(unittest.TestCase):
    def _run_existing_case(self, module_name: str, class_name: str, method: str) -> None:
        """Reuse the established Core fixture without weakening its assertions."""
        result = unittest.TestResult()
        test_type = getattr(importlib.import_module(module_name), class_name)
        test_type(method).run(result)
        details = "\n".join(text for _, text in (*result.failures, *result.errors))
        self.assertEqual([], result.skipped, details)
        self.assertEqual([], result.failures, details)
        self.assertEqual([], result.errors, details)

    @unittest.skipUnless(
        (INSTALL_ROOT / "runtimes" / "ocr-paddle").is_dir()
        and (RESOURCE_ROOT / Path(OCR_RESOURCE_MANIFEST.replace("\\", "/"))).is_file(),
        "formal local OCR runtime or resource is unavailable",
    )
    def test_formal_offline_worker_processes_fixed_text_and_failure_samples_read_only(self) -> None:
        resource = RESOURCE_ROOT / Path(OCR_RESOURCE_MANIFEST.replace("\\", "/")).parent
        fixed_text = resource / "textline-orientation" / "img_textline180_demo_res.jpg"
        self.assertTrue(fixed_text.is_file())
        before = _tree_hashes(resource)

        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            corrupt = temporary / "corrupt.png"
            corrupt.write_bytes(b"not an image")
            payload = {
                "schemaVersion": 1,
                "payloadType": "ocr_process_request",
                "items": [
                    _work_item(1, fixed_text, "fixed-text.jpg"),
                    _work_item(2, corrupt, "corrupt.png"),
                ],
            }
            with worker_hello_payload("ocr-paddle", INSTALL_ROOT) as (_, hello_payload):
                launch = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT).resolve(
                    "ocr-paddle", expected_owner="ocr",
                )
                self.assertIn("-B", launch.command)
                self.assertIn("-I", launch.command)
                frames = b"".join((
                    encode_frame(ProtocolEnvelopeV1("1.0", "request", "task10-hello", "ocr-paddle", "ocr", "hello", hello_payload, jobId="worker-test", configHash=test_config_hash())),
                    encode_frame(ProtocolEnvelopeV1("1.0", "request", "task10-process", "ocr-paddle", "ocr", "process_batch", payload, jobId="worker-test", configHash=test_config_hash())),
                    encode_frame(ProtocolEnvelopeV1("1.0", "request", "task10-shutdown", "ocr-paddle", "ocr", "shutdown", {})),
                ))
                completed = subprocess.run(
                    launch.command,
                    input=frames,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=launch.environment,
                    cwd=str(INSTALL_ROOT),
                    check=False,
                    timeout=240,
                )

        stderr = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(0, completed.returncode, stderr)
        responses = completed.stdout.splitlines()
        self.assertEqual(3, len(responses), completed.stdout.decode("utf-8", errors="replace"))
        self.assertTrue(decode_frame(responses[0], runtime_id="ocr-paddle", owner="ocr").payload["ready"])
        outcomes = parse_ocr_process_result(
            decode_frame(responses[1], runtime_id="ocr-paddle", owner="ocr").payload
        )
        self.assertEqual(
            ("success", "failed"),
            tuple(item.status for item in outcomes),
            json.dumps([item.to_dict() for item in outcomes], ensure_ascii=False),
        )
        self.assertGreater(len(outcomes[0].items), 0, "fixed clear-text sample must produce structured OCR")
        self.assertEqual("ocr_inference_failed", outcomes[1].error.code)
        self.assertEqual("result", decode_frame(responses[2], runtime_id="ocr-paddle", owner="ocr").method)
        self.assertEqual(before, _tree_hashes(resource), "formal OCR resource must remain read-only")

    def test_v9_core_lifecycle_matrix_preserves_duplicate_text_reuse_repair_export_and_isolation(self) -> None:
        cases = (
            ("tests.unit.test_ocr_runner", "OcrRunnerTests", "test_success_keeps_worker_order_duplicate_text_and_business_annotations_unchanged"),
            ("tests.unit.test_ocr_runner", "OcrRunnerTests", "test_reuses_legal_formal_sidecar_and_rewrites_only_llm_threshold_in_task_overlay"),
            ("tests.unit.test_ocr_runner", "OcrRunnerTests", "test_corrupt_failed_changed_resource_changed_image_and_force_sidecars_reinfer"),
            ("tests.unit.test_ocr_runner", "OcrRunnerTests", "test_no_text_is_a_normal_completed_result"),
            ("tests.unit.test_ocr_runner", "OcrRunnerTests", "test_normal_inference_failure_replaces_stale_text_and_settles_nonblocking_retriable_issue"),
            ("tests.unit.test_ocr_runner", "OcrRunnerTests", "test_success_sidecar_is_staged_before_completion_and_prepared_file_is_consumed"),
            ("tests.integration.test_ocr_core_runner", "OcrCoreRunnerIntegrationTests", "test_v9_ocr_and_nl_four_mode_matrix_keeps_ocr_independent_and_serial"),
            ("tests.integration.test_ocr_core_runner", "OcrCoreRunnerIntegrationTests", "test_cancellation_stops_after_inflight_ocr_sidecar_is_safely_settled"),
            ("tests.integration.test_ocr_core_runner", "OcrCoreRunnerIntegrationTests", "test_worker_restart_commits_an_already_prepared_ocr_sidecar_once"),
            ("tests.integration.test_ocr_core_runner", "OcrCoreRunnerIntegrationTests", "test_ocr_only_repair_targets_the_failed_sample_then_continues_to_nl_and_export"),
            ("tests.unit.test_nl_runner", "NlRunnerTests", "test_v5_ocr_context_is_omitted_only_when_combined_canonical_utf8_exceeds_limit"),
            ("tests.unit.test_export_commit", "ExportCommitTests", "test_ocr_enabled_and_disabled_commits_have_byte_identical_business_outputs"),
        )
        for module_name, class_name, method in cases:
            with self.subTest(test=f"{class_name}.{method}"):
                self._run_existing_case(module_name, class_name, method)


if __name__ == "__main__":
    unittest.main()
