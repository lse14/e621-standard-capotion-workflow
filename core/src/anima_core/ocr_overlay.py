from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .overlay import OverlayError, OverlayLayout
from .path_safety import ensure_within, windows_paths_equal


@dataclass(frozen=True)
class OcrWorkingSidecarView:
    """Read task-local OCR sidecars before committed dataset OCR sidecars."""

    dataset_root: Path
    layout: OverlayLayout

    def __post_init__(self) -> None:
        if not windows_paths_equal(self.dataset_root, self.layout.dataset_root):
            raise OverlayError("OCR sidecar view does not match the immutable dataset root")

    def dataset_sidecar_path(self, relative_image_path: str) -> Path:
        overlay_path = self.layout.ocr_sidecar_path(relative_image_path)
        relative = overlay_path.relative_to(self.layout.root)
        return ensure_within(self.dataset_root, self.dataset_root / relative)

    def read_bytes(self, relative_image_path: str) -> bytes | None:
        overlay_path = self.layout.ocr_sidecar_path(relative_image_path)
        if overlay_path.exists():
            if not overlay_path.is_file():
                raise OverlayError("OCR task sidecar path is not a file")
            return overlay_path.read_bytes()
        dataset_path = self.dataset_sidecar_path(relative_image_path)
        if not dataset_path.exists():
            return None
        if not dataset_path.is_file():
            raise OverlayError("OCR dataset sidecar path is not a file")
        return dataset_path.read_bytes()
