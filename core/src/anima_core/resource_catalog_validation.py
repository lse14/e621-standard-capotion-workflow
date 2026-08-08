from __future__ import annotations

import re
import threading
from typing import Any, Literal
from urllib.parse import urlparse

from .path_safety import PathSafetyError, safe_relative_path


ResourceKind = Literal[
    "replacement-index", "classification-index", "tagging-model", "dropout-model", "ocr-model", "tokenizer",
]
ProfileId = Literal["e621", "danbooru"]

MAX_MANIFEST_BYTES = 1_048_576
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMON_FIELDS_V1 = {
    "schemaVersion", "kind", "resourceId", "resourceVersion", "profile", "displayName",
    "description", "runtimeFormat", "entrypoints", "files", "metadata", "documentation",
}
COMMON_FIELDS_V2 = COMMON_FIELDS_V1 | {"distribution"}
KIND_LAYOUT: dict[ResourceKind, tuple[str, str, str, frozenset[str]]] = {
    "replacement-index": (
        "replacement-indexes", "replacementIndex", "e621-replacement-csv-v1", frozenset({"index"}),
    ),
    "classification-index": (
        "classification-indexes", "classificationIndex", "e621-classification-index-v1",
        frozenset({"dictionary", "countDatabase"}),
    ),
    "tagging-model": (
        "tagging-models", "taggingModel", "e621-eva02-onnx-v1",
        frozenset({"model", "modelData", "preprocess", "tags", "thresholds"}),
    ),
    "dropout-model": (
        "dropout-models", "dropoutModel", "lse14-scorer-5k-v1",
        frozenset({"clip", "fusion", "jtp3", "waifu"}),
    ),
}
V2_TAGGING_LAYOUTS: dict[str, frozenset[str]] = {
    "cl-tagger-v2-onnx-v1": frozenset({"model", "modelData", "metadata", "vocabulary", "thresholds"}),
    "wd-eva02-large-tagger-v3-onnx-v1": frozenset({"model", "selectedTags", "preprocess", "thresholds"}),
}
V2_CLASSIFICATION_LAYOUTS: dict[str, frozenset[str]] = {
    "danbooru-classification-index-v1": frozenset({"dictionary", "countRules", "countDatabase"}),
}
OCR_MODEL_DIRECTORY = "ocr-models"
OCR_MODEL_RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
OCR_MODEL_RUNTIME_FORMAT = "ppocrv5-server-paddle-v1"
OCR_MODEL_ENTRYPOINTS = frozenset({"detection", "recognition", "textlineOrientation"})
OCR_MODEL_IDENTITIES = {
    "detection": "PP-OCRv5_server_det",
    "recognition": "PP-OCRv5_server_rec",
    "textlineOrientation": "PP-LCNet_x1_0_textline_ori",
}
TOKENIZER_DIRECTORY = "tokenizers"
TOKENIZER_RESOURCE_IDENTITIES = {
    "tokenizer-qwen3-0.6b-anima-v1": "Qwen/Qwen3-0.6B",
    "tokenizer-qwen3-vl-4b-krea2-v1": "Qwen/Qwen3-VL-4B-Instruct",
}
TOKENIZER_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")
TOKENIZER_REVISION = re.compile(r"^[0-9a-f]{40}$")
TOKENIZER_MANIFEST_FIELDS = {
    "schemaVersion", "kind", "resourceId", "owner", "profile", "resourceVersion", "officialModelId", "revision",
    "tokenizerFamily", "contextLimit", "rootRelativePath", "files", "fingerprint", "distribution",
}
TOKENIZER_FILE_ALLOWLIST = frozenset({
    "added_tokens.json", "config.json", "merges.txt", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json", "vocab.json", "vocab.txt",
})
TOKENIZER_REQUIRED_FILES = frozenset({"config.json", "tokenizer.json"})
CATEGORY_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CACHE_LOCK = threading.Lock()


class ResourceCatalogError(ValueError):
    pass


def _text(value: object, field: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ResourceCatalogError(f"{field} must be non-blank and trimmed")
    if len(value.encode("utf-8")) > max_bytes or "\x00" in value:
        raise ResourceCatalogError(f"{field} is too long or contains NUL")
    return value


def _localized(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"zh-CN", "en"}:
        raise ResourceCatalogError(f"{field} must contain zh-CN and en")
    return {language: _text(value[language], f"{field}.{language}") for language in ("zh-CN", "en")}


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResourceCatalogError(f"{field} must be a relative path")
    try:
        return safe_relative_path(value)
    except PathSafetyError as exc:
        raise ResourceCatalogError(f"{field} is unsafe: {exc}") from exc


def _positive(value: object, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ResourceCatalogError(f"{field} must be an integer of at least {minimum}")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ResourceCatalogError(f"{field} must be a lowercase SHA-256")
    return value


def _https_url(value: object, field: str) -> str:
    result = _text(value, field, max_bytes=2048)
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ResourceCatalogError(f"{field} must be an absolute HTTPS URL without credentials")
    return result


def _distribution(value: object, *, allow_unverified_license: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("mode") not in {"bundled", "local-only"}:
        raise ResourceCatalogError("distribution mode is invalid")
    if value["mode"] == "bundled":
        if set(value) != {"mode"}:
            raise ResourceCatalogError("bundled distribution fields are invalid")
        return {"mode": "bundled"}
    if allow_unverified_license and set(value) == {"mode", "sourceUrl", "licenseStatus"}:
        if value["licenseStatus"] != "unverified":
            raise ResourceCatalogError("local-only licenseStatus is invalid")
        return {
            "mode": "local-only",
            "sourceUrl": _https_url(value["sourceUrl"], "distribution.sourceUrl"),
            "licenseStatus": "unverified",
        }
    if set(value) != {"mode", "sourceUrl", "licenseUrl"}:
        raise ResourceCatalogError("local-only distribution fields are invalid")
    return {
        "mode": "local-only",
        "sourceUrl": _https_url(value["sourceUrl"], "distribution.sourceUrl"),
        "licenseUrl": _https_url(value["licenseUrl"], "distribution.licenseUrl"),
    }


def _category_list(value: object, field: str, *, lowercase: bool) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ResourceCatalogError(f"{field} must be a bounded non-empty array")
    result = [_text(item, field) for item in value]
    if lowercase and any(not CATEGORY_ID.fullmatch(item) for item in result):
        raise ResourceCatalogError(f"{field} contains an invalid category identifier")
    if len(set(result)) != len(result):
        raise ResourceCatalogError(f"{field} must be unique")
    return result


def _metadata(
    kind: ResourceKind,
    value: object,
    *,
    schema_version: int,
    runtime_format: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResourceCatalogError("metadata must be an object")
    if kind == "ocr-model":
        if schema_version != 2 or runtime_format != OCR_MODEL_RUNTIME_FORMAT:
            raise ResourceCatalogError("ocr model metadata is invalid")
        if set(value) != {"models", "inference"}:
            raise ResourceCatalogError("ocr model metadata fields are invalid")
        models = value["models"]
        inference = value["inference"]
        if not isinstance(models, dict) or models != OCR_MODEL_IDENTITIES:
            raise ResourceCatalogError("ocr model metadata models are invalid")
        required_inference = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": True,
            "textRecScoreThresh": 0,
            "textDetLimitSideLen": 1920,
            "textDetLimitType": "max",
        }
        if not isinstance(inference, dict) or set(inference) != set(required_inference):
            raise ResourceCatalogError("ocr model metadata inference is invalid")
        boolean_keys = {
            "useDocOrientationClassify", "useDocUnwarping", "useTextlineOrientation",
        }
        if any(type(inference[key]) is not bool for key in boolean_keys):
            raise ResourceCatalogError("ocr model metadata inference is invalid")
        if type(inference["textRecScoreThresh"]) is not int or type(inference["textDetLimitSideLen"]) is not int:
            raise ResourceCatalogError("ocr model metadata inference is invalid")
        if inference != required_inference:
            raise ResourceCatalogError("ocr model metadata inference is invalid")
        return {
            "models": dict(OCR_MODEL_IDENTITIES),
            "inference": dict(required_inference),
        }
    if schema_version == 2 and kind == "tagging-model":
        if runtime_format not in V2_TAGGING_LAYOUTS:
            raise ResourceCatalogError("resource manifest v2 runtime is unsupported")
        required = {
            "tagCount", "modelCategories", "adjustableCategories", "excludedCategories",
            "vocabularyFingerprint",
        }
        if set(value) != required:
            raise ResourceCatalogError("tagging model v2 metadata fields are invalid")
        model_categories = _category_list(value["modelCategories"], "metadata.modelCategories", lowercase=False)
        adjustable = _category_list(
            value["adjustableCategories"], "metadata.adjustableCategories", lowercase=True,
        )
        excluded = _category_list(value["excludedCategories"], "metadata.excludedCategories", lowercase=True)
        normalized_model = [item.lower() for item in model_categories]
        if len(set(normalized_model)) != len(normalized_model):
            raise ResourceCatalogError("metadata.modelCategories must be unique ignoring case")
        if set(adjustable) & set(excluded) or set(adjustable) | set(excluded) != set(normalized_model):
            raise ResourceCatalogError("adjustable and excluded categories must partition modelCategories")
        tag_count = _positive(value["tagCount"], "metadata.tagCount")
        if tag_count > 1_000_000:
            raise ResourceCatalogError("metadata.tagCount exceeds the caption protocol limit")
        return {
            "tagCount": tag_count,
            "modelCategories": model_categories,
            "adjustableCategories": adjustable,
            "excludedCategories": excluded,
            "vocabularyFingerprint": _sha256(
                value["vocabularyFingerprint"], "metadata.vocabularyFingerprint",
            ),
        }
    if kind == "replacement-index":
        required = {"ruleCount", "actionCounts", "pipeReplacementCount", "literalKeepPipeCount"}
        if set(value) != required or not isinstance(value.get("actionCounts"), dict):
            raise ResourceCatalogError("replacement metadata fields are invalid")
        counts = value["actionCounts"]
        if set(counts) != {"keep", "replace", "drop"}:
            raise ResourceCatalogError("replacement actionCounts fields are invalid")
        normalized = {
            "ruleCount": _positive(value["ruleCount"], "metadata.ruleCount"),
            "actionCounts": {
                name: _positive(counts[name], f"metadata.actionCounts.{name}", allow_zero=True)
                for name in ("keep", "replace", "drop")
            },
            "pipeReplacementCount": _positive(
                value["pipeReplacementCount"], "metadata.pipeReplacementCount", allow_zero=True
            ),
            "literalKeepPipeCount": _positive(
                value["literalKeepPipeCount"], "metadata.literalKeepPipeCount", allow_zero=True
            ),
        }
        if sum(normalized["actionCounts"].values()) != normalized["ruleCount"]:
            raise ResourceCatalogError("replacement action counts do not equal ruleCount")
        return normalized
    if kind == "classification-index":
        required = {
            "dictionaryEntryCount", "wikiDataSourceId", "wikiApplicationId", "wikiSchemaVersion",
            "wikiSchemaFingerprint", "wikiPageTitles",
        }
        if runtime_format == "danbooru-classification-index-v1":
            required |= {
                "catalogSnapshot", "catalogSourceUrl", "catalogSourceSizeBytes", "catalogSourceSha256",
                "wikiSourceUrl", "wikiSourceSizeBytes", "wikiSourceSha256",
                "supportedVocabularyFingerprints",
            }
        if set(value) != required:
            raise ResourceCatalogError("classification metadata fields are invalid")
        titles = value["wikiPageTitles"]
        if not isinstance(titles, list) or not 1 <= len(titles) <= 64:
            raise ResourceCatalogError("classification wikiPageTitles is invalid")
        normalized_titles = [_text(title, "metadata.wikiPageTitles", max_bytes=512) for title in titles]
        if normalized_titles != sorted(set(normalized_titles)):
            raise ResourceCatalogError("classification wikiPageTitles must be sorted and unique")
        normalized: dict[str, Any] = {
            "dictionaryEntryCount": _positive(value["dictionaryEntryCount"], "metadata.dictionaryEntryCount"),
            "wikiDataSourceId": _text(value["wikiDataSourceId"], "metadata.wikiDataSourceId"),
            "wikiApplicationId": _positive(value["wikiApplicationId"], "metadata.wikiApplicationId"),
            "wikiSchemaVersion": _positive(value["wikiSchemaVersion"], "metadata.wikiSchemaVersion"),
            "wikiSchemaFingerprint": _sha256(value["wikiSchemaFingerprint"], "metadata.wikiSchemaFingerprint"),
            "wikiPageTitles": normalized_titles,
        }
        if runtime_format == "danbooru-classification-index-v1":
            fingerprints = value["supportedVocabularyFingerprints"]
            if not isinstance(fingerprints, list) or not 1 <= len(fingerprints) <= 16:
                raise ResourceCatalogError("classification supportedVocabularyFingerprints is invalid")
            normalized_fingerprints = [
                _sha256(item, "metadata.supportedVocabularyFingerprints") for item in fingerprints
            ]
            if normalized_fingerprints != sorted(set(normalized_fingerprints)):
                raise ResourceCatalogError(
                    "classification supportedVocabularyFingerprints must be sorted and unique"
                )
            normalized.update({
                "catalogSnapshot": _text(value["catalogSnapshot"], "metadata.catalogSnapshot"),
                "catalogSourceUrl": _https_url(value["catalogSourceUrl"], "metadata.catalogSourceUrl"),
                "catalogSourceSizeBytes": _positive(
                    value["catalogSourceSizeBytes"], "metadata.catalogSourceSizeBytes",
                ),
                "catalogSourceSha256": _sha256(
                    value["catalogSourceSha256"], "metadata.catalogSourceSha256",
                ),
                "wikiSourceUrl": _https_url(value["wikiSourceUrl"], "metadata.wikiSourceUrl"),
                "wikiSourceSizeBytes": _positive(
                    value["wikiSourceSizeBytes"], "metadata.wikiSourceSizeBytes",
                ),
                "wikiSourceSha256": _sha256(
                    value["wikiSourceSha256"], "metadata.wikiSourceSha256",
                ),
                "supportedVocabularyFingerprints": normalized_fingerprints,
            })
        return normalized
    if kind == "tagging-model":
        if set(value) != {"tagCount", "categories"} or not isinstance(value.get("categories"), list):
            raise ResourceCatalogError("tagging model metadata fields are invalid")
        categories = [_text(item, "metadata.categories") for item in value["categories"]]
        if categories != ["general", "character", "species", "rating"]:
            raise ResourceCatalogError("tagging model categories are unsupported")
        return {"tagCount": _positive(value["tagCount"], "metadata.tagCount"), "categories": categories}
    if value:
        raise ResourceCatalogError("dropout model metadata must be empty for this runtime format")
    return {}
