from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
import zipfile
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER_ROOT = ROOT / "packaging" / "installer"
SOURCE_COMMIT = "2e85063591c266a14e2111da8ec6a3602139c61e"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _artifact(artifact_id: str, relative_path: str) -> dict[str, object]:
    payload = artifact_id.encode("ascii")
    return {
        "id": artifact_id,
        "url": f"https://downloads.example.test/anima/{artifact_id}",
        "allowedHosts": ["downloads.example.test"],
        "sizeBytes": len(payload),
        "sha256": _sha256(payload),
        "relativePath": relative_path,
    }


def _variant(artifact_id: str, relative_path: str, *, peak_bytes: int = 4096) -> dict[str, object]:
    return {
        "artifacts": [_artifact(artifact_id, relative_path)],
        "peakBytes": peak_bytes,
        "probe": "fixture",
    }


def fixture_manifest() -> dict[str, object]:
    runtime_components = [
        ("core", "core"),
        ("caption-e621", "caption-e621"),
        ("classify-e621", "classify-e621"),
        ("replace-e621", "replace-e621"),
        ("nl", "nl"),
        ("policy", "policy"),
        ("export", "export"),
        ("token-budget", "token-budget"),
        ("ocr-cpu", "ocr-paddle"),
    ]
    components: list[dict[str, object]] = []
    for component_id, runtime_id in runtime_components:
        variants: dict[str, object] = {
            "cpu": _variant(f"{component_id}-cpu-wheel", f"wheels/{component_id}-cpu.whl")
        }
        if component_id in {"caption-e621", "policy"}:
            variants["cuda"] = _variant(f"{component_id}-cuda-wheel", f"wheels/{component_id}-cuda.whl")
        components.append(
            {
                "componentId": component_id,
                "kind": "runtime",
                "required": True,
                "targetRelativePath": f".runtime-build/runtimes/{runtime_id}",
                "variants": variants,
            }
        )
    components.append(
        {
            "componentId": "ocr-gpu",
            "kind": "runtime",
            "required": False,
            "targetRelativePath": ".runtime-build/runtimes/ocr-paddle-gpu",
            "variants": {"cuda": _variant("ocr-gpu-cuda-wheel", "wheels/ocr-gpu-cuda.whl")},
        }
    )
    return {
        "schemaVersion": 1,
        "releaseVersion": "fixture-v1",
        "sourceCommit": SOURCE_COMMIT,
        "allowedHosts": ["downloads.example.test"],
        "bootstrap": {
            "artifact": _artifact("cpython311-base", "bootstrap/cpython311-base.zip"),
            "entryRelativePath": "python.exe",
            "peakBytes": 4096,
        },
        "components": components,
        "cleanup": {
            "successRelativePaths": [
                ".runtime-build/bootstrap",
                ".runtime-build/cache",
                ".runtime-build/staging",
            ]
        },
    }


def _modules():
    sys.path.insert(0, str(INSTALLER_ROOT))
    try:
        sys.modules.pop("assemble", None)
        sys.modules.pop("manifest", None)
        return importlib.import_module("assemble"), importlib.import_module("manifest")
    finally:
        sys.path.pop(0)


def _install_module():
    sys.path.insert(0, str(INSTALLER_ROOT))
    try:
        sys.modules.pop("install", None)
        return importlib.import_module("install")
    finally:
        sys.path.pop(0)


def _runtime_manifest_generator():
    path = ROOT / "packaging" / "scripts" / "generate_runtime_manifests.py"
    spec = importlib.util.spec_from_file_location("runtime_manifest_generator_for_source_bootstrap_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load runtime manifest generator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_install_manifest() -> dict[str, object]:
    value = fixture_manifest()
    core = next(component for component in value["components"] if component["componentId"] == "core")
    resource = {
        "componentId": "fixture-resource",
        "kind": "resource",
        "required": True,
        "targetRelativePath": "resource-library/fixture-resource",
        "variants": {"shared": _variant("fixture-resource-json", "resource.json")},
    }
    value["components"] = [core, resource]
    return value


class SourceBootstrapInstallTests(unittest.TestCase):
    def test_nvidia_plan_has_cpu_and_gpu_ocr_but_cpu_plan_never_selects_cuda(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_manifest())

        cpu = assemble.installation_plan(manifest, accelerator="cpu")
        nvidia = assemble.installation_plan(manifest, accelerator="nvidia")

        self.assertEqual({"ocr-paddle"}, cpu.runtime_ids & {"ocr-paddle", "ocr-paddle-gpu"})
        self.assertEqual({"ocr-paddle", "ocr-paddle-gpu"}, nvidia.runtime_ids & {"ocr-paddle", "ocr-paddle-gpu"})
        self.assertNotIn("caption-e621-cuda", cpu.lock_names)
        self.assertNotIn("policy-cuda", cpu.lock_names)
        self.assertIn("caption-e621-cuda", nvidia.lock_names)
        self.assertIn("policy-cuda", nvidia.lock_names)

    def test_production_plan_requires_all_mandatory_e621_components(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_install_manifest())

        with self.assertRaisesRegex(assemble.ManifestError, "mandatory E621 components"):
            assemble.validate_mandatory_e621_components(manifest)

    def test_runtime_manifest_uses_selected_caption_cpu_lock(self) -> None:
        generator = _runtime_manifest_generator()
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            runtime = root / "runtimes" / "caption-e621"
            worker = runtime / "Lib" / "site-packages" / "anima_caption_worker"
            worker.mkdir(parents=True)
            for filename in ("python.exe", "python311.dll", "python311._pth"):
                (runtime / filename).write_bytes(filename.encode("ascii"))
            (worker / "entry.py").write_text("VALUE = 1\n", encoding="ascii")
            requirements = root / "requirements"
            requirements.mkdir()
            lock = requirements / "caption-e621-cpu.lock"
            lock.write_text("caption fixture\n", encoding="ascii")

            specs = generator.runtime_specs(lock_names={"caption-e621": "caption-e621-cpu"})
            value = generator.manifest(root, requirements, "caption-e621", specs)

            self.assertEqual(_sha256(lock.read_bytes()), value["runtime"]["dependencyLockSha256"])

    def test_skip_requires_fingerprint_all_files_and_runtime_manifest(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_manifest())
        plan = assemble.installation_plan(manifest, accelerator="cpu")
        core = next(item for item in plan.components if item.component.component_id == "core")

        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = assemble.ProjectLayout.create(root)
            layout.ensure_directories()
            target = root / ".runtime-build" / "runtimes" / "core"
            target.mkdir(parents=True)
            payload = b"core fixture"
            (target / "marker.txt").write_bytes(payload)
            runtime_manifest = root / ".runtime-build" / "manifests" / "runtimes" / "core.json"
            runtime_manifest.parent.mkdir(parents=True)
            runtime_manifest.write_text(
                json.dumps({"runtime": {"runtimeId": "core"}}, sort_keys=True), encoding="utf-8"
            )
            record = {
                "componentId": "core",
                "variant": "cpu",
                "fingerprint": assemble.component_fingerprint(core),
                "targetRelativePath": ".runtime-build\\runtimes\\core",
                "files": [
                    {
                        "relativePath": "marker.txt",
                        "sizeBytes": len(payload),
                        "sha256": _sha256(payload),
                    }
                ],
                "runtimeManifestRelativePath": ".runtime-build\\manifests\\runtimes\\core.json",
            }

            self.assertTrue(assemble.component_is_current(layout, core, record))
            (target / "marker.txt").write_bytes(b"drift")
            self.assertFalse(assemble.component_is_current(layout, core, record))
            (target / "marker.txt").write_bytes(payload)
            runtime_manifest.unlink()
            self.assertFalse(assemble.component_is_current(layout, core, record))

    def test_runtime_assembly_rejects_duplicate_wheel_paths_before_publish(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_manifest())
        item = next(
            planned
            for planned in assemble.installation_plan(manifest, accelerator="cpu").components
            if planned.component.component_id == "core"
        )
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = assemble.ProjectLayout.create(root)
            layout.ensure_directories()
            base = root / "base"
            (base / "Lib").mkdir(parents=True)
            for filename in ("python.exe", "python311.dll", "python311._pth"):
                (base / filename).write_bytes(filename.encode("ascii"))
            wheels = []
            for index in range(2):
                wheel = root / f"fixture-{index}.whl"
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("fixture.dist-info/METADATA", "Name: fixture\nVersion: 1\n")
                    archive.writestr("Lib/site-packages/shared.py", str(index))
                wheels.append(wheel)

            with self.assertRaisesRegex(assemble.AssemblyError, "duplicate wheel path"):
                assemble.assemble_runtime(
                    layout,
                    item,
                    base_runtime=base,
                    wheel_paths=wheels,
                    destination=layout.staging / "core",
                )
            self.assertFalse((layout.staging / "core").exists())

    def test_runtime_assembly_copies_owner_source_and_strips_build_helpers(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_manifest())
        item = next(
            planned
            for planned in assemble.installation_plan(manifest, accelerator="cpu").components
            if planned.component.component_id == "core"
        )
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            layout = assemble.ProjectLayout.create(root)
            layout.ensure_directories()
            base = root / "base"
            packages = base / "Lib" / "site-packages"
            for package in ("pip", "wheel", "pytest"):
                (packages / package).mkdir(parents=True, exist_ok=True)
                (packages / package / "__init__.py").write_text("VALUE = 1\n", encoding="ascii")
            for filename in ("python.exe", "python311.dll", "python311._pth"):
                (base / filename).write_bytes(filename.encode("ascii"))
            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("Lib/site-packages/fixture_pkg/__init__.py", "VALUE = 2\n")
            owner = root / "owner" / "anima_core"
            owner.mkdir(parents=True)
            (owner / "__init__.py").write_text("VALUE = 'owner'\n", encoding="ascii")

            destination = layout.staging / "core"
            files = assemble.assemble_runtime(
                layout,
                item,
                base_runtime=base,
                wheel_paths=[wheel],
                destination=destination,
                owner_sources={"anima_core": owner},
            )

            package_root = destination / "Lib" / "site-packages"
            self.assertTrue((package_root / "fixture_pkg" / "__init__.py").is_file())
            self.assertEqual("VALUE = 'owner'\n", (package_root / "anima_core" / "__init__.py").read_text(encoding="ascii"))
            self.assertFalse((package_root / "pip").exists())
            self.assertFalse((package_root / "wheel").exists())
            self.assertFalse((package_root / "pytest").exists())
            self.assertIn("Lib\\site-packages\\anima_core\\__init__.py", files)

    def test_install_state_contains_only_complete_selected_components(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_manifest())
        plan = assemble.installation_plan(manifest, accelerator="cpu")
        records = {
            item.component.component_id: {
                "componentId": item.component.component_id,
                "variant": item.variant.name,
                "fingerprint": assemble.component_fingerprint(item),
                "targetRelativePath": item.component.target_relative_path,
                "files": [{"relativePath": "marker.txt", "sizeBytes": 1, "sha256": "a" * 64}],
            }
            for item in plan.components
        }

        state = assemble.build_install_state(manifest, plan, records, completed_at_utc="2026-08-11T00:00:00Z")

        self.assertEqual(1, state["schemaVersion"])
        self.assertEqual(manifest.source_commit, state["sourceCommit"])
        self.assertEqual(manifest.fingerprint, state["installManifestSha256"])
        self.assertEqual("cpu", state["accelerator"])
        self.assertNotIn("ocr-gpu", state["components"])
        self.assertEqual("cpu", state["components"]["policy"]["variant"])
        self.assertEqual("2026-08-11T00:00:00Z", state["completedAtUtc"])

        incomplete = dict(records)
        incomplete.pop("policy")
        with self.assertRaisesRegex(assemble.AssemblyError, "required component"):
            assemble.build_install_state(manifest, plan, incomplete, completed_at_utc="2026-08-11T00:00:00Z")

    def test_runtime_assembly_never_cleans_a_destination_outside_staging(self) -> None:
        assemble, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_manifest())
        item = next(
            planned
            for planned in assemble.installation_plan(manifest, accelerator="cpu").components
            if planned.component.component_id == "core"
        )
        with tempfile.TemporaryDirectory() as temporary_name:
            container = Path(temporary_name)
            root = container / "project"
            root.mkdir()
            layout = assemble.ProjectLayout.create(root)
            layout.ensure_directories()
            base = root / "base"
            base.mkdir()
            outside = container / "outside"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("keep", encoding="ascii")

            with self.assertRaises(assemble.PathSafetyError):
                assemble.assemble_runtime(
                    layout,
                    item,
                    base_runtime=base,
                    wheel_paths=[],
                    destination=outside,
                )

            self.assertEqual("keep", marker.read_text(encoding="ascii"))

    def test_fixture_install_publishes_state_then_second_run_skips_fetches(self) -> None:
        install_module = _install_module()
        _, manifest_module = _modules()
        manifest = manifest_module.load_manifest(fixture_install_manifest())
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            base = root / "bootstrap-base"
            (base / "Lib").mkdir(parents=True)
            for filename in ("python.exe", "python311.dll", "python311._pth"):
                (base / filename).write_bytes(filename.encode("ascii"))
            source = root / "core" / "src" / "anima_core"
            source.mkdir(parents=True)
            (source / "__init__.py").write_text("VALUE = 'core'\n", encoding="ascii")
            (source / "__main__.py").write_text("VALUE = 'main'\n", encoding="ascii")
            shared_source = root / "shared" / "anima_caption_format" / "anima_caption_format"
            shared_source.mkdir(parents=True)
            (shared_source / "__init__.py").write_text("VALUE = 'format'\n", encoding="ascii")
            wheel = root / "core.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("Lib/site-packages/fixture_pkg/__init__.py", "VALUE = 1\n")
            resource = root / "resource.json"
            resource.write_text('{"fixture":true}\n', encoding="utf-8")
            paths = {"core-cpu-wheel": wheel, "fixture-resource-json": resource}
            calls: list[str] = []

            def fetch(artifact):
                calls.append(artifact.artifact_id)
                return paths[artifact.artifact_id]

            def probe(item, target):
                return target.is_dir() and item.component.component_id in {"core", "fixture-resource"}

            def write_runtime_manifest(item, layout):
                target = layout.runtime_root / "manifests" / "runtimes" / f"{item.runtime_id}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"runtime": {"runtimeId": item.runtime_id}}), encoding="utf-8")
                return str(target.relative_to(layout.project_root)).replace("/", "\\")

            first = install_module.install_project(
                project_root=root,
                source_root=root,
                manifest=manifest,
                accelerator="cpu",
                base_runtime=base,
                fetch_artifact=fetch,
                probe_component=probe,
                write_runtime_manifest=write_runtime_manifest,
                require_mandatory_e621=False,
            )

            self.assertEqual(("core", "fixture-resource"), first.installed_component_ids)
            self.assertEqual(["core-cpu-wheel", "fixture-resource-json"], calls)
            state_path = root / ".runtime-build" / "manifests" / "install-state.json"
            self.assertTrue(state_path.is_file())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual({"core", "fixture-resource"}, set(state["components"]))
            self.assertTrue((root / ".runtime-build" / "runtimes" / "core" / "python.exe").is_file())
            self.assertTrue((root / "resource-library" / "fixture-resource" / "resource.json").is_file())

            calls.clear()
            second = install_module.install_project(
                project_root=root,
                source_root=root,
                manifest=manifest,
                accelerator="cpu",
                base_runtime=base,
                fetch_artifact=fetch,
                probe_component=probe,
                write_runtime_manifest=write_runtime_manifest,
                require_mandatory_e621=False,
            )

            self.assertEqual((), second.installed_component_ids)
            self.assertEqual(("core", "fixture-resource"), second.skipped_component_ids)
            self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
