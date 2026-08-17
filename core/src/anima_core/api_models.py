from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .ocr_runtime_binding import OcrExecutionRequestV1, normalize_ocr_execution


class _ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint: str
    model: str
    backupModel: str | None = None
    apiCredentialRef: str
    systemPrompt: str
    apiPolicy: dict[str, object]


class _SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    secret: str


class _NlPromptPresetBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    type: Literal["general", "style", "character"] = "general"
    promptText: str | None = None
    basePrompt: str | None = None

    @model_validator(mode="after")
    def _require_prompt_text(self) -> "_NlPromptPresetBody":
        if self.promptText is None and self.basePrompt is None:
            raise ValueError("promptText is required")
        if self.promptText is not None and self.basePrompt is not None and self.promptText != self.basePrompt:
            raise ValueError("promptText and basePrompt must match")
        return self


class _NlDiagnosticCredentialsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint: str
    apiCredentialRef: str
    apiKey: SecretStr | None = None


class _NlModelDiscoveryBody(_NlDiagnosticCredentialsBody):
    pass


class _NlTestMessageBody(_NlDiagnosticCredentialsBody):
    model: str
    basePrompt: str


class _AmountBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount: int = Field(ge=1, le=1_000_000)


class _ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: bool


class _NlManualRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sampleId: int | None = Field(default=None, ge=1)
    issueId: str | None = Field(default=None, min_length=1, max_length=128)
    confirmed: bool

    @model_validator(mode="after")
    def _require_one_selector(self) -> "_NlManualRetryBody":
        if (self.sampleId is None) == (self.issueId is None):
            raise ValueError("manual NL retry requires exactly one sampleId or issueId")
        return self


class _NlManualRetryBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issueIds: list[str] = Field(min_length=1, max_length=1_000)
    confirmed: bool

    @model_validator(mode="after")
    def _require_unique_issue_ids(self) -> "_NlManualRetryBatchBody":
        if any(not 1 <= len(issue_id) <= 128 for issue_id in self.issueIds) or len(set(self.issueIds)) != len(self.issueIds):
            raise ValueError("manual NL retry requires unique issue IDs")
        return self


class _NlManualWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sampleId: int | None = Field(default=None, ge=1)
    issueId: str | None = Field(default=None, min_length=1, max_length=128)
    nl: str = Field(max_length=16_384)
    confirmed: bool

    @model_validator(mode="after")
    def _require_one_selector(self) -> "_NlManualWriteBody":
        if (self.sampleId is None) == (self.issueId is None):
            raise ValueError("manual NL write requires exactly one sampleId or issueId")
        return self


class _ShutdownBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str


class _SelectPathBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    purpose: Literal["source_dataset", "output_dataset", "replacement_csv"]
    currentPath: str | None = Field(default=None, max_length=32_767)


@dataclass(frozen=True)
class SelectPathRequest:
    purpose: Literal["source_dataset", "output_dataset", "replacement_csv"]
    current_path: str | None


def parse_select_path_body(value: object) -> SelectPathRequest:
    body = _SelectPathBody.model_validate(value)
    return SelectPathRequest(body.purpose, body.currentPath)


class _PreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    config: dict[str, object]
    ocrExecution: dict[str, object] | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_bare_config(cls, value: object) -> object:
        if isinstance(value, dict) and "config" not in value:
            return {"config": value}
        return value


@dataclass(frozen=True)
class CreateJobRequest:
    config: dict[str, object]
    ocrExecution: OcrExecutionRequestV1


def parse_create_job_body(value: object) -> CreateJobRequest:
    body = _PreflightBody.model_validate(value)
    return CreateJobRequest(dict(body.config), normalize_ocr_execution(body.ocrExecution))


class _WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: bool
    confirmedRebuild: bool = False


class _PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pinned: bool


class _CountReviewDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expectedVersion: int = Field(ge=1)
    source: Literal["classify", "vlm", "manual"]
    count: Literal["solo", "duo", "trio", "group"] | None = None


class _CountReviewBatchItem(_CountReviewDecisionBody):
    sampleId: int = Field(ge=1)


class _CountReviewBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    updates: list[_CountReviewBatchItem] = Field(min_length=1, max_length=500)


class _TokenBudgetRecountBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sampleId: int = Field(ge=1)
    expectedVersion: int = Field(ge=1)
    annotation: dict[str, object]


class _TokenBudgetRewriteShortBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sampleIds: list[int] = Field(min_length=1, max_length=500)
    expectedVersions: dict[str, int]


class _TokenBudgetApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sampleId: int = Field(ge=1)
    expectedVersion: int = Field(ge=1)
