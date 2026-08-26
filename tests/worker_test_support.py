"""Valid minimal worker hello payloads for embedded-runtime protocol tests."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator


_CONFIG_HASH = "a" * 64
ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT / "resource-library"
CLASSIFY_MANIFEST = "classification-indexes\\e621-classify-20260724-v1\\resource.json"
CLASSIFY_FINGERPRINT = "530323a5d1ca5c3f903c0d57b04d6f1014cdcc0ca01b8de5dc0a41e27e1d2baf"
REPLACE_MANIFEST = "replacement-indexes\\e621-replace-20260726-v2\\resource.json"
REPLACE_FINGERPRINT = "3cabbeeffd379a893a0b53d427c3dbb26ea6c587f474ae761b21afde4ee4c47b"
OCR_MANIFEST = "ocr-models\\ocr-ppocrv5-server-paddle-v1\\resource.json"
OCR_FINGERPRINT = "368c31b8af0e96cc61239097688a457a050dfcc1205d054d4e631bd20529c9ca"
OCR_INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}


def _nl_policy() -> dict[str, object]:
    return {
        "concurrency": 1, "maxRequestsPerMinute": 1, "mainAttempts": 1,
        "backupEnabled": False, "backupAttempts": 1, "connectTimeoutSeconds": 1,
        "writeTimeoutSeconds": 1, "readTimeoutSeconds": 1, "poolTimeoutSeconds": 1,
        "temperature": 0.0, "topP": 1.0, "maxTokens": 1, "maxImagePixels": 8_000_000,
        "maxImageSide": 4096, "jpegQuality": 95, "maxEncodedImageBytes": 12_582_912,
        "maxJsonContextBytes": 262_144, "maxResponseBodyBytes": 1_048_576, "maxNlBytes": 16_384,
    }


@contextmanager
def worker_hello_payload(runtime_id: str, install_root: Path) -> Iterator[tuple[str, dict[str, object]]]:
    """Yield a protocol-valid payload while any required temporary paths exist."""
    if runtime_id in {"caption-e621", "policy"}:
        yield "transport_only", {}
        return
    if runtime_id == "classify-e621":
        manifest = json.loads((RESOURCE_ROOT / Path(CLASSIFY_MANIFEST.replace("\\", "/"))).read_text(encoding="utf-8"))
        yield "normal", {
            "schemaVersion": 1, "payloadType": "classify_hello_request", "jobId": "worker-test",
            "configHash": _CONFIG_HASH, "profile": "e621",
            "resourceManifestRelativePath": CLASSIFY_MANIFEST,
            "resourceFingerprint": CLASSIFY_FINGERPRINT,
            "wikiDataSourceId": manifest["metadata"]["wikiDataSourceId"],
            "overwriteCount": False,
            "captionFormat": {"replaceUnderscoresWithSpaces": True, "preserveEscapes": True, "triggersEnabled": False, "triggerTerms": []},
        }
        return
    if runtime_id == "replace-e621":
        yield "normal", {
            "schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": "worker-test",
            "configHash": _CONFIG_HASH,
            "resourceManifestRelativePath": REPLACE_MANIFEST,
            "resourceFingerprint": REPLACE_FINGERPRINT,
        }
        return
    if runtime_id == "ocr-paddle":
        yield "normal", {
            "schemaVersion": 1, "payloadType": "ocr_hello_request", "jobId": "worker-test",
            "configHash": _CONFIG_HASH, "resourceId": "ocr-ppocrv5-server-paddle-v1",
            "resourceManifestRelativePath": OCR_MANIFEST, "resourceFingerprint": OCR_FINGERPRINT,
            "inference": dict(OCR_INFERENCE),
        }
        return
    if runtime_id == "nl":
        yield "normal", {
            "schemaVersion": 1, "payloadType": "nl_hello_request", "jobId": "worker-test",
            "configHash": _CONFIG_HASH, "endpoint": "https://example.test/v1", "model": "main",
            "backupModel": None, "apiKey": "test-secret", "systemPrompt": "describe", "apiPolicy": _nl_policy(),
        }
        return
    if runtime_id == "export":
        with TemporaryDirectory() as temporary:
            parent = Path(temporary)
            dataset = parent / "dataset"
            overlay = parent / ".dataset.anima-overlay-worker-test"
            dataset.mkdir()
            overlay.mkdir()
            (overlay / "overlay-manifest.json").write_text(json.dumps({"schemaVersion": 1, "jobId": "worker-test", "datasetRoot": str(dataset)}), encoding="utf-8")
            yield "normal", {
                "schemaVersion": 1, "payloadType": "export_hello_request", "jobId": "worker-test",
                "configHash": _CONFIG_HASH, "datasetRoot": str(dataset), "overlayRoot": str(overlay), "format": "json",
                "captionFormat": {"replaceUnderscoresWithSpaces": True, "preserveEscapes": True, "triggersEnabled": False, "triggerTerms": []},
            }
        return
    raise ValueError(f"unknown runtime id: {runtime_id}")


def test_config_hash() -> str:
    return _CONFIG_HASH
