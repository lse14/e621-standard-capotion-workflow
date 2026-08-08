"""Prepared annotation and review records owned by the Token Budget Core module."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from anima_caption_format import flat_txt_sha256
from anima_caption_format.normalizer import CaptionDisplayPolicy

from .classify_overlay import ClassifyJsonError, parse_annotation_json, serialize_annotation_json
from .contracts import canonical_json
from .db import StateDatabase
from .overlay import BaselineView, OverlayError, OverlayLayout, WorkingAnnotationView
from .path_safety import PathSafetyError, safe_relative_path, sha256_file, windows_paths_equal
from .token_budget_protocol import TokenBudgetOutcomeV1


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset({"within_budget", "trimmed"})


class TokenBudgetOverlayError(OverlayError):
    pass


def _annotation_relative_path(layout: OverlayLayout, annotation_key: str) -> str:
    target = layout.annotation_path(annotation_key, ".json")
    return safe_relative_path(str(target.relative_to(layout.root)).replace("/", "\\"))


@dataclass(frozen=True)
class TokenBudgetRecord:
    sample_id: int
    lease_id: str
    status: str
    original_tokens: int
    final_tokens: int
    removed: dict[str, list[str]]
    flat_text_sha256: str
    annotation_relative_path: str
    max_tokens: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sampleId": self.sample_id,
            "leaseId": self.lease_id,
            "status": self.status,
            "originalTokens": self.original_tokens,
            "finalTokens": self.final_tokens,
            "removed": self.removed,
            "flatTextSha256": self.flat_text_sha256,
            "annotationRelativePath": self.annotation_relative_path,
            "maxTokens": self.max_tokens,
        }


@dataclass(frozen=True)
class TokenBudgetOverlayWriter:
    database: StateDatabase
    layout: OverlayLayout
    view: WorkingAnnotationView
    job_id: str

    def __post_init__(self) -> None:
        job = self.database.get_job(self.job_id)
        if self.layout.job_id != self.job_id or not windows_paths_equal(self.layout.dataset_root, str(job["dataset_root"])):
            raise TokenBudgetOverlayError("Token Budget overlay does not match the immutable job")
        if not isinstance(job["overlay_root"], str) or not windows_paths_equal(self.layout.root, str(job["overlay_root"])):
            raise TokenBudgetOverlayError("Token Budget overlay root does not match the persisted job")

    @classmethod
    def open_for_job(cls, database: StateDatabase, job_id: str) -> "TokenBudgetOverlayWriter":
        job = database.get_job(job_id)
        root = job["overlay_root"]
        if not isinstance(root, str) or not root:
            raise TokenBudgetOverlayError("Token Budget job has no annotation overlay")
        layout = OverlayLayout.open_existing(root, job_id)
        return cls(database, layout, WorkingAnnotationView(layout=layout, baseline=BaselineView(layout.dataset_root)), job_id)

    def _record_relative(self, sample_id: int) -> str:
        if type(sample_id) is not int or sample_id < 1:
            raise TokenBudgetOverlayError("Token Budget sample identity is invalid")
        return f"token-budget\\records\\{sample_id}.json"

    def _prepared_record_relative(self, lease_id: str) -> str:
        if not isinstance(lease_id, str) or not lease_id:
            raise TokenBudgetOverlayError("Token Budget lease identity is invalid")
        return f"token-budget\\prepared\\{lease_id}.json"

    def _write_record(self, relative: str, value: Mapping[str, object]) -> Path:
        return self.layout.write_resource(relative, (canonical_json(dict(value)) + "\n").encode("utf-8"))

    def _parse_record(self, path: Path, *, sample_id: int | None = None, lease_id: str | None = None) -> TokenBudgetRecord:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TokenBudgetOverlayError("Token Budget record is unreadable") from exc
        expected = {"schemaVersion", "sampleId", "leaseId", "status", "originalTokens", "finalTokens", "removed", "flatTextSha256", "annotationRelativePath", "maxTokens"}
        if not isinstance(value, dict) or set(value) != expected or value.get("schemaVersion") != 1:
            raise TokenBudgetOverlayError("Token Budget record fields are invalid")
        record_sample = value.get("sampleId")
        record_lease = value.get("leaseId")
        status = value.get("status")
        original = value.get("originalTokens")
        final = value.get("finalTokens")
        max_tokens = value.get("maxTokens")
        digest = value.get("flatTextSha256")
        relative = value.get("annotationRelativePath")
        removed = value.get("removed")
        if (
            type(record_sample) is not int or record_sample < 1 or not isinstance(record_lease, str) or not record_lease
            or status not in _STATUSES or type(original) is not int or original < 0 or type(final) is not int or final < 0
            or type(max_tokens) is not int or max_tokens < 1 or final > max_tokens
            or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            or not isinstance(relative, str) or not isinstance(removed, dict)
        ):
            raise TokenBudgetOverlayError("Token Budget record values are invalid")
        try:
            safe = safe_relative_path(relative)
        except PathSafetyError as exc:
            raise TokenBudgetOverlayError("Token Budget annotation record path is unsafe") from exc
        if not safe.startswith("annotations\\"):
            raise TokenBudgetOverlayError("Token Budget annotation record path is invalid")
        fields = ("quality", "environment", "tags", "appearance")
        if set(removed) != set(fields) or not all(isinstance(removed[field], list) and all(isinstance(entry, str) and entry for entry in removed[field]) for field in fields):
            raise TokenBudgetOverlayError("Token Budget removal record is invalid")
        if sample_id is not None and record_sample != sample_id:
            raise TokenBudgetOverlayError("Token Budget record sample identity is invalid")
        if lease_id is not None and record_lease != lease_id:
            raise TokenBudgetOverlayError("Token Budget record lease identity is invalid")
        return TokenBudgetRecord(record_sample, record_lease, status, original, final, {field: list(removed[field]) for field in fields}, digest, safe, max_tokens)

    def prepare_and_commit(self, *, sample_id: int, lease_id: str, annotation_key: str, outcome: TokenBudgetOutcomeV1, caption_format: Mapping[str, object], max_tokens: int) -> TokenBudgetRecord:
        if outcome.status not in _STATUSES or outcome.annotation is None or outcome.flat_text_sha256 is None or outcome.removed is None or outcome.original_tokens is None or outcome.final_tokens is None:
            raise TokenBudgetOverlayError("Token Budget outcome is not committable")
        if outcome.final_tokens > max_tokens:
            raise TokenBudgetOverlayError("Token Budget outcome exceeds the frozen limit")
        try:
            policy = CaptionDisplayPolicy.from_mapping(caption_format)
            annotation = parse_annotation_json(serialize_annotation_json(outcome.annotation))
        except (ValueError, ClassifyJsonError) as exc:
            raise TokenBudgetOverlayError("Token Budget output annotation is invalid") from exc
        if annotation is None or flat_txt_sha256(annotation, policy) != outcome.flat_text_sha256:
            raise TokenBudgetOverlayError("Token Budget output hash is invalid")
        prepared, digest = self.layout.write_prepared("token_budget", lease_id, ".json", serialize_annotation_json(annotation))
        prepared_relative = safe_relative_path(str(prepared.relative_to(self.layout.root)).replace("/", "\\"))
        record = TokenBudgetRecord(sample_id, lease_id, outcome.status, outcome.original_tokens, outcome.final_tokens, outcome.removed, outcome.flat_text_sha256, _annotation_relative_path(self.layout, annotation_key), max_tokens)
        # The durable record is written before SQLite reaches prepared state so recovery can prove the annotation transaction.
        self._write_record(self._record_relative(sample_id), record.to_dict())
        self._write_record(self._prepared_record_relative(lease_id), record.to_dict())
        self.database.stage_prepared_artifact(self.job_id, sample_id, lease_id=lease_id, relative_path=prepared_relative, sha256=digest)
        self.layout.commit_prepared(prepared_relative, digest, annotation_key, ".json")
        return record

    def write_overflow_review(self, *, sample_id: int, lease_id: str, original_tokens: int, final_tokens: int, removed: Mapping[str, list[str]], max_tokens: int, resource_id: str, resource_fingerprint: str) -> None:
        fields = ("quality", "environment", "tags", "appearance")
        if type(sample_id) is not int or sample_id < 1 or not isinstance(lease_id, str) or not lease_id or type(original_tokens) is not int or type(final_tokens) is not int or type(max_tokens) is not int or final_tokens <= max_tokens or set(removed) != set(fields) or not isinstance(resource_id, str) or not resource_id or not isinstance(resource_fingerprint, str) or _SHA256.fullmatch(resource_fingerprint) is None:
            raise TokenBudgetOverlayError("Token Budget overflow review is invalid")
        value = {"schemaVersion": 1, "sampleId": sample_id, "leaseId": lease_id, "status": "overflow", "originalTokens": original_tokens, "finalTokens": final_tokens, "removed": {field: list(removed[field]) for field in fields}, "maxTokens": max_tokens, "resourceId": resource_id, "resourceFingerprint": resource_fingerprint}
        self._write_record(f"token-budget\\reviews\\{sample_id}.json", value)

    def record_for_export(self, *, sample_id: int, annotation_key: str, caption_format: Mapping[str, object], max_tokens: int) -> TokenBudgetRecord:
        path = self.layout.resource_path(self._record_relative(sample_id))
        record = self._parse_record(path, sample_id=sample_id)
        expected_path = _annotation_relative_path(self.layout, annotation_key)
        if record.annotation_relative_path != expected_path or record.max_tokens != max_tokens:
            raise TokenBudgetOverlayError("Token Budget record identity does not match Export")
        try:
            policy = CaptionDisplayPolicy.from_mapping(caption_format)
            annotation = parse_annotation_json(self.view.read(annotation_key, ".json"))
        except (ValueError, ClassifyJsonError) as exc:
            raise TokenBudgetOverlayError("Token Budget working annotation is invalid") from exc
        if annotation is None or flat_txt_sha256(annotation, policy) != record.flat_text_sha256:
            raise TokenBudgetOverlayError("Token Budget record hash does not match working annotation")
        return record

    def recover_prepared(self, job_id: str, sample_id: int, prepared_relative_path: str, expected_sha256: str) -> bool:
        if job_id != self.job_id or _SHA256.fullmatch(expected_sha256) is None:
            return False
        try:
            row = self.database.get_sample_with_state(job_id, sample_id)
            lease_id = row["lease_id"]
            if row["current_module_id"] != "token_budget" or row["status"] != "prepared" or not isinstance(lease_id, str):
                return False
            expected_relative = f"prepared\\token_budget\\{lease_id}.json"
            if safe_relative_path(prepared_relative_path) != expected_relative or row["prepared_artifact_relative_path"] != prepared_relative_path or row["prepared_artifact_sha256"] != expected_sha256:
                return False
            record = self._parse_record(self.layout.resource_path(self._prepared_record_relative(lease_id)), sample_id=sample_id, lease_id=lease_id)
            if record.annotation_relative_path != _annotation_relative_path(self.layout, str(row["annotation_key"])):
                return False
            target = self.layout.annotation_path(str(row["annotation_key"]), ".json")
            prepared = self.layout.resolve_prepared(prepared_relative_path)
            if target.is_file() and sha256_file(target) == expected_sha256:
                if prepared.is_file() and sha256_file(prepared) == expected_sha256:
                    prepared.unlink()
                return True
            if not prepared.is_file() or sha256_file(prepared) != expected_sha256:
                return False
            return sha256_file(self.layout.commit_prepared(prepared_relative_path, expected_sha256, str(row["annotation_key"]), ".json")) == expected_sha256
        except (KeyError, OSError, PathSafetyError, TokenBudgetOverlayError, ValueError):
            return False
