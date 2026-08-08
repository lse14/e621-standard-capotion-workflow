from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .classify_protocol import ClassifyCountDecisionV1
from .contracts import canonical_json


FINAL_COUNT_VALUES = frozenset({"solo", "duo", "trio", "group"})
EVIDENCE_COUNT_VALUES = frozenset({"", *FINAL_COUNT_VALUES})
OBSERVATION_COUNT_VALUES = frozenset({*FINAL_COUNT_VALUES, "unknown"})
OBSERVATION_LAYOUT_VALUES = frozenset({
    "single_scene", "multi_view", "character_sheet", "multi_panel", "unknown",
})
CLASSIFY_REVIEW_WARNING_CODES = (
    "count_source_conflict",
    "original_count_invalid",
    "count_character_lower_bound",
    "count_relationship_lower_bound",
    "count_conflict",
    "wiki_missing",
)
OBSERVATION_WARNING_CODES = (
    "count_observation_invalid",
    "count_observation_unknown",
)
COUNT_REVIEW_REASON_CODES = (
    *CLASSIFY_REVIEW_WARNING_CODES,
    "count_observation_mismatch",
    "count_observation_invalid",
    "count_observation_unknown",
)


class CountReviewProtocolError(ValueError):
    pass


def classify_review_warning_codes(decision: ClassifyCountDecisionV1) -> tuple[str, ...]:
    reported = set(decision.issueCodes)
    for warning in decision.warnings:
        code = warning.split(":", 1)[0]
        reported.add("count_conflict" if code in {"count_lower_bound", "count_non_decisive"} else code)
    return tuple(code for code in CLASSIFY_REVIEW_WARNING_CODES if code in reported)


@dataclass(frozen=True)
class CountEvidenceV1:
    value: str
    decision: ClassifyCountDecisionV1
    reviewWarningCodes: tuple[str, ...]
    schemaVersion: int = 1

    @classmethod
    def from_decision(cls, decision: ClassifyCountDecisionV1) -> "CountEvidenceV1":
        # Reparse the canonical projection so no unchecked worker object enters SQLite.
        parsed = ClassifyCountDecisionV1.from_dict(decision.to_dict())
        return cls(
            value=parsed.value,
            decision=parsed,
            reviewWarningCodes=classify_review_warning_codes(parsed),
        )

    @classmethod
    def from_dict(cls, value: object) -> "CountEvidenceV1":
        if not isinstance(value, Mapping) or set(value) != {
            "schemaVersion", "value", "decision", "reviewWarningCodes",
        }:
            raise CountReviewProtocolError("count evidence fields are invalid")
        if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
            raise CountReviewProtocolError("count evidence schemaVersion is invalid")
        decision = ClassifyCountDecisionV1.from_dict(value["decision"])
        if value["value"] not in EVIDENCE_COUNT_VALUES or value["value"] != decision.value:
            raise CountReviewProtocolError("count evidence value is invalid")
        warning_codes = value["reviewWarningCodes"]
        if (
            not isinstance(warning_codes, list)
            or any(not isinstance(code, str) for code in warning_codes)
            or tuple(warning_codes) != classify_review_warning_codes(decision)
        ):
            raise CountReviewProtocolError("count evidence review warnings are invalid")
        return cls(
            value=str(value["value"]),
            decision=decision,
            reviewWarningCodes=tuple(warning_codes),
        )

    @property
    def decision_json(self) -> str:
        return canonical_json(self.decision.to_dict())

    @property
    def review_warning_codes_json(self) -> str:
        return canonical_json(list(self.reviewWarningCodes))

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "value": self.value,
            "decision": self.decision.to_dict(),
            "reviewWarningCodes": list(self.reviewWarningCodes),
        }


@dataclass(frozen=True)
class CountObservationV1:
    status: str
    countValue: str | None
    layoutValue: str | None
    sameCharacterRepeated: bool | None
    warningCodes: tuple[str, ...]
    notRequestedReason: str | None
    schemaVersion: int = 1

    @classmethod
    def from_dict(cls, value: object) -> "CountObservationV1":
        if not isinstance(value, Mapping) or set(value) != {
            "schemaVersion", "status", "countValue", "layoutValue",
            "sameCharacterRepeated", "warningCodes", "notRequestedReason",
        }:
            raise CountReviewProtocolError("count observation fields are invalid")
        if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
            raise CountReviewProtocolError("count observation schemaVersion is invalid")
        status = value["status"]
        count_value = value["countValue"]
        layout_value = value["layoutValue"]
        repeated = value["sameCharacterRepeated"]
        warning_codes = value["warningCodes"]
        reason = value["notRequestedReason"]
        if status not in {"observed", "invalid", "not_requested"}:
            raise CountReviewProtocolError("count observation status is invalid")
        if count_value is not None and count_value not in OBSERVATION_COUNT_VALUES:
            raise CountReviewProtocolError("count observation count is invalid")
        if layout_value is not None and layout_value not in OBSERVATION_LAYOUT_VALUES:
            raise CountReviewProtocolError("count observation layout is invalid")
        if repeated is not None and type(repeated) is not bool:
            raise CountReviewProtocolError("count observation repeated flag is invalid")
        if (
            not isinstance(warning_codes, list)
            or len(warning_codes) > len(OBSERVATION_WARNING_CODES)
            or any(code not in OBSERVATION_WARNING_CODES for code in warning_codes)
            or len(set(warning_codes)) != len(warning_codes)
        ):
            raise CountReviewProtocolError("count observation warnings are invalid")
        expected_warnings: tuple[str, ...]
        if status == "observed":
            if count_value is None or layout_value is None or repeated is None or reason is not None:
                raise CountReviewProtocolError("observed count fields are incomplete")
            expected_warnings = ("count_observation_unknown",) if count_value == "unknown" else ()
        elif status == "invalid":
            if reason is not None:
                raise CountReviewProtocolError("invalid observation cannot have a not-requested reason")
            expected_warnings = ("count_observation_invalid",)
        else:
            if (
                count_value is not None or layout_value is not None or repeated is not None
                or not isinstance(reason, str) or not reason or "\x00" in reason
                or len(reason.encode("utf-8")) > 128
            ):
                raise CountReviewProtocolError("not-requested observation fields are invalid")
            expected_warnings = ()
        if tuple(warning_codes) != expected_warnings:
            raise CountReviewProtocolError("count observation warnings do not match its status")
        return cls(
            status=str(status),
            countValue=count_value if isinstance(count_value, str) else None,
            layoutValue=layout_value if isinstance(layout_value, str) else None,
            sameCharacterRepeated=repeated if type(repeated) is bool else None,
            warningCodes=tuple(warning_codes),
            notRequestedReason=reason if isinstance(reason, str) else None,
        )

    @classmethod
    def not_requested(cls, reason: str) -> "CountObservationV1":
        return cls.from_dict({
            "schemaVersion": 1,
            "status": "not_requested",
            "countValue": None,
            "layoutValue": None,
            "sameCharacterRepeated": None,
            "warningCodes": [],
            "notRequestedReason": reason,
        })

    @property
    def warning_codes_json(self) -> str:
        return canonical_json(list(self.warningCodes))

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "status": self.status,
            "countValue": self.countValue,
            "layoutValue": self.layoutValue,
            "sameCharacterRepeated": self.sameCharacterRepeated,
            "warningCodes": list(self.warningCodes),
            "notRequestedReason": self.notRequestedReason,
        }


@dataclass(frozen=True)
class InitialCountReviewDecisionV1:
    status: str
    finalCount: str | None
    selectedSource: str | None
    reviewReasons: tuple[str, ...]
    schemaVersion: int = 1

    @classmethod
    def from_dict(cls, value: object) -> "InitialCountReviewDecisionV1":
        if not isinstance(value, Mapping) or set(value) != {
            "schemaVersion", "status", "finalCount", "selectedSource", "reviewReasons",
        }:
            raise CountReviewProtocolError("initial count review decision fields are invalid")
        if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
            raise CountReviewProtocolError("initial count review decision schemaVersion is invalid")
        status = value["status"]
        final_count = value["finalCount"]
        source = value["selectedSource"]
        reasons = value["reviewReasons"]
        if (
            not isinstance(reasons, list)
            or any(reason not in COUNT_REVIEW_REASON_CODES for reason in reasons)
            or tuple(reasons) != tuple(reason for reason in COUNT_REVIEW_REASON_CODES if reason in reasons)
        ):
            raise CountReviewProtocolError("count review reasons are invalid or unordered")
        if status == "pending":
            if final_count is not None or source is not None or not reasons:
                raise CountReviewProtocolError("pending count review decision is invalid")
        elif status == "auto_resolved":
            if final_count not in FINAL_COUNT_VALUES or source not in {"consensus", "classify", "vlm"} or reasons:
                raise CountReviewProtocolError("automatic count review decision is invalid")
        else:
            raise CountReviewProtocolError("initial count review status is invalid")
        return cls(
            status=str(status),
            finalCount=final_count if isinstance(final_count, str) else None,
            selectedSource=source if isinstance(source, str) else None,
            reviewReasons=tuple(reasons),
        )

    @property
    def review_reasons_json(self) -> str:
        return canonical_json(list(self.reviewReasons))

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "status": self.status,
            "finalCount": self.finalCount,
            "selectedSource": self.selectedSource,
            "reviewReasons": list(self.reviewReasons),
        }


def initial_count_review_decision(
    evidence: CountEvidenceV1,
    observation: CountObservationV1,
) -> InitialCountReviewDecisionV1:
    classify_count = evidence.value if evidence.value in FINAL_COUNT_VALUES else None
    vlm_count = (
        observation.countValue
        if observation.status == "observed" and observation.countValue in FINAL_COUNT_VALUES
        else None
    )
    reason_set = set(evidence.reviewWarningCodes)
    if observation.status == "invalid":
        reason_set.add("count_observation_invalid")
    elif observation.status == "observed" and observation.countValue == "unknown":
        reason_set.add("count_observation_unknown")
    elif observation.status == "not_requested" and classify_count is None:
        reason_set.add("count_observation_unknown")
    if classify_count is not None and vlm_count is not None and classify_count != vlm_count:
        reason_set.add("count_observation_mismatch")
    reasons = tuple(reason for reason in COUNT_REVIEW_REASON_CODES if reason in reason_set)
    if reasons:
        return InitialCountReviewDecisionV1.from_dict({
            "schemaVersion": 1,
            "status": "pending",
            "finalCount": None,
            "selectedSource": None,
            "reviewReasons": list(reasons),
        })
    if classify_count is not None and vlm_count is not None:
        final_count, source = classify_count, "consensus"
    elif classify_count is not None:
        final_count, source = classify_count, "classify"
    elif vlm_count is not None:
        final_count, source = vlm_count, "vlm"
    else:
        raise CountReviewProtocolError("count review inputs cannot produce a decision")
    return InitialCountReviewDecisionV1.from_dict({
        "schemaVersion": 1,
        "status": "auto_resolved",
        "finalCount": final_count,
        "selectedSource": source,
        "reviewReasons": [],
    })
