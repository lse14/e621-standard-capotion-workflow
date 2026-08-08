from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .worker import ReplaceWorker, ReplaceWorkerError


PROTOCOL_VERSION = "1.0"
RUNTIME_ID = "replace-e621"
OWNER = "replace"
MAX_FRAME_BYTES = 1_048_576


def _reply(request: dict[str, object], method: str, payload: dict[str, object]) -> None:
    message: dict[str, object] = {
        "protocolVersion": PROTOCOL_VERSION, "kind": "response", "messageId": f"reply-{request['messageId']}",
        "replyTo": request["messageId"], "runtimeId": RUNTIME_ID, "owner": OWNER, "method": method, "payload": payload,
    }
    for field in ("jobId", "configHash"):
        if isinstance(request.get(field), str):
            message[field] = request[field]
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _request(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("protocolVersion") != PROTOCOL_VERSION or value.get("kind") != "request":
        raise ValueError("unsupported protocol request")
    if value.get("runtimeId") != RUNTIME_ID or value.get("owner") != OWNER:
        raise ValueError("runtime identity mismatch")
    if not isinstance(value.get("messageId"), str) or not 1 <= len(value["messageId"]) <= 120 or not isinstance(value.get("method"), str):
        raise ValueError("request identity is invalid")
    return value


def _payload_field(payload: object, field: str) -> object:
    return payload.get(field) if isinstance(payload, dict) else None


def main() -> int:
    worker = ReplaceWorker()
    hello_complete = False
    while True:
        frame = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
        if frame == b"":
            return 0
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            print("protocol frame exceeds 1 MiB or is unterminated", file=sys.stderr)
            return 2
        try:
            request = _request(json.loads(frame.decode("utf-8")))
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
                    raise ReplaceWorkerError("replace_protocol_violation", "hello envelope does not match payload")
                _reply(
                    request,
                    "hello",
                    worker.initialize(
                        payload,
                        install_root=Path(os.environ.get("ANIMA_RESOURCE_ROOT", str(Path.cwd()))),
                    ),
                )
            except ReplaceWorkerError as exc:
                _reply(request, "error", {"code": exc.code})
                print(f"replace initialization failed: {exc}", file=sys.stderr)
                return 2
            hello_complete = True
            continue
        if method == "shutdown":
            _reply(request, "result", {"accepted": True})
            return 0
        if method in {"cancel", "heartbeat"}:
            _reply(request, "result", {"accepted": True})
            continue
        if method != "process_batch":
            _reply(request, "error", {"code": "unsupported_method"})
            continue
        payload = request.get("payload")
        try:
            if not isinstance(payload, dict) or set(payload) != {"schemaVersion", "payloadType", "items"} or payload["schemaVersion"] != 1 or payload["payloadType"] != "replace_process_request" or not isinstance(payload["items"], list) or len(payload["items"]) != 1:
                raise ReplaceWorkerError("replace_protocol_violation", "replace process payload is invalid")
            if worker.hello is None or request.get("jobId") != worker.hello["jobId"] or request.get("configHash") != worker.hello["configHash"]:
                raise ReplaceWorkerError("replace_protocol_violation", "process envelope does not match initialized job")
            _reply(request, "result", worker.process(payload["items"][0]))
        except ReplaceWorkerError as exc:
            _reply(request, "error", {"code": exc.code})
            print(f"replace process failed: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
