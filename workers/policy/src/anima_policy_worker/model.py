from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from PIL import Image


class AestheticScorer(Protocol):
    load_count: int
    device_name: str

    def score(self, images: Sequence[Image.Image]) -> list[float]: ...


def load_lse14_scorer(files: dict[str, Path], device: str) -> AestheticScorer:
    # Heavy dependencies stay out of the worker control path when quality is disabled.
    from .lse14_backend import Lse14Scorer

    return Lse14Scorer(files, device)
