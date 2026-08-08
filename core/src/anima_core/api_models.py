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
    basePrompt: str


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


class _ShutdownBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str


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
