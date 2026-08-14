"""Bounded synchronous JSONL transport for isolated embedded workers."""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

from .worker_protocol import ProtocolEnvelopeV1, ProtocolError


MAX_FRAME_BYTES = 1_048_576
MAX_STDERR_TAIL_BYTES = 65_536
STDERR_READ_BYTES = 8_192


class StdioJsonlTransportError(RuntimeError):
    pass


class StdioJsonlTransport:
    """One request/response at a time; callers own protocol-level validation."""

    def __init__(self, process: subprocess.Popen[bytes], *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        if not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("JSONL frame limit is invalid")
        if process.stdin is None or process.stdout is None:
            raise StdioJsonlTransportError("worker process has no stdio pipes")
        self._process = process
        self._limit = max_frame_bytes
        self._closed = False
        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(stderr,),
                daemon=True,
                name="anima-worker-stderr",
            )
            try:
                self._stderr_thread.start()
            except Exception as exc:
                self._stderr_thread = None
                try:
                    self.close()
                except Exception as cleanup_error:
                    raise StdioJsonlTransportError("worker stderr drainer failed to start and clean up") from cleanup_error
                raise StdioJsonlTransportError("worker stderr drainer failed to start") from exc

    def _drain_stderr(self, stream: Any) -> None:
        while True:
            try:
                chunk = stream.read(STDERR_READ_BYTES)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._stderr_lock:
                self._stderr_tail.extend(chunk)
                excess = len(self._stderr_tail) - MAX_STDERR_TAIL_BYTES
                if excess > 0:
                    del self._stderr_tail[:excess]

    @property
    def stderr_tail(self) -> bytes:
        with self._stderr_lock:
            return bytes(self._stderr_tail)

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if self._closed:
            raise StdioJsonlTransportError("worker transport is closed")
        if self._process.poll() is not None:
            raise StdioJsonlTransportError("worker exited before request")
        try:
            payload = json.dumps(request.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StdioJsonlTransportError("worker request is not serializable") from exc
        if not payload or len(payload) > self._limit:
            raise StdioJsonlTransportError("worker request frame exceeds limit")
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(payload + b"\n")
            self._process.stdin.flush()
            assert self._process.stdout is not None
            frame = self._process.stdout.readline(self._limit + 2)
        except (OSError, ValueError) as exc:
            raise StdioJsonlTransportError("worker stdio exchange failed") from exc
        if not frame:
            raise StdioJsonlTransportError("worker closed stdout without a response")
        if len(frame) > self._limit + 1 or not frame.endswith(b"\n"):
            raise StdioJsonlTransportError("worker response frame exceeds limit or is unterminated")
        try:
            value: Any = json.loads(frame.decode("utf-8"))
            return ProtocolEnvelopeV1.from_dict(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
            raise StdioJsonlTransportError("worker response is not a valid protocol envelope") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        for stream in (getattr(self._process, "stdout", None), getattr(self._process, "stderr", None)):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1)

    def __enter__(self) -> "StdioJsonlTransport":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
