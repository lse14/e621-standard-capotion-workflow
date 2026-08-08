from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .api_context import ControlPlaneContext, bad_request, not_found
from .api_models import _ShutdownBody
from .db import StateDatabase
from .pipeline import PipelineError
from .resource_catalog import ResourceCatalogError
from .scheduler import SchedulerError


def build_application_router(context: ControlPlaneContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    @router.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "protocolVersion": "1.0"}

    @router.get("/api/resources")
    def list_resources() -> dict[str, object]:
        try:
            return context.resource_catalog.scan().api_dict()
        except ResourceCatalogError as exc:
            raise bad_request(exc) from exc

    def policy_control(job_id: str, action: str) -> dict[str, str]:
        database = StateDatabase.open(context.database_path)
        try:
            job = database.get_job(job_id)
            summary = database.module_summary(job_id, "dropout")
            if action == "pause":
                if job["status"] != "running" or job["current_module_id"] != "dropout" or summary["status"] != "running":
                    raise bad_request(SchedulerError("only a running policy module can be paused"))
                database.set_module_summary(job_id, "dropout", status="paused")
                database.set_job_status(job_id, "paused", current_module_id="dropout", resume_status="running")
                return {"status": "paused"}
        except KeyError as exc:
            raise not_found(exc) from exc
        finally:
            database.close()
        try:
            context.pipeline_service.resume(job_id)
            return {"status": "running"}
        except PipelineError as exc:
            raise bad_request(exc) from exc

    @router.post("/api/jobs/{job_id}/policy/pause")
    def pause_policy(job_id: str) -> dict[str, str]:
        return policy_control(job_id, "pause")

    @router.post("/api/jobs/{job_id}/policy/resume")
    def resume_policy(job_id: str) -> dict[str, str]:
        return policy_control(job_id, "resume")

    @router.post("/api/application/shutdown")
    def shutdown_application(body: _ShutdownBody) -> dict[str, bool]:
        if (
            context.shutdown_callback is None
            or not isinstance(context.shutdown_token, str)
            or len(context.shutdown_token) < 32
            or body.token != context.shutdown_token
        ):
            raise HTTPException(status_code=404, detail="route not found")
        context.shutdown_callback()
        return {"accepted": True}

    return router
