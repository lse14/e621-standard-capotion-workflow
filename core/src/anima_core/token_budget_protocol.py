from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Literal, Mapping

from anima_caption_format import flat_txt_sha256, normalize_annotation
from anima_caption_format.normalizer import CaptionDisplayPolicy

from .worker_protocol import MAX_FRAME_BYTES


TRIMMABLE_FIELDS = ("quality", "environment", "tags", "appearance")
PROTECTED_FIELDS = ("count", "character", "series", "artist", "nl")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class TokenBudgetProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class TokenBudgetOutcomeV1:
    status: Literal["within_budget", "trimmed", "overflow", "failed"]
    original_tokens: int | None
    final_tokens: int | None
    removed: dict[str, list[str]] | None
    annotation: dict[str, object] | None
    flat_text_sha256: str | None
    failure_code: str | None = None


def _frame_bound(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TokenBudgetProtocolError("Token Budget payload is not strict JSON") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise TokenBudgetProtocolError("Token Budget payload exceeds its frame limit")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TokenBudgetProtocolError(f"{label} must be an object")
    return value


def _keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise TokenBudgetProtocolError("Token Budget outcome fields are invalid")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TokenBudgetProtocolError(f"{label} is invalid")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TokenBudgetProtocolError(f"{label} is invalid")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise TokenBudgetProtocolError(f"{label} is invalid")
    return value


def _caption_policy(value: Mapping[str, object]) -> CaptionDisplayPolicy:
    try:
        return CaptionDisplayPolicy.from_mapping(value)
    except ValueError as exc:
        raise TokenBudgetProtocolError("caption format is invalid") from exc


def _normalized_annotation(value: object, policy: CaptionDisplayPolicy) -> dict[str, object]:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TokenBudgetProtocolError("annotation is not strict JSON") from exc
    result = normalize_annotation(raw, policy, export_format="both")
    if not result.valid or result.payload is None:
        raise TokenBudgetProtocolError("annotation is not a valid flat-caption payload")
    return result.payload


def _removed(value: object) -> dict[str, list[str]]:
    item = _object(value, "removed")
    _keys(item, set(TRIMMABLE_FIELDS))
    result: dict[str, list[str]] = {}
    for field in TRIMMABLE_FIELDS:
        entries = item[field]
        if not isinstance(entries, list) or not all(isinstance(entry, str) and entry for entry in entries):
            raise TokenBudgetProtocolError("removed audit is invalid")
        result[field] = list(entries)
    return result


def _validate_tail_audit(
    original: Mapping[str, object],
    final: Mapping[str, object],
    removed: Mapping[str, list[str]],
) -> None:
    for field in PROTECTED_FIELDS:
        if original[field] != final[field]:
            raise TokenBudgetProtocolError("Token Budget changed a protected field")
    changed_later = False
    for field in reversed(TRIMMABLE_FIELDS):
        original_values = original[field]
        final_values = final[field]
        if not isinstance(original_values, list) or not isinstance(final_values, list):
            raise TokenBudgetProtocolError("normalized array fields are invalid")
        if final_values != original_values[:len(final_values)] or removed[field] != original_values[len(final_values):]:
            raise TokenBudgetProtocolError("Token Budget removal audit is not an exact tail deletion")
        if changed_later and final_values:
            raise TokenBudgetProtocolError("Token Budget removal order is invalid")
        changed_later = changed_later or bool(removed[field])


def validate_token_budget_outcome(
    value: object,
    *,
    expected_sample_id: int,
    expected_lease_id: str,
    original_annotation: object,
    caption_format: Mapping[str, object],
    max_tokens: int,
) -> TokenBudgetOutcomeV1:
    """Validate untrusted worker output before an overlay transaction exists."""
    _integer(expected_sample_id, "expected sampleId", minimum=1)
    _identifier(expected_lease_id, "expected leaseId")
    _integer(max_tokens, "maxTokens", minimum=1)
    _frame_bound(value)
    item = _object(value, "Token Budget outcome")
    status = item.get("status")
    if status not in {"within_budget", "trimmed", "overflow", "failed"}:
        raise TokenBudgetProtocolError("Token Budget outcome status is invalid")
    if status == "failed":
        _keys(item, {"schemaVersion", "payloadType", "sampleId", "leaseId", "status", "code"})
        if item["schemaVersion"] != 1 or item["payloadType"] != "token_budget_outcome":
            raise TokenBudgetProtocolError("Token Budget failure identity is invalid")
        if _integer(item["sampleId"], "sampleId", minimum=1) != expected_sample_id or _identifier(item["leaseId"], "leaseId") != expected_lease_id:
            raise TokenBudgetProtocolError("Token Budget failure identity does not match the lease")
        return TokenBudgetOutcomeV1("failed", None, None, None, None, None, _identifier(item["code"], "code"))

    common = {"schemaVersion", "payloadType", "sampleId", "leaseId", "status", "originalTokens", "finalTokens", "removed"}
    expected = common | ({"annotation", "flatTextSha256"} if status != "overflow" else set())
    _keys(item, expected)
    if item["schemaVersion"] != 1 or item["payloadType"] != "token_budget_outcome":
        raise TokenBudgetProtocolError("Token Budget outcome identity is invalid")
    if _integer(item["sampleId"], "sampleId", minimum=1) != expected_sample_id or _identifier(item["leaseId"], "leaseId") != expected_lease_id:
        raise TokenBudgetProtocolError("Token Budget outcome identity does not match the lease")
    original_tokens = _integer(item["originalTokens"], "originalTokens")
    final_tokens = _integer(item["finalTokens"], "finalTokens")
    removed = _removed(item["removed"])
    policy = _caption_policy(caption_format)
    original = _normalized_annotation(original_annotation, policy)
    if status == "overflow":
        if original_tokens <= max_tokens or final_tokens <= max_tokens:
            raise TokenBudgetProtocolError("overflow token counts are invalid")
        if any(removed[field] != original[field] for field in TRIMMABLE_FIELDS):
            raise TokenBudgetProtocolError("overflow must report every trimmable value as removed")
        return TokenBudgetOutcomeV1("overflow", original_tokens, final_tokens, removed, None, None)

    final = _normalized_annotation(item["annotation"], policy)
    digest = _sha256(item["flatTextSha256"], "flatTextSha256")
    if digest != flat_txt_sha256(final, policy):
        raise TokenBudgetProtocolError("flatTextSha256 does not describe the final annotation")
    _validate_tail_audit(original, final, removed)
    if status == "within_budget":
        if original_tokens != final_tokens or final_tokens > max_tokens or any(removed.values()) or final != original:
            raise TokenBudgetProtocolError("within_budget outcome is invalid")
    elif original_tokens <= max_tokens or final_tokens > max_tokens or not any(removed.values()):
        raise TokenBudgetProtocolError("trimmed outcome is invalid")
    return TokenBudgetOutcomeV1(status, original_tokens, final_tokens, removed, final, digest)
