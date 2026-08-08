from __future__ import annotations

import asyncio
import json
import sys

from .protocol import NlProtocolError, parse_process
from .worker import NlWorker, NlWorkerError


PROTOCOL_VERSION = "1.0"
RUNTIME_ID = "nl"
OWNER = "nl"
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
    if value.get("runtimeId") != RUNTIME_ID or value.get("owner") != OWNER or not isinstance(value.get("messageId"), str) or not 1 <= len(value["messageId"]) <= 120:
        raise ValueError("runtime identity mismatch")
    return value


async def main() -> int:
    worker = NlWorker()
    hello_complete = False
    try:
        while True:
            frame = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME_BYTES + 2)
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
            method = request.get("method")
            if not hello_complete:
                if method != "hello":
                    return 2
                try:
                    payload = request.get("payload")
                    if not isinstance(payload, dict) or request.get("jobId") != payload.get("jobId") or request.get("configHash") != payload.get("configHash"):
                        raise NlWorkerError("nl_protocol_violation", "hello envelope does not match payload")
                    _reply(request, "hello", await worker.initialize(payload))
                    hello_complete = True
                except (NlWorkerError, NlProtocolError) as exc:
                    code = exc.code if isinstance(exc, NlWorkerError) else "nl_protocol_violation"
                    _reply(request, "error", {"code": code})
                    print(f"NL initialization failed: {exc}", file=sys.stderr)
                    return 2
                continue
            if method == "shutdown":
                _reply(request, "result", {"accepted": True})
                return 0
            if method == "cancel":
                worker.cancel()
                _reply(request, "result", {"accepted": True})
                continue
            if method == "heartbeat":
                _reply(request, "result", {"accepted": True})
                continue
            if method != "process_batch":
                _reply(request, "error", {"code": "unsupported_method"})
                continue
            try:
                if worker.hello is None or request.get("jobId") != worker.hello.jobId or request.get("configHash") != worker.hello.configHash:
                    raise NlWorkerError("nl_protocol_violation", "process envelope does not match initialized job")
                items, allowance = parse_process(request.get("payload"))
                results = await worker.process(items, allowance)
                _reply(request, "result", {"schemaVersion": 1, "payloadType": "nl_process_result", "items": results})
            except (NlWorkerError, NlProtocolError) as exc:
                code = exc.code if isinstance(exc, NlWorkerError) else "nl_protocol_violation"
                _reply(request, "error", {"code": code})
                print(f"NL process failed: {exc}", file=sys.stderr)
                return 2
    finally:
        await worker.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
