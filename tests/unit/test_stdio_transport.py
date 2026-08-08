from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.stdio_transport import StdioJsonlTransport, StdioJsonlTransportError
from anima_core.worker_protocol import ProtocolEnvelopeV1


class _Process:
    def __init__(self, response: bytes) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(response)
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class StdioTransportTests(unittest.TestCase):
    def _request(self) -> ProtocolEnvelopeV1:
        return ProtocolEnvelopeV1(
            protocolVersion="1.0", kind="request", messageId="request-1", runtimeId="caption-e621",
            owner="caption", method="hello", payload={}, jobId="job-1", configHash="hash",
        )

    def test_exchanges_one_utf8_jsonl_envelope(self) -> None:
        response = ProtocolEnvelopeV1(
            protocolVersion="1.0", kind="response", messageId="reply-1", runtimeId="caption-e621",
            owner="caption", method="hello", payload={}, replyTo="request-1", jobId="job-1", configHash="hash",
        )
        process = _Process(json.dumps(response.to_dict()).encode("utf-8") + b"\n")
        transport = StdioJsonlTransport(process)  # type: ignore[arg-type]
        actual = transport.exchange(self._request())
        self.assertEqual("reply-1", actual.messageId)
        self.assertTrue(process.stdin.getvalue().endswith(b"\n"))
        transport.close()

    def test_rejects_unterminated_or_oversized_response(self) -> None:
        for response in (b"{}", b"x" * (1_048_578) + b"\n"):
            with self.subTest(size=len(response)):
                transport = StdioJsonlTransport(_Process(response))  # type: ignore[arg-type]
                with self.assertRaises(StdioJsonlTransportError):
                    transport.exchange(self._request())


if __name__ == "__main__":
    unittest.main()
