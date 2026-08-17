from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .api_context import ControlPlaneContext, bad_request, conflict, not_found
from .api_models import (
    _AmountBody,
    _ConfirmBody,
    _NlManualRetryBatchBody,
    _NlManualRetryBody,
    _NlManualWriteBody,
    _NlModelDiscoveryBody,
    _NlPromptPresetBody,
    _NlTestMessageBody,
    _ProfileBody,
    _SecretBody,
)
from .credentials import CredentialStoreError, DpapiCredentialStore
from .db import StateDatabase
from .nl_profiles import NlApiProfile, NlProfileError, default_nl_prompt
from .nl_prompt_presets import (
    NlPromptPreset,
    PromptPresetConflictError,
    PromptPresetNotFoundError,
    PromptPresetValidationError,
)
from .nl_manual_review import NlManualWriteService
from .repair import RepairPreparationError
from .pipeline_dispatch import PipelineError
from .scheduler import BoundedScheduler, SchedulerError


def _profile_response(profile: NlApiProfile, *, credentials: DpapiCredentialStore) -> dict[str, object]:
    # Loading verifies current-user availability without exposing the secret.
    try:
        credentials.load(profile.apiCredentialRef)
        has_credential = True
    except CredentialStoreError:
        has_credential = False
    return {
        "profileId": profile.profileId,
        "endpoint": profile.endpoint,
        "model": profile.model,
        "backupModel": profile.backupModel,
        "apiCredentialRef": profile.apiCredentialRef,
        "systemPrompt": profile.systemPrompt,
        "apiPolicy": profile.apiPolicy,
        "hasCredential": has_credential,
    }


def _preset_response(preset: NlPromptPreset, *, detail: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "presetId": preset.presetId,
        "name": preset.name,
        "type": preset.type,
        "builtIn": preset.builtIn,
        "sha256": preset.sha256,
        "sizeBytes": preset.sizeBytes,
    }
    if detail:
        # Keep basePrompt in the response for older local clients; both names
        # resolve to the same saved prompt source.
        result["promptText"] = preset.promptText
        result["basePrompt"] = preset.promptText
    return result


def _resolved_diagnostic_key(context: ControlPlaneContext, body: _NlModelDiscoveryBody | _NlTestMessageBody) -> str | None:
    transient = body.apiKey.get_secret_value().strip() if body.apiKey is not None else ""
    if transient:
        return transient
    try:
        return context.credential_store.load(body.apiCredentialRef)
    except CredentialStoreError:
        return None


def _credential_unavailable(*, test_message: bool) -> dict[str, object]:
    if test_message:
        return {
            "ok": False,
            "latencyMs": 0,
            "actualModel": None,
            "replyText": None,
            "usage": None,
            "errorCode": "credential_unavailable",
            "errorReason": "credential unavailable",
        }
    return {
        "ok": False,
        "latencyMs": 0,
        "models": [],
        "errorCode": "credential_unavailable",
        "errorReason": "credential unavailable",
    }


def build_nl_router(context: ControlPlaneContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/nl/default-prompt")
    def nl_default_prompt(
        promptVersion: str | None = None,
    ) -> dict[str, object]:
        """F25: the single source of truth behind the editor default and 'restore default'."""
        try:
            return default_nl_prompt(prompt_version=promptVersion or "nl-default-prompt-v2")
        except NlProfileError as exc:
            raise bad_request(exc) from exc

    @router.get("/api/nl/prompt-presets")
    def list_prompt_presets() -> dict[str, object]:
        try:
            return {"presets": list(context.prompt_preset_store.list_summaries())}
        except PromptPresetValidationError as exc:
            raise bad_request(exc) from exc

    @router.get("/api/nl/prompt-presets/{preset_id}")
    def get_prompt_preset(preset_id: str) -> dict[str, object]:
        try:
            return _preset_response(context.prompt_preset_store.get(preset_id), detail=True)
        except PromptPresetNotFoundError as exc:
            raise not_found(exc) from exc
        except PromptPresetValidationError as exc:
            raise bad_request(exc) from exc

    @router.post("/api/nl/prompt-presets")
    def create_prompt_preset(body: _NlPromptPresetBody) -> dict[str, object]:
        try:
            return _preset_response(
                context.prompt_preset_store.create(
                    name=body.name,
                    preset_type=body.type,
                    prompt_text=body.promptText,
                    base_prompt=body.basePrompt,
                ),
                detail=True,
            )
        except PromptPresetValidationError as exc:
            raise bad_request(exc) from exc

    @router.put("/api/nl/prompt-presets/{preset_id}")
    def update_prompt_preset(preset_id: str, body: _NlPromptPresetBody) -> dict[str, object]:
        try:
            return _preset_response(
                context.prompt_preset_store.update(
                    preset_id,
                    name=body.name,
                    preset_type=body.type,
                    prompt_text=body.promptText,
                    base_prompt=body.basePrompt,
                ),
                detail=True,
            )
        except PromptPresetConflictError as exc:
            raise conflict(exc) from exc
        except PromptPresetNotFoundError as exc:
            raise not_found(exc) from exc
        except PromptPresetValidationError as exc:
            raise bad_request(exc) from exc

    @router.post("/api/nl/prompt-presets/{preset_id}/reset")
    def reset_prompt_preset(preset_id: str) -> dict[str, object]:
        try:
            return _preset_response(context.prompt_preset_store.reset(preset_id), detail=True)
        except PromptPresetConflictError as exc:
            raise conflict(exc) from exc
        except PromptPresetNotFoundError as exc:
            raise not_found(exc) from exc
        except PromptPresetValidationError as exc:
            raise bad_request(exc) from exc

    @router.delete("/api/nl/prompt-presets/{preset_id}")
    def delete_prompt_preset(preset_id: str) -> dict[str, bool]:
        try:
            context.prompt_preset_store.delete(preset_id)
        except PromptPresetConflictError as exc:
            raise conflict(exc) from exc
        except PromptPresetNotFoundError as exc:
            raise not_found(exc) from exc
        except PromptPresetValidationError as exc:
            raise bad_request(exc) from exc
        return {"deleted": True}

    @router.post("/api/nl/diagnostics/models")
    def discover_nl_models(body: _NlModelDiscoveryBody) -> dict[str, object]:
        api_key = _resolved_diagnostic_key(context, body)
        if api_key is None:
            return _credential_unavailable(test_message=False)
        return context.nl_diagnostic_client.discover_models(endpoint=body.endpoint, api_key=api_key)

    @router.post("/api/nl/diagnostics/test-message")
    def test_nl_message(body: _NlTestMessageBody) -> dict[str, object]:
        api_key = _resolved_diagnostic_key(context, body)
        if api_key is None:
            return _credential_unavailable(test_message=True)
        return context.nl_diagnostic_client.test_message(
            endpoint=body.endpoint,
            model=body.model,
            api_key=api_key,
            base_prompt=body.basePrompt,
        )

    @router.get("/api/nl/profiles")
    def list_nl_profiles() -> dict[str, object]:
        try:
            profiles = context.profile_store.load_all()
        except NlProfileError as exc:
            raise bad_request(exc) from exc
        return {"profiles": [_profile_response(profile, credentials=context.credential_store) for profile in profiles]}

    @router.put("/api/nl/profiles/{profile_id}")
    def save_nl_profile(profile_id: str, body: _ProfileBody) -> dict[str, object]:
        try:
            profile = NlApiProfile.from_dict({"profileId": profile_id, **body.model_dump()})
            context.profile_store.save(profile)
            return _profile_response(profile, credentials=context.credential_store)
        except NlProfileError as exc:
            raise bad_request(exc) from exc

    @router.delete("/api/nl/profiles/{profile_id}")
    def delete_nl_profile(profile_id: str) -> dict[str, bool]:
        try:
            context.profile_store.delete(profile_id)
        except NlProfileError as exc:
            raise bad_request(exc) from exc
        return {"deleted": True}

    @router.put("/api/nl/credentials/{reference}")
    def save_nl_credential(reference: str, body: _SecretBody) -> dict[str, bool]:
        try:
            context.credential_store.save(reference, body.secret)
        except CredentialStoreError as exc:
            raise bad_request(exc) from exc
        return {"stored": True}

    @router.delete("/api/nl/credentials/{reference}")
    def delete_nl_credential(reference: str) -> dict[str, bool]:
        try:
            context.credential_store.delete(reference)
        except CredentialStoreError as exc:
            raise bad_request(exc) from exc
        return {"deleted": True}

    def nl_control(job_id: str, action: str, body: _AmountBody | _ConfirmBody | None = None) -> dict[str, Any]:
        if action in {"pause", "resume"}:
            try:
                status = (
                    context.pipeline_service.pause_module(job_id, "nl")
                    if action == "pause"
                    else context.pipeline_service.resume_module(job_id, "nl")
                )
                return {"status": status}
            except KeyError as exc:
                raise not_found(exc) from exc
            except PipelineError as exc:
                raise bad_request(exc) from exc
        database = StateDatabase.open(context.database_path)
        try:
            scheduler = BoundedScheduler(database)
            try:
                if action == "budget":
                    assert isinstance(body, _AmountBody)
                    revision = database.add_api_budget_extra(job_id, body.amount)
                    return {"apiBudgetExtra": int(database.get_job(job_id)["api_budget_extra"]), "apiBudgetRevision": revision}
                assert isinstance(body, _ConfirmBody)
                return {"requeued": scheduler.confirm_nl_api_outcome_unknown(job_id, confirmed=body.confirmed)}
            except (PipelineError, SchedulerError, ValueError) as exc:
                raise bad_request(exc) from exc
        finally:
            database.close()

    @router.post("/api/jobs/{job_id}/nl/pause")
    def pause_nl(job_id: str) -> dict[str, Any]:
        return nl_control(job_id, "pause")

    @router.post("/api/jobs/{job_id}/nl/resume")
    def resume_nl(job_id: str) -> dict[str, Any]:
        return nl_control(job_id, "resume")

    @router.post("/api/jobs/{job_id}/nl/api-budget")
    def add_nl_budget(job_id: str, body: _AmountBody) -> dict[str, Any]:
        return nl_control(job_id, "budget", body)

    @router.post("/api/jobs/{job_id}/nl/confirm-api-outcomes")
    def confirm_nl_outcomes(job_id: str, body: _ConfirmBody) -> dict[str, Any]:
        return nl_control(job_id, "confirm", body)

    @router.post("/api/jobs/{job_id}/nl/manual-retry")
    def manual_nl_retry(job_id: str, body: _NlManualRetryBody) -> dict[str, Any]:
        if not body.confirmed:
            raise bad_request(ValueError("manual NL retry requires explicit confirmation"))
        released_parent_lock = False
        try:
            released_parent_lock = context.preparation_service.release_lock_for_repair(job_id)
            result = context.repair_service.prepare_manual_nl(
                job_id, sample_id=body.sampleId, issue_id=body.issueId, confirmed=body.confirmed,
            )
            context.pipeline_service.start(result.repairJobId)
            return {
                "jobId": result.repairJobId,
                "parentJobId": result.parentJobId,
                "sampleId": body.sampleId,
                "issueId": body.issueId,
                "targetCount": result.targetCount,
                "started": True,
            }
        except KeyError as exc:
            raise not_found(exc) from exc
        except (RepairPreparationError, PipelineError, SchedulerError, ValueError) as exc:
            if released_parent_lock:
                context.preparation_service.restore_lock_after_repair_failure(job_id)
            raise bad_request(exc) from exc

    @router.post("/api/jobs/{job_id}/nl/manual-retry-batch")
    def manual_nl_retry_batch(job_id: str, body: _NlManualRetryBatchBody) -> dict[str, Any]:
        if not body.confirmed:
            raise bad_request(ValueError("manual NL retry requires explicit confirmation"))
        released_parent_lock = False
        try:
            released_parent_lock = context.preparation_service.release_lock_for_repair(job_id)
            result = context.repair_service.prepare_manual_nl_batch(
                job_id, issue_ids=body.issueIds, confirmed=body.confirmed,
            )
            context.pipeline_service.start(result.repairJobId)
            return {
                "jobId": result.repairJobId,
                "parentJobId": result.parentJobId,
                "issueIds": body.issueIds,
                "targetCount": result.targetCount,
                "started": True,
            }
        except KeyError as exc:
            raise not_found(exc) from exc
        except (RepairPreparationError, PipelineError, SchedulerError, ValueError) as exc:
            if released_parent_lock:
                context.preparation_service.restore_lock_after_repair_failure(job_id)
            raise bad_request(exc) from exc

    @router.post("/api/jobs/{job_id}/nl/manual-write")
    def manual_nl_write(job_id: str, body: _NlManualWriteBody) -> dict[str, Any]:
        if not body.confirmed:
            raise bad_request(ValueError("manual NL write requires explicit confirmation"))
        try:
            NlManualWriteService.validate_text(body.nl)
        except ValueError as exc:
            raise bad_request(exc) from exc
        released_parent_lock = False
        try:
            released_parent_lock = context.preparation_service.release_lock_for_repair(job_id)
            result = context.repair_service.prepare_manual_nl(
                job_id, sample_id=body.sampleId, issue_id=body.issueId, confirmed=body.confirmed, for_manual_write=True,
            )
            database = StateDatabase.open(context.database_path)
            try:
                target = database.connection.execute(
                    "SELECT sample_id FROM repair_targets WHERE repair_job_id=?", (result.repairJobId,),
                ).fetchone()
                if target is None:
                    raise RepairPreparationError("manual NL write repair target is missing")
                NlManualWriteService(database, result.repairJobId).seed(
                    sample_id=int(target["sample_id"]), issue_id=body.issueId, nl=body.nl, confirmed=body.confirmed,
                )
            finally:
                database.close()
            context.pipeline_service.start(result.repairJobId)
            return {
                "jobId": result.repairJobId,
                "parentJobId": result.parentJobId,
                "sampleId": body.sampleId,
                "issueId": body.issueId,
                "targetCount": result.targetCount,
                "started": True,
            }
        except KeyError as exc:
            raise not_found(exc) from exc
        except (RepairPreparationError, PipelineError, SchedulerError, ValueError) as exc:
            if released_parent_lock:
                context.preparation_service.restore_lock_after_repair_failure(job_id)
            raise bad_request(exc) from exc

    return router
