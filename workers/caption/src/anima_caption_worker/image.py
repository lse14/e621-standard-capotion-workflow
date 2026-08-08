from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path
from typing import BinaryIO, Sequence


class CaptionImageDecodeError(RuntimeError):
    pass


class CaptionSourceFingerprintError(RuntimeError):
    pass


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except OSError as exc:
        raise CaptionSourceFingerprintError(f"unable to inspect source path: {path}") from exc
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def validate_dataset_root(path: str | Path) -> Path:
    root = Path(os.path.abspath(os.fspath(path)))
    if not root.is_absolute() or not root.is_dir():
        raise CaptionSourceFingerprintError("caption dataset root is missing or is not a directory")
    current = Path(root.anchor)
    for part in root.parts[1:] if root.anchor else root.parts:
        current = current / part
        if _is_reparse(current):
            raise CaptionSourceFingerprintError(f"caption dataset root contains a reparse point: {current}")
    return root


def resolve_image_path(dataset_root: Path, relative_image_path: str) -> Path:
    relative = Path(relative_image_path.replace("\\", os.sep))
    target = Path(os.path.abspath(dataset_root / relative))
    try:
        if os.path.normcase(os.path.commonpath((str(dataset_root), str(target)))) != os.path.normcase(str(dataset_root)):
            raise CaptionSourceFingerprintError("caption image path escapes the dataset root")
        parts = target.relative_to(dataset_root).parts
    except ValueError as exc:
        raise CaptionSourceFingerprintError("caption image path is outside the dataset volume") from exc
    current = dataset_root
    for part in parts:
        current = current / part
        if not current.exists():
            raise CaptionSourceFingerprintError(f"caption source path is missing: {relative_image_path}")
        if _is_reparse(current):
            raise CaptionSourceFingerprintError(f"caption source path contains a reparse point: {relative_image_path}")
    try:
        information = os.lstat(target)
    except OSError as exc:
        raise CaptionSourceFingerprintError(f"unable to inspect caption source: {relative_image_path}") from exc
    if not stat.S_ISREG(information.st_mode):
        raise CaptionSourceFingerprintError(f"caption source is not a regular file: {relative_image_path}")
    return target


def _verify_fingerprint(stream: BinaryIO, item: dict[str, object]) -> None:
    try:
        information = os.fstat(stream.fileno())
    except (AttributeError, OSError) as exc:
        raise CaptionSourceFingerprintError("unable to inspect the opened caption source") from exc
    if information.st_size != item["imageSize"] or information.st_mtime_ns != item["imageMtimeNs"]:
        raise CaptionSourceFingerprintError("caption source size or mtime changed after preflight")
    expected_file_id = item.get("imageFileId")
    actual_file_id = f"{getattr(information, 'st_dev', 0)}:{getattr(information, 'st_ino', 0)}"
    if expected_file_id is not None and expected_file_id != actual_file_id:
        raise CaptionSourceFingerprintError("caption source file identity changed after preflight")


def load_image_rgb(dataset_root: Path, item: dict[str, object]):
    try:
        from PIL import Image, ImageFile, ImageOps
    except ImportError as exc:
        raise CaptionImageDecodeError("Pillow is unavailable in the caption runtime") from exc

    expected_formats = {
        "jpeg": {"jpeg", "jpg"},
        "png": {"png"},
        "webp": {"webp"},
        "bmp": {"bmp"},
    }
    path = resolve_image_path(dataset_root, str(item["relativeImagePath"]))
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise CaptionSourceFingerprintError("unable to open the caption source after preflight") from exc
    with stream:
        _verify_fingerprint(stream, item)
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getattr(Image, "DecompressionBombWarning", Warning))
                with Image.open(stream) as source:
                    actual_format = str(source.format or "").lower()
                    if actual_format not in expected_formats[str(item["imageFormat"])]:
                        raise CaptionImageDecodeError("image bytes do not match the manifest image format")
                    if getattr(source, "n_frames", 1) != 1:
                        raise CaptionImageDecodeError("multi-frame images are not supported")
                    try:
                        source.seek(1)
                    except EOFError:
                        source.seek(0)
                    else:
                        raise CaptionImageDecodeError("multi-frame images are not supported")
                    source.load()
                    transposed = ImageOps.exif_transpose(source)
                    transposed.load()
                    rgba = transposed.convert("RGBA")
                    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    rgb = Image.alpha_composite(white, rgba).convert("RGB")
                stream.seek(0)
                with Image.open(stream) as verification:
                    verification.verify()
        except CaptionImageDecodeError:
            raise
        except Exception as exc:
            raise CaptionImageDecodeError("image cannot be completely decoded under the frozen policy") from exc
    return rgb


def resize_for_model(image):
    try:
        from PIL import Image
    except ImportError as exc:
        raise CaptionImageDecodeError("Pillow is unavailable in the caption runtime") from exc
    if image.mode != "RGB" or image.width < 1 or image.height < 1:
        raise CaptionImageDecodeError("decoded image must be a non-empty RGB image")
    padded_size = (max(image.width, 512), max(image.height, 512))
    canvas = Image.new("RGB", padded_size, "white")
    canvas.paste(image, ((padded_size[0] - image.width) // 2, (padded_size[1] - image.height) // 2))
    return canvas.resize((448, 448), Image.Resampling.BICUBIC)


def image_to_tensor(image, mean: Sequence[float], std: Sequence[float]):
    try:
        import numpy as np
    except ImportError as exc:
        raise CaptionImageDecodeError("NumPy is unavailable in the caption runtime") from exc
    resized = resize_for_model(image)
    pixels = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    mean_array = np.asarray(mean, dtype=np.float32)
    std_array = np.asarray(std, dtype=np.float32)
    if mean_array.shape != (3,) or std_array.shape != (3,) or not np.all(np.isfinite(std_array)) or np.any(std_array <= 0):
        raise CaptionImageDecodeError("caption normalization metadata is invalid")
    normalized = (pixels - mean_array) / std_array
    return np.ascontiguousarray(np.transpose(normalized, (2, 0, 1))[None, ...], dtype=np.float32)


def cl_image_to_tensor(image):
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise CaptionImageDecodeError("CL preprocessing dependencies are unavailable") from exc
    if image.mode != "RGB" or image.width < 1 or image.height < 1:
        raise CaptionImageDecodeError("decoded image must be a non-empty RGB image")
    resized = image.resize((384, 384), Image.Resampling.BICUBIC)
    pixels = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    normalized = (pixels - np.float32(0.5)) / np.float32(0.5)
    return np.ascontiguousarray(np.transpose(normalized, (2, 0, 1))[None, ...], dtype=np.float32)


def wd_image_to_tensor(image):
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise CaptionImageDecodeError("WD preprocessing dependencies are unavailable") from exc
    if image.mode != "RGB" or image.width < 1 or image.height < 1:
        raise CaptionImageDecodeError("decoded image must be a non-empty RGB image")
    size = max(image.width, image.height)
    canvas = Image.new("RGB", (size, size), "white")
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    resized = canvas.resize((448, 448), Image.Resampling.BICUBIC)
    rgb = np.asarray(resized, dtype=np.float32)
    bgr = rgb[:, :, ::-1]
    return np.ascontiguousarray(bgr[None, ...], dtype=np.float32)


def load_model_input(
    dataset_root: Path,
    item: dict[str, object],
    mean: Sequence[float],
    std: Sequence[float],
):
    return image_to_tensor(load_image_rgb(dataset_root, item), mean, std)
