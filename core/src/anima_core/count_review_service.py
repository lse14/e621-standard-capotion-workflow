from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from .contracts import COUNT_REVIEW_SCHEMA_VERSIONS
from .count_review_protocol import (
    CLASSIFY_REVIEW_WARNING_CODES,
    COUNT_REVIEW_REASON_CODES,
    EVIDENCE_COUNT_VALUES,
    FINAL_COUNT_VALUES,
    CountEvidenceV1,
    CountObservationV1,
    initial_count_review_decision,
)
from .db import MAX_COUNT_PAGE_SIZE, StateDatabase


class CountReviewError(RuntimeError):
    pass


class CountReviewConflictError(ValueError):
    pass


@dataclass(frozen=True)
class CountReviewInitialization:
    targets: int
    decisions: int
    pending: int
    inserted: int


def _stored_json(value: object, *, field: str) -> object:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65_536:
        raise CountReviewError(f"stored {field} is invalid")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CountReviewError(f"stored {field} is invalid JSON") from exc


def _stored_ordered_codes(value: object, *, field: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    parsed = _stored_json(value, field=field)
    if (
        not isinstance(parsed, list)
        or any(not isinstance(code, str) for code in parsed)
        or tuple(parsed) != tuple(code for code in allowed if code in parsed)
    ):
        raise CountReviewError(f"stored {field} is invalid")
    return tuple(parsed)


def count_review_decision_response(row: Mapping[str, object]) -> dict[str, object]:
    keys = set(row.keys()) if hasattr(row, "keys") else set(row)
    status = row["status"] if "status" in keys else row["decision_status"]
    final_count = row["final_count"]
    source = row["selected_source"]
    version = row["version"]
    reasons = _stored_ordered_codes(
        row["review_reasons_json"], field="count review reasons", allowed=COUNT_REVIEW_REASON_CODES
    )
    if type(version) is not int or version < 1:
        raise CountReviewError("stored count review version is invalid")
    if status == "pending":
        if final_count is not None or source is not None or not reasons:
            raise CountReviewError("stored pending count review decision is invalid")
    elif status == "auto_resolved":
        if final_count not in FINAL_COUNT_VALUES or source not in {"consensus", "classify", "vlm"} or reasons:
            raise CountReviewError("stored automatic count review decision is invalid")
    elif status == "manual_resolved":
        if final_count not in FINAL_COUNT_VALUES or source not in {"classify", "vlm", "manual"}:
            raise CountReviewError("stored manual count review decision is invalid")
    else:
        raise CountReviewError("stored count review status is invalid")
    return {
        "status": status,
        "finalCount": final_count,
        "selectedSource": source,
        "reviewReasons": list(reasons),
        "version": version,
        "resolvedAt": row["resolved_at"],
        "appliedAt": row["applied_at"],
    }


def _evidence_from_row(row: Mapping[str, object]) -> CountEvidenceV1:
    if row["evidence_schema_version"] is None:
        raise CountReviewError("count evidence is missing for a review target")
    try:
        return CountEvidenceV1.from_dict({
            "schemaVersion": row["evidence_schema_version"],
            "value": row["evidence_value"],
            "decision": _stored_json(row["decision_json"], field="count decision"),
            "reviewWarningCodes": _stored_json(
                row["review_warning_codes_json"], field="count evidence warnings"
            ),
        })
    except (ValueError, TypeError, KeyError) as exc:
        raise CountReviewError("stored count evidence is invalid") from exc


def _observation_from_row(row: Mapping[str, object]) -> CountObservationV1:
    if row["observation_schema_version"] is None:
        raise CountReviewError("count observation is missing for a review target")
    repeated = row["same_character_repeated"]
    try:
        return CountObservationV1.from_dict({
            "schemaVersion": row["observation_schema_version"],
            "status": row["observation_status"],
            "countValue": row["observation_count_value"],
            "layoutValue": row["observation_layout_value"],
            "sameCharacterRepeated": None if repeated is None else bool(repeated),
            "warningCodes": _stored_json(
                row["observation_warning_codes_json"], field="count observation warnings"
            ),
            "notRequestedReason": row["not_requested_reason"],
        })
    except (ValueError, TypeError, KeyError) as exc:
        raise CountReviewError("stored count observation is invalid") from exc


class CountReviewService:
    def __init__(self, database: StateDatabase, job_id: str) -> None:
        self.database = database
        self.job_id = job_id

    def initialize(self) -> CountReviewInitialization:
        inserted = 0
        cursor: int | None = None
        while True:
            page = self.database.page_count_review_inputs(
                self.job_id, after_sample_id=cursor, limit=MAX_COUNT_PAGE_SIZE
            )
            if not page:
                break
            decisions = []
            for row in page:
                evidence = _evidence_from_row(row)
                observation = _observation_from_row(row)
                decisions.append((
                    int(row["sample_id"]),
                    initial_count_review_decision(evidence, observation),
                ))
            inserted += self.database.insert_initial_count_review_decisions(
                self.job_id, decisions
            )
            cursor = int(page[-1]["sample_id"])
        targets = self.database.count_current_review_targets(self.job_id)
        decision_count = self.database.count_current_review_decisions(self.job_id)
        if decision_count != targets:
            raise CountReviewError("count review initialization is incomplete")
        return CountReviewInitialization(
            targets=targets,
            decisions=decision_count,
            pending=self.database.count_current_review_decisions(
                self.job_id, status="pending"
            ),
            inserted=inserted,
        )

    def page(
        self,
        *,
        after_sample_id: int | None = None,
        status: str | None = None,
        reason: str | None = None,
        classify_count: str | None = None,
        vlm_count: str | None = None,
        mismatch_only: bool = False,
        limit: int = 200,
    ) -> dict[str, object]:
        job = self.database.get_job(self.job_id)
        if int(job["config_schema_version"]) not in COUNT_REVIEW_SCHEMA_VERSIONS:
            raise CountReviewError("count review is unavailable for this task")
        rows = self.database.page_count_review_items(
            self.job_id,
            after_sample_id=after_sample_id,
            status=status,
            reason=reason,
            classify_count=classify_count,
            vlm_count=vlm_count,
            mismatch_only=mismatch_only,
            limit=limit,
        )
        items: list[dict[str, object]] = []
        for row in rows:
            evidence_value = row["evidence_value"]
            if evidence_value not in EVIDENCE_COUNT_VALUES:
                raise CountReviewError("stored count evidence summary is invalid")
            evidence_warnings = _stored_ordered_codes(
                row["review_warning_codes_json"],
                field="count evidence warnings",
                allowed=CLASSIFY_REVIEW_WARNING_CODES,
            )
            repeated = row["same_character_repeated"]
            try:
                observation = CountObservationV1.from_dict({
                    "schemaVersion": row["observation_schema_version"],
                    "status": row["observation_status"],
                    "countValue": row["observation_count_value"],
                    "layoutValue": row["observation_layout_value"],
                    "sameCharacterRepeated": None if repeated is None else bool(repeated),
                    "warningCodes": _stored_json(
                        row["observation_warning_codes_json"], field="count observation warnings"
                    ),
                    "notRequestedReason": row["not_requested_reason"],
                })
            except (ValueError, TypeError, KeyError) as exc:
                raise CountReviewError("stored count observation summary is invalid") from exc
            items.append({
                "sampleId": int(row["sample_id"]),
                "relativeImagePath": str(row["relative_image_path"]),
                "classify": {
                    "count": evidence_value or None,
                    "warningCodes": list(evidence_warnings),
                },
                "vlm": {
                    "status": observation.status,
                    "count": observation.countValue,
                    "layout": observation.layoutValue,
                    "sameCharacterRepeated": observation.sameCharacterRepeated,
                    "warningCodes": list(observation.warningCodes),
                    "notRequestedReason": observation.notRequestedReason,
                },
                "decision": count_review_decision_response(row),
            })
        return {
            "items": items,
            "targetCount": self.database.count_current_review_targets(self.job_id),
            "pendingCount": self.database.count_current_review_decisions(self.job_id, status="pending"),
            "nextAfterSampleId": int(rows[-1]["sample_id"]) if len(rows) == limit else None,
        }

    def resolve(
        self,
        sample_id: int,
        *,
        expected_version: int,
        source: str,
        count: str | None = None,
    ) -> Mapping[str, object]:
        try:
            return self.database.update_count_review_decision(
                self.job_id,
                sample_id,
                expected_version=expected_version,
                source=source,
                count=count,
            )
        except ValueError as exc:
            if str(exc) == "count review decision version conflict":
                raise CountReviewConflictError(str(exc)) from exc
            raise

    def resolve_batch(
        self,
        updates: list[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        try:
            return list(self.database.update_count_review_decisions(self.job_id, updates))
        except ValueError as exc:
            if str(exc) == "count review decision version conflict":
                raise CountReviewConflictError(str(exc)) from exc
            raise

    def confirm(self, *, confirmed: bool, expected_config_hash: str) -> bool:
        if confirmed is not True:
            raise CountReviewError("explicit count review confirmation is required")
        return self.database.confirm_count_review(
            self.job_id, expected_config_hash=expected_config_hash
        )
