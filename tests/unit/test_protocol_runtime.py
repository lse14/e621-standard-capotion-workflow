from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.launcher import WorkerLauncher, clean_environment_for_test
from anima_core.profiles import ProfileUnavailableError, module_availability, require_available
from anima_core import runtime_manifest
from anima_core.runtime_manifest import (
    RuntimeBundleManifestV1,
    RuntimeManifestError,
    validate_runtime_isolation,
)
from anima_core.worker_protocol import MAX_FRAME_BYTES, ProtocolEnvelopeV1, ProtocolError, decode_frame, encode_frame, read_frames, validate_hello


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bundle(runtime_id: str, owner: str, interpreter: str, critical: dict[str, str], dll: list[str] | None = None) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "runtime": {
            "runtimeId": runtime_id,
            "owner": owner,
            "pythonVersion": "3.11.15",
            "interpreterRelativePath": interpreter,
            "dependencyLockSha256": "a" * 64,
            "protocolVersion": "1.0",
            "criticalFilesSha256": critical,
        },
        "launch": {
            "entryModule": "anima_caption_worker.entry",
            "arguments": ["-B", "-I", "-u", "-m"],
            "protocolTransport": "stdio-jsonl",
            "maxFrameBytes": 1048576,
            "dllDirectoriesRelative": dll or [],
        },
    }


class ProtocolAndRuntimeTests(unittest.TestCase):
    def test_optional_ocr_runtime_state_rejects_partial_and_gpu_without_cpu(self) -> None:
        try:
            inspect = runtime_manifest.inspect_optional_ocr_runtime_state
        except AttributeError:
            self.fail("optional OCR runtime state inspection is unavailable")

        def add_runtime(install: Path, runtime_id: str) -> None:
            runtime = install / "runtimes" / runtime_id
            runtime.mkdir(parents=True)
            interpreter = runtime / "python.exe"
            interpreter.write_bytes(runtime_id.encode("ascii"))
            (runtime / "python311._pth").write_text("Lib\nLib\\site-packages\n", encoding="ascii")
            lock = install / "manifests" / "requirements" / f"{runtime_id}.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_bytes(b"runtime-lock")
            relative = f"runtimes\\{runtime_id}\\python.exe"
            value = _bundle(runtime_id, "ocr", relative, {relative: _sha(interpreter.read_bytes())})
            value["runtime"]["dependencyLockSha256"] = _sha(lock.read_bytes())
            manifests = install / "manifests" / "runtimes"
            manifests.mkdir(parents=True, exist_ok=True)
            (manifests / f"{runtime_id}.json").write_text(json.dumps(value), encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            self.assertEqual("none", inspect(install))
            add_runtime(install, "ocr-paddle-gpu")
            with self.assertRaisesRegex(RuntimeManifestError, "CPU fallback"):
                inspect(install)

        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            add_runtime(install, "ocr-paddle")
            self.assertEqual("cpu", inspect(install))
            add_runtime(install, "ocr-paddle-gpu")
            self.assertEqual("gpu", inspect(install))
            (install / "manifests" / "requirements" / "ocr-paddle.lock").unlink()
            with self.assertRaisesRegex(RuntimeManifestError, "partial"):
                inspect(install)

    def test_ocr_and_token_budget_runtimes_are_assembled_while_gpu_is_declared_only(self) -> None:
        try:
            from anima_core.runtime_manifest import (
                ASSEMBLED_RUNTIME_IDS,
                DECLARED_UNASSEMBLED_RUNTIMES,
                runtime_lifecycle,
            )
        except ImportError:
            self.fail("assembled runtime lifecycle declaration is unavailable")
        self.assertEqual("assembled", runtime_lifecycle("ocr-paddle"))
        self.assertEqual("assembled", runtime_lifecycle("token-budget"))
        try:
            gpu_lifecycle = runtime_lifecycle("ocr-paddle-gpu")
        except RuntimeManifestError:
            gpu_lifecycle = "unknown"
        self.assertEqual("declared-unassembled", gpu_lifecycle)
        self.assertEqual(9, len(ASSEMBLED_RUNTIME_IDS))
        self.assertIn("ocr-paddle", ASSEMBLED_RUNTIME_IDS)
        self.assertIn("token-budget", ASSEMBLED_RUNTIME_IDS)
        self.assertNotIn("ocr-paddle", DECLARED_UNASSEMBLED_RUNTIMES)
        self.assertNotIn("token-budget", DECLARED_UNASSEMBLED_RUNTIMES)
        self.assertNotIn("ocr-paddle-gpu", ASSEMBLED_RUNTIME_IDS)
        self.assertEqual(
            ("ocr", "anima_ocr_worker.entry", "ocr-paddle-gpu"),
            DECLARED_UNASSEMBLED_RUNTIMES["ocr-paddle-gpu"],
        )

    def test_core_entrypoint_verifies_the_running_embedded_runtime(self) -> None:
        completed = subprocess.run(
            [str(ROOT / ".runtime-build" / "runtimes" / "core" / "python.exe"), "-B", "-I", "-m", "anima_core", "--check-runtime"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("anima-core-runtime-ok", completed.stdout.strip())

    def test_protocol_roundtrip_and_rejections(self) -> None:
        request = ProtocolEnvelopeV1("1.0", "request", "request-1", "caption-e621", "caption", "hello", {"x": 1})
        self.assertEqual(request, decode_frame(encode_frame(request), runtime_id="caption-e621", owner="caption"))
        with self.assertRaises(ProtocolError):
            decode_frame(b"not-json\n")
        with self.assertRaises(ProtocolError):
            decode_frame(b'{"protocolVersion":"9.0"}\n')
        with self.assertRaises(ProtocolError):
            decode_frame(b"x" * (MAX_FRAME_BYTES + 1))
        with self.assertRaises(ProtocolError):
            list(read_frames(io.BytesIO(b"{}")))
        with self.assertRaises(ProtocolError):
            list(read_frames(io.BytesIO(b"x" * (MAX_FRAME_BYTES + 2))))
        frames = list(read_frames(io.BytesIO(encode_frame(request) + encode_frame(request)), runtime_id="caption-e621", owner="caption"))
        self.assertEqual(2, len(frames))
        hello = ProtocolEnvelopeV1(
            "1.0", "response", "hello-1", "caption-e621", "caption", "hello",
            {"executable": "C:\\install\\runtimes\\caption-e621\\python.exe", "pythonVersion": "3.11.15"}, replyTo="request-1",
        )
        validate_hello(hello, expected_runtime_id="caption-e621", expected_owner="caption", expected_python_version="3.11.15")
        with self.assertRaises(ProtocolError):
            validate_hello(hello, expected_runtime_id="caption-e621", expected_owner="nl", expected_python_version="3.11.15")

    def test_protocol_accepts_the_assembled_ocr_worker_owner(self) -> None:
        response = ProtocolEnvelopeV1(
            "1.0", "response", "hello-ocr", "ocr-paddle", "ocr", "hello",
            {"executable": "C:\\install\\runtimes\\ocr-paddle\\python.exe", "pythonVersion": "3.11.15"},
            replyTo="request-ocr",
        )

        try:
            decoded = decode_frame(encode_frame(response), runtime_id="ocr-paddle", owner="ocr")
        except ProtocolError:
            decoded = None
        self.assertEqual(response, decoded)

    def test_profile_boundary(self) -> None:
        self.assertEqual("e621", require_available("e621").profile)
        self.assertEqual("danbooru", require_available("danbooru").profile)
        with self.assertRaises(ProfileUnavailableError):
            require_available("unknown")
        self.assertEqual("skipped", module_availability("e621", "dropout", enabled=False))
        self.assertEqual("pending", module_availability("e621", "dropout", enabled=True))
        self.assertEqual("skipped", module_availability("danbooru", "replace", enabled=True))
        self.assertEqual("pending", module_availability("danbooru", "dropout", enabled=True))

    def test_manifest_and_launcher_only_resolve_embedded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            runtime = install / "runtimes" / "caption-e621"
            runtime.mkdir(parents=True)
            interpreter = runtime / "python.exe"
            interpreter.write_bytes(b"embedded-only")
            (runtime / "python311._pth").write_text("Lib\nLib\\site-packages\n", encoding="utf-8")
            dll = runtime / "dll"
            dll.mkdir()
            relative = "runtimes\\caption-e621\\python.exe"
            manifest_value = _bundle("caption-e621", "caption", relative, {relative: _sha(b"embedded-only")}, ["runtimes\\caption-e621\\dll"])
            manifests = install / "manifests" / "runtimes"
            manifests.mkdir(parents=True)
            lock = install / "manifests" / "requirements" / "caption-e621.lock"
            lock.parent.mkdir(parents=True)
            lock.write_bytes(b"caption-lock")
            manifest_value["runtime"]["dependencyLockSha256"] = _sha(b"caption-lock")
            (manifests / "caption-e621.json").write_text(json.dumps(manifest_value), encoding="utf-8")
            manifest = RuntimeBundleManifestV1.load(manifests / "caption-e621.json")
            self.assertEqual(interpreter, manifest.verify_files(install))
            launch = WorkerLauncher.from_install_root(install).resolve("caption-e621", expected_owner="caption", verify_interpreter=False)
            self.assertEqual(str(interpreter), launch.command[0])
            self.assertEqual(("-B", "-I", "-u", "-m"), launch.command[1:5])
            fake_path = install / "fake-system-python"
            fake_path.mkdir()
            (fake_path / "python.exe").write_bytes(b"not-the-runtime")
            clean = clean_environment_for_test((dll,), {
                "PATH": str(fake_path), "PYTHONPATH": str(fake_path), "PYTHONHOME": str(fake_path),
                "PIP_INDEX_URL": "bad", "SYSTEMROOT": "C:\\Windows",
            })
            self.assertNotIn("PYTHONPATH", clean)
            self.assertNotIn("PYTHONHOME", clean)
            self.assertNotIn(str(fake_path), clean["PATH"])
            self.assertNotEqual(str(fake_path / "python.exe"), launch.command[0])
            self.assertTrue(launch.environment["PATH"].split(";")[0].endswith("dll"))
            tampered = dict(manifest_value)
            tampered["runtime"] = dict(manifest_value["runtime"])
            tampered["runtime"]["interpreterRelativePath"] = "..\\python.exe"
            with self.assertRaises(RuntimeManifestError):
                RuntimeBundleManifestV1.from_dict(tampered)
            duplicate = RuntimeBundleManifestV1.from_dict(manifest_value)
            with self.assertRaises(RuntimeManifestError):
                validate_runtime_isolation([manifest, duplicate], install)

    def test_runtime_rejects_production_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            runtime = install / "runtimes" / "caption-e621"
            (runtime / "Lib" / "site-packages" / "pip").mkdir(parents=True)
            interpreter = runtime / "python.exe"
            interpreter.write_bytes(b"embedded-only")
            (runtime / "python311._pth").write_text("Lib\n", encoding="utf-8")
            relative = "runtimes\\caption-e621\\python.exe"
            lock = install / "manifests" / "requirements" / "caption-e621.lock"
            lock.parent.mkdir(parents=True)
            lock.write_bytes(b"caption-lock")
            value = _bundle("caption-e621", "caption", relative, {relative: _sha(b"embedded-only")})
            value["runtime"]["dependencyLockSha256"] = _sha(b"caption-lock")
            manifest = RuntimeBundleManifestV1.from_dict(value)
            with self.assertRaises(RuntimeManifestError):
                manifest.verify_files(install)

    def test_runtime_manifest_allows_setuptools_only_for_exact_ocr_pairs(self) -> None:
        def manifest_for(install: Path, runtime_id: str, owner: str, packages: tuple[str, ...]) -> RuntimeBundleManifestV1:
            runtime = install / "runtimes" / runtime_id
            packages_root = runtime / "Lib" / "site-packages"
            for package in packages:
                (packages_root / package).mkdir(parents=True, exist_ok=True)
            interpreter = runtime / "python.exe"
            interpreter.write_bytes(b"embedded-only")
            (runtime / "python311._pth").write_text("Lib\nLib\\site-packages\n", encoding="utf-8")
            lock = install / "manifests" / "requirements" / f"{runtime_id}.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_bytes(b"runtime-lock")
            relative = f"runtimes\\{runtime_id}\\python.exe"
            value = _bundle(runtime_id, owner, relative, {relative: _sha(b"embedded-only")})
            value["runtime"]["dependencyLockSha256"] = _sha(b"runtime-lock")
            return RuntimeBundleManifestV1.from_dict(value)

        for runtime_id, owner in (("ocr-paddle", "ocr"), ("ocr-paddle-gpu", "ocr")):
            with self.subTest(runtime_id=runtime_id, owner=owner):
                with tempfile.TemporaryDirectory() as temporary:
                    install = Path(temporary)
                    manifest = manifest_for(install, runtime_id, owner, ("setuptools",))
                    try:
                        interpreter = manifest.verify_files(install)
                    except RuntimeManifestError as exc:
                        self.fail(f"exact OCR runtime pair must retain setuptools: {exc}")
                    self.assertTrue(interpreter.is_file())
                for prohibited in ("pip", "wheel", "pytest"):
                    with tempfile.TemporaryDirectory() as temporary:
                        install = Path(temporary)
                        manifest = manifest_for(install, runtime_id, owner, ("setuptools", prohibited))
                        with self.assertRaisesRegex(RuntimeManifestError, prohibited):
                            manifest.verify_files(install)

        for runtime_id, owner in (("ocr-paddle-test", "ocr"), ("ocr-paddle-gpu", "core"), ("caption-e621", "caption")):
            with self.subTest(runtime_id=runtime_id, owner=owner):
                with tempfile.TemporaryDirectory() as temporary:
                    install = Path(temporary)
                    manifest = manifest_for(install, runtime_id, owner, ("setuptools",))
                    with self.assertRaisesRegex(RuntimeManifestError, "setuptools"):
                        manifest.verify_files(install)


if __name__ == "__main__":
    unittest.main()
