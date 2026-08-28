from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Mapping


SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
MAX_PROCESS_ITEMS = 500
TOKENIZER_IDS = frozenset({"tokenizer-qwen3-0.6b-anima-v1", "tokenizer-qwen3-vl-4b-krea2-v1"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TokenBudgetPayloadError(ValueError):
    pass


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TokenBudgetPayloadError(f"{label} must be an object")
    return value


def _keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise TokenBudgetPayloadError(f"{label} fields are invalid")


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TokenBudgetPayloadError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TokenBudgetPayloadError(f"{label} is invalid")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise TokenBudgetPayloadError(f"{label} is invalid")
    return value


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TokenBudgetPayloadError(f"{label} is invalid")
    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or any(part in {"", ".", ".."} or ":" in part for part in normalized.split("\\")):
        raise TokenBudgetPayloadError(f"{label} is invalid")
    return normalized


def ensure_frame_bound(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TokenBudgetPayloadError("Token Budget payload is not strict JSON") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise TokenBudgetPayloadError("Token Budget payload exceeds its frame limit")


@dataclass(frozen=True)
class TokenBudgetHelloRequest:
    job_id: str
    config_hash: str
    resource_id: str
    resource_manifest_relative_path: str
    resource_fingerprint: str
    context_limit: int
    max_tokens: int


@dataclass(frozen=True)
class TokenBudgetWorkItem:
    sample_id: int
    lease_id: str
    annotation: dict[str, object]


@dataclass(frozen=True)
class TokenBudgetProcessRequest:
    caption_format: dict[str, object]
    items: tuple[TokenBudgetWorkItem, ...]


def parse_hello(value: object) -> TokenBudgetHelloRequest:
    item = _object(value, "Token Budget hello")
    _keys(item, {"schemaVersion", "payloadType", "jobId", "configHash", "resourceId", "resourceManifestRelativePath", "resourceFingerprint", "contextLimit", "maxTokens"}, "Token Budget hello")
    if item["schemaVersion"] != SCHEMA_VERSION or item["payloadType"] != "token_budget_hello_request":
        raise TokenBudgetPayloadError("Token Budget hello identity is invalid")
    resource_id = item["resourceId"]
    if resource_id not in TOKENIZER_IDS:
        raise TokenBudgetPayloadError("Token Budget resourceId is invalid")
    context_limit = _integer(item["contextLimit"], "contextLimit", minimum=1)
    max_tokens = _integer(item["maxTokens"], "maxTokens", minimum=1)
    if max_tokens > context_limit:
        raise TokenBudgetPayloadError("maxTokens exceeds contextLimit")
    result = TokenBudgetHelloRequest(
        _identifier(item["jobId"], "jobId"), _sha256(item["configHash"], "configHash"), resource_id,
        _relative(item["resourceManifestRelativePath"], "resourceManifestRelativePath"),
        _sha256(item["resourceFingerprint"], "resourceFingerprint"), context_limit, max_tokens,
    )
    ensure_frame_bound(hello_to_dict(result))
    return result


def hello_to_dict(value: TokenBudgetHelloRequest) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION, "payloadType": "token_budget_hello_request", "jobId": value.job_id,
        "configHash": value.config_hash, "resourceId": value.resource_id,
        "resourceManifestRelativePath": value.resource_manifest_relative_path,
        "resourceFingerprint": value.resource_fingerprint, "contextLimit": value.context_limit, "maxTokens": value.max_tokens,
    }


def parse_process(value: object) -> TokenBudgetProcessRequest:
    item = _object(value, "Token Budget process request")
    _keys(item, {"schemaVersion", "payloadType", "captionFormat", "items"}, "Token Budget process request")
    values = item["items"]
    if item["schemaVersion"] != SCHEMA_VERSION or item["payloadType"] != "token_budget_process_request" or not isinstance(values, list) or not 1 <= len(values) <= MAX_PROCESS_ITEMS:
        raise TokenBudgetPayloadError("Token Budget process request is invalid")
    caption_format = _object(item["captionFormat"], "captionFormat")
    caption_fields = {"replaceUnderscoresWithSpaces", "preserveEscapes", "triggersEnabled", "triggerTerms"}
    if set(caption_format) not in (caption_fields, caption_fields | {"flatTxtLayout"}):
        raise TokenBudgetPayloadError("captionFormat fields are invalid")
    if "flatTxtLayout" in caption_format and caption_format["flatTxtLayout"] not in {"single_line", "nl_newline"}:
        raise TokenBudgetPayloadError("captionFormat.flatTxtLayout is invalid")
    if any(type(caption_format[name]) is not bool for name in ("replaceUnderscoresWithSpaces", "preserveEscapes", "triggersEnabled")) or not isinstance(caption_format["triggerTerms"], list) or not all(isinstance(term, str) for term in caption_format["triggerTerms"]):
        raise TokenBudgetPayloadError("captionFormat is invalid")
    parsed: list[TokenBudgetWorkItem] = []
    for raw in values:
        work = _object(raw, "Token Budget work item")
        _keys(work, {"schemaVersion", "sampleId", "leaseId", "annotation"}, "Token Budget work item")
        if work["schemaVersion"] != SCHEMA_VERSION:
            raise TokenBudgetPayloadError("Token Budget work item schemaVersion is invalid")
        parsed.append(TokenBudgetWorkItem(_integer(work["sampleId"], "sampleId", minimum=1), _identifier(work["leaseId"], "leaseId"), _object(work["annotation"], "annotation")))
    if len({(entry.sample_id, entry.lease_id) for entry in parsed}) != len(parsed):
        raise TokenBudgetPayloadError("Token Budget process request has duplicate identities")
    result = TokenBudgetProcessRequest(dict(caption_format), tuple(parsed))
    ensure_frame_bound(process_to_dict(result))
    return result


def process_to_dict(value: TokenBudgetProcessRequest) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION, "payloadType": "token_budget_process_request", "captionFormat": dict(value.caption_format),
        "items": [{"schemaVersion": SCHEMA_VERSION, "sampleId": item.sample_id, "leaseId": item.lease_id, "annotation": item.annotation} for item in value.items],
    }


def process_result(outcomes: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {"schemaVersion": SCHEMA_VERSION, "payloadType": "token_budget_process_result", "outcomes": outcomes}
    ensure_frame_bound(result)
    return result
