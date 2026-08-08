from __future__ import annotations

import hashlib
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .protocol import OcrWorkItem


MAX_PIXELS = 40_000_000
MAX_SIDE = 16_384
FORMATS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".bmp": "bmp",
}


class OcrImageError(RuntimeError):
    pass


class OcrImageDecodeError(OcrImageError):
    def __init__(self, message: str, *, width: int | None = None, height: int | None = None) -> None:
        super().__init__(message)
        self.width = width
        self.height = height


class OcrImageTooLargeError(OcrImageError):
    def __init__(self, width: int, height: int) -> None:
        super().__init__("OCR image dimensions exceed the first-release safety limit.")
        self.width = width
        self.height = height


class OcrSourceFingerprintError(OcrImageError):
    pass


@dataclass(frozen=True)
class DecodedImage:
    image: object
    width: int
    height: int
    size_bytes: int
    sha256: str


def image_exceeds_limits(width: int, height: int) -> bool:
    return width < 1 or height < 1 or width * height > MAX_PIXELS or width > MAX_SIDE or height > MAX_SIDE


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except OSError as exc:
        raise OcrSourceFingerprintError("OCR source cannot be inspected") from exc
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _source_path(item: OcrWorkItem) -> Path:
    path = Path(item.image_path)
    if not path.is_absolute() or _is_reparse(path):
        raise OcrSourceFingerprintError("OCR source path is invalid")
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise OcrSourceFingerprintError("OCR source cannot be inspected") from exc
    if not stat.S_ISREG(mode):
        raise OcrSourceFingerprintError("OCR source is not a regular file")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OcrSourceFingerprintError("OCR source cannot be read") from exc
    return digest.hexdigest()


def verify_source_fingerprint(item: OcrWorkItem) -> Path:
    path = _source_path(item)
    try:
        before = path.stat()
    except OSError as exc:
        raise OcrSourceFingerprintError("OCR source cannot be inspected") from exc
    if before.st_size != item.image_size or _sha256(path) != item.image_sha256:
        raise OcrSourceFingerprintError("OCR source size or SHA-256 changed after preflight")
    try:
        after = path.stat()
    except OSError as exc:
        raise OcrSourceFingerprintError("OCR source cannot be inspected") from exc
    if after.st_size != item.image_size:
        raise OcrSourceFingerprintError("OCR source size changed during verification")
    return path


def _visible_dimensions(source: object) -> tuple[int, int]:
    width = getattr(source, "width", None)
    height = getattr(source, "height", None)
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise OcrImageDecodeError("OCR image dimensions are invalid")
    try:
        orientation = source.getexif().get(274, 1)
    except Exception as exc:
        raise OcrImageDecodeError("OCR image EXIF metadata is invalid", width=width, height=height) from exc
    if orientation in {5, 6, 7, 8}:
        return height, width
    return width, height


def decode_and_verify(item: OcrWorkItem) -> DecodedImage:
    path = verify_source_fingerprint(item)
    suffix = path.suffix.lower()
    expected_format = FORMATS.get(suffix)
    if expected_format is None:
        raise OcrImageDecodeError("OCR image extension is unsupported")
    try:
        from PIL import Image, ImageFile, ImageOps
    except ImportError as exc:
        raise OcrImageDecodeError("Pillow is unavailable in the OCR runtime") from exc
    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = None
        with path.open("rb") as stream:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            with Image.open(stream) as source:
                visible_width, visible_height = _visible_dimensions(source)
                actual_format = str(source.format or "").lower()
                if actual_format != expected_format:
                    raise OcrImageDecodeError(
                        "OCR image bytes do not match their extension",
                        width=visible_width,
                        height=visible_height,
                    )
                if getattr(source, "n_frames", 1) != 1:
                    raise OcrImageDecodeError("OCR does not support multi-frame images", width=visible_width, height=visible_height)
                try:
                    source.seek(1)
                except EOFError:
                    source.seek(0)
                else:
                    raise OcrImageDecodeError("OCR does not support multi-frame images", width=visible_width, height=visible_height)
                if image_exceeds_limits(visible_width, visible_height):
                    raise OcrImageTooLargeError(visible_width, visible_height)
                source.load()
                transposed = ImageOps.exif_transpose(source)
                transposed.load()
                if image_exceeds_limits(transposed.width, transposed.height):
                    raise OcrImageTooLargeError(transposed.width, transposed.height)
                rgba = transposed.convert("RGBA")
                white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                rgb = Image.alpha_composite(white, rgba).convert("RGB")
        with path.open("rb") as verification_stream:
            with Image.open(verification_stream) as verification:
                verification.verify()
    except (OcrImageDecodeError, OcrImageTooLargeError):
        raise
    except Exception as exc:
        raise OcrImageDecodeError("OCR image cannot be decoded under the frozen policy") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels
    verify_source_fingerprint(item)
    if rgb.width < 1 or rgb.height < 1 or not math.isfinite(float(rgb.width * rgb.height)):
        raise OcrImageDecodeError("OCR image dimensions are invalid")
    return DecodedImage(rgb, rgb.width, rgb.height, item.image_size, item.image_sha256)
