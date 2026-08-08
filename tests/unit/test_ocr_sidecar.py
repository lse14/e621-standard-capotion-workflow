from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

import anima_core.ocr_sidecar as ocr_sidecar_module  # noqa: E402
from anima_core.ocr_sidecar import (  # noqa: E402
    FIXED_OCR_INFERENCE_SETTINGS,
    OcrSidecarError,
    compact_ocr_context,
    is_reusable,
    ocr_sidecar_relative_path,
    parse_ocr_sidecar,
    position_from_bbox,
    serialize_ocr_sidecar,
    with_llm_threshold,
)


INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}


def _item(
    index: int,
    text: str,
    confidence: float,
    *,
    bbox_pixels: list[float],
    bbox: list[float],
    position: str,
) -> dict[str, object]:
    left, top, right, bottom = bbox_pixels
    normalized_left, normalized_top, normalized_right, normalized_bottom = bbox
    return {
        "index": index,
        "text": text,
        "confidence": confidence,
        "polygonPixels": [[left, top], [right, top], [right, bottom], [left, bottom]],
        "polygon": [
            [normalized_left, normalized_top],
            [normalized_right, normalized_top],
            [normalized_right, normalized_bottom],
            [normalized_left, normalized_bottom],
        ],
        "bboxPixels": bbox_pixels,
        "bbox": bbox,
        "position": position,
        "textlineOrientationDegrees": 0,
        "includedForLlm": confidence >= 0.5,
    }


def _sidecar(*, status: str = "success") -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "relativeImagePath": "cats\\poster.jpg",
        "image": {"width": 100, "height": 100, "sizeBytes": 123456, "sha256": "a" * 64},
        "status": status,
        "engine": {
            "backend": "paddle",
            "resourceId": "ocr-ppocrv5-server-paddle-v1",
            "resourceFingerprint": "b" * 64,
        },
        "settings": {"llmMinConfidence": 0.5, "inference": copy.deepcopy(INFERENCE)},
        "items": [
            _item(0, "Hello", 0.98, bbox_pixels=[0, 0, 30, 30], bbox=[0, 0, 0.3, 0.3], position="top-left"),
            _item(1, "Hello", 0.49, bbox_pixels=[60, 60, 100, 100], bbox=[0.6, 0.6, 1, 1], position="bottom-right"),
        ],
        "error": None,
    }
    if status == "no_text":
        value["items"] = []
    elif status == "failed":
        value["items"] = []
        value["error"] = {
            "code": "ocr_inference_failed",
            "message": "The OCR engine could not recognize this image.",
            "retriable": True,
        }
    return value


def _raw(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _parse_valid(
    value: dict[str, object] | bytes,
    *,
    expected_relative_image_path: str | None = None,
):
    raw = value if isinstance(value, bytes) else _raw(value)
    try:
        return parse_ocr_sidecar(raw, expected_relative_image_path=expected_relative_image_path)
    except OcrSidecarError as exc:
        raise AssertionError(f"valid OCR sidecar was rejected: {exc}") from exc


def _oversize_sidecar() -> dict[str, object]:
    value = _sidecar(status="failed")
    value["image"] = {
        "width": 20_001,
        "height": 2_000,
        "sizeBytes": 123456,
        "sha256": "a" * 64,
    }
    value["error"] = {
        "code": "ocr_image_too_large",
        "message": "OCR image dimensions exceed the first-release safety limit.",
        "retriable": False,
        "details": {
            "actualPixels": 40_002_000,
            "maxPixels": 40_000_000,
            "maxSide": 16_384,
        },
    }
    return value


class OcrSidecarTests(unittest.TestCase):
    def test_success_roundtrip_preserves_duplicate_text_order_and_compact_context(self) -> None:
        value = _sidecar()
        value["items"][1]["confidence"] = 0.98  # type: ignore[index]
        value["items"][1]["includedForLlm"] = True  # type: ignore[index]
        sidecar = _parse_valid(value, expected_relative_image_path="cats\\poster.jpg")
        self.assertEqual(["Hello", "Hello"], [item.text for item in sidecar.items])
        self.assertEqual([0, 1], [item.index for item in sidecar.items])
        self.assertEqual(
            {"items": [["top-left", "Hello"], ["bottom-right", "Hello"]]},
            compact_ocr_context(sidecar),
        )
        encoded = serialize_ocr_sidecar(sidecar)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded, serialize_ocr_sidecar(_parse_valid(encoded)))
        self.assertEqual(sidecar, _parse_valid(encoded, expected_relative_image_path="cats\\poster.jpg"))
        serialized = json.loads(encoded)
        self.assertEqual(
            {"schemaVersion", "relativeImagePath", "image", "status", "engine", "settings", "items", "error"},
            set(serialized),
        )
        self.assertNotIn("imageBytes", serialized)
        self.assertNotIn("businessJson", serialized)
        self.assertNotIn("apiKey", serialized)
        self.assertNotIn("prompt", serialized)

    def test_threshold_only_changes_included_for_llm_and_does_not_affect_reuse(self) -> None:
        sidecar = _parse_valid(_sidecar())
        rewritten = with_llm_threshold(sidecar, 0.98)
        self.assertEqual(["Hello", "Hello"], [item.text for item in rewritten.items])
        self.assertEqual([True, False], [item.includedForLlm for item in rewritten.items])
        self.assertEqual(0.98, rewritten.settings.llmMinConfidence)
        self.assertTrue(
            is_reusable(
                rewritten,
                image_size=123456,
                image_sha256="a" * 64,
                resource_fingerprint="b" * 64,
                inference_settings=INFERENCE,
            )
        )
        self.assertFalse(
            is_reusable(
                rewritten,
                image_size=123456,
                image_sha256="c" * 64,
                resource_fingerprint="b" * 64,
                inference_settings=INFERENCE,
            )
        )
        self.assertFalse(
            is_reusable(
                rewritten,
                image_size=123456,
                image_sha256="a" * 64,
                resource_fingerprint="b" * 64,
                inference_settings={**INFERENCE, "textRecScoreThresh": 0.1},
            )
        )

    def test_no_text_and_failed_shapes_are_strict(self) -> None:
        no_text = _parse_valid(_sidecar(status="no_text"))
        self.assertEqual((), no_text.items)
        self.assertEqual({"items": []}, compact_ocr_context(no_text))
        failed = _parse_valid(_sidecar(status="failed"))
        self.assertFalse(
            is_reusable(
                failed,
                image_size=123456,
                image_sha256="a" * 64,
                resource_fingerprint="b" * 64,
                inference_settings=INFERENCE,
            )
        )
        with self.assertRaises(OcrSidecarError):
            compact_ocr_context(failed)
        invalid = _sidecar(status="no_text")
        invalid["items"] = [_sidecar()["items"][0]]
        with self.assertRaises(OcrSidecarError):
            parse_ocr_sidecar(_raw(invalid))

        normal_with_details = _sidecar(status="failed")
        normal_with_details["error"]["details"] = {  # type: ignore[index]
            "actualPixels": 10_000,
            "maxPixels": 40_000_000,
            "maxSide": 16_384,
        }
        with self.assertRaises(OcrSidecarError):
            parse_ocr_sidecar(_raw(normal_with_details))

        normal_with_absolute_path = _sidecar(status="failed")
        normal_with_absolute_path["error"]["message"] = "OCR failed for C:\\dataset\\poster.jpg"  # type: ignore[index]
        with self.assertRaises(OcrSidecarError):
            parse_ocr_sidecar(_raw(normal_with_absolute_path))

    def test_inference_settings_are_exact_six_keys_with_strict_fixed_types(self) -> None:
        self.assertEqual(INFERENCE, FIXED_OCR_INFERENCE_SETTINGS)
        self.assertEqual(40_000_000, getattr(ocr_sidecar_module, "MAX_OCR_IMAGE_PIXELS", None))
        self.assertEqual(16_384, getattr(ocr_sidecar_module, "MAX_OCR_IMAGE_SIDE", None))
        self.assertEqual(INFERENCE, _parse_valid(_sidecar()).settings.inference)

        numeric_zero = _sidecar()
        numeric_zero["settings"]["inference"]["textRecScoreThresh"] = 0.0  # type: ignore[index]
        self.assertEqual(INFERENCE, _parse_valid(numeric_zero).settings.inference)

        missing = dict(INFERENCE)
        missing.pop("textDetLimitType")
        invalid_settings = (
            {**INFERENCE, "textDetLimitSideLen": "1920"},
            {**INFERENCE, "textDetLimitSideLen": True},
            {**INFERENCE, "textDetLimitSideLen": 1920.0},
            {**INFERENCE, "textDetLimitSideLen": 960},
            {**INFERENCE, "textDetLimitType": "min"},
            {**INFERENCE, "textRecScoreThresh": True},
            missing,
            {**INFERENCE, "unexpected": 1},
        )
        for inference in invalid_settings:
            with self.subTest(inference=inference):
                candidate = _sidecar()
                candidate["settings"]["inference"] = inference  # type: ignore[index]
                with self.assertRaises(OcrSidecarError):
                    parse_ocr_sidecar(_raw(candidate))

        sidecar = _parse_valid(_sidecar())
        for inference in (missing, {**INFERENCE, "textDetLimitSideLen": 960}, {**INFERENCE, "unexpected": 1}):
            with self.subTest(reuse_inference=inference):
                self.assertFalse(
                    is_reusable(
                        sidecar,
                        image_size=123456,
                        image_sha256="a" * 64,
                        resource_fingerprint="b" * 64,
                        inference_settings=inference,
                    )
                )

    def test_oversize_failure_has_strict_details_and_is_never_reusable(self) -> None:
        failed = _parse_valid(_oversize_sidecar())
        self.assertEqual("ocr_image_too_large", failed.error.code)
        self.assertFalse(failed.error.retriable)
        self.assertEqual(40_002_000, failed.error.details.actualPixels)
        self.assertEqual(40_000_000, failed.error.details.maxPixels)
        self.assertEqual(16_384, failed.error.details.maxSide)
        self.assertFalse(
            is_reusable(
                failed,
                image_size=123456,
                image_sha256="a" * 64,
                resource_fingerprint="b" * 64,
                inference_settings=INFERENCE,
            )
        )

        invalid_candidates: list[tuple[str, dict[str, object]]] = []
        for label, details in (
            ("actual-mismatch", {"actualPixels": 40_001_999, "maxPixels": 40_000_000, "maxSide": 16_384}),
            ("actual-negative", {"actualPixels": -1, "maxPixels": 40_000_000, "maxSide": 16_384}),
            ("actual-boolean", {"actualPixels": True, "maxPixels": 40_000_000, "maxSide": 16_384}),
            ("max-pixels-wrong", {"actualPixels": 40_002_000, "maxPixels": 40_000_001, "maxSide": 16_384}),
            ("max-side-wrong", {"actualPixels": 40_002_000, "maxPixels": 40_000_000, "maxSide": 16_383}),
            ("details-extra", {"actualPixels": 40_002_000, "maxPixels": 40_000_000, "maxSide": 16_384, "extra": 1}),
        ):
            candidate = _oversize_sidecar()
            candidate["error"]["details"] = details  # type: ignore[index]
            invalid_candidates.append((label, candidate))

        missing_details = _oversize_sidecar()
        missing_details["error"].pop("details")  # type: ignore[union-attr]
        invalid_candidates.append(("missing-details", missing_details))
        wrong_retriable = _oversize_sidecar()
        wrong_retriable["error"]["retriable"] = True  # type: ignore[index]
        invalid_candidates.append(("wrong-retriable", wrong_retriable))
        error_extra = _oversize_sidecar()
        error_extra["error"]["extra"] = 1  # type: ignore[index]
        invalid_candidates.append(("error-extra", error_extra))
        absolute_path_message = _oversize_sidecar()
        absolute_path_message["error"]["message"] = "OCR rejected C:\\dataset\\poster.jpg"  # type: ignore[index]
        invalid_candidates.append(("absolute-path-message", absolute_path_message))

        for label, candidate in invalid_candidates:
            with self.subTest(case=label), self.assertRaises(OcrSidecarError):
                parse_ocr_sidecar(_raw(candidate))

    def test_nonfailed_sidecars_accept_limits_and_reject_any_oversize_dimension(self) -> None:
        for width, height in ((10_000, 4_000), (16_384, 1)):
            with self.subTest(boundary=(width, height)):
                candidate = _sidecar(status="no_text")
                candidate["image"]["width"] = width  # type: ignore[index]
                candidate["image"]["height"] = height  # type: ignore[index]
                _parse_valid(candidate)

        for width, height in ((8_001, 5_000), (16_385, 1)):
            with self.subTest(oversize=(width, height)), self.assertRaises(OcrSidecarError):
                candidate = _sidecar(status="no_text")
                candidate["image"]["width"] = width  # type: ignore[index]
                candidate["image"]["height"] = height  # type: ignore[index]
                parse_ocr_sidecar(_raw(candidate))

    def test_parser_enforces_paths_hashes_geometry_and_clean_text(self) -> None:
        cleaned = _sidecar()
        cleaned["items"][0]["text"] = "\x01Hello\x7f"  # type: ignore[index]
        self.assertEqual("Hello", _parse_valid(cleaned).items[0].text)

        for label, mutate in (
            ("absolute-path", lambda value: value.__setitem__("relativeImagePath", "C:\\poster.jpg")),
            ("wrong-hash", lambda value: value["image"].__setitem__("sha256", "A" * 64)),  # type: ignore[index]
            ("unknown-field", lambda value: value.__setitem__("prompt", "secret")),
            ("blank-after-control-removal", lambda value: value["items"][0].__setitem__("text", "\x00\x01")),  # type: ignore[index]
            ("non-finite-confidence", lambda value: value["items"][0].__setitem__("confidence", math.nan)),  # type: ignore[index]
            ("wrong-position", lambda value: value["items"][0].__setitem__("position", "bottom-right")),  # type: ignore[index]
            ("polygon-outside-bbox", lambda value: value["items"][0]["polygon"].__setitem__(0, [0.9, 0.9])),  # type: ignore[index]
        ):
            with self.subTest(case=label):
                candidate = _sidecar()
                mutate(candidate)
                with self.assertRaises(OcrSidecarError):
                    parse_ocr_sidecar(_raw(candidate))

    def test_nine_grid_uses_following_cell_at_boundaries_and_clamps_one(self) -> None:
        self.assertEqual("top-center", position_from_bbox((0.3, 0.0, 11 / 30, 0.2)))
        self.assertEqual("middle-right", position_from_bbox((0.6, 0.3, 11 / 15, 11 / 30)))
        self.assertEqual("bottom-right", position_from_bbox((0.9, 0.9, 1.0, 1.0)))
        for bbox in ((-0.1, 0, 0.1, 0.1), (0.8, 0, 0.7, 0.1), (0, 0, math.inf, 0.1)):
            with self.subTest(bbox=bbox), self.assertRaises(OcrSidecarError):
                position_from_bbox(bbox)

    def test_sidecar_path_keeps_the_original_image_extension(self) -> None:
        self.assertEqual(
            "ocr_annotations\\cats\\poster.jpg.ocr.json",
            ocr_sidecar_relative_path("cats\\poster.jpg"),
        )


if __name__ == "__main__":
    unittest.main()
