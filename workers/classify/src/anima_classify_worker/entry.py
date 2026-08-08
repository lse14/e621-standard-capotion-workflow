from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .protocol import ClassifyPayloadError, validate_process_payload
from .worker import ClassifyWorker, ClassifyWorkerInitializationError


PROTOCOL_VERSION = "1.0"
RUNTIME_ID = "classify-e621"
OWNER = "classify"
MAX_FRAME_BYTES = 1_048_576


def _reply(request: dict[str, object], method: str, payload: dict[str, object]) -> None:
    message = {"protocolVersion": PROTOCOL_VERSION, "kind": "response", "messageId": f"reply-{request['messageId']}", "replyTo": request["messageId"], "runtimeId": RUNTIME_ID, "owner": OWNER, "method": method, "payload": payload}
    if isinstance(request.get("jobId"), str):
        message["jobId"] = request["jobId"]
    if isinstance(request.get("configHash"), str):
        message["configHash"] = request["configHash"]
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


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
    worker = ClassifyWorker()
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
            try:
                if request.get("jobId") != _payload_field(payload, "jobId") or request.get("configHash") != _payload_field(payload, "configHash"):
                    raise ClassifyWorkerInitializationError(
                        "classify_protocol_violation",
                        "hello envelope does not match classify payload",
                    )
                initialized = worker.initialize(
                    payload,
                    install_root=Path(os.environ.get("ANIMA_RESOURCE_ROOT", str(Path.cwd()))),
                )
            except ClassifyWorkerInitializationError as exc:
                _reply(request, "error", {"code": exc.code})
                print(f"classify initialization failed: {exc}", file=sys.stderr)
                return 2
            _reply(request, "hello", initialized)
            hello_complete = True
            continue
        if method == "shutdown":
            _reply(request, "result", {"accepted": True})
            worker.close()
            return 0
        if method in {"cancel", "heartbeat"}:
            _reply(request, "result", {"accepted": True})
            continue
        if method == "process_batch":
            try:
                payload = validate_process_payload(request.get("payload"))
                if worker.hello is None or request.get("jobId") != worker.hello["jobId"] or request.get("configHash") != worker.hello["configHash"]:
                    raise ClassifyPayloadError("process envelope does not match initialized job")
                outcome = worker.process(payload["items"][0])
            except (ClassifyPayloadError, ClassifyWorkerInitializationError) as exc:
                _reply(request, "error", {"code": "classify_protocol_violation"})
                print(f"classify process protocol error: {exc}", file=sys.stderr)
                return 2
            _reply(request, "result", outcome)
            continue
        _reply(request, "error", {"code": "unsupported_method"})
    worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
