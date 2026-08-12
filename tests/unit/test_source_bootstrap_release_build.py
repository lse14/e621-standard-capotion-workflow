from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "packaging" / "requirements"
BOOTSTRAP_BUILDER = ROOT / "packaging" / "scripts" / "build_bootstrap_runtime.ps1"
BOOTSTRAP_ASSET_VERIFIER = ROOT / "packaging" / "scripts" / "Test-BootstrapRuntimeAsset.ps1"
MANIFEST_BUILDER = ROOT / "packaging" / "scripts" / "build_install_manifest.py"


def _load_manifest_builder():
    if not MANIFEST_BUILDER.is_file():
        return None
    name = "source_bootstrap_manifest_builder_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MANIFEST_BUILDER)
    if spec is None or spec.loader is None:
        raise AssertionError("manifest builder module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _artifact(artifact_id: str, relative_path: str, sha256: str) -> dict[str, object]:
    return {
        "id": artifact_id,
        "url": f"https://downloads.example.test/{relative_path}",
        "allowedHosts": ["downloads.example.test"],
        "sizeBytes": 1,
        "sha256": sha256,
        "relativePath": relative_path,
    }


def _inventory() -> dict[str, object]:
    wheel_hash = "a" * 64
    bootstrap_hash = "b" * 64
    return {
        "manifest": {
            "schemaVersion": 1,
            "releaseVersion": "source-bootstrap-v1",
            "sourceCommit": "c" * 40,
            "allowedHosts": ["downloads.example.test"],
            "bootstrap": {
                "artifact": _artifact("cpython311-base", "bootstrap/cpython311-base.zip", bootstrap_hash),
                "entryRelativePath": "python.exe",
                "peakBytes": 1,
            },
            "components": [
                {
                    "componentId": "caption-e621",
                    "kind": "runtime",
                    "required": True,
                    "targetRelativePath": "runtimes/caption-e621",
                    "variants": {
                        "cpu": {
                            "artifacts": [_artifact("wheel-numpy", "wheels/numpy-1.0-py3-none-any.whl", wheel_hash)],
                            "peakBytes": 1,
                            "probe": "caption-offline",
                        }
                    },
                }
            ],
            "cleanup": {"successRelativePaths": [".runtime-build/source-bootstrap"]},
        },
        "releaseArtifacts": {
            "schemaVersion": 1,
            "releaseVersion": "source-bootstrap-v1",
            "artifacts": [
                {
                    "id": "cpython311-base",
                    "publishedUrl": "https://downloads.example.test/bootstrap/cpython311-base.zip",
                    "sizeBytes": 1,
                    "sha256": bootstrap_hash,
                }
            ],
        },
        "variantLocks": {"caption-e621:cpu": "caption-e621-cpu"},
    }


class SourceBootstrapReleaseBuildTests(unittest.TestCase):
    def test_cpu_variants_have_no_cuda_distribution(self) -> None:
        required = {
            "caption-e621-cpu.in", "caption-e621-cpu.lock", "caption-e621-cuda.in", "caption-e621-cuda.lock",
            "policy-cpu.in", "policy-cpu.lock", "policy-cuda.in", "policy-cuda.lock",
        }
        missing = sorted(name for name in required if not (REQUIREMENTS / name).is_file())
        self.assertEqual([], missing, f"missing explicit source-bootstrap variant inputs: {missing}")
        for name in ("caption-e621-cpu.in", "caption-e621-cpu.lock", "policy-cpu.in", "policy-cpu.lock"):
            with self.subTest(path=name):
                text = (REQUIREMENTS / name).read_text(encoding="utf-8").lower()
                self.assertNotIn("onnxruntime-gpu", text)
                self.assertNotIn("+cu", text)
                self.assertNotIn("cuda", text)
        self.assertIn("onnxruntime-gpu==1.26.0", (REQUIREMENTS / "caption-e621-cuda.in").read_text(encoding="utf-8"))
        self.assertIn("torch==2.9.1+cu128", (REQUIREMENTS / "policy-cuda.in").read_text(encoding="utf-8"))
        self.assertIn("torch==2.9.1+cpu", (REQUIREMENTS / "policy-cpu.in").read_text(encoding="utf-8"))

    def test_bootstrap_runtime_builder_has_an_offline_stdlib_contract_and_parses(self) -> None:
        self.assertTrue(BOOTSTRAP_BUILDER.is_file(), "bootstrap runtime builder must exist")
        source = BOOTSTRAP_BUILDER.read_text(encoding="utf-8")
        self.assertIn("python311.dll", source)
        self.assertIn("python311._pth", source)
        self.assertIn("-B -I -c", source)
        self.assertIn("Add-Type -AssemblyName System.IO.Compression\n", source)
        self.assertIn("System.IO.Compression", source)
        self.assertNotIn("pip install", source.lower())
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "(Resolve-Path 'packaging\\scripts\\build_bootstrap_runtime.ps1'),[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_bootstrap_asset_verifier_rejects_tampering(self) -> None:
        self.assertTrue(BOOTSTRAP_ASSET_VERIFIER.is_file(), "bootstrap asset verifier must exist")
        base_runtime = Path(sys.executable).resolve().parent
        self.assertTrue((base_runtime / "python311.dll").is_file(), "tests require the project-embedded CPython base")
        test_root = ROOT / ".test-tmp"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as temporary_name:
            temporary = Path(temporary_name)
            asset = temporary / "cpython-3.11.15-win-amd64.zip"
            provenance = temporary / "cpython-3.11.15-win-amd64.provenance.json"
            source_commit = "a" * 40
            built = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP_BUILDER),
                    "-BaseRuntime", str(base_runtime),
                    "-OutputZip", str(asset),
                    "-ProvenanceOutput", str(provenance),
                    "-SourceCommit", source_commit,
                    "-ReleaseVersion", "source-bootstrap-test",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(0, built.returncode, built.stdout + built.stderr)

            verified = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP_ASSET_VERIFIER),
                    "-ProjectRoot", str(ROOT),
                    "-AssetZip", str(asset),
                    "-Provenance", str(provenance),
                    "-ExpectedSourceCommit", source_commit,
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(0, verified.returncode, verified.stdout + verified.stderr)

            with asset.open("r+b") as target:
                target.seek(-1, 2)
                original = target.read(1)
                target.seek(-1, 2)
                target.write(bytes([original[0] ^ 0x01]))
            tampered = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP_ASSET_VERIFIER),
                    "-ProjectRoot", str(ROOT),
                    "-AssetZip", str(asset),
                    "-Provenance", str(provenance),
                    "-ExpectedSourceCommit", source_commit,
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, tampered.returncode)
            self.assertIn("SHA-256", tampered.stdout + tampered.stderr)

    def test_bootstrap_asset_verifier_rejects_backslash_parent_entry(self) -> None:
        self.assertTrue(BOOTSTRAP_ASSET_VERIFIER.is_file(), "bootstrap asset verifier must exist")
        test_root = ROOT / ".test-tmp"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as temporary_name:
            temporary = Path(temporary_name)
            asset = temporary / "cpython-3.11.15-win-amd64.zip"
            provenance = temporary / "cpython-3.11.15-win-amd64.provenance.json"
            with zipfile.ZipFile(asset, "w") as archive:
                archive.writestr("python.exe", b"not an executable")
                archive.writestr("python311.dll", b"not a dll")
                archive.writestr("python311._pth", b"Lib\nLib/site-packages\n")
                archive.writestr("Lib/os.py", b"pass\n")
                archive.writestr("Lib\\..\\outside.txt", b"unsafe")
            asset_bytes = asset.read_bytes()
            provenance.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "releaseVersion": "source-bootstrap-test",
                        "sourceCommit": "a" * 40,
                        "pythonVersion": "3.11.15",
                        "assetFileName": asset.name,
                        "assetSizeBytes": len(asset_bytes),
                        "assetSha256": hashlib.sha256(asset_bytes).hexdigest(),
                        "buildScriptSha256": hashlib.sha256(BOOTSTRAP_BUILDER.read_bytes()).hexdigest(),
                        "offlineProbe": "bootstrap-stdlib-ok",
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP_ASSET_VERIFIER),
                    "-ProjectRoot", str(ROOT),
                    "-AssetZip", str(asset),
                    "-Provenance", str(provenance),
                    "-ExpectedSourceCommit", "a" * 40,
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("unsafe entry", completed.stdout + completed.stderr)

    def test_manifest_builder_rejects_missing_artifact_identity_and_lock_mismatch(self) -> None:
        module = _load_manifest_builder()
        self.assertIsNotNone(module, "source bootstrap manifest builder must exist")
        with tempfile.TemporaryDirectory() as temporary_name:
            requirements_root = Path(temporary_name)
            (requirements_root / "caption-e621-cpu.lock").write_text(
                "numpy==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="ascii"
            )
            inventory = _inventory()
            manifest = module.build_manifest(inventory, requirements_root)
            self.assertEqual("source-bootstrap-v1", manifest["releaseVersion"])

            missing_identity = copy.deepcopy(inventory)
            del missing_identity["manifest"]["components"][0]["variants"]["cpu"]["artifacts"][0]["sizeBytes"]
            with self.assertRaisesRegex(module.ManifestBuildError, "artifact fields"):
                module.build_manifest(missing_identity, requirements_root)

            mismatched_lock = copy.deepcopy(inventory)
            mismatched_lock["manifest"]["components"][0]["variants"]["cpu"]["artifacts"][0]["sha256"] = "d" * 64
            with self.assertRaisesRegex(module.ManifestBuildError, "does not match wheel lock"):
                module.build_manifest(mismatched_lock, requirements_root)

            incomplete_release = copy.deepcopy(inventory)
            del incomplete_release["releaseArtifacts"]["artifacts"][0]["publishedUrl"]
            with self.assertRaisesRegex(module.ManifestBuildError, "release artifact record"):
                module.build_manifest(incomplete_release, requirements_root)


if __name__ == "__main__":
    unittest.main()
