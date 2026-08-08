from __future__ import annotations

import json
import sys

from .protocol import ExportProtocolError, parse_process
from .worker import ExportWorker, ExportWorkerError


PROTOCOL_VERSION = "1.0"
RUNTIME_ID = "export"
OWNER = "export"
MAX_FRAME_BYTES = 1_048_576


def _reply(request: dict[str, object], method: str, payload: dict[str, object]) -> None:
    message = {"protocolVersion": PROTOCOL_VERSION, "kind": "response", "messageId": f"reply-{request['messageId']}", "replyTo": request["messageId"], "runtimeId": RUNTIME_ID, "owner": OWNER, "method": method, "payload": payload}
    if isinstance(request.get("jobId"), str): message["jobId"] = request["jobId"]
    if isinstance(request.get("configHash"), str): message["configHash"] = request["configHash"]
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    hello_complete = False; worker = ExportWorker()
    while True:
        frame = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
        if frame == b"":
            break
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            print("protocol frame exceeds 1 MiB or is unterminated", file=sys.stderr)
            return 2
        try:
            request = json.loads(frame.decode("utf-8"))
            if not isinstance(request, dict) or request.get("protocolVersion") != PROTOCOL_VERSION or request.get("kind") != "request" or request.get("runtimeId") != RUNTIME_ID or request.get("owner") != OWNER or not isinstance(request.get("messageId"), str) or not 1 <= len(request["messageId"]) <= 120:
                raise ValueError("runtime identity mismatch")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"protocol error: {exc}", file=sys.stderr)
            return 2
        method = request.get("method")
        if not hello_complete:
            if method != "hello":
                return 2
            try:
                initialized = worker.initialize(request.get("payload"))
                if request.get("jobId") != worker.hello.job_id or request.get("configHash") != worker.hello.config_hash: raise ExportWorkerError("export_protocol_violation", "hello identity mismatch")
            except (ExportWorkerError, ExportProtocolError) as exc:
                _reply(request, "error", {"code": exc.code if isinstance(exc, ExportWorkerError) else "export_protocol_violation"}); return 2
            _reply(request, "hello", initialized)
            hello_complete = True
        elif method == "shutdown":
            _reply(request, "result", {"accepted": True})
            return 0
        elif method in {"cancel", "heartbeat"}:
            _reply(request, "result", {"accepted": True})
        elif method == "process_batch":
            try:
                if request.get("jobId") != worker.hello.job_id or request.get("configHash") != worker.hello.config_hash: raise ExportWorkerError("export_protocol_violation", "process identity mismatch")
                _reply(request, "result", worker.process(parse_process(request.get("payload"))))
            except (ExportWorkerError, ExportProtocolError) as exc:
                _reply(request, "error", {"code": exc.code if isinstance(exc, ExportWorkerError) else "export_protocol_violation"}); return 2
        else:
            _reply(request, "error", {"code": "unsupported_method"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
