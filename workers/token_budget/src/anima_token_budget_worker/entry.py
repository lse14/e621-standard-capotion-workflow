from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import BinaryIO

from .protocol import MAX_FRAME_BYTES, TokenBudgetPayloadError
from .worker import TokenBudgetWorker, TokenBudgetWorkerInitializationError


RUNTIME_ID = "token-budget"
OWNER = "token-budget"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONFIG_HASH = re.compile(r"^[0-9a-f]{64}$")


def _reply(stream: BinaryIO, request: dict[str, object], method: str, payload: dict[str, object]) -> None:
    value: dict[str, object] = {
        "protocolVersion": "1.0", "kind": "response", "messageId": f"reply-{request['messageId']}",
        "replyTo": request["messageId"], "runtimeId": RUNTIME_ID, "owner": OWNER, "method": method, "payload": payload,
    }
    for name in ("jobId", "configHash"):
        if name in request:
            value[name] = request[name]
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        value["method"] = "error"
        value["payload"] = {"code": "token_budget_protocol_violation"}
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    stream.write(encoded + b"\n")
    stream.flush()


def _request(line: bytes) -> dict[str, object]:
    if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
        raise TokenBudgetPayloadError("Token Budget input frame is invalid")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenBudgetPayloadError("Token Budget input frame is invalid") from exc
    if not isinstance(value, dict) or set(value) - {"protocolVersion", "kind", "messageId", "runtimeId", "owner", "method", "payload", "jobId", "configHash"}:
        raise TokenBudgetPayloadError("Token Budget envelope is invalid")
    if (
        value.get("protocolVersion") != "1.0" or value.get("runtimeId") != RUNTIME_ID or value.get("owner") != OWNER
        or value.get("kind") != "request" or value.get("method") not in {"hello", "process_batch", "cancel", "heartbeat", "shutdown"}
        or not isinstance(value.get("messageId"), str) or not _IDENTIFIER.fullmatch(value["messageId"])
        or not isinstance(value.get("payload"), dict)
        or ("jobId" in value and (not isinstance(value["jobId"], str) or not _IDENTIFIER.fullmatch(value["jobId"])))
        or ("configHash" in value and (not isinstance(value["configHash"], str) or not _CONFIG_HASH.fullmatch(value["configHash"])))
    ):
        raise TokenBudgetPayloadError("Token Budget envelope is invalid")
    return value


def main(input_stream: BinaryIO | None = None, output_stream: BinaryIO | None = None) -> int:
    source = input_stream or sys.stdin.buffer
    output = output_stream or sys.stdout.buffer
    worker = TokenBudgetWorker()
    initialized = False
    for line in source:
        try:
            request = _request(line)
        except TokenBudgetPayloadError:
            return 2
        method = request["method"]
        if not initialized:
            if method != "hello" or request.get("jobId") != request["payload"].get("jobId") or request.get("configHash") != request["payload"].get("configHash"):
                return 2
            try:
                hello = worker.initialize(request["payload"], resource_root=Path(os.environ.get("ANIMA_RESOURCE_ROOT", str(Path.cwd()))))
            except TokenBudgetWorkerInitializationError:
                _reply(output, request, "error", {"code": "token_budget_initialization_failed"})
                return 2
            _reply(output, request, "hello", hello)
            initialized = True
            continue
        if method == "shutdown":
            _reply(output, request, "result", {"accepted": True})
            return 0
        if method in {"cancel", "heartbeat"}:
            _reply(output, request, "result", {"accepted": True})
            continue
        if method != "process_batch":
            _reply(output, request, "error", {"code": "unsupported_method"})
            continue
        if request.get("jobId") != worker.hello.job_id or request.get("configHash") != worker.hello.config_hash:
            _reply(output, request, "error", {"code": "token_budget_protocol_violation"})
            return 2
        try:
            outcome = worker.process(request["payload"])
        except (TokenBudgetPayloadError, TokenBudgetWorkerInitializationError):
            _reply(output, request, "error", {"code": "token_budget_protocol_violation"})
            return 2
        _reply(output, request, "result", outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
