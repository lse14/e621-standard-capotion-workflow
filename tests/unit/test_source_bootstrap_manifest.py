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
        duplicate_artifact = copy.deepcopy(duplicate["components"][0]["variants"]["cpu"]["artifacts"][0])
        duplicate_artifact["id"] = "core-wheel-copy"
        duplicate_artifact["relativePath"] = "WHEELS\\CORE.WHL"
        duplicate["components"][0]["variants"]["cpu"]["artifacts"].append(duplicate_artifact)
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

    def test_source_tree_artifact_is_resource_only_and_has_no_network_identity(self) -> None:
        module = self._module()
        value = minimal_manifest()
        resource = {
            "componentId": "e621-indexes",
            "kind": "resource",
            "required": True,
            "targetRelativePath": "resource-library/e621-indexes",
            "variants": {
                "shared": {
                    "artifacts": [{
                        "id": "e621-index-resource-json",
                        "delivery": "source-tree",
                        "sourceRelativePath": "resource-library/classification-indexes/e621-classify-20260724-v1/resource.json",
                        "sizeBytes": 1,
                        "sha256": _sha256(b"x"),
                        "relativePath": "classification-indexes/e621-classify-20260724-v1/resource.json",
                    }],
                    "peakBytes": 1,
                    "probe": "indexes",
                }
            },
        }
        value["components"].append(resource)
        manifest = module.load_manifest(value)
        artifact = manifest.components[-1].variants["shared"].artifacts[0]
        self.assertEqual("source-tree", artifact.delivery)
        self.assertIsNone(artifact.url)
        self.assertEqual(
            "resource-library\\classification-indexes\\e621-classify-20260724-v1\\resource.json",
            artifact.source_relative_path,
        )

        invalid_runtime = copy.deepcopy(value)
        invalid_runtime["components"][0]["variants"]["cpu"]["artifacts"] = [resource["variants"]["shared"]["artifacts"][0]]
        with self.assertRaisesRegex(module.ManifestError, "source-tree"):
            module.load_manifest(invalid_runtime)

    def test_source_tree_resource_artifact_paths_are_component_local(self) -> None:
        module = self._module()
        value = minimal_manifest()
        for component_id, target, source in (
            (
                "e621-indexes",
                "resource-library/classification-indexes/e621-classify-20260724-v1",
                "resource-library/classification-indexes/e621-classify-20260724-v1/resource.json",
            ),
            (
                "e621-replacement-indexes",
                "resource-library/replacement-indexes/e621-replace-20260726-v2",
                "resource-library/replacement-indexes/e621-replace-20260726-v2/resource.json",
            ),
        ):
            value["components"].append(
                {
                    "componentId": component_id,
                    "kind": "resource",
                    "required": True,
                    "targetRelativePath": target,
                    "variants": {
                        "shared": {
                            "artifacts": [{
                                "id": component_id + "-manifest",
                                "delivery": "source-tree",
                                "sourceRelativePath": source,
                                "sizeBytes": 1,
                                "sha256": _sha256(b"x"),
                                "relativePath": "resource.json",
                            }],
                            "peakBytes": 1,
                            "probe": "indexes",
                        }
                    },
                }
            )

        manifest = module.load_manifest(value)

        self.assertEqual(
            ["resource.json"],
            [artifact.relative_path for artifact in manifest.components[-1].variants["shared"].artifacts],
        )

    def test_candidate_release_artifact_is_inventory_only(self) -> None:
        module = self._module()
        value = minimal_manifest()
        value["bootstrap"]["artifact"] = {
            "id": "cpython311-base",
            "delivery": "candidate-release",
            "candidatePath": ".release-candidate/bootstrap/cpython311-base.zip",
            "sizeBytes": 1,
            "sha256": _sha256(b"x"),
            "relativePath": "bootstrap/cpython311-base.zip",
        }

        with self.assertRaisesRegex(module.ManifestError, "candidate-release"):
            module.load_manifest(value)

        manifest = module.load_manifest(value, allow_candidate_delivery=True)
        artifact = manifest.bootstrap_artifact
        self.assertEqual("candidate-release", artifact.delivery)
        self.assertEqual(".release-candidate\\bootstrap\\cpython311-base.zip", artifact.candidate_path)

    def test_release_artifact_record_requires_public_identity(self) -> None:
        module = self._module()
        record = {
            "schemaVersion": 1,
            "releaseVersion": "source-bootstrap-v1",
            "publicationState": "published",
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

        candidate = {
            "schemaVersion": 1,
            "releaseVersion": "source-bootstrap-v1",
            "publicationState": "candidate",
            "artifacts": [{
                "id": "cpython311-base",
                "candidatePath": ".release-candidate/bootstrap/cpython.zip",
                "candidateSizeBytes": 123,
                "candidateSha256": _sha256(b"release-artifact"),
            }],
        }
        self.assertEqual("candidate", module.validate_release_artifacts(candidate)["publicationState"])


if __name__ == "__main__":
    unittest.main()
