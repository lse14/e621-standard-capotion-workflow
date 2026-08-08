from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .protocol import PolicyProtocolError, parse_process


PROTOCOL_VERSION = "1.0"
RUNTIME_ID = "policy"
OWNER = "policy"
MAX_FRAME_BYTES = 1_048_576


def _reply(request: dict[str, object], method: str, payload: dict[str, object]) -> None:
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
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _request(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    if value.get("protocolVersion") != PROTOCOL_VERSION or value.get("kind") != "request":
        raise ValueError("unsupported protocol request")
    if value.get("runtimeId") != RUNTIME_ID or value.get("owner") != OWNER:
        raise ValueError("runtime identity mismatch")
    message_id = value.get("messageId")
    if not isinstance(message_id, str) or not 1 <= len(message_id) <= 120:
        raise ValueError("request messageId is invalid")
    return value


def main() -> int:
    hello_complete = False
    transport_only = os.environ.get("ANIMA_TEST_TRANSPORT_ONLY") == "1"
    worker = None
    if not transport_only:
        # Keep image and model dependencies out of the protocol-only startup path.
        from .worker import PolicyWorker
        worker = PolicyWorker()
    while True:
        frame = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
        if frame == b"":
            break
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            print("protocol frame exceeds 1 MiB or is unterminated", file=sys.stderr)
            return 2
        try:
            request = _request(json.loads(frame.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"protocol error: {exc}", file=sys.stderr)
            return 2
        method = request.get("method")
        if not hello_complete:
            if method != "hello":
                print("hello is required before any worker method", file=sys.stderr)
                return 2
            if request.get("payload") == {} and transport_only:
                _reply(request, "hello", {"executable": sys.executable, "pythonVersion": ".".join(map(str, sys.version_info[:3]))})
                hello_complete = True
                continue
            try:
                from .worker import PolicyWorkerError
                assert worker is not None
                initialized = worker.initialize(
                    request.get("payload"),
                    install_root=Path(os.environ.get("ANIMA_RESOURCE_ROOT", str(Path.cwd()))),
                )
                if request.get("jobId") != worker.hello.jobId or request.get("configHash") != worker.hello.configHash:  # type: ignore[union-attr]
                    raise PolicyWorkerError("policy_protocol_violation", "hello envelope does not match payload")
            except (PolicyWorkerError, PolicyProtocolError) as exc:
                code = exc.code if isinstance(exc, PolicyWorkerError) else "policy_protocol_violation"
                _reply(request, "error", {"code": code})
                print(f"policy initialization failed: {exc}", file=sys.stderr)
                return 2
            _reply(request, "hello", initialized)
            hello_complete = True
            continue
        if method == "shutdown":
            _reply(request, "result", {"accepted": True})
            return 0
        if method in {"cancel", "heartbeat"}:
            _reply(request, "result", {"accepted": True})
            continue
        if method == "process_batch":
            try:
                from .worker import PolicyWorkerError
                assert worker is not None
                if request.get("jobId") != worker.hello.jobId or request.get("configHash") != worker.hello.configHash:  # type: ignore[union-attr]
                    raise PolicyWorkerError("policy_protocol_violation", "process envelope does not match initialized job")
                result = worker.process(parse_process(request.get("payload")))
            except (PolicyWorkerError, PolicyProtocolError) as exc:
                code = exc.code if isinstance(exc, PolicyWorkerError) else "policy_protocol_violation"
                _reply(request, "error", {"code": code})
                print(f"policy process failed: {exc}", file=sys.stderr)
                return 2
            _reply(request, "result", result)
            continue
        _reply(request, "error", {"code": "unsupported_method"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
