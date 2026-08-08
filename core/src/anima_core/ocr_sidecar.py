from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import PureWindowsPath
from typing import Literal


OCR_SIDECAR_SCHEMA_VERSION = 1
MAX_SIDECAR_BYTES = 1_048_576
MAX_ITEMS = 10_000
MAX_PATH_BYTES = 16_384
MAX_TEXT_BYTES = 16_384
MAX_ERROR_CODE_BYTES = 128
MAX_ERROR_MESSAGE_BYTES = 1_024
MAX_OCR_IMAGE_PIXELS = 40_000_000
MAX_OCR_IMAGE_SIDE = 16_384
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ABSOLUTE_WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)")
POSITIONS = (
    "top-left",
    "top-center",
    "top-right",
    "middle-left",
    "middle-center",
    "middle-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)
OcrPosition = Literal[
    "top-left",
    "top-center",
    "top-right",
    "middle-left",
    "middle-center",
    "middle-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
]
OcrStatus = Literal["success", "no_text", "failed"]
FIXED_OCR_INFERENCE_SETTINGS: dict[str, object] = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_NORMALIZATION_TOLERANCE = 1e-6


class OcrSidecarError(ValueError):
    pass


@dataclass(frozen=True)
class OcrImage:
    width: int
    height: int
    sizeBytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "sizeBytes": self.sizeBytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class OcrEngine:
    backend: Literal["paddle"]
    resourceId: str
    resourceFingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "resourceId": self.resourceId,
            "resourceFingerprint": self.resourceFingerprint,
        }


@dataclass(frozen=True)
class OcrSettings:
    llmMinConfidence: float
    inference: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "llmMinConfidence": self.llmMinConfidence,
            "inference": dict(self.inference),
        }


@dataclass(frozen=True)
class OcrItem:
    index: int
    text: str
    confidence: float
    polygonPixels: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
    polygon: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
    bboxPixels: tuple[float, float, float, float]
    bbox: tuple[float, float, float, float]
    position: OcrPosition
    textlineOrientationDegrees: float
    includedForLlm: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "text": self.text,
            "confidence": self.confidence,
            "polygonPixels": [list(point) for point in self.polygonPixels],
            "polygon": [list(point) for point in self.polygon],
            "bboxPixels": list(self.bboxPixels),
            "bbox": list(self.bbox),
            "position": self.position,
            "textlineOrientationDegrees": self.textlineOrientationDegrees,
            "includedForLlm": self.includedForLlm,
        }


@dataclass(frozen=True)
class OcrFailureDetails:
    actualPixels: int
    maxPixels: int
    maxSide: int

    def to_dict(self) -> dict[str, object]:
        return {
            "actualPixels": self.actualPixels,
            "maxPixels": self.maxPixels,
            "maxSide": self.maxSide,
        }


@dataclass(frozen=True)
class OcrFailure:
    code: Literal["ocr_inference_failed", "ocr_image_too_large"]
    message: str
    retriable: bool
    details: OcrFailureDetails | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
        }
        if self.details is not None:
            result["details"] = self.details.to_dict()
        return result


@dataclass(frozen=True)
class OcrSidecar:
    relativeImagePath: str
    image: OcrImage
    status: OcrStatus
    engine: OcrEngine
    settings: OcrSettings
    items: tuple[OcrItem, ...]
    error: OcrFailure | None
    schemaVersion: Literal[1] = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "relativeImagePath": self.relativeImagePath,
            "image": self.image.to_dict(),
            "status": self.status,
            "engine": self.engine.to_dict(),
            "settings": self.settings.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "error": None if self.error is None else self.error.to_dict(),
        }


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OcrSidecarError(f"{field} must be an object")
    return value


def _keys(value: dict[str, object], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise OcrSidecarError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise OcrSidecarError(f"unknown fields: {', '.join(sorted(extra))}")


def _utf8_bytes(value: str, field: str, maximum: int) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise OcrSidecarError(f"{field} is not valid UTF-8 text") from exc
    if size > maximum:
        raise OcrSidecarError(f"{field} exceeds its byte limit")


def _number(value: object, field: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OcrSidecarError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise OcrSidecarError(f"{field} must be a finite number")
    if minimum is not None and result < minimum:
        raise OcrSidecarError(f"{field} is below its minimum")
    if maximum is not None and result > maximum:
        raise OcrSidecarError(f"{field} exceeds its maximum")
    return result


def _integer(value: object, field: str, *, minimum: int = 0, maximum: int = 9_223_372_036_854_775_807) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise OcrSidecarError(f"{field} must be an integer in range")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise OcrSidecarError(f"{field} must be a lowercase SHA-256")
    return value


def _resource_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not RESOURCE_ID.fullmatch(value):
        raise OcrSidecarError(f"{field} is invalid")
    return value


def _relative_image_path(value: object, field: str = "relativeImagePath") -> str:
    if not isinstance(value, str) or not value:
        raise OcrSidecarError(f"{field} must be a non-empty string")
    _utf8_bytes(value, field, MAX_PATH_BYTES)
    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or normalized.startswith("\\") or not path.suffix:
        raise OcrSidecarError(f"{field} must be a safe relative image path with an extension")
    for component in normalized.split("\\"):
        if not component or component in {".", ".."} or ":" in component or component.endswith((".", " ")):
            raise OcrSidecarError(f"{field} contains an unsafe component")
        if component.split(".", 1)[0].upper() in _RESERVED:
            raise OcrSidecarError(f"{field} contains a reserved component")
    return normalized


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        raise OcrSidecarError("item text must be a string")
    _utf8_bytes(value, "item text", MAX_TEXT_BYTES)
    cleaned = "".join(character for character in value if unicodedata.category(character) != "Cc")
    _utf8_bytes(cleaned, "item text", MAX_TEXT_BYTES)
    if not cleaned.strip():
        raise OcrSidecarError("item text is blank after control-character removal")
    return cleaned


def _inference_settings(value: object) -> dict[str, object]:
    item = _object(value, "settings.inference")
    _keys(item, set(FIXED_OCR_INFERENCE_SETTINGS))
    for name in ("useDocOrientationClassify", "useDocUnwarping", "useTextlineOrientation"):
        if type(item[name]) is not bool or item[name] is not FIXED_OCR_INFERENCE_SETTINGS[name]:
            raise OcrSidecarError(f"settings.inference.{name} is invalid")
    if _number(item["textRecScoreThresh"], "settings.inference.textRecScoreThresh") != 0:
        raise OcrSidecarError("settings.inference.textRecScoreThresh is invalid")
    limit = _integer(item["textDetLimitSideLen"], "settings.inference.textDetLimitSideLen")
    if not 1920 <= limit <= 3840 or limit % 32:
        raise OcrSidecarError("settings.inference.textDetLimitSideLen is invalid")
    if type(item["textDetLimitType"]) is not str or item["textDetLimitType"] != "max":
        raise OcrSidecarError("settings.inference.textDetLimitType is invalid")
    return {**FIXED_OCR_INFERENCE_SETTINGS, "textDetLimitSideLen": limit}


def _image(value: object) -> OcrImage:
    item = _object(value, "image")
    _keys(item, {"width", "height", "sizeBytes", "sha256"})
    return OcrImage(
        width=_integer(item["width"], "image.width", minimum=1, maximum=1_000_000),
        height=_integer(item["height"], "image.height", minimum=1, maximum=1_000_000),
        sizeBytes=_integer(item["sizeBytes"], "image.sizeBytes", minimum=1),
        sha256=_sha256(item["sha256"], "image.sha256"),
    )


def _engine(value: object) -> OcrEngine:
    item = _object(value, "engine")
    _keys(item, {"backend", "resourceId", "resourceFingerprint"})
    if item["backend"] != "paddle":
        raise OcrSidecarError("engine.backend is invalid")
    return OcrEngine(
        backend="paddle",
        resourceId=_resource_id(item["resourceId"], "engine.resourceId"),
        resourceFingerprint=_sha256(item["resourceFingerprint"], "engine.resourceFingerprint"),
    )


def _settings(value: object) -> OcrSettings:
    item = _object(value, "settings")
    _keys(item, {"llmMinConfidence", "inference"})
    return OcrSettings(
        llmMinConfidence=_number(item["llmMinConfidence"], "settings.llmMinConfidence", minimum=0, maximum=1),
        inference=_inference_settings(item["inference"]),
    )


def _bbox(value: object, field: str, *, width: float, height: float) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise OcrSidecarError(f"{field} must contain four coordinates")
    left = _number(value[0], f"{field}[0]", minimum=0, maximum=width)
    top = _number(value[1], f"{field}[1]", minimum=0, maximum=height)
    right = _number(value[2], f"{field}[2]", minimum=0, maximum=width)
    bottom = _number(value[3], f"{field}[3]", minimum=0, maximum=height)
    if not left < right or not top < bottom:
        raise OcrSidecarError(f"{field} must have positive area")
    return (left, top, right, bottom)


def _polygon(
    value: object,
    field: str,
    *,
    width: float,
    height: float,
    bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise OcrSidecarError(f"{field} must contain four points")
    points: list[tuple[float, float]] = []
    left, top, right, bottom = bbox
    for index, point in enumerate(value):
        if not isinstance(point, list) or len(point) != 2:
            raise OcrSidecarError(f"{field}[{index}] must be a coordinate pair")
        x = _number(point[0], f"{field}[{index}][0]", minimum=0, maximum=width)
        y = _number(point[1], f"{field}[{index}][1]", minimum=0, maximum=height)
        if x < left - _NORMALIZATION_TOLERANCE or x > right + _NORMALIZATION_TOLERANCE:
            raise OcrSidecarError(f"{field}[{index}] escapes bbox")
        if y < top - _NORMALIZATION_TOLERANCE or y > bottom + _NORMALIZATION_TOLERANCE:
            raise OcrSidecarError(f"{field}[{index}] escapes bbox")
        points.append((x, y))
    return tuple(points)  # type: ignore[return-value]


def position_from_bbox(bbox: tuple[float, float, float, float]) -> OcrPosition:
    if not isinstance(bbox, tuple) or len(bbox) != 4:
        raise OcrSidecarError("bbox must be a four-value tuple")
    left, top, right, bottom = (
        _number(value, f"bbox[{index}]", minimum=0, maximum=1) for index, value in enumerate(bbox)
    )
    if not left < right or not top < bottom:
        raise OcrSidecarError("bbox must have positive area")
    center_x = min(1.0, max(0.0, (left + right) / 2))
    center_y = min(1.0, max(0.0, (top + bottom) / 2))
    column = min(2, int(center_x * 3))
    row = min(2, int(center_y * 3))
    return (
        ("top-left", "top-center", "top-right"),
        ("middle-left", "middle-center", "middle-right"),
        ("bottom-left", "bottom-center", "bottom-right"),
    )[row][column]  # type: ignore[return-value]


def _same_normalized(pixel: float, dimension: int, normalized: float) -> bool:
    return math.isclose(pixel / dimension, normalized, rel_tol=0.0, abs_tol=_NORMALIZATION_TOLERANCE)


def _item(value: object, *, index: int, image: OcrImage, threshold: float) -> OcrItem:
    item = _object(value, f"items[{index}]")
    _keys(
        item,
        {
            "index",
            "text",
            "confidence",
            "polygonPixels",
            "polygon",
            "bboxPixels",
            "bbox",
            "position",
            "textlineOrientationDegrees",
            "includedForLlm",
        },
    )
    if _integer(item["index"], f"items[{index}].index", minimum=0, maximum=MAX_ITEMS - 1) != index:
        raise OcrSidecarError("OCR item indexes must be sequential from zero")
    confidence = _number(item["confidence"], f"items[{index}].confidence", minimum=0, maximum=1)
    bbox_pixels = _bbox(item["bboxPixels"], f"items[{index}].bboxPixels", width=image.width, height=image.height)
    bbox = _bbox(item["bbox"], f"items[{index}].bbox", width=1, height=1)
    polygon_pixels = _polygon(
        item["polygonPixels"],
        f"items[{index}].polygonPixels",
        width=image.width,
        height=image.height,
        bbox=bbox_pixels,
    )
    polygon = _polygon(item["polygon"], f"items[{index}].polygon", width=1, height=1, bbox=bbox)
    for coordinate_index, (pixel, normalized) in enumerate(zip(bbox_pixels, bbox, strict=True)):
        dimension = image.width if coordinate_index in {0, 2} else image.height
        if not _same_normalized(pixel, dimension, normalized):
            raise OcrSidecarError(f"items[{index}] normalized bbox does not match pixels")
    for pixel_point, normalized_point in zip(polygon_pixels, polygon, strict=True):
        if not _same_normalized(pixel_point[0], image.width, normalized_point[0]) or not _same_normalized(
            pixel_point[1], image.height, normalized_point[1]
        ):
            raise OcrSidecarError(f"items[{index}] normalized polygon does not match pixels")
    position = item["position"]
    if position not in POSITIONS or position != position_from_bbox(bbox):
        raise OcrSidecarError(f"items[{index}].position is invalid")
    if type(item["includedForLlm"]) is not bool or item["includedForLlm"] != (confidence >= threshold):
        raise OcrSidecarError(f"items[{index}].includedForLlm is invalid")
    return OcrItem(
        index=index,
        text=_clean_text(item["text"]),
        confidence=confidence,
        polygonPixels=polygon_pixels,
        polygon=polygon,
        bboxPixels=bbox_pixels,
        bbox=bbox,
        position=position,  # type: ignore[arg-type]
        textlineOrientationDegrees=_number(
            item["textlineOrientationDegrees"], f"items[{index}].textlineOrientationDegrees"
        ),
        includedForLlm=item["includedForLlm"],  # type: ignore[arg-type]
    )


def _image_exceeds_limits(image: OcrImage) -> bool:
    return (
        image.width * image.height > MAX_OCR_IMAGE_PIXELS
        or image.width > MAX_OCR_IMAGE_SIDE
        or image.height > MAX_OCR_IMAGE_SIDE
    )


def _failure(value: object, *, image: OcrImage) -> OcrFailure:
    item = _object(value, "error")
    _keys(item, {"code", "message", "retriable"}, {"details"})
    code = item["code"]
    message = item["message"]
    if code == "ocr_inference_failed":
        _keys(item, {"code", "message", "retriable"})
        retriable = True
        details = None
        if _image_exceeds_limits(image):
            raise OcrSidecarError("oversize OCR image must use ocr_image_too_large")
    elif code == "ocr_image_too_large":
        _keys(item, {"code", "message", "retriable", "details"})
        detail_value = _object(item["details"], "error.details")
        _keys(detail_value, {"actualPixels", "maxPixels", "maxSide"})
        details = OcrFailureDetails(
            actualPixels=_integer(detail_value["actualPixels"], "error.details.actualPixels"),
            maxPixels=_integer(detail_value["maxPixels"], "error.details.maxPixels"),
            maxSide=_integer(detail_value["maxSide"], "error.details.maxSide"),
        )
        if details.actualPixels != image.width * image.height:
            raise OcrSidecarError("error.details.actualPixels does not match image dimensions")
        if details.maxPixels != MAX_OCR_IMAGE_PIXELS or details.maxSide != MAX_OCR_IMAGE_SIDE:
            raise OcrSidecarError("error.details safety limits are invalid")
        if not _image_exceeds_limits(image):
            raise OcrSidecarError("ocr_image_too_large requires an oversize image")
        retriable = False
    else:
        raise OcrSidecarError("error.code is invalid")
    _utf8_bytes(code, "error.code", MAX_ERROR_CODE_BYTES)
    if not isinstance(message, str) or not message.strip() or any(
        unicodedata.category(character) == "Cc" for character in message
    ):
        raise OcrSidecarError("error.message is invalid")
    _utf8_bytes(message, "error.message", MAX_ERROR_MESSAGE_BYTES)
    if ABSOLUTE_WINDOWS_PATH.search(message):
        raise OcrSidecarError("error.message must not contain an absolute path")
    if type(item["retriable"]) is not bool or item["retriable"] is not retriable:
        raise OcrSidecarError("error.retriable is invalid")
    return OcrFailure(code=code, message=message, retriable=retriable, details=details)


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OcrSidecarError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise OcrSidecarError(f"non-finite JSON value: {value}")


def parse_ocr_sidecar(raw: bytes, *, expected_relative_image_path: str | None = None) -> OcrSidecar:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_SIDECAR_BYTES:
        raise OcrSidecarError("OCR sidecar bytes are invalid")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_json_object_pairs, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrSidecarError("OCR sidecar is not strict UTF-8 JSON") from exc
    item = _object(value, "OCR sidecar")
    _keys(item, {"schemaVersion", "relativeImagePath", "image", "status", "engine", "settings", "items", "error"})
    if item["schemaVersion"] != OCR_SIDECAR_SCHEMA_VERSION:
        raise OcrSidecarError("OCR sidecar schemaVersion is invalid")
    relative_image_path = _relative_image_path(item["relativeImagePath"])
    if expected_relative_image_path is not None and relative_image_path != _relative_image_path(expected_relative_image_path):
        raise OcrSidecarError("OCR sidecar relativeImagePath does not match the expected image")
    status = item["status"]
    if status not in {"success", "no_text", "failed"}:
        raise OcrSidecarError("OCR sidecar status is invalid")
    image = _image(item["image"])
    if status != "failed" and _image_exceeds_limits(image):
        raise OcrSidecarError("oversize OCR image must use a failed sidecar")
    settings = _settings(item["settings"])
    items_value = item["items"]
    if not isinstance(items_value, list) or len(items_value) > MAX_ITEMS:
        raise OcrSidecarError("OCR sidecar items are invalid")
    parsed_items = tuple(_item(entry, index=index, image=image, threshold=settings.llmMinConfidence) for index, entry in enumerate(items_value))
    error_value = item["error"]
    if status == "success":
        if not parsed_items or error_value is not None:
            raise OcrSidecarError("success OCR sidecar must have items and no error")
        error = None
    elif status == "no_text":
        if parsed_items or error_value is not None:
            raise OcrSidecarError("no_text OCR sidecar must have no items or error")
        error = None
    else:
        if parsed_items:
            raise OcrSidecarError("failed OCR sidecar must have no items")
        error = _failure(error_value, image=image)
    return OcrSidecar(
        relativeImagePath=relative_image_path,
        image=image,
        status=status,  # type: ignore[arg-type]
        engine=_engine(item["engine"]),
        settings=settings,
        items=parsed_items,
        error=error,
    )


def serialize_ocr_sidecar(sidecar: OcrSidecar) -> bytes:
    if not isinstance(sidecar, OcrSidecar):
        raise OcrSidecarError("sidecar must be an OcrSidecar")
    try:
        raw = json.dumps(sidecar.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise OcrSidecarError("sidecar cannot be encoded as UTF-8 JSON") from exc
    normalized = parse_ocr_sidecar(raw)
    result = json.dumps(
        normalized.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    if len(result) > MAX_SIDECAR_BYTES:
        raise OcrSidecarError("OCR sidecar exceeds the byte limit")
    return result


def with_llm_threshold(sidecar: OcrSidecar, threshold: float) -> OcrSidecar:
    if not isinstance(sidecar, OcrSidecar):
        raise OcrSidecarError("sidecar must be an OcrSidecar")
    validated_threshold = _number(threshold, "llmMinConfidence", minimum=0, maximum=1)
    items = tuple(
        replace(item, includedForLlm=item.confidence >= validated_threshold)
        for item in sidecar.items
    )
    return replace(sidecar, settings=replace(sidecar.settings, llmMinConfidence=validated_threshold), items=items)


def is_reusable(
    sidecar: OcrSidecar,
    *,
    image_size: int,
    image_sha256: str,
    resource_fingerprint: str,
    inference_settings: dict[str, object],
) -> bool:
    if not isinstance(sidecar, OcrSidecar) or sidecar.status not in {"success", "no_text"}:
        return False
    try:
        expected_inference = _inference_settings(inference_settings)
        expected_image_sha256 = _sha256(image_sha256, "image_sha256")
        expected_resource_fingerprint = _sha256(resource_fingerprint, "resource_fingerprint")
    except OcrSidecarError:
        return False
    return (
        type(image_size) is int
        and image_size > 0
        and sidecar.image.sizeBytes == image_size
        and sidecar.image.sha256 == expected_image_sha256
        and sidecar.engine.resourceFingerprint == expected_resource_fingerprint
        and sidecar.settings.inference == expected_inference
    )


def compact_ocr_context(sidecar: OcrSidecar) -> dict[str, object]:
    if not isinstance(sidecar, OcrSidecar) or sidecar.status == "failed":
        raise OcrSidecarError("failed OCR sidecars have no NL context")
    return {
        "items": [
            [item.position, item.text]
            for item in sidecar.items
            if item.includedForLlm
        ]
    }


def ocr_sidecar_relative_path(relative_image_path: str) -> str:
    return f"ocr_annotations\\{_relative_image_path(relative_image_path)}.ocr.json"
