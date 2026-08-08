from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from .image import OcrSourceFingerprintError
from .model import ModelFactory
from .protocol import MAX_FRAME_BYTES, OcrPayloadError
from .worker import OcrWorker, OcrWorkerInitializationError


PROTOCOL_VERSION = "1.0"


def _runtime_id_for_process() -> str:
    runtime_id = Path(sys.executable).resolve().parent.name
    return runtime_id if runtime_id in {"ocr-paddle", "ocr-paddle-gpu"} else "ocr-paddle"


RUNTIME_ID = _runtime_id_for_process()
OWNER = "ocr"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _bounded_identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("protocol identity is invalid")
    return value


def _valid_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("protocol frame must be an object")
    allowed = {
        "protocolVersion", "kind", "messageId", "runtimeId", "owner", "method", "payload", "jobId", "configHash",
    }
    if set(value) - allowed or value.get("protocolVersion") != PROTOCOL_VERSION or value.get("kind") != "request":
        raise ValueError("unsupported protocol request")
    if value.get("runtimeId") != RUNTIME_ID or value.get("owner") != OWNER:
        raise ValueError("runtime identity mismatch")
    _bounded_identifier(value.get("messageId"))
    if value.get("method") not in {"hello", "process_batch", "cancel", "shutdown", "heartbeat"}:
        raise ValueError("unsupported worker method")
    if not isinstance(value.get("payload"), dict):
        raise ValueError("protocol payload is invalid")
    for name in ("jobId", "configHash"):
        if name in value:
            _bounded_identifier(value[name])
    return value


def _reply(output: BinaryIO, request: dict[str, object], method: str, payload: dict[str, object]) -> None:
    frame: dict[str, object] = {
        "protocolVersion": PROTOCOL_VERSION,
        "kind": "response",
        "messageId": f"reply-{request['messageId']}",
        "replyTo": request["messageId"],
        "runtimeId": RUNTIME_ID,
        "owner": OWNER,
        "method": method,
        "payload": payload,
    }
    for name in ("jobId", "configHash"):
        if name in request:
            frame[name] = request[name]
    try:
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        data = b""
    if len(data) > MAX_FRAME_BYTES:
        frame["method"] = "error"
        frame["payload"] = {"code": "ocr_protocol_violation"}
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    output.write(data + b"\n")
    output.flush()


def _diagnostic(stream: TextIO, message: str) -> None:
    stream.write(message + "\n")
    stream.flush()


def run(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    error_stream: TextIO,
    *,
    model_factory: ModelFactory | None = None,
) -> int:
    worker = OcrWorker() if model_factory is None else OcrWorker(model_factory=model_factory)
    initialized = False
    while True:
        frame = input_stream.readline(MAX_FRAME_BYTES + 2)
        if frame == b"":
            return 0
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            _diagnostic(error_stream, "OCR protocol frame exceeds 1 MiB or is unterminated")
            return 2
        try:
            request = _valid_request(json.loads(frame.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            _diagnostic(error_stream, f"OCR protocol error: {str(exc)[:160]}")
            return 2
        method = request["method"]
        if not initialized:
            if method != "hello":
                _diagnostic(error_stream, "OCR hello is required before any worker method")
                return 2
            payload = request["payload"]
            assert isinstance(payload, dict)
            if request.get("jobId") != payload.get("jobId") or request.get("configHash") != payload.get("configHash"):
                _reply(output_stream, request, "error", {"code": "ocr_protocol_violation"})
                _diagnostic(error_stream, "OCR hello envelope does not match payload")
                return 2
            try:
                hello = worker.initialize(
                    payload,
                    resource_root=Path(os.environ.get("ANIMA_RESOURCE_ROOT", str(Path.cwd()))),
                )
            except OcrWorkerInitializationError:
                _reply(output_stream, request, "error", {"code": "ocr_initialization_failed"})
                _diagnostic(error_stream, "OCR initialization failed")
                return 2
            _reply(output_stream, request, "hello", hello)
            initialized = True
            continue
        if method == "shutdown":
            _reply(output_stream, request, "result", {"accepted": True})
            return 0
        if method in {"cancel", "heartbeat"}:
            _reply(output_stream, request, "result", {"accepted": True})
            continue
        if method != "process_batch":
            _reply(output_stream, request, "error", {"code": "unsupported_method"})
            continue
        try:
            hello = worker.hello
            assert hello is not None
            if request.get("jobId") != hello.job_id or request.get("configHash") != hello.config_hash:
                raise OcrPayloadError("process envelope does not match initialized job")
            outcome = worker.process(request["payload"])
        except OcrSourceFingerprintError:
            _reply(output_stream, request, "error", {"code": "ocr_source_fingerprint_mismatch"})
            _diagnostic(error_stream, "OCR source fingerprint mismatch")
            return 2
        except (OcrPayloadError, OcrWorkerInitializationError):
            _reply(output_stream, request, "error", {"code": "ocr_protocol_violation"})
            _diagnostic(error_stream, "OCR process protocol error")
            return 2
        _reply(output_stream, request, "result", outcome)


def main() -> int:
    return run(sys.stdin.buffer, sys.stdout.buffer, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
