from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "packaging" / "installer" / "download.py"


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    url: str
    allowed_hosts: tuple[str, ...]
    size_bytes: int
    sha256: str
    relative_path: str = "fixture.bin"


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], payload: bytes, *, url: str) -> None:
        self.status = status
        self.headers = headers
        self.url = url
        self._payload = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)


class ErrorAfterPayloadResponse(FakeResponse):
    def read(self, size: int = -1) -> bytes:
        payload = super().read(size)
        if payload:
            return payload
        raise OSError("connection closed after the complete payload")


class ScriptedTransport:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict[str, str], tuple[str, ...]]] = []

    def open(self, url: str, headers: dict[str, str], allowed_hosts: tuple[str, ...]) -> FakeResponse:
        self.requests.append((url, dict(headers), allowed_hosts))
        if not self._responses:
            raise AssertionError("unexpected transport request")
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def artifact_for(payload: bytes, *, url: str = "https://downloads.example.test/fixture.bin") -> Artifact:
    return Artifact(
        "fixture-artifact",
        url,
        ("downloads.example.test",),
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )


def _load_module():
    if not MODULE_PATH.is_file():
        return None
    name = "source_bootstrap_download_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("download module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SourceBootstrapDownloadTests(unittest.TestCase):
    def _module(self):
        module = _load_module()
        self.assertIsNotNone(module, "source bootstrap download module must exist")
        return module

    def test_download_resumes_matching_partial_with_range(self) -> None:
        module = self._module()
        payload = b"verified artifact payload"
        artifact = artifact_for(payload)
        with tempfile.TemporaryDirectory() as temporary_name:
            cache = Path(temporary_name)
            partial = cache / f"{artifact.sha256}.partial"
            partial.write_bytes(payload[:9])
            transport = ScriptedTransport([
                FakeResponse(
                    206,
                    {"Content-Range": f"bytes 9-{len(payload) - 1}/{len(payload)}"},
                    payload[9:],
                    url=artifact.url,
                )
            ])

            verified = module.download_verified(artifact, cache, transport=transport)

            self.assertEqual(payload, verified.read_bytes())
            self.assertEqual("bytes=9-", transport.requests[0][1]["Range"])
            self.assertEqual("Anima-Source-Bootstrap/1.0", transport.requests[0][1]["User-Agent"])
            self.assertFalse(partial.exists())

    def test_download_restarts_when_server_ignores_range(self) -> None:
        module = self._module()
        payload = b"server ignores range"
        artifact = artifact_for(payload)
        with tempfile.TemporaryDirectory() as temporary_name:
            cache = Path(temporary_name)
            (cache / f"{artifact.sha256}.partial").write_bytes(payload[:4])
            transport = ScriptedTransport([
                FakeResponse(200, {}, payload, url=artifact.url),
                FakeResponse(200, {}, payload, url=artifact.url),
            ])

            verified = module.download_verified(artifact, cache, transport=transport)

            self.assertEqual(payload, verified.read_bytes())
            self.assertEqual("bytes=4-", transport.requests[0][1]["Range"])
            self.assertNotIn("Range", transport.requests[1][1])

    def test_progressive_responses_do_not_exhaust_the_no_progress_retry_budget(self) -> None:
        module = self._module()
        payload = b"progress across four connections"
        artifact = artifact_for(payload)
        boundaries = (0, 8, 16, 24, len(payload))
        responses = []
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            status = 200 if index == 0 else 206
            headers = {} if index == 0 else {"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"}
            responses.append(FakeResponse(status, headers, payload[start:end], url=artifact.url))
        with tempfile.TemporaryDirectory() as temporary_name:
            verified = module.download_verified(
                artifact,
                Path(temporary_name),
                transport=ScriptedTransport(responses),
                attempts=1,
            )

            self.assertEqual(payload, verified.read_bytes())

    def test_complete_verified_partial_survives_an_error_while_reading_eof(self) -> None:
        module = self._module()
        payload = b"complete before eof error"
        artifact = artifact_for(payload)
        response = ErrorAfterPayloadResponse(200, {}, payload, url=artifact.url)
        with tempfile.TemporaryDirectory() as temporary_name:
            verified = module.download_verified(
                artifact,
                Path(temporary_name),
                transport=ScriptedTransport([response]),
                attempts=1,
            )

            self.assertEqual(payload, verified.read_bytes())

    def test_download_rejects_redirect_to_unknown_host(self) -> None:
        module = self._module()
        payload = b"wrong host"
        artifact = artifact_for(payload)
        with tempfile.TemporaryDirectory() as temporary_name:
            transport = ScriptedTransport([
                FakeResponse(200, {}, payload, url="https://evil.example/fixture.bin"),
            ])

            with self.assertRaisesRegex(module.ManualDownloadRequired, "allowed host"):
                module.download_verified(artifact, Path(temporary_name), transport=transport)

    def test_hash_mismatch_deletes_complete_payload_and_prints_manual_details(self) -> None:
        module = self._module()
        expected = b"expected payload"
        actual = b"corrupted payload"
        artifact = artifact_for(expected)
        with tempfile.TemporaryDirectory() as temporary_name:
            cache = Path(temporary_name)
            transport = ScriptedTransport([FakeResponse(200, {}, actual, url=artifact.url)])

            with self.assertRaisesRegex(module.ManualDownloadRequired, artifact.sha256) as raised:
                module.download_verified(artifact, cache, transport=transport)

            self.assertIn(artifact.url, str(raised.exception))
            self.assertIn(str(artifact.size_bytes), str(raised.exception))
            self.assertFalse((cache / artifact.sha256).exists())
            self.assertFalse((cache / f"{artifact.sha256}.partial").exists())

    def test_transient_failure_preserves_only_resumable_partial(self) -> None:
        module = self._module()
        payload = b"retry payload"
        artifact = artifact_for(payload)
        with tempfile.TemporaryDirectory() as temporary_name:
            cache = Path(temporary_name)
            partial = cache / f"{artifact.sha256}.partial"
            partial.write_bytes(payload[:3])
            transport = ScriptedTransport([OSError("offline"), OSError("offline"), OSError("offline")])

            with self.assertRaisesRegex(module.ManualDownloadRequired, "Download failed"):
                module.download_verified(artifact, cache, transport=transport)

            self.assertEqual(payload[:3], partial.read_bytes())
            self.assertFalse((cache / artifact.sha256).exists())


if __name__ == "__main__":
    unittest.main()
