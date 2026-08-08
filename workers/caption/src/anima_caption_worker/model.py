from __future__ import annotations

import csv
import json
import math
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .image import cl_image_to_tensor, image_to_tensor, wd_image_to_tensor
from .resource import WorkerCaptionResource


EXPECTED_TAG_COUNT = 8_783
EXPECTED_CATEGORIES = frozenset({"general", "character", "species", "rating"})
EXPECTED_MEAN = (0.7058010139741189, 0.6675836722220271, 0.6626594977602157)
EXPECTED_STD = (0.3200751353807431, 0.3323665192448117, 0.3356149715782511)
CL_TAG_COUNT = 106_536
CL_CATEGORIES = frozenset({"general", "character", "copyright", "meta", "rating", "quality"})
CL_ADJUSTABLE = frozenset({"general", "character", "copyright"})
CL_EXCLUDED = frozenset({"meta", "rating", "quality"})
CL_DEFAULT_THRESHOLD = 0.55
WD_TAG_COUNT = 10_861
WD_CATEGORIES = frozenset({"general", "character", "rating"})
WD_ADJUSTABLE = frozenset({"general", "character"})
WD_EXCLUDED = frozenset({"rating"})
WD_CATEGORY_IDS = {"0": "general", "4": "character", "9": "rating"}
WD_PREPROCESS = {
    "inputSize": [448, 448],
    "layout": "NHWC",
    "padToSquare": True,
    "backgroundColor": "#FFFFFF",
    "interpolation": "bicubic",
    "channelOrder": "BGR",
    "valueRange": [0, 255],
}


class CaptionModelError(RuntimeError):
    pass


class CaptionMetadataError(CaptionModelError):
    pass


class CaptionSessionError(CaptionModelError):
    pass


class CaptionInferenceError(CaptionModelError):
    pass


class SessionInput(Protocol):
    name: str
    shape: Sequence[object]
    type: str


class Session(Protocol):
    def get_inputs(self) -> Sequence[SessionInput]: ...
    def get_providers(self) -> Sequence[str]: ...
    def run(self, output_names: object, input_feed: dict[str, object]) -> Sequence[object]: ...


SessionFactory = Callable[[Path], Session]


@dataclass(frozen=True)
class CaptionMetadata:
    tag_names: tuple[str, ...]
    categories: tuple[str, ...]
    default_thresholds: dict[str, float]
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    profile: str = "e621"
    adjustable_categories: tuple[str, ...] = ("general", "character", "species", "rating")
    excluded_categories: tuple[str, ...] = ()
    input_width: int = 448
    input_height: int = 448


@dataclass(frozen=True)
class CaptionPrediction:
    raw_tag: str
    score: float
    category: str
    model_index: int


def _single_rating(predictions: list[CaptionPrediction]) -> list[CaptionPrediction]:
    """E621 ratings are mutually exclusive; keep only the highest scoring one."""
    kept: list[CaptionPrediction] = []
    rating_seen = False
    for prediction in predictions:
        if prediction.category == "rating":
            if rating_seen:
                continue
            rating_seen = True
        kept.append(prediction)
    return kept


def resolve_thresholds(policy: dict[str, object], defaults: dict[str, float]) -> dict[str, float]:
    expected_categories = frozenset(defaults)
    if not expected_categories:
        raise CaptionMetadataError("caption model has no adjustable threshold categories")
    mode = policy.get("mode")
    if mode == "model_default":
        values = defaults
    elif mode == "uniform":
        values = {category: policy.get("uniformThreshold") for category in expected_categories}
    elif mode == "per_category":
        raw_values = policy.get("categoryThresholds")
        if not isinstance(raw_values, dict):
            raise CaptionMetadataError("per-category threshold policy is invalid")
        values = raw_values
    else:
        raise CaptionMetadataError("caption threshold policy is invalid")
    if set(values) != expected_categories:
        raise CaptionMetadataError("caption thresholds must exactly match the adjustable model categories")
    resolved: dict[str, float] = {}
    for category, raw_value in values.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise CaptionMetadataError(f"threshold for {category} is not numeric")
        value = float(raw_value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise CaptionMetadataError(f"threshold for {category} is outside 0..1")
        resolved[category] = value
    return resolved


def _load_json(path: Path, field: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaptionMetadataError(f"{field} is not valid UTF-8 JSON") from exc


def load_metadata(paths: dict[str, Path]) -> CaptionMetadata:
    tags = _load_json(paths["tags.json"], "tags.json")
    if not isinstance(tags, dict):
        raise CaptionMetadataError("tags.json must be an object")
    names = tags.get("tag_names")
    index_categories = tags.get("idx_to_category")
    if not isinstance(names, list) or len(names) != EXPECTED_TAG_COUNT or not isinstance(index_categories, dict):
        raise CaptionMetadataError("tags.json does not describe exactly 8783 tags")
    if set(index_categories) != {str(index) for index in range(EXPECTED_TAG_COUNT)}:
        raise CaptionMetadataError("tags.json category indices are incomplete")
    validated_names: list[str] = []
    categories: list[str] = []
    seen: set[str] = set()
    for index, raw_name in enumerate(names):
        if (
            not isinstance(raw_name, str)
            or not raw_name
            or raw_name != raw_name.strip()
            or len(raw_name.encode("utf-8")) > 512
            or any(character in raw_name for character in ",\r\n\x00")
            or raw_name in seen
        ):
            raise CaptionMetadataError(f"tags.json contains an invalid or duplicate tag at index {index}")
        category = index_categories[str(index)]
        if category not in EXPECTED_CATEGORIES:
            raise CaptionMetadataError(f"tags.json contains an unknown category at index {index}")
        seen.add(raw_name)
        validated_names.append(raw_name)
        categories.append(category)

    thresholds = _load_json(paths["thresholds.json"], "thresholds.json")
    if not isinstance(thresholds, dict) or set(thresholds) != EXPECTED_CATEGORIES:
        raise CaptionMetadataError("thresholds.json must contain exactly four categories")
    validated_thresholds: dict[str, float] = {}
    for category, raw_value in thresholds.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise CaptionMetadataError(f"threshold for {category} is not numeric")
        value = float(raw_value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise CaptionMetadataError(f"threshold for {category} is outside 0..1")
        validated_thresholds[category] = value

    preprocess = _load_json(paths["preprocess.json"], "preprocess.json")
    expected = {
        "test": [
            {"type": "PadToSize", "size": [512, 512], "background_color": "white"},
            {"type": "Resize", "size": [448, 448], "interpolation": "bicubic"},
            {"type": "CenterCrop", "size": [448, 448]},
            {"type": "ToTensor"},
            {"type": "Normalize", "mean": list(EXPECTED_MEAN), "std": list(EXPECTED_STD)},
        ]
    }
    if preprocess != expected:
        raise CaptionMetadataError("preprocess.json does not match the frozen EVA02 pipeline")
    return CaptionMetadata(
        tuple(validated_names), tuple(categories), validated_thresholds,
        EXPECTED_MEAN, EXPECTED_STD,
    )


def _default_session_factory(model_path: Path) -> Session:
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise CaptionSessionError("onnxruntime is unavailable in the caption runtime") from exc
    if hasattr(ort, "preload_dlls"):
        try:
            with redirect_stdout(sys.stderr):
                ort.preload_dlls()
        except Exception:
            # CUDA support is optional; a preload failure must not block CPU.
            pass
    try:
        available = ort.get_available_providers()
    except Exception as exc:
        raise CaptionSessionError("unable to query ONNX Runtime execution providers") from exc
    providers = [provider for provider in ("CUDAExecutionProvider", "CPUExecutionProvider") if provider in available]
    if "CPUExecutionProvider" not in providers:
        raise CaptionSessionError(f"CPUExecutionProvider is required; available providers: {available}")
    try:
        return ort.InferenceSession(str(model_path), providers=providers)
    except Exception as exc:
        if providers[0] == "CUDAExecutionProvider":
            try:
                return ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            except Exception as cpu_exc:
                raise CaptionSessionError("unable to create the EVA02 ONNX session with CUDA or CPU") from cpu_exc
        raise CaptionSessionError("unable to create the EVA02 ONNX session") from exc


def _session_details(
    model_path: Path,
    *,
    session_factory: SessionFactory | None,
    expected_shape: tuple[object, object, object, object],
    dynamic_batch: bool,
    runtime_name: str,
) -> tuple[Session, str, str]:
    factory = session_factory or _default_session_factory
    try:
        session = factory(model_path)
    except CaptionModelError:
        raise
    except Exception as exc:
        raise CaptionSessionError("caption session factory failed") from exc
    try:
        inputs = list(session.get_inputs())
        providers = list(session.get_providers())
    except Exception as exc:
        raise CaptionSessionError("unable to inspect the ONNX session") from exc
    shape = list(inputs[0].shape) if len(inputs) == 1 else []
    if dynamic_batch:
        batch = shape[0] if len(shape) == 4 else object()
        batch_valid = type(batch) is int and batch == 1 or batch is None or isinstance(batch, str) and bool(batch)
        shape_valid = len(shape) == 4 and batch_valid and tuple(shape[1:]) == tuple(expected_shape[1:])
    else:
        shape_valid = shape in (list(expected_shape), ["batch_size", *expected_shape[1:]])
    if not shape_valid:
        raise CaptionSessionError(f"{runtime_name} ONNX model input shape is invalid")
    if not isinstance(inputs[0].name, str) or not inputs[0].name:
        raise CaptionSessionError("ONNX model input has no name")
    if inputs[0].type != "tensor(float)":
        raise CaptionSessionError("ONNX model input must use float32")
    if not providers or not isinstance(providers[0], str) or not providers[0]:
        raise CaptionSessionError("ONNX session did not report an execution provider")
    return session, inputs[0].name, providers[0]


def _model_probabilities(
    session: Session,
    input_name: str,
    tensor: object,
    *,
    tag_count: int,
    apply_sigmoid: bool,
) -> object:
    try:
        import numpy as np
    except ImportError as exc:
        raise CaptionInferenceError("NumPy is unavailable in the caption runtime") from exc
    try:
        outputs = list(session.run(None, {input_name: tensor}))
    except Exception as exc:
        raise CaptionInferenceError("ONNX Runtime failed to infer the caption") from exc
    if len(outputs) != 1:
        raise CaptionInferenceError("caption model must return exactly one output")
    scores = np.asarray(outputs[0])
    if scores.shape != (1, tag_count) or not np.issubdtype(scores.dtype, np.number):
        raise CaptionInferenceError("caption model output shape or dtype is invalid")
    flat = scores[0]
    if not np.all(np.isfinite(flat)):
        raise CaptionInferenceError("caption model returned a non-finite score")
    probabilities = flat.astype(np.float64)
    if apply_sigmoid:
        decay = np.exp(-np.abs(probabilities))
        probabilities = np.where(
            probabilities >= 0.0,
            1.0 / (1.0 + decay),
            decay / (1.0 + decay),
        )
    elif np.any(probabilities < 0.0) or np.any(probabilities > 1.0):
        raise CaptionInferenceError("probability model returned a score outside 0..1")
    return probabilities


def _select_predictions(
    metadata: CaptionMetadata,
    probabilities: object,
    thresholds: dict[str, float],
    *,
    single_rating: bool,
) -> tuple[CaptionPrediction, ...]:
    if set(thresholds) != set(metadata.adjustable_categories):
        raise CaptionInferenceError("caption thresholds do not match the loaded adapter")
    predictions: list[CaptionPrediction] = []
    for index, (raw_tag, category) in enumerate(
        zip(metadata.tag_names, metadata.categories, strict=True)
    ):
        if category in metadata.excluded_categories:
            continue
        if category not in thresholds:
            raise CaptionInferenceError(f"caption model category is not routable: {category}")
        score = float(probabilities[index])  # type: ignore[index]
        if score >= thresholds[category]:
            predictions.append(CaptionPrediction(raw_tag, score, category, index))
    predictions.sort(key=lambda item: (-item.score, item.model_index))
    deduplicated: list[CaptionPrediction] = []
    seen: set[str] = set()
    for prediction in predictions:
        if prediction.raw_tag in seen:
            continue
        seen.add(prediction.raw_tag)
        deduplicated.append(prediction)
    if single_rating:
        deduplicated = _single_rating(deduplicated)
    return tuple(deduplicated)


def _valid_tag_name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(character in value for character in ",\r\n\x00")
    ):
        raise CaptionMetadataError(f"{field} contains an invalid tag")
    return value


def _manifest_categories(
    metadata: dict[str, object],
    *,
    tag_count: int,
    model_categories: frozenset[str],
    adjustable: frozenset[str],
    excluded: frozenset[str],
) -> None:
    raw_model = metadata.get("modelCategories")
    raw_adjustable = metadata.get("adjustableCategories")
    raw_excluded = metadata.get("excludedCategories")
    if (
        metadata.get("tagCount") != tag_count
        or not isinstance(raw_model, list)
        or {str(item).lower() for item in raw_model} != model_categories
        or not isinstance(raw_adjustable, list)
        or set(raw_adjustable) != adjustable
        or not isinstance(raw_excluded, list)
        or set(raw_excluded) != excluded
    ):
        raise CaptionMetadataError("resource manifest metadata does not match the selected adapter")


def _load_thresholds(path: Path, expected_categories: frozenset[str]) -> dict[str, float]:
    raw = _load_json(path, path.name)
    if not isinstance(raw, dict) or set(raw) != expected_categories:
        raise CaptionMetadataError("thresholds do not match the adjustable adapter categories")
    values: dict[str, float] = {}
    for category in sorted(expected_categories):
        value = raw[category]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CaptionMetadataError(f"threshold for {category} is not numeric")
        threshold = float(value)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise CaptionMetadataError(f"threshold for {category} is outside 0..1")
        values[category] = threshold
    return values


def _indexed_tag_names(value: object, expected_count: int) -> tuple[str, ...]:
    if isinstance(value, list):
        raw_names = value
    elif isinstance(value, dict) and set(value) == {str(index) for index in range(expected_count)}:
        raw_names = [value[str(index)] for index in range(expected_count)]
    else:
        raise CaptionMetadataError("model vocabulary idx_to_tag is invalid")
    if len(raw_names) != expected_count:
        raise CaptionMetadataError("model vocabulary tag count is invalid")
    names = tuple(_valid_tag_name(name, "model vocabulary") for name in raw_names)
    if len(set(names)) != len(names):
        raise CaptionMetadataError("model vocabulary contains duplicate tags")
    return names


def load_cl_metadata(paths: dict[str, Path], manifest_metadata: dict[str, object]) -> CaptionMetadata:
    _manifest_categories(
        manifest_metadata,
        tag_count=CL_TAG_COUNT,
        model_categories=CL_CATEGORIES,
        adjustable=CL_ADJUSTABLE,
        excluded=CL_EXCLUDED,
    )
    model_metadata = _load_json(paths["metadata"], "model_metadata.json")
    if not isinstance(model_metadata, dict) or not model_metadata:
        raise CaptionMetadataError("model_metadata.json must be a non-empty object")
    for key in ("tag_count", "num_tags", "num_classes"):
        if key in model_metadata and model_metadata[key] != CL_TAG_COUNT:
            raise CaptionMetadataError(f"model_metadata.json {key} does not match v2_00")
    for key in ("image_size", "input_size"):
        if key in model_metadata:
            value = model_metadata[key]
            if value not in (384, "384", [384, 384], [3, 384, 384]):
                raise CaptionMetadataError(f"model_metadata.json {key} does not describe 384px input")
    vocabulary = _load_json(paths["vocabulary"], "model_vocabulary.json")
    if not isinstance(vocabulary, dict):
        raise CaptionMetadataError("model_vocabulary.json must be an object")
    required = {"idx_to_tag", "tag_to_idx", "tag_to_category"}
    if not required.issubset(vocabulary):
        raise CaptionMetadataError("model_vocabulary.json fields are incomplete")
    names = _indexed_tag_names(vocabulary["idx_to_tag"], CL_TAG_COUNT)
    tag_to_idx = vocabulary["tag_to_idx"]
    tag_to_category = vocabulary["tag_to_category"]
    if not isinstance(tag_to_idx, dict) or set(tag_to_idx) != set(names):
        raise CaptionMetadataError("model vocabulary tag_to_idx is invalid")
    if any(type(tag_to_idx[name]) is not int or tag_to_idx[name] != index for index, name in enumerate(names)):
        raise CaptionMetadataError("model vocabulary indices are inconsistent")
    if not isinstance(tag_to_category, dict) or set(tag_to_category) != set(names):
        raise CaptionMetadataError("model vocabulary tag_to_category is invalid")
    categories: list[str] = []
    for name in names:
        category = tag_to_category[name]
        if not isinstance(category, str) or category.lower() not in CL_CATEGORIES:
            raise CaptionMetadataError(f"model vocabulary category is invalid for {name}")
        categories.append(category.lower())
    thresholds = _load_thresholds(paths["thresholds"], CL_ADJUSTABLE)
    if any(value != CL_DEFAULT_THRESHOLD for value in thresholds.values()):
        raise CaptionMetadataError("CL v2_00 model defaults must be 0.55")
    return CaptionMetadata(
        names,
        tuple(categories),
        thresholds,
        (0.5, 0.5, 0.5),
        (0.5, 0.5, 0.5),
        profile="danbooru",
        adjustable_categories=tuple(sorted(CL_ADJUSTABLE)),
        excluded_categories=tuple(sorted(CL_EXCLUDED)),
        input_width=384,
        input_height=384,
    )


def load_wd_metadata(paths: dict[str, Path], manifest_metadata: dict[str, object]) -> CaptionMetadata:
    _manifest_categories(
        manifest_metadata,
        tag_count=WD_TAG_COUNT,
        model_categories=WD_CATEGORIES,
        adjustable=WD_ADJUSTABLE,
        excluded=WD_EXCLUDED,
    )
    names: list[str] = []
    categories: list[str] = []
    try:
        with paths["selectedTags"].open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not {"name", "category"}.issubset(reader.fieldnames):
                raise CaptionMetadataError("selected_tags.csv columns are incomplete")
            for row in reader:
                names.append(_valid_tag_name(row.get("name"), "selected_tags.csv"))
                raw_category = row.get("category")
                if raw_category not in WD_CATEGORY_IDS:
                    raise CaptionMetadataError("selected_tags.csv category is invalid")
                categories.append(WD_CATEGORY_IDS[raw_category])
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CaptionMetadataError("selected_tags.csv is unreadable") from exc
    if len(names) != WD_TAG_COUNT or len(set(names)) != len(names):
        raise CaptionMetadataError("selected_tags.csv tag count or uniqueness is invalid")
    preprocess = _load_json(paths["preprocess"], "preprocess.json")
    if preprocess != WD_PREPROCESS:
        raise CaptionMetadataError("preprocess.json does not match the WD EVA02-Large pipeline")
    thresholds = _load_thresholds(paths["thresholds"], WD_ADJUSTABLE)
    return CaptionMetadata(
        tuple(names),
        tuple(categories),
        thresholds,
        (0.0, 0.0, 0.0),
        (1.0, 1.0, 1.0),
        profile="danbooru",
        adjustable_categories=tuple(sorted(WD_ADJUSTABLE)),
        excluded_categories=tuple(sorted(WD_EXCLUDED)),
        input_width=448,
        input_height=448,
    )


class TaggerAdapter(Protocol):
    metadata: CaptionMetadata
    provider: str
    session_loads: int

    def preprocess(self, image: object) -> object: ...
    def predict(self, tensor: object, thresholds: dict[str, float]) -> tuple[CaptionPrediction, ...]: ...


class CaptionModel:
    """Frozen E621 adapter retained as the compatibility-facing class."""

    def __init__(self, paths: dict[str, Path], *, session_factory: SessionFactory | None = None) -> None:
        self.metadata = load_metadata(paths)
        self.session, self.input_name, self.provider = _session_details(
            paths["model.onnx"],
            session_factory=session_factory,
            expected_shape=(1, 3, 448, 448),
            dynamic_batch=False,
            runtime_name="E621 EVA02",
        )
        self.session_loads = 1

    def preprocess(self, image: object) -> object:
        return image_to_tensor(image, self.metadata.mean, self.metadata.std)

    def predict(self, tensor: object, thresholds: dict[str, float]) -> tuple[CaptionPrediction, ...]:
        probabilities = _model_probabilities(
            self.session,
            self.input_name,
            tensor,
            tag_count=EXPECTED_TAG_COUNT,
            apply_sigmoid=True,
        )
        return _select_predictions(self.metadata, probabilities, thresholds, single_rating=True)


class ClTaggerAdapter:
    def __init__(
        self,
        paths: dict[str, Path],
        manifest_metadata: dict[str, object],
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.metadata = load_cl_metadata(paths, manifest_metadata)
        self.session, self.input_name, self.provider = _session_details(
            paths["model"],
            session_factory=session_factory,
            expected_shape=(1, 3, 384, 384),
            dynamic_batch=True,
            runtime_name="CL v2_00",
        )
        self.session_loads = 1

    def preprocess(self, image: object) -> object:
        return cl_image_to_tensor(image)

    def predict(self, tensor: object, thresholds: dict[str, float]) -> tuple[CaptionPrediction, ...]:
        probabilities = _model_probabilities(
            self.session,
            self.input_name,
            tensor,
            tag_count=CL_TAG_COUNT,
            apply_sigmoid=True,
        )
        return _select_predictions(self.metadata, probabilities, thresholds, single_rating=False)


class WdTaggerAdapter:
    def __init__(
        self,
        paths: dict[str, Path],
        manifest_metadata: dict[str, object],
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.metadata = load_wd_metadata(paths, manifest_metadata)
        self.session, self.input_name, self.provider = _session_details(
            paths["model"],
            session_factory=session_factory,
            expected_shape=(1, 448, 448, 3),
            dynamic_batch=True,
            runtime_name="WD EVA02-Large",
        )
        self.session_loads = 1

    def preprocess(self, image: object) -> object:
        return wd_image_to_tensor(image)

    def predict(self, tensor: object, thresholds: dict[str, float]) -> tuple[CaptionPrediction, ...]:
        probabilities = _model_probabilities(
            self.session,
            self.input_name,
            tensor,
            tag_count=WD_TAG_COUNT,
            apply_sigmoid=False,
        )
        return _select_predictions(self.metadata, probabilities, thresholds, single_rating=False)


def create_tagger_adapter(
    resource: WorkerCaptionResource,
    *,
    session_factory: SessionFactory | None = None,
) -> TaggerAdapter:
    if resource.runtime_format == "e621-eva02-onnx-v1" and resource.profile == "e621":
        paths = {
            "model.onnx": resource.entrypoints["model"],
            "model.onnx.data": resource.entrypoints["modelData"],
            "preprocess.json": resource.entrypoints["preprocess"],
            "tags.json": resource.entrypoints["tags"],
            "thresholds.json": resource.entrypoints["thresholds"],
        }
        return CaptionModel(paths, session_factory=session_factory)
    if resource.runtime_format == "cl-tagger-v2-onnx-v1" and resource.profile == "danbooru":
        return ClTaggerAdapter(resource.entrypoints, resource.metadata, session_factory=session_factory)
    if resource.runtime_format == "wd-eva02-large-tagger-v3-onnx-v1" and resource.profile == "danbooru":
        return WdTaggerAdapter(resource.entrypoints, resource.metadata, session_factory=session_factory)
    raise CaptionMetadataError("caption resource has no compatible tagger adapter")
