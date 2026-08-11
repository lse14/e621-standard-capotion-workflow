from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "packaging" / "installer" / "manifest.py"
SOURCE_COMMIT = "2e85063591c266a14e2111da8ec6a3602139c61e"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _artifact(
    artifact_id: str,
    relative_path: str,
    *,
    url: str = "https://downloads.example.test/anima/fixture.bin",
    allowed_hosts: list[str] | None = None,
    payload: bytes = b"fixture-artifact",
) -> dict[str, object]:
    return {
        "id": artifact_id,
        "url": url,
        "allowedHosts": allowed_hosts or ["downloads.example.test"],
        "sizeBytes": len(payload),
        "sha256": _sha256(payload),
        "relativePath": relative_path,
    }


def minimal_manifest() -> dict[str, object]:
    caption_cpu = _artifact("caption-cpu-wheel", "wheels/caption-cpu.whl")
    caption_cuda = _artifact("caption-cuda-wheel", "wheels/caption-cuda.whl")
    return {
        "schemaVersion": 1,
        "releaseVersion": "source-bootstrap-v1",
        "sourceCommit": SOURCE_COMMIT,
        "allowedHosts": ["downloads.example.test", "huggingface.co"],
        "bootstrap": {
            "artifact": _artifact("cpython311-base", "bootstrap/cpython311-base.zip"),
            "entryRelativePath": "python.exe",
            "peakBytes": 4096,
        },
        "components": [
            {
                "componentId": "core",
                "kind": "runtime",
                "required": True,
                "targetRelativePath": "runtimes/core",
                "variants": {
                    "cpu": {
                        "artifacts": [_artifact("core-wheel", "wheels/core.whl")],
                        "peakBytes": 8192,
                        "probe": "core",
                    }
                },
            },
            {
                "componentId": "caption-e621",
                "kind": "runtime",
                "required": True,
                "targetRelativePath": "runtimes/caption-e621",
                "variants": {
                    "cpu": {"artifacts": [caption_cpu], "peakBytes": 8192, "probe": "caption-cpu"},
                    "cuda": {"artifacts": [caption_cuda], "peakBytes": 16384, "probe": "caption-cuda"},
                },
            },
        ],
        "cleanup": {
            "successRelativePaths": [
                ".runtime-build/bootstrap",
                ".runtime-build/cache",
                ".runtime-build/staging",
            ]
        },
    }


def _load_module():
    if not MODULE_PATH.is_file():
        return None
    name = "source_bootstrap_manifest_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("manifest module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SourceBootstrapManifestTests(unittest.TestCase):
    def _module(self):
        module = _load_module()
        self.assertIsNotNone(module, "source bootstrap manifest module must exist")
        return module

    def test_valid_manifest_has_stable_fingerprint_and_variant_selection(self) -> None:
        module = self._module()
        manifest = module.load_manifest(minimal_manifest())

        self.assertEqual(manifest.fingerprint, module.sha256_bytes(module.canonical_json(minimal_manifest())))
        self.assertEqual("cpu", manifest.select_components("cpu")["caption-e621"].variant)
        self.assertEqual("cuda", manifest.select_components("nvidia")["caption-e621"].variant)

    def test_manifest_rejects_floating_huggingface_revision(self) -> None:
        module = self._module()
        value = minimal_manifest()
        artifact = value["components"][0]["variants"]["cpu"]["artifacts"][0]
        artifact["url"] = "https://huggingface.co/Qwen/Qwen3-0.6B/resolve/main/tokenizer.json"
        artifact["allowedHosts"] = ["huggingface.co"]
        artifact["repository"] = "Qwen/Qwen3-0.6B"
        artifact["revision"] = "main"

        with self.assertRaisesRegex(module.ManifestError, "full commit SHA"):
            module.load_manifest(value)

    def test_manifest_rejects_unknown_redirect_host_and_non_https_url(self) -> None:
        module = self._module()
        host_value = minimal_manifest()
        artifact = host_value["components"][0]["variants"]["cpu"]["artifacts"][0]
        artifact["allowedHosts"] = ["downloads.example.test", "evil.example"]
        with self.assertRaisesRegex(module.ManifestError, "allowed host"):
            module.load_manifest(host_value)

        url_value = minimal_manifest()
        url_value["bootstrap"]["artifact"]["url"] = "http://downloads.example.test/anima/base.zip"
        with self.assertRaisesRegex(module.ManifestError, "HTTPS"):
            module.load_manifest(url_value)

    def test_manifest_rejects_unsafe_duplicate_or_unidentified_artifacts(self) -> None:
        module = self._module()
        unsafe = minimal_manifest()
        unsafe["components"][0]["targetRelativePath"] = "..\\outside"
        with self.assertRaisesRegex(module.ManifestError, "relative path"):
            module.load_manifest(unsafe)

        duplicate = minimal_manifest()
        duplicate["components"][1]["variants"]["cpu"]["artifacts"][0]["relativePath"] = "WHEELS\\CORE.WHL"
        with self.assertRaisesRegex(module.ManifestError, "duplicate artifact target"):
            module.load_manifest(duplicate)

        bad_digest = minimal_manifest()
        bad_digest["bootstrap"]["artifact"]["sha256"] = "A" * 64
        with self.assertRaisesRegex(module.ManifestError, "SHA-256"):
            module.load_manifest(bad_digest)

    def test_manifest_rejects_cuda_payload_in_cpu_variant(self) -> None:
        module = self._module()
        value = minimal_manifest()
        artifact = value["components"][1]["variants"]["cpu"]["artifacts"][0]
        artifact["id"] = "caption+cu128-wheel"

        with self.assertRaisesRegex(module.ManifestError, "CPU variant"):
            module.load_manifest(value)

    def test_release_artifact_record_requires_public_identity(self) -> None:
        module = self._module()
        record = {
            "schemaVersion": 1,
            "releaseVersion": "source-bootstrap-v1",
            "artifacts": [{"id": "cpython311-base", "publishedUrl": "", "sizeBytes": 0, "sha256": ""}],
        }

        with self.assertRaisesRegex(module.ManifestError, "release artifact"):
            module.validate_release_artifacts(record)

        complete = copy.deepcopy(record)
        complete["artifacts"][0].update(
            {
                "publishedUrl": "https://github.com/lse14/anima-idg-standard-annotation-processing/releases/download/v1/cpython.zip",
                "sizeBytes": 123,
                "sha256": _sha256(b"release-artifact"),
            }
        )
        self.assertEqual("source-bootstrap-v1", module.validate_release_artifacts(complete)["releaseVersion"])


if __name__ == "__main__":
    unittest.main()
