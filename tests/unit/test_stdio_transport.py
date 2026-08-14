from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_large_worker_stderr_cannot_block_jsonl_exchange(self) -> None:
        child = (
            "import json,sys;"
            "sys.stderr.buffer.write(b'x'*(1024*1024)+b'END');sys.stderr.buffer.flush();"
            "request=json.loads(sys.stdin.buffer.readline());"
            "reply={'protocolVersion':'1.0','kind':'response','messageId':'reply-1',"
            "'runtimeId':request['runtimeId'],'owner':request['owner'],'method':request['method'],"
            "'payload':{},'replyTo':request['messageId'],'jobId':request['jobId'],'configHash':request['configHash']};"
            "sys.stdout.write(json.dumps(reply)+'\\n');sys.stdout.flush()"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", child],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        transport = StdioJsonlTransport(process)
        result: dict[str, object] = {}

        def exchange() -> None:
            try:
                result["response"] = transport.exchange(self._request())
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=exchange, daemon=True)
        thread.start()
        thread.join(timeout=3)
        blocked = thread.is_alive()
        if blocked:
            process.kill()
            process.wait(timeout=5)
            thread.join(timeout=5)
        transport.close()

        self.assertFalse(blocked, result)
        self.assertNotIn("error", result)
        self.assertEqual("reply-1", result["response"].messageId)  # type: ignore[union-attr]
        self.assertLessEqual(len(transport.stderr_tail), 65_536)
        self.assertTrue(transport.stderr_tail.endswith(b"END"))

    def test_stderr_drainer_start_failure_reaps_worker_and_wraps_error(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys;sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with patch("anima_core.stdio_transport.threading.Thread.start", side_effect=RuntimeError("injected start failure")):
                with self.assertRaisesRegex(StdioJsonlTransportError, "stderr drainer"):
                    StdioJsonlTransport(process)
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin is not None and process.stdin.closed)
            self.assertTrue(process.stdout is not None and process.stdout.closed)
            self.assertTrue(process.stderr is not None and process.stderr.closed)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
