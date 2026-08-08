from __future__ import annotations

from fastapi import APIRouter, Query
from starlette.responses import FileResponse

from .api_context import ControlPlaneContext, bad_request, conflict, not_found
from .api_models import _ConfirmBody, _CountReviewBatchBody, _CountReviewDecisionBody
from .count_review_preview import (
    CountReviewPreviewError,
    CountReviewPreviewNotFoundError,
    resolve_count_review_image,
)
from .count_review_service import (
    CountReviewConflictError,
    CountReviewError,
    CountReviewService,
    count_review_decision_response,
)
from .db import DEFAULT_PAGE_SIZE, StateDatabase
from .pipeline import PipelineError


def build_count_review_router(context: ControlPlaneContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/{job_id}/count-review")
    def list_count_review(
        job_id: str,
        afterSampleId: int = Query(default=0, ge=0),
        status: str | None = Query(default=None, max_length=32),
        reason: str | None = Query(default=None, max_length=64),
        classifyCount: str | None = Query(default=None, max_length=16),
        vlmCount: str | None = Query(default=None, max_length=16),
        mismatchOnly: bool = False,
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=500),
    ) -> dict[str, object]:
        database = StateDatabase.open(context.database_path)
        try:
            try:
                return CountReviewService(database, job_id).page(
                    after_sample_id=afterSampleId or None,
                    status=status,
                    reason=reason,
                    classify_count=classifyCount,
                    vlm_count=vlmCount,
                    mismatch_only=mismatchOnly,
                    limit=limit,
                )
            except KeyError as exc:
                raise not_found(exc) from exc
            except (CountReviewError, ValueError) as exc:
                raise bad_request(exc) from exc
        finally:
            database.close()

    @router.get("/api/jobs/{job_id}/count-review/{sample_id}/image")
    def count_review_image(job_id: str, sample_id: int) -> FileResponse:
        database = StateDatabase.open(context.database_path)
        try:
            try:
                image = resolve_count_review_image(database, job_id, sample_id)
            except (KeyError, CountReviewPreviewNotFoundError) as exc:
                raise not_found(exc) from exc
            except CountReviewPreviewError as exc:
                raise bad_request(exc) from exc
        finally:
            database.close()
        return FileResponse(
            image.path,
            media_type=image.media_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.put("/api/jobs/{job_id}/count-review/{sample_id}")
    def update_count_review(
        job_id: str,
        sample_id: int,
        body: _CountReviewDecisionBody,
    ) -> dict[str, object]:
        database = StateDatabase.open(context.database_path)
        try:
            try:
                row = CountReviewService(database, job_id).resolve(
                    sample_id,
                    expected_version=body.expectedVersion,
                    source=body.source,
                    count=body.count,
                )
                return {"sampleId": sample_id, "decision": count_review_decision_response(row)}
            except KeyError as exc:
                raise not_found(exc) from exc
            except CountReviewConflictError as exc:
                raise conflict(exc) from exc
            except (CountReviewError, ValueError) as exc:
                raise bad_request(exc) from exc
        finally:
            database.close()

    @router.post("/api/jobs/{job_id}/count-review/batch")
    def update_count_review_batch(
        job_id: str,
        body: _CountReviewBatchBody,
    ) -> dict[str, object]:
        database = StateDatabase.open(context.database_path)
        try:
            try:
                rows = CountReviewService(database, job_id).resolve_batch(
                    [item.model_dump() for item in body.updates]
                )
                return {
                    "items": [
                        {
                            "sampleId": int(row["sample_id"]),
                            "decision": count_review_decision_response(row),
                        }
                        for row in rows
                    ]
                }
            except KeyError as exc:
                raise not_found(exc) from exc
            except CountReviewConflictError as exc:
                raise conflict(exc) from exc
            except (CountReviewError, ValueError) as exc:
                raise bad_request(exc) from exc
        finally:
            database.close()

    @router.post("/api/jobs/{job_id}/count-review/confirm")
    def confirm_count_review(job_id: str, body: _ConfirmBody) -> dict[str, object]:
        try:
            started = context.pipeline_service.confirm_count_review(job_id, confirmed=body.confirmed)
            return {"jobId": job_id, "started": started}
        except KeyError as exc:
            raise not_found(exc) from exc
        except (CountReviewError, PipelineError, ValueError) as exc:
            raise bad_request(exc) from exc

    return router
