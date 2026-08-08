from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator, Literal, Mapping

from . import PROTOCOL_VERSION


MAX_FRAME_BYTES = 1_048_576
KINDS = frozenset({"request", "response", "event"})
METHODS = frozenset({"hello", "process_batch", "cancel", "shutdown", "heartbeat", "result", "error"})
OWNERS = frozenset({"caption", "classify", "replace", "ocr", "nl", "policy", "export", "token-budget"})
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ProtocolEnvelopeV1:
    protocolVersion: Literal["1.0"]
    kind: Literal["request", "response", "event"]
    messageId: str
    runtimeId: str
    owner: str
    method: str
    payload: dict[str, object]
    replyTo: str | None = None
    jobId: str | None = None
    configHash: str | None = None

    @classmethod
    def from_dict(cls, value: object, *, runtime_id: str | None = None, owner: str | None = None) -> "ProtocolEnvelopeV1":
        if not isinstance(value, dict):
            raise ProtocolError("protocol frame must be an object")
        protocol_version = value.get("protocolVersion")
        kind = value.get("kind")
        message_id = value.get("messageId")
        frame_runtime_id = value.get("runtimeId")
        frame_owner = value.get("owner")
        method = value.get("method")
        payload = value.get("payload", {})
        if protocol_version != PROTOCOL_VERSION:
            raise ProtocolError("protocolVersion mismatch")
        if kind not in KINDS or not isinstance(kind, str):
            raise ProtocolError("unknown protocol kind")
        if not isinstance(message_id, str) or not IDENTIFIER.fullmatch(message_id):
            raise ProtocolError("messageId is invalid")
        if not isinstance(frame_runtime_id, str) or not IDENTIFIER.fullmatch(frame_runtime_id):
            raise ProtocolError("runtimeId is invalid")
        if frame_owner not in OWNERS:
            raise ProtocolError("owner is invalid")
        if not isinstance(method, str) or method not in METHODS:
            raise ProtocolError("unknown protocol method")
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be an object")
        reply_to = value.get("replyTo")
        job_id = value.get("jobId")
        config_hash = value.get("configHash")
        for name, item in (("replyTo", reply_to), ("jobId", job_id), ("configHash", config_hash)):
            if item is not None and (not isinstance(item, str) or not IDENTIFIER.fullmatch(item)):
                raise ProtocolError(f"{name} is invalid")
        if kind == "response" and not reply_to:
            raise ProtocolError("response requires replyTo")
        if runtime_id is not None and frame_runtime_id != runtime_id:
            raise ProtocolError("runtimeId does not match launch manifest")
        if owner is not None and frame_owner != owner:
            raise ProtocolError("owner does not match launch manifest")
        return cls(
            protocolVersion="1.0", kind=kind, messageId=message_id, runtimeId=frame_runtime_id,
            owner=frame_owner, method=method, payload=dict(payload), replyTo=reply_to,
            jobId=job_id, configHash=config_hash,
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "protocolVersion": self.protocolVersion,
            "kind": self.kind,
            "messageId": self.messageId,
            "runtimeId": self.runtimeId,
            "owner": self.owner,
            "method": self.method,
            "payload": self.payload,
        }
        if self.replyTo is not None:
            value["replyTo"] = self.replyTo
        if self.jobId is not None:
            value["jobId"] = self.jobId
        if self.configHash is not None:
            value["configHash"] = self.configHash
        return value


def encode_frame(envelope: ProtocolEnvelopeV1) -> bytes:
    data = json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("protocol frame exceeds 1 MiB")
    return data + b"\n"


def decode_frame(data: bytes, *, runtime_id: str | None = None, owner: str | None = None) -> ProtocolEnvelopeV1:
    if not data:
        raise ProtocolError("protocol frame is empty")
    if data.endswith(b"\n"):
        data = data[:-1]
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("protocol frame exceeds 1 MiB")
    if b"\n" in data or b"\r" in data:
        raise ProtocolError("protocol frame contains an embedded line break")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("protocol frame is not UTF-8 JSON") from exc
    return ProtocolEnvelopeV1.from_dict(value, runtime_id=runtime_id, owner=owner)


def read_frames(stream: BinaryIO, *, runtime_id: str | None = None, owner: str | None = None) -> Iterator[ProtocolEnvelopeV1]:
    while True:
        frame = stream.readline(MAX_FRAME_BYTES + 2)
        if frame == b"":
            return
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            raise ProtocolError("protocol stream contains an overlong or unterminated frame")
        yield decode_frame(frame, runtime_id=runtime_id, owner=owner)


def write_frame(stream: BinaryIO, envelope: ProtocolEnvelopeV1) -> None:
    stream.write(encode_frame(envelope))
    stream.flush()


def validate_hello(envelope: ProtocolEnvelopeV1, *, expected_runtime_id: str, expected_owner: str, expected_python_version: str) -> None:
    if envelope.kind != "response" or envelope.method != "hello":
        raise ProtocolError("worker must answer hello first")
    if envelope.runtimeId != expected_runtime_id or envelope.owner != expected_owner:
        raise ProtocolError("worker hello identity mismatch")
    executable = envelope.payload.get("executable")
    python_version = envelope.payload.get("pythonVersion")
    if not isinstance(executable, str) or not executable:
        raise ProtocolError("worker hello has no executable")
    if python_version != expected_python_version:
        raise ProtocolError("worker hello Python version mismatch")
