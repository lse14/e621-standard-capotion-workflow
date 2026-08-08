from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "packaging" / "scripts"
CORE_SOURCE = ROOT / "core" / "src"
CORE_PYTHON = ROOT / ".runtime-build" / "runtimes" / "core" / "python.exe"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(CORE_SOURCE))

from anima_core.resource_catalog_validation import OCR_MODEL_IDENTITIES


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class OcrComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _module():
        try:
            return importlib.import_module("ocr_component")
        except ModuleNotFoundError:
            raise AssertionError("missing OCR component validator")

    def test_isolated_cli_can_import_the_local_model_stager(self) -> None:
        script = (
            "import importlib,runpy,sys;"
            "runpy.run_path(sys.argv[1],run_name='ocr_component_release');"
            "importlib.import_module('ocr_resource');"
            "print('ok')"
        )
        completed = subprocess.run(
            [str(CORE_PYTHON), "-B", "-I", "-c", script, str(SCRIPTS / "ocr_component.py")],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("ok", completed.stdout.strip())

    def test_isolated_component_builder_cli_loads_its_validator(self) -> None:
        completed = subprocess.run(
            [
                str(CORE_PYTHON),
                "-B",
                "-I",
                str(SCRIPTS / "build_optional_ocr_components.py"),
                "--help",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--destination-root", completed.stdout)

    def test_isolated_component_installer_cli_parses_arguments(self) -> None:
        completed = subprocess.run(
            [
                str(CORE_PYTHON),
                "-B",
                "-I",
                str(SCRIPTS / "ocr_component.py"),
                "--help",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("{install}", completed.stdout)

    def test_isolated_component_installer_reports_structured_runtime_error(self) -> None:
        completed = subprocess.run(
            [
                str(CORE_PYTHON),
                "-B",
                "-I",
                str(SCRIPTS / "ocr_component.py"),
                "install",
                "--app-root",
                str(ROOT),
                "--mode",
                "cpu",
                "--model-root",
                str(ROOT / "missing-model-archives"),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertEqual("ocr_models_required", json.loads(completed.stderr)["error"])

    def test_default_cpu_probe_passes_cpu_device_to_worker_engine(self) -> None:
        module = self._module()

        class FixturePackage:
            fingerprint = "fixture-fingerprint"

            def verify_files(self, *, verify_hashes: bool) -> None:
                if verify_hashes is not True:
                    raise AssertionError("probe must verify model hashes")

        captured: dict[str, object] = {}
        original_load = module.ResourcePackage.load
        original_run = module.subprocess.run
        try:
            module.ResourcePackage.load = lambda *_args: FixturePackage()

            def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["arguments"] = arguments
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout=json.dumps({
                        "paddle": "3.2.2",
                        "paddleocr": "3.7.0",
                        "paddlex": "3.7.2",
                        "cuda": False,
                        "device": "cpu",
                    }),
                    stderr="",
                )

            module.subprocess.run = run
            value = module._default_probe("ocr-paddle", self.root / "runtime", self.root / "library")
        finally:
            module.ResourcePackage.load = original_load
            module.subprocess.run = original_run

        self.assertEqual("cpu", value["device"])
        self.assertIn("device='cpu'", str(captured["arguments"][4]))

    def _runtime(self, app_root: Path, runtime_id: str) -> None:
        runtime = app_root / "runtimes" / runtime_id
        runtime.mkdir(parents=True)
        interpreter = runtime / "python.exe"
        interpreter.write_bytes(b"fixture-interpreter")
        (runtime / "python311._pth").write_text("Lib\nLib\\site-packages\n", encoding="ascii")
        critical = f"runtimes\\{runtime_id}\\python.exe"
        lock = app_root / "manifests" / "requirements" / f"{runtime_id}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(f"{runtime_id}==1\n".encode("ascii"))
        manifest = {
            "schemaVersion": 1,
            "runtime": {
                "runtimeId": runtime_id,
                "owner": "ocr",
                "pythonVersion": "3.11.15",
                "interpreterRelativePath": critical,
                "dependencyLockSha256": _sha256(lock.read_bytes()),
                "protocolVersion": "1.0",
                "criticalFilesSha256": {critical: _sha256(interpreter.read_bytes())},
            },
            "launch": {
                "entryModule": "anima_ocr_worker.entry",
                "arguments": ["-B", "-I", "-u", "-m"],
                "protocolTransport": "stdio-jsonl",
                "maxFrameBytes": 1048576,
                "dllDirectoriesRelative": [],
            },
        }
        target = app_root / "manifests" / "runtimes"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{runtime_id}.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )

    def _resource(self, app_root: Path) -> None:
        package = app_root / "resource-library" / "ocr-models" / "ocr-ppocrv5-server-paddle-v1"
        package.mkdir(parents=True)
        files = {}
        entrypoints = {}
        for role, directory in {
            "detection": "detection",
            "recognition": "recognition",
            "textlineOrientation": "textline-orientation",
        }.items():
            target = package / directory
            target.mkdir()
            inference = target / "inference.json"
            inference.write_text("{}\n", encoding="ascii")
            relative = f"{directory}\\inference.json"
            files[relative] = {
                "sizeBytes": inference.stat().st_size,
                "sha256": _sha256(inference.read_bytes()),
            }
            entrypoints[role] = relative
        manifest = {
            "schemaVersion": 2,
            "kind": "ocr-model",
            "resourceId": "ocr-ppocrv5-server-paddle-v1",
            "resourceVersion": "ppocrv5-server-paddle-v1",
            "profile": "shared",
            "displayName": {"en": "OCR", "zh-CN": "OCR"},
            "description": {"en": "fixture", "zh-CN": "fixture"},
            "runtimeFormat": "ppocrv5-server-paddle-v1",
            "entrypoints": entrypoints,
            "files": files,
            "metadata": {
                "models": dict(OCR_MODEL_IDENTITIES),
                "inference": {
                    "useDocOrientationClassify": False,
                    "useDocUnwarping": False,
                    "useTextlineOrientation": True,
                    "textRecScoreThresh": 0,
                    "textDetLimitSideLen": 1920,
                    "textDetLimitType": "max",
                },
            },
            "distribution": {
                "mode": "local-only",
                "sourceUrl": "https://example.invalid/models",
                "licenseStatus": "unverified",
            },
            "documentation": [],
        }
        (package / "resource.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _complete(self, *, gpu: bool = False) -> Path:
        app_root = self.root / ("gpu" if gpu else "cpu")
        (app_root / "runtimes" / "core").mkdir(parents=True)
        (app_root / "runtimes" / "core" / "python.exe").write_bytes(b"core")
        self._runtime(app_root, "ocr-paddle")
        self._resource(app_root)
        if gpu:
            self._runtime(app_root, "ocr-paddle-gpu")
        return app_root

    def _component(self, *, manifest_overrides: dict[str, object] | None = None) -> Path:
        root = self.root / "component"
        payload = root / "payload"
        files = {
            "runtimes\\ocr-paddle\\python.exe": b"runtime",
            "manifests\\runtimes\\ocr-paddle.json": b"manifest",
            "manifests\\requirements\\ocr-paddle.lock": b"lock",
        }
        records = []
        for relative, content in files.items():
            target = payload / Path(relative.replace("\\", "/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            records.append({"path": relative, "sizeBytes": len(content), "sha256": _sha256(content)})
        root.mkdir(exist_ok=True)
        manifest = {
            "schemaVersion": 1,
            "componentId": "ocr-cpu",
            "runtimeIds": ["ocr-paddle"],
            "requiresResourceId": "ocr-ppocrv5-server-paddle-v1",
            "files": sorted(records, key=lambda item: str(item["path"]).casefold()),
        }
        if manifest_overrides:
            manifest.update(manifest_overrides)
        (root / "component.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_inspect_none_has_no_formal_ocr_targets(self) -> None:
        module = self._module()
        (self.root / "runtimes" / "core").mkdir(parents=True)
        (self.root / "runtimes" / "core" / "python.exe").write_bytes(b"core")
        self.assertEqual("none", module.inspect_ocr_installation(self.root))

    def test_inspect_complete_cpu_and_gpu_states(self) -> None:
        module = self._module()
        self.assertEqual("cpu", module.inspect_ocr_installation(self._complete()))
        self.assertEqual("gpu", module.inspect_ocr_installation(self._complete(gpu=True)))

    def test_partial_state_fails_closed(self) -> None:
        module = self._module()
        app_root = self.root / "partial"
        (app_root / "runtimes" / "core").mkdir(parents=True)
        (app_root / "runtimes" / "core" / "python.exe").write_bytes(b"core")
        (app_root / "runtimes" / "ocr-paddle").mkdir(parents=True)
        with self.assertRaisesRegex(module.OcrComponentError, "partial"):
            module.inspect_ocr_installation(app_root)

    def test_gpu_without_cpu_requires_cpu_fallback(self) -> None:
        module = self._module()
        app_root = self.root / "gpu-only"
        (app_root / "runtimes" / "core").mkdir(parents=True)
        (app_root / "runtimes" / "core" / "python.exe").write_bytes(b"core")
        self._runtime(app_root, "ocr-paddle-gpu")
        with self.assertRaisesRegex(module.OcrComponentError, "CPU fallback"):
            module.inspect_ocr_installation(app_root)

    def test_component_manifest_accepts_complete_payload(self) -> None:
        module = self._module()
        manifest = module.load_component_manifest(self._component())
        self.assertEqual("ocr-cpu", manifest.component_id)
        self.assertEqual(("ocr-paddle",), manifest.runtime_ids)
        self.assertEqual(3, len(manifest.files))

    def test_component_manifest_accepts_bounded_real_runtime_scale(self) -> None:
        module = self._module()
        component = self._component()
        manifest_path = component / "component.json"
        manifest = manifest_path.read_bytes()
        target_size = 5 * 1024 * 1024
        self.assertLess(len(manifest), target_size)
        manifest_path.write_bytes(manifest + (b" " * (target_size - len(manifest))))

        loaded = module.load_component_manifest(component)

        self.assertEqual("ocr-cpu", loaded.component_id)

    def test_component_manifest_accepts_zero_byte_runtime_files(self) -> None:
        module = self._module()
        component = self._component()
        manifest_path = component / "component.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = component / "payload" / Path("runtimes/ocr-paddle/python.exe")
        target.write_bytes(b"")
        record = next(item for item in manifest["files"] if item["path"] == "runtimes\\ocr-paddle\\python.exe")
        record["sizeBytes"] = 0
        record["sha256"] = _sha256(b"")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        loaded = module.load_component_manifest(component)

        self.assertEqual(0, next(item for item in loaded.files if item.path == record["path"]).size_bytes)

    def test_component_manifest_rejects_unsafe_paths_and_records(self) -> None:
        module = self._module()
        cases = (
            {"path": "..\\escape.txt"},
            {"path": "C:\\escape.txt"},
            {"path": "\\\\server\\share\\escape.txt"},
            {"sizeBytes": True},
            {"sha256": "A" * 64},
        )
        for override in cases:
            with self.subTest(override=override):
                manifest_root = self._component()
                records = json.loads((manifest_root / "component.json").read_text(encoding="utf-8"))["files"]
                record = dict(records[0])
                record.update(override)
                records[0] = record
                value = json.loads((manifest_root / "component.json").read_text(encoding="utf-8"))
                value["files"] = records
                (manifest_root / "component.json").write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(module.OcrComponentError):
                    module.load_component_manifest(manifest_root)

    def test_component_manifest_rejects_duplicates_and_extra_payload_files(self) -> None:
        module = self._module()
        root = self._component()
        value = json.loads((root / "component.json").read_text(encoding="utf-8"))
        value["files"].append(dict(value["files"][0]))
        (root / "component.json").write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(module.OcrComponentError, "duplicate"):
            module.load_component_manifest(root)

        root = self._component()
        extra = root / "payload" / "extra.bin"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(module.OcrComponentError, "extra"):
            module.load_component_manifest(root)

    def test_builder_publishes_selected_components_only_after_validation(self) -> None:
        source = self._complete(gpu=True)
        requirements = source / "packaging" / "requirements"
        requirements.mkdir(parents=True)
        for runtime_id in ("ocr-paddle", "ocr-paddle-gpu"):
            (requirements / f"{runtime_id}.lock").write_bytes(
                (source / "manifests" / "requirements" / f"{runtime_id}.lock").read_bytes()
            )
        destination = self.root / "optional-components"
        try:
            builder = importlib.import_module("build_optional_ocr_components")
        except ModuleNotFoundError:
            raise AssertionError("missing optional OCR component builder")

        published = builder.build_components(source, destination, mode="gpu")

        self.assertEqual(("ocr-cpu", "ocr-gpu"), tuple(sorted(published)))
        self.assertTrue((destination / "ocr-cpu" / "component.json").is_file())
        self.assertTrue((destination / "ocr-gpu" / "component.json").is_file())
        self.assertFalse(any(destination.glob(".*.staging-*")))

    def test_builder_manifest_uses_windows_path_order(self) -> None:
        source = self._complete()
        requirements = source / "packaging" / "requirements"
        requirements.mkdir(parents=True)
        (requirements / "ocr-paddle.lock").write_bytes(
            (source / "manifests" / "requirements" / "ocr-paddle.lock").read_bytes()
        )
        runtime = source / "runtimes" / "ocr-paddle"
        for relative in (
            "Lib/site-packages/modelscope/models/nlp/chatglm/__init__.py",
            "Lib/site-packages/modelscope/models/nlp/chatglm2/__init__.py",
        ):
            target = runtime / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(relative.encode("ascii"))

        builder = importlib.import_module("build_optional_ocr_components")
        destination = self.root / "ordered-components"

        published = builder.build_components(source, destination, mode="cpu")

        self.assertEqual(("ocr-cpu",), tuple(sorted(published)))
        manifest = importlib.import_module("ocr_component").load_component_manifest(
            published["ocr-cpu"]
        )
        self.assertEqual(
            [
                "runtimes\\ocr-paddle\\Lib\\site-packages\\modelscope\\models\\nlp\\chatglm2\\__init__.py",
                "runtimes\\ocr-paddle\\Lib\\site-packages\\modelscope\\models\\nlp\\chatglm\\__init__.py",
            ],
            [record.path for record in manifest.files if "chatglm" in record.path],
        )

    def _optional_components(self, app_root: Path, *, mode: str) -> Path:
        source = self._complete(gpu=True)
        requirements = source / "packaging" / "requirements"
        requirements.mkdir(parents=True)
        for runtime_id in ("ocr-paddle", "ocr-paddle-gpu"):
            (requirements / f"{runtime_id}.lock").write_bytes(
                (source / "manifests" / "requirements" / f"{runtime_id}.lock").read_bytes()
            )
        builder = importlib.import_module("build_optional_ocr_components")
        destination = app_root / "optional-components"
        builder.build_components(source, destination, mode=mode)
        return source

    @staticmethod
    def _copy_resource_stager(source: Path):
        def stage(_model_root: Path, stage_library: Path) -> Path:
            target = stage_library / "ocr-models" / "ocr-ppocrv5-server-paddle-v1"
            shutil.copytree(
                source / "resource-library" / "ocr-models" / "ocr-ppocrv5-server-paddle-v1",
                target,
            )
            return target

        return stage

    def _empty_app(self, name: str, *, development: bool = False) -> Path:
        app_root = self.root / name
        runtime_root = app_root / ".runtime-build" if development else app_root
        (runtime_root / "runtimes" / "core").mkdir(parents=True)
        (runtime_root / "runtimes" / "core" / "python.exe").write_bytes(b"core")
        return app_root

    def test_install_none_is_zero_write_and_cpu_gpu_publish_complete_states(self) -> None:
        module = self._module()
        app_root = self._empty_app("app")
        source = self._optional_components(app_root, mode="gpu")
        model_root = app_root / "model-archives"
        model_root.mkdir()
        before = {
            str(path.relative_to(app_root)): path.read_bytes()
            for path in app_root.rglob("*") if path.is_file()
        }

        unchanged = module.install_optional_ocr(app_root, mode="none", model_root=model_root)

        self.assertEqual("unchanged", unchanged["status"])
        self.assertEqual(before, {
            str(path.relative_to(app_root)): path.read_bytes()
            for path in app_root.rglob("*") if path.is_file()
        })
        cpu = module.install_optional_ocr(
            app_root,
            mode="cpu",
            model_root=model_root,
            model_stager=self._copy_resource_stager(source),
            probe=lambda runtime_id, _runtime, _library: {"runtimeId": runtime_id},
        )
        self.assertEqual("ready", cpu["status"])
        self.assertEqual(["ocr-paddle"], cpu["runtimeIds"])
        self.assertEqual("cpu", module.inspect_ocr_installation(app_root))

        gpu = module.install_optional_ocr(
            app_root,
            mode="gpu",
            model_root=model_root,
            model_stager=self._copy_resource_stager(source),
            probe=lambda runtime_id, _runtime, _library: {"runtimeId": runtime_id},
        )
        self.assertEqual("ready", gpu["status"])
        self.assertEqual(["ocr-paddle", "ocr-paddle-gpu"], gpu["runtimeIds"])
        self.assertEqual("gpu", module.inspect_ocr_installation(app_root))

    def test_install_rejects_missing_models_and_rolls_back_failed_publication(self) -> None:
        module = self._module()
        app_root = self._empty_app("rollback")
        source = self._optional_components(app_root, mode="cpu")
        with self.assertRaisesRegex(module.OcrComponentError, "ocr_models_required"):
            module.install_optional_ocr(
                app_root,
                mode="cpu",
                model_root=app_root / "missing-model-archives",
                model_stager=self._copy_resource_stager(source),
                probe=lambda *_: {},
            )
        self.assertEqual("none", module.inspect_ocr_installation(app_root))

        model_root = app_root / "model-archives"
        model_root.mkdir()

        def fail_on_manifest(source_path: str | os.PathLike[str], target_path: str | os.PathLike[str]) -> None:
            if Path(target_path).name == "ocr-paddle.json":
                raise OSError("fixture publication failure")
            os.replace(source_path, target_path)

        with self.assertRaises(module.OcrComponentError):
            module.install_optional_ocr(
                app_root,
                mode="cpu",
                model_root=model_root,
                model_stager=self._copy_resource_stager(source),
                probe=lambda *_: {},
                replacer=fail_on_manifest,
            )
        self.assertEqual("none", module.inspect_ocr_installation(app_root))
        self.assertFalse((app_root / "resource-library" / "ocr-models" / "ocr-ppocrv5-server-paddle-v1").exists())

    def test_install_accepts_development_runtime_root_and_rejects_outside_model_root(self) -> None:
        module = self._module()
        app_root = self._empty_app("development", development=True)
        source = self._optional_components(app_root, mode="cpu")
        model_root = app_root / "model-archives"
        model_root.mkdir()
        module.install_optional_ocr(
            app_root,
            mode="cpu",
            model_root=model_root,
            model_stager=self._copy_resource_stager(source),
            probe=lambda *_: {},
        )
        self.assertTrue((app_root / ".runtime-build" / "runtimes" / "ocr-paddle").is_dir())
        self.assertEqual("cpu", module.inspect_ocr_installation(app_root))

        outside = self.root.parent / (self.root.name + "-outside-models")
        outside.mkdir(exist_ok=True)
        try:
            with self.assertRaises(module.OcrComponentError):
                module.install_optional_ocr(app_root, mode="cpu", model_root=outside)
        finally:
            outside.rmdir()


if __name__ == "__main__":
    unittest.main()
