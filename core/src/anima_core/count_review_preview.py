from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .contracts import COUNT_REVIEW_SCHEMA_VERSIONS
from .db import StateDatabase
from .path_safety import PathSafetyError, ensure_within, image_format, safe_relative_path


IMAGE_MIME_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


class CountReviewPreviewError(RuntimeError):
    pass


class CountReviewPreviewNotFoundError(CountReviewPreviewError):
    pass


@dataclass(frozen=True)
class CountReviewImage:
    path: Path
    media_type: str


def resolve_count_review_image(
    database: StateDatabase,
    job_id: str,
    sample_id: int,
) -> CountReviewImage:
    if type(sample_id) is not int or sample_id < 1:
        raise CountReviewPreviewNotFoundError("count review image does not exist")
    job = database.get_job(job_id)
    if int(job["config_schema_version"]) not in COUNT_REVIEW_SCHEMA_VERSIONS:
        raise CountReviewPreviewError("count review is unavailable for this task")
    database.get_count_review_decision(job_id, sample_id)
    sample = database.get_sample_with_state(job_id, sample_id)
    try:
        relative = safe_relative_path(str(sample["relative_image_path"]))
        dataset_root = Path(str(job["dataset_root"]))
        target = ensure_within(
            dataset_root,
            dataset_root / Path(relative.replace("\\", os.sep)),
        )
        if not target.is_file():
            raise CountReviewPreviewNotFoundError("count review image does not exist")
        actual_format = image_format(target)
        if actual_format != sample["image_format"] or actual_format not in IMAGE_MIME_TYPES:
            raise CountReviewPreviewError("count review image format does not match its manifest")
    except CountReviewPreviewError:
        raise
    except (OSError, PathSafetyError, TypeError, ValueError) as exc:
        raise CountReviewPreviewError("count review image path is unsafe") from exc
    return CountReviewImage(target, IMAGE_MIME_TYPES[actual_format])
