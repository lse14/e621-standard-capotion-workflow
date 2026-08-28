from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import BinaryIO

from .image import CaptionSourceFingerprintError
from .protocol import CaptionPayloadError, validate_process_payload
from .worker import CaptionWorker, CaptionWorkerInitializationError


PROTOCOL_VERSION = "1.0"
RUNTIME_ID = "caption-e621"
OWNER = "caption"
MAX_FRAME_BYTES = 1_048_576


def _reply(
    request: dict[str, object],
    method: str,
    payload: dict[str, object],
    *,
    output: BinaryIO | None = None,
) -> None:
    message: dict[str, object] = {
        "protocolVersion": PROTOCOL_VERSION,
        "kind": "response",
        "messageId": f"reply-{request['messageId']}",
        "replyTo": request["messageId"],
        "runtimeId": RUNTIME_ID,
        "owner": OWNER,
        "method": method,
        "payload": payload,
    }
    if isinstance(request.get("jobId"), str):
        message["jobId"] = request["jobId"]
    if isinstance(request.get("configHash"), str):
        message["configHash"] = request["configHash"]
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        message["method"] = "error"
        message["payload"] = {"code": "caption_protocol_violation"}
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    stream = output or sys.stdout.buffer
    stream.write(encoded + b"\n")
    stream.flush()


def _valid_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("frame must be an object")
    if value.get("protocolVersion") != PROTOCOL_VERSION or value.get("kind") != "request":
        raise ValueError("unsupported protocol request")
    if value.get("runtimeId") != RUNTIME_ID or value.get("owner") != OWNER:
        raise ValueError("runtime identity mismatch")
    if not isinstance(value.get("messageId"), str) or not 1 <= len(value["messageId"]) <= 120 or not isinstance(value.get("method"), str):
        raise ValueError("request has no messageId or method")
    return value


def _payload_field(payload: object, field: str) -> object:
    return payload.get(field) if isinstance(payload, dict) else None


def main() -> int:
    hello_complete = False
    worker = CaptionWorker()
    while True:
        frame = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
        if frame == b"":
            break
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            print("protocol frame exceeds 1 MiB or is unterminated", file=sys.stderr)
            return 2
        try:
            request = _valid_request(json.loads(frame.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"protocol error: {exc}", file=sys.stderr)
            return 2
        method = request["method"]
        if not hello_complete:
            if method != "hello":
                print("hello is required before any worker method", file=sys.stderr)
                return 2
            payload = request.get("payload")
            if payload == {} and os.environ.get("ANIMA_TEST_TRANSPORT_ONLY") == "1":
                _reply(request, "hello", {"executable": sys.executable, "pythonVersion": ".".join(map(str, sys.version_info[:3]))})
                hello_complete = True
                continue
            try:
                if request.get("jobId") != _payload_field(payload, "jobId") or request.get("configHash") != _payload_field(payload, "configHash"):
                    raise CaptionWorkerInitializationError(
                        "caption_protocol_violation",
                        "hello envelope does not match caption payload",
                    )
                initialized = worker.initialize(
                    payload,
                    install_root=Path(os.environ.get("ANIMA_RESOURCE_ROOT", str(Path.cwd()))),
                )
            except CaptionWorkerInitializationError as exc:
                _reply(request, "error", {"code": exc.code})
                print(f"caption initialization failed: {exc}", file=sys.stderr)
                return 2
            _reply(request, "hello", initialized)
            hello_complete = True
            continue
        if method == "shutdown":
            _reply(request, "result", {"accepted": True})
            return 0
        if method == "cancel":
            _reply(request, "result", {"accepted": True})
            continue
        if method == "heartbeat":
            _reply(request, "result", {"accepted": True})
            continue
        if method == "process_batch":
            try:
                payload = validate_process_payload(request.get("payload"))
                if worker.hello is None or request.get("jobId") != worker.hello["jobId"] or request.get("configHash") != worker.hello["configHash"]:
                    raise CaptionPayloadError("process envelope does not match initialized job")
                outcomes = worker.process_batch(payload["items"])
            except CaptionSourceFingerprintError as exc:
                _reply(request, "error", {"code": "caption_source_fingerprint_mismatch"})
                print(f"caption source fingerprint error: {exc}", file=sys.stderr)
                return 2
            except (CaptionPayloadError, CaptionWorkerInitializationError) as exc:
                _reply(request, "error", {"code": "caption_protocol_violation"})
                print(f"caption process protocol error: {exc}", file=sys.stderr)
                return 2
            _reply(
                request,
                "result",
                {"schemaVersion": 1, "payloadType": "caption_process_result", "outcomes": outcomes},
            )
            continue
        _reply(request, "error", {"code": "unsupported_method"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
