from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.caption_protocol import CaptionProcessResultV1, CaptionResultV1
from anima_core.launcher import WorkerLauncher
from anima_core.ocr_protocol import parse_ocr_process_result
from anima_core.runtime_manifest import RuntimeBundleManifestV1, runtime_lifecycle, validate_runtime_isolation
from anima_core.worker_protocol import ProtocolEnvelopeV1, decode_frame, encode_frame
from tests.worker_test_support import test_config_hash, worker_hello_payload


INSTALL_ROOT = Path(os.environ.get("ANIMA_INSTALL_ROOT", ROOT / ".runtime-build"))
RESOURCE_ROOT = Path(os.environ.get("ANIMA_RESOURCE_ROOT", ROOT / "resource-library"))
CAPTION_RESOURCE_MANIFEST = "tagging-models\\caption-e621-eva02-large-full-v1\\resource.json"
CAPTION_RESOURCE_FINGERPRINT = "ba31816d7e8283ab13f8127419fdb5ea9f322344fc88bb01f6d3a64afab62ec3"
OCR_RESOURCE_MANIFEST = "ocr-models\\ocr-ppocrv5-server-paddle-v1\\resource.json"
OCR_RESOURCE_FINGERPRINT = "368c31b8af0e96cc61239097688a457a050dfcc1205d054d4e631bd20529c9ca"
RUNTIME_OWNERS = {
    "caption-e621": "caption",
    "classify-e621": "classify",
    "replace-e621": "replace",
    "nl": "nl",
    "policy": "policy",
    "export": "export",
    "ocr-paddle": "ocr",
}


@unittest.skipUnless((INSTALL_ROOT / "manifests" / "runtimes").is_dir(), "embedded release tree has not been built")
class EmbeddedRuntimeTests(unittest.TestCase):
    def test_gpu_runtime_declaration_has_optional_all_or_none_formal_assembly(self) -> None:
        self.assertEqual("declared-unassembled", runtime_lifecycle("ocr-paddle-gpu"))
        formal_targets = (
            (INSTALL_ROOT / "runtimes" / "ocr-paddle-gpu", True),
            (INSTALL_ROOT / "manifests" / "runtimes" / "ocr-paddle-gpu.json", False),
            (INSTALL_ROOT / "manifests" / "requirements" / "ocr-paddle-gpu.lock", False),
            (ROOT / "packaging" / "requirements" / "ocr-paddle-gpu.lock", False),
        )
        self.assertFalse((ROOT / "packaging" / "wheelhouse" / "ocr-paddle-gpu").exists())
        present = tuple(path.exists() for path, _ in formal_targets)
        self.assertIn(present, ((False,) * len(formal_targets), (True,) * len(formal_targets)))
        if not any(present):
            return

        for path, is_directory in formal_targets:
            self.assertEqual(is_directory, path.is_dir(), str(path))

        runtime_path, manifest_path, manifest_lock_path, packaging_lock_path = (
            path for path, _ in formal_targets
        )
        self.assertEqual(packaging_lock_path.read_bytes(), manifest_lock_path.read_bytes())
        gpu_manifest = RuntimeBundleManifestV1.load(manifest_path)
        self.assertEqual(("ocr-paddle-gpu", "ocr", "anima_ocr_worker.entry"), (
            gpu_manifest.runtime.runtimeId,
            gpu_manifest.runtime.owner,
            gpu_manifest.launch.entryModule,
        ))
        gpu_interpreter = gpu_manifest.verify_interpreter(INSTALL_ROOT, timeout_seconds=30)

        default_manifests = self._manifests()
        gpu_selection = [
            manifest for manifest in default_manifests
            if manifest.runtime.runtimeId != "ocr-paddle"
        ] + [gpu_manifest]
        self.assertEqual(8, len(gpu_selection))
        validate_runtime_isolation(gpu_selection, INSTALL_ROOT)
        self.assertEqual(8, len({manifest.runtime.runtimeId for manifest in gpu_selection}))
        self.assertEqual(8, len({manifest.runtime.owner for manifest in gpu_selection}))
        self.assertEqual(8, len({str(manifest.resolve_interpreter(INSTALL_ROOT).resolve()).casefold() for manifest in gpu_selection}))

        cpu_manifest = next(manifest for manifest in default_manifests if manifest.runtime.runtimeId == "ocr-paddle")
        cpu_interpreter = cpu_manifest.resolve_interpreter(INSTALL_ROOT)
        self.assertNotEqual(str(cpu_interpreter.resolve()).casefold(), str(gpu_interpreter.resolve()).casefold())
        self.assertNotEqual(
            str((cpu_interpreter.parent / "Lib" / "site-packages").resolve()).casefold(),
            str((gpu_interpreter.parent / "Lib" / "site-packages").resolve()).casefold(),
        )

    def test_ocr_runtime_is_assembled(self) -> None:
        self.assertTrue((INSTALL_ROOT / "runtimes" / "ocr-paddle").is_dir())
        self.assertTrue((INSTALL_ROOT / "manifests" / "runtimes" / "ocr-paddle.json").is_file())
        self.assertTrue((INSTALL_ROOT / "manifests" / "requirements" / "ocr-paddle.lock").is_file())
        self.assertTrue((ROOT / "workers" / "ocr" / "src" / "anima_ocr_worker" / "entry.py").is_file())
        manifest = RuntimeBundleManifestV1.load(INSTALL_ROOT / "manifests" / "runtimes" / "ocr-paddle.json")
        self.assertEqual(("ocr-paddle", "ocr", "anima_ocr_worker.entry"), (
            manifest.runtime.runtimeId, manifest.runtime.owner, manifest.launch.entryModule,
        ))

    def test_ocr_runtime_manifest_generation_requires_explicit_assembly_opt_in(self) -> None:
        generator = ROOT / "packaging" / "scripts" / "generate_runtime_manifests.py"
        source = generator.read_text(encoding="utf-8")

        self.assertIn("ASSEMBLED_OCR_RUNTIME", source)
        self.assertIn("--include-ocr-paddle", source)
        self.assertIn("include_ocr_paddle", source)

    def _manifests(self) -> list[RuntimeBundleManifestV1]:
        return [
            RuntimeBundleManifestV1.load(INSTALL_ROOT / "manifests" / "runtimes" / f"{runtime_id}.json")
            for runtime_id in ("core", *RUNTIME_OWNERS)
        ]

    def test_eight_manifests_hashes_versions_and_paths_are_isolated(self) -> None:
        manifests = self._manifests()
        self.assertEqual(8, len(manifests))
        validate_runtime_isolation(manifests, INSTALL_ROOT)
        interpreters = []
        for manifest in manifests:
            interpreter = manifest.verify_interpreter(INSTALL_ROOT, timeout_seconds=30)
            interpreters.append(interpreter)
            self.assertEqual("3.11.15", manifest.runtime.pythonVersion)
        self.assertEqual(8, len({str(path).lower() for path in interpreters}))
        self.assertEqual(8, len({str(path.parent / "Lib" / "site-packages").lower() for path in interpreters}))

    def test_isolated_mode_ignores_host_python_and_has_no_pip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            poison = Path(temporary)
            environment = dict(os.environ)
            environment.update({
                "PYTHONPATH": str(poison),
                "PYTHONHOME": str(poison),
                "PIP_INDEX_URL": "https://invalid.example",
                "VIRTUAL_ENV": str(poison),
                "CONDA_PREFIX": str(poison),
                "UV_PROJECT_ENVIRONMENT": str(poison),
            })
            for manifest in self._manifests():
                interpreter = manifest.resolve_interpreter(INSTALL_ROOT)
                completed = subprocess.run(
                    [str(interpreter), "-B", "-I", "-c", "import json,site,sys; print(json.dumps({'path':sys.path,'user':site.ENABLE_USER_SITE}))"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment, check=False, timeout=30,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                value = json.loads(completed.stdout)
                self.assertFalse(value["user"])
                self.assertNotIn(str(poison), value["path"])
                self.assertTrue(all(str(Path(item)).lower().startswith(str(interpreter.parent).lower()) for item in value["path"]))
                pip = subprocess.run(
                    [str(interpreter), "-B", "-I", "-m", "pip", "--version"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment, check=False, timeout=30,
                )
                self.assertNotEqual(0, pip.returncode)
                self.assertIn("No module named pip", pip.stderr)

    def test_runtime_owned_dependencies_import(self) -> None:
        checks = {
            "core": "import fastapi,uvicorn,PIL",
            "caption-e621": "import numpy,onnxruntime,PIL",
            "classify-e621": "import sqlite3,json",
            "replace-e621": "import csv,json",
            "nl": "import httpx,PIL",
            "policy": "import numpy,open_clip,PIL,safetensors,torch,torchvision",
            "export": "import sqlite3,zipfile",
            "ocr-paddle": "import paddle,paddleocr,paddlex,PIL",
        }
        for runtime_id, statement in checks.items():
            manifest = RuntimeBundleManifestV1.load(INSTALL_ROOT / "manifests" / "runtimes" / f"{runtime_id}.json")
            completed = subprocess.run(
                [str(manifest.resolve_interpreter(INSTALL_ROOT)), "-B", "-I", "-c", statement],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=60,
            )
            self.assertEqual(0, completed.returncode, f"{runtime_id}: {completed.stderr}")

    def test_embedded_core_runtime_accepts_local_only_ocr_resource(self) -> None:
        core = RuntimeBundleManifestV1.load(INSTALL_ROOT / "manifests" / "runtimes" / "core.json")
        script = (
            "import json; from pathlib import Path; from anima_core.resource_catalog import ResourceCatalog; "
            f"snapshot = ResourceCatalog(Path({str(RESOURCE_ROOT)!r})).scan(); "
            "print(json.dumps([(item.relative_path, item.reason) for item in snapshot.invalid if item.relative_path.startswith('ocr-models')]))"
        )
        completed = subprocess.run(
            [str(core.resolve_interpreter(INSTALL_ROOT)), "-B", "-I", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], json.loads(completed.stdout))

    def test_real_workers_complete_hello_and_shutdown(self) -> None:
        launcher = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT)
        for runtime_id, owner in RUNTIME_OWNERS.items():
            with self.subTest(runtime_id=runtime_id):
                with worker_hello_payload(runtime_id, INSTALL_ROOT) as (mode, payload):
                    if mode == "transport_only":
                        launch = launcher.resolve(runtime_id, expected_owner=owner)
                        environment = dict(launch.environment)
                        environment["ANIMA_TEST_TRANSPORT_ONLY"] = "1"
                        process = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=environment, cwd=str(INSTALL_ROOT))
                    else:
                        process = launcher.spawn(runtime_id, expected_owner=owner)
                    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
                    job_id = None if mode == "transport_only" else "worker-test"
                    config_hash = None if mode == "transport_only" else test_config_hash()
                    process.stdin.write(encode_frame(ProtocolEnvelopeV1("1.0", "request", "hello-1", runtime_id, owner, "hello", payload, jobId=job_id, configHash=config_hash)))
                    process.stdin.flush()
                    hello = decode_frame(process.stdout.readline(), runtime_id=runtime_id, owner=owner)
                    self.assertEqual("hello", hello.method)
                    self.assertTrue(hello.payload.get("ready", True))
                    process.stdin.write(encode_frame(ProtocolEnvelopeV1("1.0", "request", "shutdown-1", runtime_id, owner, "shutdown", {})))
                    process.stdin.flush()
                    self.assertEqual("result", decode_frame(process.stdout.readline(), runtime_id=runtime_id, owner=owner).method)
                    process.stdin.close()
                    stderr = process.stderr.read().decode("utf-8", errors="replace")
                    self.assertEqual(0, process.wait(timeout=30), stderr)
                    process.stdout.close()
                    process.stderr.close()

    def test_ocr_embedded_worker_processes_fixed_resource_sample(self) -> None:
        sample = RESOURCE_ROOT / Path(OCR_RESOURCE_MANIFEST.replace("\\", "/")).parent / "textline-orientation" / "img_textline180_demo_res.jpg"
        self.assertTrue(sample.is_file())
        information = sample.stat()
        process_payload = {
            "schemaVersion": 1,
            "payloadType": "ocr_process_request",
            "items": [{
                "schemaVersion": 1,
                "sampleId": 1,
                "leaseId": "ocr-real-sample",
                "relativeImagePath": sample.name,
                "imagePath": str(sample),
                "imageSize": information.st_size,
                "imageSha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
            }],
        }
        with worker_hello_payload("ocr-paddle", INSTALL_ROOT) as (_, hello_payload):
            launch = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT).resolve(
                "ocr-paddle", expected_owner="ocr",
            )
            frames = b"".join((
                encode_frame(ProtocolEnvelopeV1("1.0", "request", "hello-ocr-real", "ocr-paddle", "ocr", "hello", hello_payload, jobId="worker-test", configHash=test_config_hash())),
                encode_frame(ProtocolEnvelopeV1("1.0", "request", "process-ocr-real", "ocr-paddle", "ocr", "process_batch", process_payload, jobId="worker-test", configHash=test_config_hash())),
                encode_frame(ProtocolEnvelopeV1("1.0", "request", "shutdown-ocr-real", "ocr-paddle", "ocr", "shutdown", {})),
            ))
            completed = subprocess.run(
                launch.command, input=frames, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=launch.environment, cwd=str(INSTALL_ROOT), check=False, timeout=180,
            )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", errors="replace"))
        responses = completed.stdout.splitlines()
        self.assertEqual(3, len(responses), completed.stdout.decode("utf-8", errors="replace"))
        self.assertEqual("hello", decode_frame(responses[0], runtime_id="ocr-paddle", owner="ocr").method)
        result = decode_frame(responses[1], runtime_id="ocr-paddle", owner="ocr")
        self.assertEqual("result", result.method)
        outcome = parse_ocr_process_result(result.payload)[0]
        self.assertIn(outcome.status, {"success", "no_text"})
        self.assertEqual("result", decode_frame(responses[2], runtime_id="ocr-paddle", owner="ocr").method)

    @unittest.skipUnless(
        (RESOURCE_ROOT / Path(CAPTION_RESOURCE_MANIFEST.replace("\\", "/"))).is_file(),
        "caption E621 resource has not been assembled",
    )
    def test_caption_embedded_worker_loads_the_pinned_model_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset_root = Path(temporary)
            rgb_path = dataset_root / "rgb.png"
            Image.new("RGB", (32, 24), (192, 96, 48)).save(rgb_path)
            alpha_path = dataset_root / "alpha.png"
            Image.new("RGBA", (24, 32), (32, 128, 224, 128)).save(alpha_path)
            oriented_path = dataset_root / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (20, 30), (48, 160, 80)).save(oriented_path, exif=exif)
            image_specs = (
                (1, rgb_path, "png"),
                (2, alpha_path, "png"),
                (3, oriented_path, "jpeg"),
            )
            payload = {
                "schemaVersion": 1,
                "payloadType": "caption_hello_request",
                "jobId": "job-real-caption",
                "configHash": "a" * 64,
                "profile": "e621",
                "datasetRoot": str(dataset_root),
                "resourceManifestRelativePath": CAPTION_RESOURCE_MANIFEST,
                "resourceFingerprint": CAPTION_RESOURCE_FINGERPRINT,
                "thresholdPolicy": {"mode": "model_default"},
                "captionFormat": {
                    "replaceUnderscoresWithSpaces": True,
                    "preserveEscapes": True,
                    "triggersEnabled": True,
                    "triggerTerms": ["anima_style"],
                },
                "imageDecode": {
                    "extensions": [".jpg", ".jpeg", ".png", ".webp", ".bmp"],
                    "rejectMultiFrame": True,
                    "applyExifTranspose": True,
                    "alphaBackground": "#FFFFFF",
                },
            }
            hello = ProtocolEnvelopeV1(
                "1.0",
                "request",
                "hello-caption-real",
                "caption-e621",
                "caption",
                "hello",
                payload,
                jobId="job-real-caption",
                configHash="a" * 64,
            )
            caption_items: list[dict[str, object]] = []
            for sample_id, image_path, image_format in image_specs:
                information = image_path.stat()
                caption_items.append({
                    "schemaVersion": 1,
                    "sampleId": sample_id,
                    "leaseId": f"lease-caption-real-{sample_id}",
                    "source": "e621",
                    "relativeImagePath": image_path.name,
                    "annotationKey": image_path.stem,
                    "imageFormat": image_format,
                    "imageFrameCount": 1,
                    "imageFileId": f"{getattr(information, 'st_dev', 0)}:{getattr(information, 'st_ino', 0)}",
                    "imageSize": information.st_size,
                    "imageMtimeNs": information.st_mtime_ns,
                })
            process_request = ProtocolEnvelopeV1(
                "1.0",
                "request",
                "process-caption-real-batch",
                "caption-e621",
                "caption",
                "process_batch",
                {"schemaVersion": 1, "payloadType": "caption_process_request", "items": caption_items},
                jobId="job-real-caption",
                configHash="a" * 64,
            )
            shutdown = ProtocolEnvelopeV1(
                "1.0",
                "request",
                "shutdown-caption-real",
                "caption-e621",
                "caption",
                "shutdown",
                {},
            )
            launch = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT).resolve(
                "caption-e621",
                expected_owner="caption",
            )
            completed = subprocess.run(
                launch.command,
                input=encode_frame(hello) + encode_frame(process_request) + encode_frame(shutdown),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=launch.environment,
                cwd=str(INSTALL_ROOT),
                check=False,
                timeout=300,
            )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", errors="replace"))
        responses = completed.stdout.splitlines()
        self.assertEqual(3, len(responses), completed.stdout.decode("utf-8", errors="replace"))
        initialized = decode_frame(responses[0], runtime_id="caption-e621", owner="caption")
        self.assertEqual("hello", initialized.method)
        self.assertTrue(initialized.payload["ready"])
        self.assertEqual(1, initialized.payload["modelSessionLoads"])
        self.assertEqual(8_783, initialized.payload["tagCount"])
        self.assertEqual(CAPTION_RESOURCE_FINGERPRINT, initialized.payload["resourceFingerprint"])
        self.assertIn(initialized.payload["provider"], {"CUDAExecutionProvider", "CPUExecutionProvider"})
        processed = decode_frame(responses[1], runtime_id="caption-e621", owner="caption")
        self.assertEqual("result", processed.method)
        batch_result = CaptionProcessResultV1.from_dict(processed.payload)
        self.assertEqual(len(image_specs), len(batch_result.outcomes))
        for expected_sample_id, outcome in enumerate(batch_result.outcomes, start=1):
            self.assertIsInstance(outcome, CaptionResultV1)
            self.assertEqual(expected_sample_id, outcome.sampleId)
            self.assertGreater(len(outcome.tags), 0)
            self.assertLessEqual(len(outcome.tags), 8_783)
            self.assertTrue(outcome.formattedTxt.startswith("anima style, "))
            self.assertEqual(1, outcome.modelSessionLoads)
            self.assertEqual(initialized.payload["provider"], outcome.provider)
            self.assertTrue(all(
                left.score >= right.score
                for left, right in zip(outcome.tags, outcome.tags[1:], strict=False)
            ))
        self.assertLessEqual(len(responses[1]), 1_048_576)
        self.assertEqual(
            "result",
            decode_frame(responses[2], runtime_id="caption-e621", owner="caption").method,
        )


if __name__ == "__main__":
    unittest.main()
