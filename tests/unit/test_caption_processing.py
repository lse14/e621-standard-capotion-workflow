from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    raise unittest.SkipTest("caption worker tests must run in the caption-e621 embedded runtime")


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "caption" / "src"))

from anima_caption_worker.formatting import CaptionFormattingError, display_tag, format_caption
from anima_caption_worker.image import (
    CaptionImageDecodeError,
    CaptionSourceFingerprintError,
    cl_image_to_tensor,
    image_to_tensor,
    load_image_rgb,
    resize_for_model,
    wd_image_to_tensor,
)
from anima_caption_worker.model import (
    CL_ADJUSTABLE,
    CL_CATEGORIES,
    CL_EXCLUDED,
    CL_TAG_COUNT,
    EXPECTED_MEAN,
    EXPECTED_STD,
    WD_ADJUSTABLE,
    WD_CATEGORIES,
    WD_EXCLUDED,
    WD_PREPROCESS,
    WD_TAG_COUNT,
    CaptionInferenceError,
    CaptionMetadata,
    CaptionMetadataError,
    CaptionModel,
    CaptionPrediction,
    CaptionSessionError,
    ClTaggerAdapter,
    WdTaggerAdapter,
    _select_predictions,
    resolve_thresholds,
)
from anima_caption_worker.worker import CaptionWorker
from anima_core.caption_protocol import CaptionIssueResultV1, CaptionResultV1, parse_caption_outcome


CATEGORIES = ("general", "character", "species", "rating")
DEFAULT_THRESHOLDS = {"general": 0.6, "character": 0.65, "species": 0.6, "rating": 0.65}


def _work_item(path: Path, image_format: str) -> dict[str, object]:
    information = path.stat()
    return {
        "schemaVersion": 1,
        "sampleId": 1,
        "leaseId": "lease-1",
        "source": "e621",
        "relativeImagePath": path.name,
        "annotationKey": path.stem,
        "imageFormat": image_format,
        "imageFrameCount": 1,
        "imageFileId": f"{getattr(information, 'st_dev', 0)}:{getattr(information, 'st_ino', 0)}",
        "imageSize": information.st_size,
        "imageMtimeNs": information.st_mtime_ns,
    }


class _OutputSession:
    def __init__(self, output: object) -> None:
        self.output = output

    def run(self, _: object, __: dict[str, object]) -> list[object]:
        return [self.output]


class _AdapterSession(_OutputSession):
    def __init__(self, output: object, shape: list[object]) -> None:
        super().__init__(output)
        self.input = SimpleNamespace(name="images", shape=shape, type="tensor(float)")

    def get_inputs(self) -> list[object]:
        return [self.input]

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]


def _prediction_model(output: object) -> CaptionModel:
    names = tuple(f"tag_{index}" for index in range(8_783))
    categories = tuple(CATEGORIES[index % len(CATEGORIES)] for index in range(8_783))
    model = object.__new__(CaptionModel)
    model.metadata = CaptionMetadata(names, categories, dict(DEFAULT_THRESHOLDS), EXPECTED_MEAN, EXPECTED_STD)
    model.session = _OutputSession(output)
    model.input_name = "input"
    model.provider = "CPUExecutionProvider"
    model.session_loads = 1
    return model


class _WorkerModel:
    def __init__(self, predictions: tuple[CaptionPrediction, ...] = ()) -> None:
        self.metadata = SimpleNamespace(mean=EXPECTED_MEAN, std=EXPECTED_STD)
        self.provider = "CPUExecutionProvider"
        self.session_loads = 1
        self.predictions = predictions
        self.error: Exception | None = None

    def predict(self, _: object, __: dict[str, float]) -> tuple[CaptionPrediction, ...]:
        if self.error is not None:
            raise self.error
        return self.predictions

    def preprocess(self, _: object) -> object:
        return object()


def _ready_worker(dataset_root: Path, model: _WorkerModel) -> CaptionWorker:
    worker = CaptionWorker()
    worker.hello = {
        "profile": "e621",
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": True,
            "triggerTerms": ["anima_style"],
        }
    }
    worker.model = model  # type: ignore[assignment]
    worker.dataset_root = dataset_root
    worker.thresholds = dict(DEFAULT_THRESHOLDS)
    return worker


class CaptionProcessingTests(unittest.TestCase):
    def test_three_threshold_modes_are_exact_and_bounded(self) -> None:
        self.assertEqual(DEFAULT_THRESHOLDS, resolve_thresholds({"mode": "model_default"}, DEFAULT_THRESHOLDS))
        self.assertEqual(
            {category: 0.0 for category in CATEGORIES},
            resolve_thresholds({"mode": "uniform", "uniformThreshold": 0}, DEFAULT_THRESHOLDS),
        )
        per_category = {"general": 0.0, "character": 0.25, "species": 0.75, "rating": 1.0}
        self.assertEqual(
            per_category,
            resolve_thresholds({"mode": "per_category", "categoryThresholds": per_category}, DEFAULT_THRESHOLDS),
        )
        for invalid in (
            {"mode": "uniform", "uniformThreshold": True},
            {"mode": "uniform", "uniformThreshold": -0.01},
            {"mode": "uniform", "uniformThreshold": 1.01},
            {"mode": "uniform", "uniformThreshold": float("nan")},
            {"mode": "uniform", "uniformThreshold": float("inf")},
            {"mode": "per_category", "categoryThresholds": {"general": 0.5}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(CaptionMetadataError):
                resolve_thresholds(invalid, DEFAULT_THRESHOLDS)

    def test_cl_adapter_uses_384_nchw_sigmoid_and_drops_excluded_categories(self) -> None:
        import numpy as np

        self.assertEqual(106_536, CL_TAG_COUNT)
        names = ["general_tag", "character_tag", "copyright_tag", "meta_tag", "rating_tag", "quality_tag"]
        categories = ["General", "Character", "Copyright", "Meta", "Rating", "Quality"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "model": root / "model.onnx",
                "metadata": root / "model_metadata.json",
                "vocabulary": root / "model_vocabulary.json",
                "thresholds": root / "thresholds.json",
            }
            paths["model"].write_bytes(b"model")
            paths["metadata"].write_text(json.dumps({"image_size": 384, "num_classes": 6}), encoding="utf-8")
            paths["vocabulary"].write_text(json.dumps({
                "idx_to_tag": {str(index): name for index, name in enumerate(names)},
                "tag_to_idx": {name: index for index, name in enumerate(names)},
                "tag_to_category": dict(zip(names, categories, strict=True)),
            }), encoding="utf-8")
            paths["thresholds"].write_text(json.dumps({
                "general": 0.55, "character": 0.55, "copyright": 0.55,
            }), encoding="utf-8")
            manifest_metadata = {
                "tagCount": 6,
                "modelCategories": list(categories),
                "adjustableCategories": ["general", "character", "copyright"],
                "excludedCategories": ["meta", "rating", "quality"],
            }
            logits = np.asarray([[0.0, 1.0, 2.0, 100.0, 100.0, 100.0]], dtype=np.float32)
            with mock.patch("anima_caption_worker.model.CL_TAG_COUNT", 6):
                adapter = ClTaggerAdapter(
                    paths,
                    manifest_metadata,
                    session_factory=lambda _: _AdapterSession(logits, [None, 3, 384, 384]),
                )
                predictions = adapter.predict(
                    object(),
                    {"general": 0.5, "character": 0.5, "copyright": 0.5},
                )
                self.assertEqual([2, 1, 0], [prediction.model_index for prediction in predictions])
                self.assertAlmostEqual(0.5, predictions[-1].score)
                self.assertEqual(
                    {"general", "character", "copyright"},
                    {prediction.category for prediction in predictions},
                )
                self.assertEqual(
                    "anima style, copyright tag, character tag, general tag",
                    format_caption(predictions, {
                        "replaceUnderscoresWithSpaces": True,
                        "preserveEscapes": True,
                        "triggersEnabled": True,
                        "triggerTerms": ["anima_style"],
                    }),
                )
                adapter.session.output = np.asarray([[0.0, 0.0, 0.0, 0.0, np.nan, 0.0]], dtype=np.float32)
                with self.assertRaises(CaptionInferenceError):
                    adapter.predict(object(), {category: 0.5 for category in CL_ADJUSTABLE})
                with self.assertRaises(CaptionMetadataError):
                    resolve_thresholds(
                        {"mode": "per_category", "categoryThresholds": {"general": 0.5}},
                        adapter.metadata.default_thresholds,
                    )
                with self.assertRaises(CaptionMetadataError):
                    changed = dict(manifest_metadata)
                    changed["excludedCategories"] = ["meta", "rating"]
                    ClTaggerAdapter(
                        paths,
                        changed,
                        session_factory=lambda _: _AdapterSession(logits, [1, 3, 384, 384]),
                    )
                with self.assertRaises(CaptionSessionError):
                    ClTaggerAdapter(
                        paths,
                        manifest_metadata,
                        session_factory=lambda _: _AdapterSession(logits, [1, 384, 384, 3]),
                    )

    def test_wd_adapter_uses_nhwc_probabilities_and_drops_rating(self) -> None:
        import numpy as np

        self.assertEqual(10_861, WD_TAG_COUNT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "model": root / "model.onnx",
                "selectedTags": root / "selected_tags.csv",
                "preprocess": root / "preprocess.json",
                "thresholds": root / "thresholds.json",
            }
            paths["model"].write_bytes(b"model")
            paths["selectedTags"].write_text(
                "tag_id,name,category,count\n0,safe,9,1\n1,solo,0,1\n2,character_name,4,1\n3,blue_sky,0,1\n",
                encoding="utf-8",
            )
            paths["preprocess"].write_text(json.dumps(WD_PREPROCESS), encoding="utf-8")
            paths["thresholds"].write_text(
                json.dumps({"general": 0.5296, "character": 0.5296}),
                encoding="utf-8",
            )
            manifest_metadata = {
                "tagCount": 4,
                "modelCategories": ["general", "character", "rating"],
                "adjustableCategories": ["general", "character"],
                "excludedCategories": ["rating"],
            }
            probabilities = np.asarray([[1.0, 0.5296, 0.8, 0.5]], dtype=np.float32)
            with mock.patch("anima_caption_worker.model.WD_TAG_COUNT", 4):
                adapter = WdTaggerAdapter(
                    paths,
                    manifest_metadata,
                    session_factory=lambda _: _AdapterSession(probabilities, ["batch", 448, 448, 3]),
                )
                predictions = adapter.predict(object(), adapter.metadata.default_thresholds)
                self.assertEqual([2, 1], [prediction.model_index for prediction in predictions])
                self.assertAlmostEqual(0.8, predictions[0].score, places=6)
                self.assertAlmostEqual(0.5296, predictions[1].score, places=6)
                self.assertEqual(
                    "anima style, character name, solo",
                    format_caption(predictions, {
                        "replaceUnderscoresWithSpaces": True,
                        "preserveEscapes": True,
                        "triggersEnabled": True,
                        "triggerTerms": ["anima_style"],
                    }),
                )
                adapter.session.output = np.asarray([[1.01, 0.8, 0.8, 0.8]], dtype=np.float32)
                with self.assertRaises(CaptionInferenceError):
                    adapter.predict(object(), adapter.metadata.default_thresholds)
                with self.assertRaises(CaptionSessionError):
                    WdTaggerAdapter(
                        paths,
                        manifest_metadata,
                        session_factory=lambda _: _AdapterSession(probabilities, [1, 3, 448, 448]),
                    )

    def test_shared_prediction_selection_is_score_stable_and_exactly_deduplicated(self) -> None:
        metadata = CaptionMetadata(
            ("same_tag", "same_tag", "other_tag"),
            ("general", "general", "general"),
            {"general": 0.5},
            (0.5, 0.5, 0.5),
            (0.5, 0.5, 0.5),
            profile="danbooru",
            adjustable_categories=("general",),
        )
        selected = _select_predictions(metadata, [0.6, 0.9, 0.9], {"general": 0.5}, single_rating=False)
        self.assertEqual([1, 2], [prediction.model_index for prediction in selected])

    def test_cl_and_wd_preprocessing_are_distinct(self) -> None:
        import numpy as np
        from PIL import Image

        image = Image.new("RGB", (100, 50), "red")
        cl_tensor = cl_image_to_tensor(image)
        self.assertEqual((1, 3, 384, 384), cl_tensor.shape)
        np.testing.assert_allclose([1.0, -1.0, -1.0], cl_tensor[0, :, 0, 0], atol=1e-6)

        wd_tensor = wd_image_to_tensor(image)
        self.assertEqual((1, 448, 448, 3), wd_tensor.shape)
        np.testing.assert_allclose([255.0, 255.0, 255.0], wd_tensor[0, 0, 0], atol=1e-6)
        np.testing.assert_allclose([0.0, 0.0, 255.0], wd_tensor[0, 224, 224], atol=1e-6)

    def test_inference_is_inclusive_score_sorted_and_index_stable(self) -> None:
        import numpy as np

        logits = np.full((1, 8_783), -10.0, dtype=np.float32)
        logits[0, 10] = 2.2
        logits[0, 2] = 1.4
        logits[0, 4] = 1.4
        logits[0, 5] = 0.0
        logits[0, 0] = 100.0
        model = _prediction_model(logits)
        predictions = model.predict(object(), {category: 0.5 for category in CATEGORIES})
        self.assertEqual([0, 10, 2, 4, 5], [prediction.model_index for prediction in predictions])
        self.assertAlmostEqual(0.5, predictions[-1].score)
        rating_count = sum(1 for index in range(8_783) if CATEGORIES[index % len(CATEGORIES)] == "rating")
        self.assertEqual(
            8_783 - rating_count + 1,
            len(model.predict(object(), {category: 0.0 for category in CATEGORIES})),
        )
        self.assertEqual([0], [
            prediction.model_index
            for prediction in model.predict(object(), {category: 1.0 for category in CATEGORIES})
        ])

    def test_inference_rejects_bad_shape_and_nonfinite_scores_but_keeps_finite_probabilities(self) -> None:
        import numpy as np

        invalid_outputs = [
            np.zeros((8_783,), dtype=np.float32),
            np.full((1, 8_783), np.nan, dtype=np.float32),
        ]
        for output in invalid_outputs:
            with self.subTest(shape=output.shape), self.assertRaises(CaptionInferenceError):
                _prediction_model(output).predict(object(), {category: 0.5 for category in CATEGORIES})
        finite = np.full((1, 8_783), -2.0, dtype=np.float32)
        finite[0, 7] = 1.5
        predictions = _prediction_model(finite).predict(
            object(),
            {category: 0.5 for category in CATEGORIES},
        )
        self.assertEqual([7], [prediction.model_index for prediction in predictions])
        self.assertAlmostEqual(0.8175744761936437, predictions[0].score)

    def test_raw_logits_become_sigmoid_probabilities_and_rating_is_mutually_exclusive(self) -> None:
        """F04/F44: linear_168 emits logits, and E621 ratings are exclusive."""
        import numpy as np

        logits = np.full((1, 8_783), -50.0, dtype=np.float32)
        logits[0, 0] = 0.0  # general
        logits[0, 3] = 1.0  # rating
        logits[0, 7] = 2.0  # rating, the exclusive winner
        logits[0, 11] = 0.5  # rating
        model = _prediction_model(logits)
        predictions = model.predict(object(), {category: 0.5 for category in CATEGORIES})
        self.assertEqual([7, 0], [prediction.model_index for prediction in predictions])
        self.assertAlmostEqual(0.8807970779778823, predictions[0].score)
        self.assertAlmostEqual(0.5, predictions[1].score)
        # Before the fix a raw logit of 0.0 was below every non-zero threshold and a
        # uniform threshold of 0 selected only non-negative scores instead of all tags.
        self.assertNotIn(
            0,
            [prediction.model_index for prediction in model.predict(object(), {category: 0.9 for category in CATEGORIES})],
        )
        extreme = np.full((1, 8_783), -1_000.0, dtype=np.float32)
        extreme[0, 0] = 1_000.0
        with np.errstate(over="raise", invalid="raise"):
            saturated = _prediction_model(extreme).predict(object(), {category: 0.0 for category in CATEGORIES})
        self.assertEqual(1.0, saturated[0].score)
        self.assertEqual(0.0, saturated[-1].score)

    def test_txt_format_order_underscore_and_escape_matrix(self) -> None:
        predictions = (
            CaptionPrediction("blue_eyes", 0.9, "general", 0),
            CaptionPrediction(r"artist_(name)\tag", 0.8, "character", 1),
        )
        policy = {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": True,
            "triggerTerms": ["anima_style", "trigger_(x)"],
        }
        self.assertEqual(
            r"anima style, trigger \(x\), blue eyes, artist \(name\)\\tag",
            format_caption(predictions, policy),
        )
        raw_policy = {
            "replaceUnderscoresWithSpaces": False,
            "preserveEscapes": False,
            "triggersEnabled": False,
            "triggerTerms": [],
        }
        self.assertEqual(r"blue_eyes, artist_(name)\tag", format_caption(predictions, raw_policy))
        self.assertEqual(r"a\_b", display_tag(r"a\_b", raw_policy))
        with self.assertRaises(CaptionFormattingError):
            format_caption((), policy)

    def test_trigger_terms_blank_after_display_are_rejected_before_inference(self) -> None:
        """D16: `my_style_` used to pass hello and fail once per inferred image."""
        from anima_caption_worker.protocol import CaptionPayloadError, validate_caption_format

        base = {"replaceUnderscoresWithSpaces": True, "preserveEscapes": False, "triggersEnabled": True}
        for term in ("my_style_", "_trigger", "___"):
            with self.subTest(term=term), self.assertRaises(CaptionPayloadError):
                validate_caption_format({**base, "triggerTerms": [term]})
            with self.assertRaises(CaptionFormattingError):
                display_tag(term, {**base, "triggerTerms": []})
        self.assertEqual(
            ["my_style"],
            validate_caption_format({**base, "triggerTerms": ["my_style"]})["triggerTerms"],
        )
        self.assertEqual(
            ["my_style_"],
            validate_caption_format(
                {**base, "replaceUnderscoresWithSpaces": False, "triggerTerms": ["my_style_"]}
            )["triggerTerms"],
        )

    def test_txt_format_all_display_and_trigger_combinations(self) -> None:
        prediction = (CaptionPrediction(r"under_score_(x)\z", 0.9, "general", 0),)
        trigger = r"trigger_term_(t)\q"
        expected_model = {
            (False, False): r"under_score_(x)\z",
            (True, False): r"under score (x)\z",
            (False, True): r"under_score_\(x\)\\z",
            (True, True): r"under score \(x\)\\z",
        }
        expected_trigger = {
            (False, False): r"trigger_term_(t)\q",
            (True, False): r"trigger term (t)\q",
            (False, True): r"trigger_term_\(t\)\\q",
            (True, True): r"trigger term \(t\)\\q",
        }
        for replace_underscores in (False, True):
            for preserve_escapes in (False, True):
                for triggers_enabled in (False, True):
                    with self.subTest(
                        replace=replace_underscores,
                        escapes=preserve_escapes,
                        triggers=triggers_enabled,
                    ):
                        policy = {
                            "replaceUnderscoresWithSpaces": replace_underscores,
                            "preserveEscapes": preserve_escapes,
                            "triggersEnabled": triggers_enabled,
                            "triggerTerms": [trigger] if triggers_enabled else [],
                        }
                        key = (replace_underscores, preserve_escapes)
                        expected = expected_model[key]
                        if triggers_enabled:
                            expected = expected_trigger[key] + ", " + expected
                        actual = format_caption(prediction, policy)
                        self.assertEqual(expected, actual)
                        self.assertFalse(actual.startswith("\ufeff"))
                        self.assertNotRegex(actual, "[\\r\\n]")

    def test_transparency_exif_and_model_tensor_semantics(self) -> None:
        import numpy as np
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transparent = root / "transparent.png"
            Image.new("RGBA", (1, 1), (255, 0, 0, 128)).save(transparent)
            rgb = load_image_rgb(root, _work_item(transparent, "png"))
            self.assertEqual("RGB", rgb.mode)
            self.assertEqual((255, 127, 127), rgb.getpixel((0, 0)))

            oriented = root / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (2, 3), "red").save(oriented, exif=exif)
            self.assertEqual((3, 2), load_image_rgb(root, _work_item(oriented, "jpeg")).size)

            resized = resize_for_model(Image.new("RGB", (100, 50), "red"))
            self.assertEqual((448, 448), resized.size)
            self.assertEqual((255, 255, 255), resized.getpixel((0, 0)))
            tensor = image_to_tensor(Image.new("RGB", (100, 50), "red"), EXPECTED_MEAN, EXPECTED_STD)
            self.assertEqual((1, 3, 448, 448), tensor.shape)
            self.assertEqual(np.float32, tensor.dtype)
            self.assertTrue(tensor.flags.c_contiguous)
            center = tensor[0, :, 224, 224] * np.asarray(EXPECTED_STD) + np.asarray(EXPECTED_MEAN)
            np.testing.assert_allclose([1.0, 0.0, 0.0], center, atol=1e-5)

    def test_supported_formats_alpha_modes_and_independent_padding(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            formats = (
                ("sample.jpg", "jpeg", "JPEG"),
                ("sample.jpeg", "jpeg", "JPEG"),
                ("sample.png", "png", "PNG"),
                ("sample.webp", "webp", "WEBP"),
                ("sample.bmp", "bmp", "BMP"),
            )
            for name, image_format, pillow_format in formats:
                with self.subTest(name=name):
                    path = root / name
                    try:
                        Image.new("RGB", (7, 5), (24, 48, 96)).save(path, format=pillow_format)
                    except OSError:
                        if pillow_format == "WEBP":
                            self.skipTest("this Pillow build has no WebP support")
                        raise
                    loaded = load_image_rgb(root, _work_item(path, image_format))
                    self.assertEqual(("RGB", (7, 5)), (loaded.mode, loaded.size))

            la = root / "alpha-la.png"
            Image.new("LA", (1, 1), (0, 128)).save(la)
            self.assertEqual((127, 127, 127), load_image_rgb(root, _work_item(la, "png")).getpixel((0, 0)))

            palette = root / "alpha-p.png"
            palette_image = Image.new("P", (1, 1), 0)
            palette_image.putpalette([0, 0, 255] + [0, 0, 0] * 255)
            palette_image.save(palette, transparency=bytes([128] + [255] * 255))
            self.assertEqual((127, 127, 255), load_image_rgb(root, _work_item(palette, "png")).getpixel((0, 0)))

            disguised = root / "disguised.jpg"
            Image.new("RGB", (4, 4), "red").save(disguised, format="PNG")
            with self.assertRaises(CaptionImageDecodeError):
                load_image_rgb(root, _work_item(disguised, "jpeg"))

            wide = resize_for_model(Image.new("RGB", (700, 300), "red"))
            self.assertEqual((255, 255, 255), wide.getpixel((224, 0)))
            self.assertGreater(wide.getpixel((224, 224))[0], 250)
            self.assertLess(wide.getpixel((224, 224))[1], 5)

            tall = resize_for_model(Image.new("RGB", (300, 700), "blue"))
            self.assertEqual((255, 255, 255), tall.getpixel((0, 224)))
            self.assertGreater(tall.getpixel((224, 224))[2], 250)
            self.assertLess(tall.getpixel((224, 224))[0], 5)

            exact = resize_for_model(Image.new("RGB", (512, 512), (1, 2, 3)))
            self.assertEqual((1, 2, 3), exact.getpixel((224, 224)))

    def test_decompression_bomb_policy_is_blocking(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "too-large.png"
            Image.new("RGB", (3, 3), "red").save(path)
            with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1):
                with self.assertRaises(CaptionImageDecodeError):
                    load_image_rgb(root, _work_item(path, "png"))

    def test_truncated_and_multi_frame_images_are_rejected(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            truncated = root / "truncated.jpg"
            Image.new("RGB", (64, 64), "red").save(truncated)
            data = truncated.read_bytes()
            truncated.write_bytes(data[: len(data) // 2])
            with self.assertRaises(CaptionImageDecodeError):
                load_image_rgb(root, _work_item(truncated, "jpeg"))

            animated = root / "animated.webp"
            try:
                Image.new("RGB", (8, 8), "red").save(
                    animated,
                    format="WEBP",
                    save_all=True,
                    append_images=[Image.new("RGB", (8, 8), "blue")],
                    duration=100,
                    loop=0,
                )
            except OSError:
                self.skipTest("this Pillow build has no animated WebP support")
            with self.assertRaises(CaptionImageDecodeError):
                load_image_rgb(root, _work_item(animated, "webp"))

    def test_worker_returns_result_no_tags_and_retriable_issues(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "sample.png"
            Image.new("RGB", (8, 8), "red").save(image_path)
            item = _work_item(image_path, "png")
            predictions = (CaptionPrediction("blue_eyes", 0.9, "general", 0),)
            model = _WorkerModel(predictions)
            worker = _ready_worker(root, model)

            result = parse_caption_outcome(worker.process(item))
            self.assertIsInstance(result, CaptionResultV1)
            self.assertEqual("anima style, blue eyes", result.formattedTxt)

            worker.hello["profile"] = "danbooru"
            danbooru_item = {**item, "source": "danbooru"}
            danbooru = parse_caption_outcome(worker.process(danbooru_item))
            self.assertIsInstance(danbooru, CaptionResultV1)
            self.assertEqual("danbooru", danbooru.source)
            worker.hello["profile"] = "e621"

            model.predictions = ()
            no_tags = parse_caption_outcome(worker.process(item))
            self.assertIsInstance(no_tags, CaptionIssueResultV1)
            self.assertEqual("caption_no_tags", no_tags.code)
            self.assertFalse(no_tags.retriable)
            self.assertIsNone(no_tags.repairStartModule)

            model.error = CaptionInferenceError("injected inference error")
            inference = parse_caption_outcome(worker.process(item))
            self.assertIsInstance(inference, CaptionIssueResultV1)
            self.assertEqual("caption_inference_failed", inference.code)
            self.assertTrue(inference.retriable)

            bad_path = root / "bad.png"
            bad_path.write_bytes(b"not an image")
            decode = parse_caption_outcome(worker.process(_work_item(bad_path, "png")))
            self.assertIsInstance(decode, CaptionIssueResultV1)
            self.assertEqual("caption_image_decode_failed", decode.code)

            changed = dict(item)
            changed["imageMtimeNs"] = int(changed["imageMtimeNs"]) + 1
            with self.assertRaises(CaptionSourceFingerprintError):
                worker.process(changed)


if __name__ == "__main__":
    unittest.main()
