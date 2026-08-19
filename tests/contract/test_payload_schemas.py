"""F45: contracts/schemas is validated against real payloads instead of being decoration.

Before this test the only readers of contracts/schemas asserted `$id` strings, so every
schema could drift away from the code that produces the payloads without any failure.
"""
from __future__ import annotations

import json
import math
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.caption_protocol import (
    CaptionHelloResultV1,
    CaptionIssueResultV1,
    CaptionResultV1,
    CaptionTagV1,
    CaptionWorkItemV1,
)
from anima_core.classify_protocol import (
    ClassifyCountDecisionV1,
    ClassifyHelloResultV1,
    ClassifyIssueResultV1,
    ClassifyProjectionV1,
    ClassifyResultV1,
)
from anima_core.contracts import (
    COUNT_REVIEW_SCHEMA_VERSIONS,
    JobConfig,
    canonical_json,
    job_config_supports_nl_v4,
    job_config_supports_ocr,
    job_config_supports_token_budget,
    pipeline_module_ids,
    profile_supports_job_config_schema,
    validate_job_config,
)
from anima_core.job_preflight import config_from_dict
from tests.worker_test_support import worker_hello_payload
import anima_core.contracts as contracts

SCHEMAS = ROOT / "contracts" / "schemas"
INSTALL_ROOT = ROOT / ".runtime-build"
_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def _equal(value: object, expected: object) -> bool:
    # True == 1 in Python; JSON Schema treats booleans and numbers as different types.
    if isinstance(value, bool) != isinstance(expected, bool):
        return False
    return value == expected


def _matches_type(value: object, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int)
    if name == "number":
        return isinstance(value, (int, float)) and math.isfinite(float(value))
    return isinstance(value, _TYPES[name])


def validate(instance: object, schema: object, root: dict, path: str = "$") -> list[str]:
    """Validate against the JSON Schema 2020-12 subset the frozen contracts actually use."""
    if not isinstance(schema, dict):
        return [] if schema is True else [f"{path}: schema is not a boolean-true or object"]
    errors: list[str] = []
    if "$ref" in schema:
        reference = str(schema["$ref"])
        if not reference.startswith("#/"):
            raise ValueError(f"unsupported $ref: {reference}")
        target: object = root
        for part in reference[2:].split("/"):
            target = target[part]  # type: ignore[index]
        errors.extend(validate(instance, target, root, path))
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_matches_type(instance, str(name)) for name in names):
            errors.append(f"{path}: expected type {schema['type']}, got {type(instance).__name__}")
    if "const" in schema and not _equal(instance, schema["const"]):
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and not any(_equal(instance, option) for option in schema["enum"]):
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength")
        if "pattern" in schema and re.search(str(schema["pattern"]), instance) is None:
            errors.append(f"{path}: does not match {schema['pattern']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in instance}) != len(instance):
            errors.append(f"{path}: items are not unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], root, f"{path}[{index}]"))
        if "prefixItems" in schema:
            for index, item_schema in enumerate(schema["prefixItems"]):
                if index < len(instance):
                    errors.extend(validate(instance[index], item_schema, root, f"{path}[{index}]"))
    if isinstance(instance, dict):
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append(f"{path}: fewer than minProperties")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append(f"{path}: more than maxProperties")
        if "propertyNames" in schema:
            for name in instance:
                errors.extend(validate(name, schema["propertyNames"], root, f"{path}.<propertyName>"))
        for name in schema.get("required", ()):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                errors.extend(validate(value, properties[name], root, f"{path}.{name}"))
            else:
                additional = schema.get("additionalProperties")
                if additional is False:
                    errors.append(f"{path}: unexpected property {name!r}")
                elif isinstance(additional, dict):
                    errors.extend(validate(value, additional, root, f"{path}.{name}"))
    for option in schema.get("allOf", ()):
        errors.extend(validate(instance, option, root, path))
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        results = [validate(instance, option, root, path) for option in schema[keyword]]
        matched = sum(1 for result in results if not result)
        if (keyword == "anyOf" and not matched) or (keyword == "oneOf" and matched != 1):
            closest = min(results, key=len) if results else []
            errors.append(f"{path}: matched {matched} {keyword} branches; closest branch says {closest}")
    if "not" in schema and not validate(instance, schema["not"], root, path):
        errors.append(f"{path}: must not match the 'not' schema")
    if "if" in schema and not validate(instance, schema["if"], root, path):
        errors.extend(validate(instance, schema.get("then", True), root, path))
    elif "if" in schema:
        errors.extend(validate(instance, schema.get("else", True), root, path))
    return errors


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _wire(payload: dict) -> dict:
    """Validate what crosses stdio: dataclass tuples become JSON arrays on the way out."""
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _hello(runtime_id: str) -> dict:
    with worker_hello_payload(runtime_id, INSTALL_ROOT) as (mode, payload):
        assert mode == "normal", runtime_id
        return dict(payload)


def _classify_projection() -> ClassifyProjectionV1:
    return ClassifyProjectionV1(
        quality=(), count="solo", character="nala_(the_lion_king)", series="", artist="",
        appearance=("mammal",), tags=("blush",), environment=("forest",), nl="",
    )


def _classify_decision() -> ClassifyCountDecisionV1:
    return ClassifyCountDecisionV1(
        value="solo", baseValue="solo", selectedSource="wiki_tags", originalRaw=None, originalNormalized=None,
        wikiValue="solo", matchedTags=("solo",), conflict=False, issueCodes=(), warnings=(), appliedLowerBounds=(),
    )


def _caption_item() -> CaptionWorkItemV1:
    return CaptionWorkItemV1(
        sampleId=7, leaseId="lease-7", relativeImagePath="nested\\image.png", annotationKey="nested\\image",
        imageFormat="png", imageSize=123, imageMtimeNs=456, imageFileId="1:2",
    )


# Literal payloads mirror the worker lines named in each comment; the schema is the contract
# and this table is what makes a drift in either direction fail.
POLICY_HELLO_RESULT = {  # workers/policy/src/anima_policy_worker/worker.py:113-121
    "schemaVersion": 1, "payloadType": "policy_hello_result", "ready": True, "qualityEnabled": True,
    "device": "CPUExecutionProvider", "modelLoadCount": 1, "resourceFingerprint": "c" * 64,
}
POLICY_HELLO_REQUEST = {  # core/src/anima_core/policy_runner.py:137-143
    "schemaVersion": 1, "payloadType": "policy_hello_request", "jobId": "job-1", "configHash": "a" * 64,
    "datasetRoot": "E:\\dataset", "overlayRoot": "E:\\.dataset.anima-overlay-job-1",
    "resourceManifestRelativePath": "manifests\\resources\\policy-lse14.json", "resourceFingerprint": "c" * 64,
    "policy": {
        "policyVersion": "dataset-batch-policy-v1", "seed": "anima-policy-default-v1",
        "artist": {"enabled": True, "dropoutProbability": 0.0},
        "quality": {"enabled": True, "dropoutProbability": 0.0, "device": "auto", "batchSize": 4, "resourceId": "lse14-scorer-5k-v1"},
        "appearanceNl": {
            "enabled": True, "solo": {"dropNl": 0.7, "dropAppearance": 0.05},
            "nonSolo": {"dropNl": 0.05, "dropAppearance": 0.7}, "unknown": {"dropNl": 0.15, "dropAppearance": 0.15},
        },
    },
}
POLICY_PROCESS_REQUEST = {  # core/src/anima_core/policy_runner.py:226-231
    "schemaVersion": 1, "payloadType": "policy_process_request",
    "items": [{
        "schemaVersion": 1, "sampleId": 1, "leaseId": "lease-1", "relativeImagePath": "1_artist\\a.png",
        "annotationKey": "1_artist\\a", "imageSize": 1024, "imageMtimeNs": 1_000_000, "imageFileId": "volume:1",
    }],
}
POLICY_BATCH_RESULT = {  # workers/policy/src/anima_policy_worker/worker.py:255-269 and :182-192
    "schemaVersion": 1, "payloadType": "policy_batch_result", "modelLoadCount": 1,
    "outcomes": [
        {
            "schemaVersion": 1, "status": "prepared", "sampleId": 1, "leaseId": "lease-1",
            "relativeImagePath": "1_artist\\a.png", "preparedRelativePath": "prepared\\dropout\\lease-1.json",
            "sha256": "d" * 64, "aestheticScore": 6.5, "quality": ["masterpiece", "best quality"],
            "decision": {"artistDropped": False, "qualityDropped": False, "appearanceNlAction": "drop_nl"},
        },
        {
            "schemaVersion": 1, "status": "issue", "sampleId": 2, "leaseId": "lease-2",
            "relativeImagePath": "1_artist\\b.png", "code": "policy_json_missing",
            "message": "working JSON does not exist", "retriable": True, "repairStartModule": "classify",
        },
    ],
}
EXPORT_HELLO_RESULT = {"schemaVersion": 1, "payloadType": "export_hello_result", "ready": True}  # worker.py:39
EXPORT_PROCESS_REQUEST = {  # core/src/anima_core/export_runner.py process request
    "schemaVersion": 1, "payloadType": "export_process_request",
    "items": [{"schemaVersion": 1, "sampleId": 1, "leaseId": "lease-1", "relativeImagePath": "a.png", "annotationKey": "a"}],
}
EXPORT_BATCH_RESULT = {  # workers/export/src/anima_export_worker/worker.py:50-63
    "schemaVersion": 1, "payloadType": "export_batch_result",
    "outcomes": [
        {
            "schemaVersion": 1, "status": "prepared", "sampleId": 1, "leaseId": "lease-1", "relativeImagePath": "a.png",
            "artifacts": [
                {"kind": "json", "relativePath": "prepared\\export\\lease-1.json", "sha256": "e" * 64},
                {"kind": "txt", "relativePath": "prepared\\export\\lease-1.txt", "sha256": "f" * 64},
            ],
            "conversions": {"missing_field_defaulted": 2, "array_duplicate_removed": 1},
        },
        {
            "schemaVersion": 1, "status": "issue", "sampleId": 2, "leaseId": "lease-2", "relativeImagePath": "b.png",
            "code": "final_json_invalid",
            "fieldErrors": [{"field": "artist", "code": "tag_not_flat_txt_representable"}, {"code": "json_read_failed"}],
        },
    ],
}
REPLACE_RESULT = {  # workers/replace/src/anima_replace_worker/worker.py:62-66
    "schemaVersion": 1, "payloadType": "replace_result", "sampleId": 1, "leaseId": "lease-1", "source": "e621",
    "relativeImagePath": "a.png", "replaced": 3, "dropped": 1, "passthrough": 5, "keepRewritten": 2,
    "projection": {
        "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
        "appearance": ["fat"], "tags": ["blush"], "environment": [], "nl": "",
    },
}
REPLACE_HELLO_RESULT = {  # workers/replace/src/anima_replace_worker/worker.py:39-45
    "schemaVersion": 1, "payloadType": "replace_hello_result", "ready": True, "indexLoads": 1,
    "ruleCount": 86922, "resourceFingerprint": "b" * 64, "keepNonCanonical": 269, "canonicalDirectionConflict": 0,
}
REPLACE_CUSTOM_HELLO_REQUEST = {  # core/src/anima_core/replace_runner.py:112
    "schemaVersion": 1, "payloadType": "replace_hello_request", "jobId": "job-1", "configHash": "a" * 64,
    "profile": "e621", "customIndexOverlayRoot": "E:\\.dataset.anima-overlay-job-1",
    "customIndexPath": "resources\\replace\\custom.csv", "customIndexSha256": "b" * 64, "customIndexRuleCount": 12,
}

OCR_INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}
OCR_HELLO_REQUEST = {
    "schemaVersion": 1,
    "payloadType": "ocr_hello_request",
    "jobId": "job-1",
    "configHash": "a" * 64,
    "resourceId": "ocr-ppocrv5-server-paddle-v1",
    "resourceManifestRelativePath": "ocr-models\\ocr-ppocrv5-server-paddle-v1\\resource.json",
    "resourceFingerprint": "b" * 64,
    "inference": OCR_INFERENCE,
}
OCR_HELLO_RESULT = {
    "schemaVersion": 1,
    "payloadType": "ocr_hello_result",
    "ready": True,
    "executable": "C:\\app\\runtimes\\ocr-paddle\\python.exe",
    "pythonVersion": "3.11.15",
    "modelSessionLoads": 1,
    "resourceFingerprint": "b" * 64,
}
OCR_PROCESS_REQUEST = {
    "schemaVersion": 1,
    "payloadType": "ocr_process_request",
    "items": [
        {
            "schemaVersion": 1,
            "sampleId": 1,
            "leaseId": "lease-1",
            "relativeImagePath": "cats\\poster.jpg",
            "imagePath": "C:\\dataset\\cats\\poster.jpg",
            "imageSize": 123456,
            "imageSha256": "c" * 64,
        },
        {
            "schemaVersion": 1,
            "sampleId": 2,
            "leaseId": "lease-2",
            "relativeImagePath": "cats\\empty.jpg",
            "imagePath": "C:\\dataset\\cats\\empty.jpg",
            "imageSize": 123457,
            "imageSha256": "d" * 64,
        },
        {
            "schemaVersion": 1,
            "sampleId": 3,
            "leaseId": "lease-3",
            "relativeImagePath": "cats\\failed.jpg",
            "imagePath": "C:\\dataset\\cats\\failed.jpg",
            "imageSize": 123458,
            "imageSha256": "e" * 64,
        },
    ],
}
OCR_PROCESS_RESULT = {
    "schemaVersion": 1,
    "payloadType": "ocr_process_result",
    "items": [
        {
            "schemaVersion": 1,
            "status": "success",
            "sampleId": 1,
            "leaseId": "lease-1",
            "relativeImagePath": "cats\\poster.jpg",
            "image": {"width": 100, "height": 80, "sizeBytes": 123456, "sha256": "c" * 64},
            "items": [
                {
                    "text": "Hello",
                    "confidence": 0.98,
                    "polygonPixels": [[0, 0], [30, 0], [30, 20], [0, 20]],
                    "bboxPixels": [0, 0, 30, 20],
                    "textlineOrientationDegrees": 0,
                },
                {
                    "text": "Hello",
                    "confidence": 0.49,
                    "polygonPixels": [[60, 60], [100, 60], [100, 80], [60, 80]],
                    "bboxPixels": [60, 60, 100, 80],
                    "textlineOrientationDegrees": 0,
                },
            ],
        },
        {
            "schemaVersion": 1,
            "status": "no_text",
            "sampleId": 2,
            "leaseId": "lease-2",
            "relativeImagePath": "cats\\empty.jpg",
            "image": {"width": 100, "height": 80, "sizeBytes": 123457, "sha256": "d" * 64},
            "items": [],
        },
        {
            "schemaVersion": 1,
            "status": "failed",
            "sampleId": 3,
            "leaseId": "lease-3",
            "relativeImagePath": "cats\\failed.jpg",
            "image": {"width": 100, "height": 80, "sizeBytes": 123458, "sha256": "e" * 64},
            "items": [],
            "error": {
                "code": "ocr_inference_failed",
                "message": "The OCR engine failed for this image.",
                "retriable": True,
            },
        },
        {
            "schemaVersion": 1,
            "status": "failed",
            "sampleId": 4,
            "leaseId": "lease-4",
            "relativeImagePath": "cats\\oversize.jpg",
            "image": {"width": 20_001, "height": 2_000, "sizeBytes": 123459, "sha256": "f" * 64},
            "items": [],
            "error": {
                "code": "ocr_image_too_large",
                "message": "OCR image dimensions exceed the first-release safety limit.",
                "retriable": False,
                "details": {
                    "actualPixels": 40_002_000,
                    "maxPixels": 40_000_000,
                    "maxSide": 16_384,
                },
            },
        },
    ],
}


CAPTION_HELLO_REQUEST = {  # tests/unit/test_caption_protocol.py mirrors the same frozen payload
    "schemaVersion": 1, "payloadType": "caption_hello_request", "jobId": "job-1", "configHash": "a" * 64,
    "profile": "e621", "datasetRoot": "E:\\dataset",
    "resourceManifestRelativePath": "manifests\\resources\\caption-e621.json", "resourceFingerprint": "b" * 64,
    "thresholdPolicy": {"mode": "model_default"},
    "captionFormat": {"replaceUnderscoresWithSpaces": True, "preserveEscapes": True, "triggersEnabled": True, "triggerTerms": ["anima_style"]},
    "imageDecode": {"extensions": [".jpg", ".jpeg", ".png", ".webp", ".bmp"], "rejectMultiFrame": True, "applyExifTranspose": True, "alphaBackground": "#FFFFFF"},
}


def _payloads() -> list[tuple[str, str, dict]]:
    caption_item = _caption_item()
    return [
        ("caption-worker-v1", "caption_hello_request", CAPTION_HELLO_REQUEST),
        ("caption-worker-v1", "caption_danbooru_hello_request", {
            **CAPTION_HELLO_REQUEST,
            "profile": "danbooru",
            "resourceManifestRelativePath": "tagging-models\\caption-danbooru-cl-tagger-v2-00\\resource.json",
            "thresholdPolicy": {
                "mode": "per_category",
                "categoryThresholds": {"general": 0.55, "character": 0.55, "copyright": 0.55},
            },
        }),
        ("caption-worker-v1", "caption_hello_result", CaptionHelloResultV1(
            executable="C:\\app\\runtimes\\caption-e621\\python.exe", provider="CPUExecutionProvider",
            resourceFingerprint="b" * 64,
        ).to_dict()),
        ("caption-worker-v1", "caption_danbooru_hello_result", CaptionHelloResultV1(
            executable="C:\\app\\runtimes\\caption-e621\\python.exe", provider="CPUExecutionProvider",
            resourceFingerprint="b" * 64, tagCount=106_536,
        ).to_dict()),
        ("caption-worker-v1", "caption_result", CaptionResultV1(
            sampleId=caption_item.sampleId, leaseId=caption_item.leaseId,
            relativeImagePath=caption_item.relativeImagePath, tags=(CaptionTagV1("blush", 0.87, "general"),),
            formattedTxt="blush", provider="CPUExecutionProvider",
        ).to_dict()),
        ("caption-worker-v1", "caption_danbooru_result", CaptionResultV1(
            sampleId=caption_item.sampleId, leaseId=caption_item.leaseId,
            relativeImagePath=caption_item.relativeImagePath,
            tags=(CaptionTagV1("hatsune_miku", 0.91, "character"),),
            formattedTxt="hatsune_miku", provider="CPUExecutionProvider", source="danbooru",
        ).to_dict()),
        ("caption-worker-v1", "caption_issue", CaptionIssueResultV1(
            sampleId=caption_item.sampleId, leaseId=caption_item.leaseId,
            relativeImagePath=caption_item.relativeImagePath, code="caption_no_tags", retriable=False,
            message="No model tags matched the frozen thresholds.",
        ).to_dict()),
        ("classify-worker-v1", "classify_hello_request", _hello("classify-e621")),
        ("classify-worker-v1", "classify_danbooru_hello_request", {
            **_hello("classify-e621"),
            "profile": "danbooru",
            "resourceManifestRelativePath": "classification-indexes\\danbooru-classify-20260727-v1\\resource.json",
            "wikiDataSourceId": "danbooru-wiki-test-v1",
        }),
        ("classify-worker-v1", "classify_hello_result", ClassifyHelloResultV1(
            "C:\\app\\runtimes\\classify-e621\\python.exe", "b" * 64,
        ).to_dict()),
        ("classify-worker-v1", "classify_result", ClassifyResultV1(
            sampleId=1, leaseId="lease-1", relativeImagePath="a.png", projection=_classify_projection(),
            countDecision=_classify_decision(), inputTagCount=4, outputTagCount=3, droppedTagCount=1,
        ).to_dict()),
        ("classify-worker-v1", "classify_danbooru_result", ClassifyResultV1(
            sampleId=1,
            leaseId="lease-1",
            relativeImagePath="a.png",
            projection=ClassifyProjectionV1(
                (), "duo", "hatsune_miku", "vocaloid", "", (), ("2girls",), (), "",
            ),
            countDecision=ClassifyCountDecisionV1(
                value="duo", baseValue="duo", selectedSource="wiki_tags", originalRaw=None,
                originalNormalized=None, wikiValue="duo", matchedTags=("2girls", "multiple_girls"),
                conflict=False, issueCodes=(), warnings=("count_lower_bound:danbooru:multiple_girls",),
                appliedLowerBounds=("danbooru_girl",),
            ),
            inputTagCount=4,
            outputTagCount=3,
            droppedTagCount=1,
            source="danbooru",
        ).to_dict()),
        ("classify-worker-v1", "classify_issue", ClassifyIssueResultV1(
            sampleId=1, leaseId="lease-1", relativeImagePath="a.png", code="classify_no_writable_tags",
            retriable=False, message="no writable tag survived the projection", repairStartModule=None,
        ).to_dict()),
        ("replace-worker-v1", "replace_hello_request", _hello("replace-e621")),
        ("replace-worker-v1", "replace_hello_request_custom", REPLACE_CUSTOM_HELLO_REQUEST),
        ("replace-worker-v1", "replace_hello_result", REPLACE_HELLO_RESULT),
        ("replace-worker-v1", "replace_result", REPLACE_RESULT),
        ("ocr-worker-v1", "ocr_hello_request", OCR_HELLO_REQUEST),
        ("ocr-worker-v1", "ocr_hello_result", OCR_HELLO_RESULT),
        ("ocr-worker-v1", "ocr_process_request", OCR_PROCESS_REQUEST),
        ("ocr-worker-v1", "ocr_process_result", OCR_PROCESS_RESULT),
        ("token-budget-worker-v1", "token_budget_hello_request", {
            "schemaVersion": 1, "payloadType": "token_budget_hello_request", "jobId": "worker-test",
            "configHash": "a" * 64, "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
            "resourceManifestRelativePath": "tokenizers\\tokenizer-qwen3-0.6b-anima-v1\\resource.json",
            "resourceFingerprint": "b" * 64, "contextLimit": 32768, "maxTokens": 512,
        }),
        ("token-budget-worker-v1", "token_budget_process_result", {
            "schemaVersion": 1, "payloadType": "token_budget_process_result", "outcomes": [{
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": 1, "leaseId": "lease-1",
                "status": "overflow", "originalTokens": 513, "finalTokens": 513,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
            }],
        }),
        ("nl-worker-v1", "nl_hello_request", _hello("nl")),
        ("export-worker-v1", "export_hello_request", _hello("export")),
        ("export-worker-v1", "export_hello_result", EXPORT_HELLO_RESULT),
        ("export-worker-v1", "export_process_request", EXPORT_PROCESS_REQUEST),
        ("export-worker-v1", "export_batch_result", EXPORT_BATCH_RESULT),
        ("policy-worker-v1", "policy_hello_request", POLICY_HELLO_REQUEST),
        ("policy-worker-v1", "policy_hello_result", POLICY_HELLO_RESULT),
        ("policy-worker-v1", "policy_process_request", POLICY_PROCESS_REQUEST),
        ("policy-worker-v1", "policy_batch_result", POLICY_BATCH_RESULT),
    ]


class PayloadSchemaTests(unittest.TestCase):
    def test_job_config_v9_removes_task_profile_and_separates_classify_input_modes(self) -> None:
        schema_path = SCHEMAS / "job-config-v9.schema.json"
        self.assertTrue(schema_path.is_file(), "JobConfig v9 schema must exist before v9 is accepted")
        document = _schema("job-config-v9")
        payload = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental",
            sourceRoot="C:\\data", schemaVersion=8,
        ).to_dict()
        payload.pop("profile")
        payload["schemaVersion"] = 9
        payload["classify"] = {
            "enabled": True,
            "indexMode": "bundled",
            "resourceId": "classify-e621-20260724-v1",
            "overwriteJson": False,
            "overwriteCount": False,
        }
        self.assertEqual([], validate(_wire(payload), document, document))

        with_profile = json.loads(json.dumps(payload))
        with_profile["profile"] = "e621"
        self.assertNotEqual([], validate(with_profile, document, document))

        mixed = json.loads(json.dumps(payload))
        mixed["classify"]["indexMode"] = "custom"
        mixed["classify"]["customResourcePath"] = "C:\\resources\\resource.json"
        self.assertNotEqual([], validate(mixed, document, document))

    def test_every_worker_owns_a_schema_and_each_file_is_wellformed(self) -> None:
        names = {path.name[: -len(".schema.json")] for path in SCHEMAS.glob("*.schema.json")}
        self.assertEqual(
            {
                "caption-worker-v1", "classify-worker-v1", "replace-worker-v1", "ocr-worker-v1", "token-budget-worker-v1", "nl-worker-v1",
                "policy-worker-v1", "export-worker-v1", "job-config-v2", "job-config-v3", "job-config-v4",
                "job-config-v5", "job-config-v6", "job-config-v7", "job-config-v8", "job-config-v9",
                "sample-manifest-v1",
                "worker-envelope-v1",
            },
            names,
            "every module's stdio protocol must have a frozen schema",
        )
        for name in sorted(names):
            with self.subTest(schema=name):
                document = _schema(name)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", document["$schema"])
                self.assertTrue(str(document["$id"]).startswith("anima://contracts/"))

    def test_real_worker_payloads_validate_against_their_schema(self) -> None:
        for name, label, payload in _payloads():
            with self.subTest(schema=name, payload=label):
                document = _schema(name)
                self.assertEqual([], validate(_wire(payload), document, document), f"{label} does not match {name}")

    def test_schemas_reject_an_unknown_property_and_a_wrong_constant(self) -> None:
        # Without this the validator could accept everything and the suite would prove nothing.
        for name, label, payload in _payloads():
            with self.subTest(schema=name, payload=label):
                document = _schema(name)
                self.assertNotEqual([], validate(_wire({**payload, "unexpected": 1}), document, document))
                self.assertNotEqual([], validate(_wire({**payload, "schemaVersion": 99}), document, document))

    def test_ocr_schema_freezes_six_settings_and_strict_failure_union(self) -> None:
        document = _schema("ocr-worker-v1")
        self.assertEqual([], validate(_wire(OCR_HELLO_REQUEST), document, document))
        self.assertEqual([], validate(_wire(OCR_PROCESS_RESULT), document, document))

        extended_request = {
            **OCR_HELLO_REQUEST,
            "requestedDevice": "auto",
            "expectedRuntimeId": "ocr-paddle",
            "expectedRuntimeFingerprint": "c" * 64,
        }
        extended_result = {
            **OCR_HELLO_RESULT,
            "requestedDevice": "auto",
            "observedDevice": "cpu",
            "runtimeId": "ocr-paddle",
            "runtimeFingerprint": "c" * 64,
            "paddleVersion": "3.2.2",
            "compiledWithCuda": False,
            "cudaVersion": None,
            "gpuName": None,
            "totalVramBytes": None,
        }
        self.assertEqual([], validate(_wire(extended_request), document, document))
        self.assertEqual([], validate(_wire(extended_result), document, document))
        for payload, field in ((extended_request, "expectedRuntimeId"), (extended_result, "gpuName"), (extended_result, "totalVramBytes")):
            with self.subTest(partial=field):
                malformed = _wire(payload)
                malformed.pop(field)
                self.assertNotEqual([], validate(malformed, document, document))
        for requested_device, expected_runtime_id in (
            ("cuda", "ocr-paddle"),
            ("cpu", "ocr-paddle-gpu"),
        ):
            with self.subTest(requested_device=requested_device, expected_runtime_id=expected_runtime_id):
                malformed = {
                    **extended_request,
                    "requestedDevice": requested_device,
                    "expectedRuntimeId": expected_runtime_id,
                }
                self.assertNotEqual([], validate(_wire(malformed), document, document))

        missing = dict(OCR_INFERENCE)
        missing.pop("textDetLimitType")
        for inference in (
            {**OCR_INFERENCE, "textDetLimitSideLen": "1920"},
            {**OCR_INFERENCE, "textDetLimitSideLen": True},
            {**OCR_INFERENCE, "textDetLimitSideLen": 1920.0},
            {**OCR_INFERENCE, "textDetLimitSideLen": 960},
            {**OCR_INFERENCE, "textDetLimitType": "min"},
            missing,
            {**OCR_INFERENCE, "unexpected": 1},
        ):
            with self.subTest(inference=inference):
                payload = _wire(OCR_HELLO_REQUEST)
                payload["inference"] = inference
                self.assertNotEqual([], validate(payload, document, document))

        normal_with_details = _wire(OCR_PROCESS_RESULT)
        normal_with_details["items"][2]["error"]["details"] = {
            "actualPixels": 8_000,
            "maxPixels": 40_000_000,
            "maxSide": 16_384,
        }
        self.assertNotEqual([], validate(normal_with_details, document, document))

        for item_index in (2, 3):
            with self.subTest(error_branch=item_index, case="absolute-path-message"):
                payload = _wire(OCR_PROCESS_RESULT)
                payload["items"][item_index]["error"]["message"] = "OCR failed for C:\\dataset\\image.png"
                self.assertNotEqual([], validate(payload, document, document))

        for label, mutate in (
            ("missing-details", lambda error: error.pop("details")),
            ("wrong-retriable", lambda error: error.__setitem__("retriable", True)),
            ("boolean-actual", lambda error: error["details"].__setitem__("actualPixels", True)),
            ("wrong-max-pixels", lambda error: error["details"].__setitem__("maxPixels", 40_000_001)),
            ("wrong-max-side", lambda error: error["details"].__setitem__("maxSide", 16_383)),
            ("details-extra", lambda error: error["details"].__setitem__("extra", 1)),
            ("error-extra", lambda error: error.__setitem__("extra", 1)),
        ):
            with self.subTest(case=label):
                payload = _wire(OCR_PROCESS_RESULT)
                mutate(payload["items"][3]["error"])
                self.assertNotEqual([], validate(payload, document, document))

    def test_frozen_job_config_matches_the_job_config_schema(self) -> None:
        v2_document = _schema("job-config-v2")
        v3_document = _schema("job-config-v3")
        v4_document = _schema("job-config-v4")
        in_place = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\data")
        full_copy = JobConfig(
            profile="e621", workMode="full_copy", overwriteMode="rebuild", sourceRoot="C:\\data", outputRoot="D:\\out",
        )
        for config in (in_place, full_copy):
            with self.subTest(workMode=config.workMode):
                current = _wire(config.to_dict())
                self.assertEqual([], validate(current, v3_document, v3_document))
                self.assertNotEqual([], validate(current, v2_document, v2_document))
                self.assertNotEqual([], validate(current, v4_document, v4_document))

                legacy = _wire(config.to_dict())
                legacy["schemaVersion"] = 2
                legacy.pop("countReview")
                legacy["nl"]["promptVersion"] = "nl-default-prompt-v1"
                self.assertEqual([], validate(legacy, v2_document, v2_document))
                self.assertNotEqual([], validate(legacy, v3_document, v3_document))

        legacy = _wire(in_place.to_dict())
        legacy["schemaVersion"] = 2
        legacy.pop("countReview")
        legacy["nl"]["promptVersion"] = "nl-default-prompt-v1"
        legacy["imageDecode"].pop("invalidImageAction")
        self.assertEqual([], validate(legacy, v2_document, v2_document))
        invalid = in_place.to_dict()
        invalid["imageDecode"]["invalidImageAction"] = "ignore"
        self.assertNotEqual([], validate(_wire(invalid), v3_document, v3_document))

    def test_job_config_v4_uses_resource_driven_threshold_categories(self) -> None:
        document = _schema("job-config-v4")
        categories = {"general": 0.55, "character": 0.55, "copyright": 0.55}
        config = JobConfig(
            profile="danbooru",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=4,
            caption={
                "enabled": True,
                "thresholdMode": "per_category",
                "categoryThresholds": categories,
                "overwriteTxt": False,
                "resourceId": "caption-danbooru-cl-tagger-v2-00",
            },
            replace={"enabled": False, "indexMode": "bundled"},
        )
        payload = _wire(config.to_dict())
        self.assertEqual([], validate(payload, document, document))
        validate_job_config(config, adjustable_categories=tuple(categories))
        self.assertEqual(pipeline_module_ids(3), pipeline_module_ids(4))

        for label, changed in (
            ("empty", {}),
            ("invalid-key", {"General": 0.5}),
            ("invalid-value", {"general": True}),
            ("out-of-range", {"general": 1.01}),
        ):
            with self.subTest(case=label):
                candidate = {**payload, "caption": {**payload["caption"], "categoryThresholds": changed}}
                self.assertNotEqual([], validate(candidate, document, document))

        wrong_categories = JobConfig(
            **{
                **config.__dict__,
                "caption": {**config.caption, "categoryThresholds": {"general": 0.5, "character": 0.5}},
            }
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_job_config(wrong_categories, adjustable_categories=tuple(categories))

        legacy_danbooru = JobConfig(
            profile="danbooru", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\data",
        )
        with self.assertRaisesRegex(ValueError, "profile_not_available:danbooru"):
            validate_job_config(legacy_danbooru)

    def test_job_config_v5_freezes_independent_ocr_without_changing_v2_to_v4(self) -> None:
        self.assertEqual(
            ("caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "export"),
            pipeline_module_ids(5),
        )
        self.assertEqual(("caption", "classify", "replace", "nl", "dropout", "export"), pipeline_module_ids(2))
        self.assertEqual(
            ("caption", "classify", "replace", "nl", "count_review", "dropout", "export"),
            pipeline_module_ids(3),
        )
        self.assertEqual(pipeline_module_ids(3), pipeline_module_ids(4))
        self.assertTrue(profile_supports_job_config_schema("e621", 5))
        self.assertTrue(profile_supports_job_config_schema("danbooru", 5))
        self.assertFalse(profile_supports_job_config_schema("danbooru", 3))

        v5_document = _schema("job-config-v5")
        v5 = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=5,
            nl={
                "enabled": True,
                "reuseOriginalNl": True,
                "apiEnabled": True,
                "useImage": True,
                "useFullJson": False,
                "systemPrompt": "Describe the image.",
                "promptVersion": "nl-default-prompt-v3",
            },
        )
        payload = _wire(v5.to_dict())
        self.assertEqual(
            {
                "enabled": False,
                "llmMinConfidence": 0.5,
                "forceReprocess": False,
                "resourceId": "ocr-ppocrv5-server-paddle-v1",
            },
            payload["ocr"],
        )
        self.assertEqual(
            ("caption", "classify", "replace", "ocr", "nl", "countReview", "dropout", "export"),
            tuple(name for name in payload if name in {"caption", "classify", "replace", "ocr", "nl", "countReview", "dropout", "export"}),
        )
        self.assertEqual([], validate(payload, v5_document, v5_document))
        validate_job_config(v5)
        with self.assertRaisesRegex(ValueError, "legacy JobConfig is incompatible"):
            config_from_dict(payload)

        v5_with_v2_prompt = {**payload, "nl": {**payload["nl"], "promptVersion": "nl-default-prompt-v2"}}
        self.assertNotEqual([], validate(_wire(v5_with_v2_prompt), v5_document, v5_document))
        with self.assertRaises(ValueError):
            config_from_dict(v5_with_v2_prompt)

        for label, changed in (
            ("enabled-must-be-boolean", {"enabled": 1}),
            ("force-must-be-boolean", {"forceReprocess": 0}),
            ("confidence-must-be-finite", {"llmMinConfidence": math.nan}),
            ("confidence-must-be-in-range", {"llmMinConfidence": 1.01}),
            ("resource-id-must-be-valid", {"resourceId": "ocr_invalid"}),
            ("device-is-not-supported", {"device": "cpu"}),
            ("batch-is-not-supported", {"batch": 1}),
            ("prefetch-is-not-supported", {"prefetch": 1}),
            ("threads-is-not-supported", {"threads": 1}),
        ):
            with self.subTest(case=label):
                candidate = {**payload, "ocr": {**payload["ocr"], **changed}}
                self.assertNotEqual([], validate(_wire(candidate), v5_document, v5_document))
                with self.assertRaises(ValueError):
                    config_from_dict(candidate)

        for schema_version, expected_hash in (
            (2, "950b32172780dc4857801056aaaa87a3512c08d907e67801480d231a66b9d4c2"),
            (3, "41eab9dc75134098954de0a24cf9f9fa1fc0ad9672be5a4a461d6e965c62157f"),
            (4, "8dbf56d3aede07d317a7e3296fb401c675ecae9d344c89c651bb3c8067e25e85"),
        ):
            with self.subTest(legacy_schema=schema_version):
                legacy = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    schemaVersion=schema_version,
                    countReview=None if schema_version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                )
                if schema_version == 2:
                    legacy.nl["promptVersion"] = "nl-default-prompt-v1"
                legacy_payload = _wire(legacy.to_dict())
                self.assertNotIn("ocr", legacy_payload)
                self.assertEqual(expected_hash, legacy.config_hash)
                rejected = {**legacy_payload, "ocr": dict(payload["ocr"])}
                with self.assertRaises(ValueError):
                    config_from_dict(rejected)

        linked = {**payload, "nl": {**payload["nl"], "ocr": True}}
        self.assertNotEqual([], validate(_wire(linked), v5_document, v5_document))
        with self.assertRaises(ValueError):
            config_from_dict(linked)

    def test_job_config_v6_freezes_token_budget_and_nl_routing_contracts(self) -> None:
        schema_path = SCHEMAS / "job-config-v6.schema.json"
        self.assertTrue(schema_path.is_file(), "JobConfig v6 schema must exist before v6 is accepted")
        if not schema_path.is_file():
            return
        document = _schema("job-config-v6")
        base = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=5,
        ).to_dict()
        payload = {
            **base,
            "schemaVersion": 6,
            "nl": {
                **base["nl"],
                "promptVersion": "nl-default-prompt-v4",
                "captionPreset": "general",
                "lengthDistribution": {"short": 33, "medium": 34, "long": 33},
                "lengthSeed": "anima-nl-length-v1",
            },
            "tokenBudget": {
                "enabled": True,
                "maxTokens": 512,
                "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
            },
        }
        self.assertEqual([], validate(_wire(payload), document, document))
        config = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=6,
            nl=dict(payload["nl"]),
            tokenBudget=dict(payload["tokenBudget"]),
        )
        self.assertEqual(payload, config.to_dict())
        validate_job_config(config)
        self.assertEqual(
            {"enabled": True, "maxTokens": 512, "resourceId": "tokenizer-qwen3-0.6b-anima-v1"},
            config.tokenBudget,
        )
        frozen_token_budget = {
            **payload["tokenBudget"],
            "resourceManifestRelativePath": r"tokenizers\tokenizer-qwen3-0.6b-anima-v1\resource.json",
            "resourceFingerprint": "a" * 64,
            "contextLimit": 512,
        }
        frozen_payload = {**payload, "tokenBudget": frozen_token_budget}
        self.assertEqual([], validate(_wire(frozen_payload), document, document))
        validate_job_config(JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=6,
            nl=dict(frozen_payload["nl"]),
            tokenBudget=dict(frozen_payload["tokenBudget"]),
        ))

        invalid = (
            ("prompt-version", {**payload, "nl": {**payload["nl"], "promptVersion": "nl-default-prompt-v3"}}, True),
            ("preset", {**payload, "nl": {**payload["nl"], "captionPreset": "other"}}, True),
            ("distribution-bool", {**payload, "nl": {**payload["nl"], "lengthDistribution": {"short": True, "medium": 34, "long": 33}}}, True),
            ("distribution-total", {**payload, "nl": {**payload["nl"], "lengthDistribution": {"short": 34, "medium": 34, "long": 33}}}, False),
            ("empty-seed", {**payload, "nl": {**payload["nl"], "lengthSeed": ""}}, True),
            ("long-seed", {**payload, "nl": {**payload["nl"], "lengthSeed": "x" * 257}}, True),
            ("token-budget-partial-freeze", {**payload, "tokenBudget": {**payload["tokenBudget"], "contextLimit": 4096}}, True),
            ("token-budget-enabled-bool", {**payload, "tokenBudget": {**payload["tokenBudget"], "enabled": 1}}, True),
            ("token-budget-max-bool", {**payload, "tokenBudget": {**payload["tokenBudget"], "maxTokens": True}}, True),
            ("token-budget-max-positive", {**payload, "tokenBudget": {**payload["tokenBudget"], "maxTokens": 0}}, True),
        )
        for label, candidate, schema_rejects in invalid:
            with self.subTest(case=label):
                if schema_rejects:
                    self.assertNotEqual([], validate(_wire(candidate), document, document))
                else:
                    self.assertEqual([], validate(_wire(candidate), document, document))
                with self.assertRaises(ValueError):
                    validate_job_config(JobConfig(
                        profile="e621",
                        workMode="in_place",
                        overwriteMode="incremental",
                        sourceRoot="C:\\data",
                        schemaVersion=6,
                        nl=dict(candidate["nl"]),
                        tokenBudget=dict(candidate["tokenBudget"]),
                    ))

        for schema_version in (2, 3, 4, 5):
            with self.subTest(legacy_schema=schema_version):
                legacy = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    countReview=None if schema_version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                    schemaVersion=schema_version,
                )
                if schema_version == 2:
                    legacy.nl["promptVersion"] = "nl-default-prompt-v1"
                legacy_payload = legacy.to_dict()
                legacy_payload["tokenBudget"] = dict(payload["tokenBudget"])
                legacy_payload["nl"] = {**legacy_payload["nl"], "captionPreset": "general"}
                legacy_document = _schema(f"job-config-v{schema_version}")
                self.assertNotEqual([], validate(_wire(legacy_payload), legacy_document, legacy_document))
                with self.assertRaises(ValueError):
                    validate_job_config(JobConfig(
                        profile="e621",
                        workMode="in_place",
                        overwriteMode="incremental",
                        sourceRoot="C:\\data",
                        countReview=None if schema_version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                        schemaVersion=schema_version,
                        nl={**legacy.nl, "captionPreset": "general"},
                        tokenBudget=dict(payload["tokenBudget"]),
                    ))

    def test_job_config_v7_adds_only_strict_ocr_device_to_v6(self) -> None:
        schema_path = SCHEMAS / "job-config-v7.schema.json"
        self.assertTrue(schema_path.is_file(), "JobConfig v7 schema must exist before v7 is accepted")
        if not schema_path.is_file():
            return
        document = _schema("job-config-v7")
        v7 = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=7,
        )
        payload = _wire(v7.to_dict())
        self.assertEqual("auto", payload["ocr"]["device"])
        self.assertEqual("nl-default-prompt-v4", payload["nl"]["promptVersion"])
        self.assertEqual(
            {
                "enabled": True,
                "maxTokens": 512,
                "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
            },
            payload["tokenBudget"],
        )
        self.assertEqual(pipeline_module_ids(6), pipeline_module_ids(7))
        self.assertEqual([], validate(payload, document, document))
        validate_job_config(v7)

        frozen_ocr_without_device = {
            key: value
            for key, value in payload["ocr"].items()
            if key != "device"
        }
        frozen_ocr_without_device.update({
            "resourceManifestRelativePath": r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json",
            "resourceFingerprint": "c" * 64,
        })
        frozen_ocr_config = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=7,
            ocr=frozen_ocr_without_device,
        )
        self.assertEqual("auto", frozen_ocr_config.to_dict()["ocr"]["device"])
        validate_job_config(frozen_ocr_config)

        for profile in ("e621", "danbooru"):
            with self.subTest(profile=profile):
                self.assertTrue(profile_supports_job_config_schema(profile, 7))

        for device in ("auto", "cuda", "cpu"):
            with self.subTest(device=device):
                candidate = {**payload, "ocr": {**payload["ocr"], "device": device}}
                self.assertEqual([], validate(candidate, document, document))
                candidate_config = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    schemaVersion=7,
                    ocr=dict(candidate["ocr"]),
                )
                validate_job_config(candidate_config)
                self.assertEqual(device, candidate_config.to_dict()["ocr"]["device"])

        for device in ("gpu", "CUDA", "", None, 1, True):
            with self.subTest(device=device):
                candidate = {**payload, "ocr": {**payload["ocr"], "device": device}}
                self.assertNotEqual([], validate(candidate, document, document))
                with self.assertRaises(ValueError):
                    validate_job_config(JobConfig(
                        profile="e621",
                        workMode="in_place",
                        overwriteMode="incremental",
                        sourceRoot="C:\\data",
                        schemaVersion=7,
                        ocr=dict(candidate["ocr"]),
                    ))

        for version in range(2, 7):
            with self.subTest(legacy_version=version):
                legacy = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    countReview=None if version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                    schemaVersion=version,
                )
                if version == 2:
                    legacy.nl["promptVersion"] = "nl-default-prompt-v1"
                candidate = legacy.to_dict()
                if version in {5, 6}:
                    candidate["ocr"] = {
                        **candidate["ocr"],
                        "device": "cpu",
                    }
                else:
                    candidate["ocr"] = {
                        "enabled": False,
                        "llmMinConfidence": 0.5,
                        "forceReprocess": False,
                        "resourceId": "ocr-ppocrv5-server-paddle-v1",
                        "device": "cpu",
                }
                legacy_document = _schema(f"job-config-v{version}")
                self.assertNotEqual([], validate(_wire(candidate), legacy_document, legacy_document))
                with self.assertRaisesRegex(ValueError, "ocr device"):
                    validate_job_config(JobConfig(
                        profile="e621",
                        workMode="in_place",
                        overwriteMode="incremental",
                        sourceRoot="C:\\data",
                        countReview=None if version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                        schemaVersion=version,
                        nl=dict(candidate["nl"]),
                        ocr=dict(candidate["ocr"]),
                    ))

    def test_job_config_v8_adds_txt_input_mode_without_changing_v2_to_v7(self) -> None:
        schema_path = SCHEMAS / "job-config-v8.schema.json"
        self.assertTrue(schema_path.is_file(), "JobConfig v8 schema must exist before v8 is accepted")
        document = _schema("job-config-v8")
        v8 = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="C:\\data",
            schemaVersion=8,
        )
        self.assertEqual(
            {"inputTxtMode": "tag", "taggerFallbackOnMissingTxt": True},
            {key: v8.caption[key] for key in ("inputTxtMode", "taggerFallbackOnMissingTxt")},
        )
        v8.nl["systemPrompt"] = "Describe the image."
        payload = _wire(v8.to_dict())
        self.assertEqual((1881, "a85332ab0c70e3838a386b7489f48e37aee39d5966aab481a9b14de5b2019377"), (
            len(canonical_json(JobConfig(
                profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\data", schemaVersion=7,
            ).to_dict()).encode("utf-8")),
            JobConfig(
                profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\data", schemaVersion=7,
            ).config_hash,
        ))
        self.assertTrue(hasattr(contracts, "job_config_supports_caption_input_txt_mode"))
        self.assertTrue(hasattr(contracts, "job_config_supports_ocr_device"))
        self.assertTrue(contracts.job_config_supports_caption_input_txt_mode(8))
        self.assertTrue(contracts.job_config_supports_ocr_device(8))
        self.assertEqual(frozenset({3, 4, 5, 6, 7, 8, 9}), COUNT_REVIEW_SCHEMA_VERSIONS)
        self.assertEqual((True, True, True), (
            job_config_supports_ocr(8),
            job_config_supports_nl_v4(8),
            job_config_supports_token_budget(8),
        ))
        for profile in ("e621", "danbooru"):
            with self.subTest(profile=profile):
                self.assertTrue(profile_supports_job_config_schema(profile, 8))
        self.assertEqual(pipeline_module_ids(7), pipeline_module_ids(8))
        self.assertEqual([], validate(payload, document, document))
        validate_job_config(v8)
        with self.assertRaisesRegex(ValueError, "legacy JobConfig is incompatible"):
            config_from_dict(payload)

        for label, mutate, defaulted_key in (
            ("missing-input-mode", lambda candidate: candidate["caption"].pop("inputTxtMode"), "inputTxtMode"),
            ("missing-fallback", lambda candidate: candidate["caption"].pop("taggerFallbackOnMissingTxt"), "taggerFallbackOnMissingTxt"),
            ("invalid-input-mode", lambda candidate: candidate["caption"].__setitem__("inputTxtMode", "tags"), None),
            ("non-boolean-fallback", lambda candidate: candidate["caption"].__setitem__("taggerFallbackOnMissingTxt", 1), None),
            ("unknown-caption-field", lambda candidate: candidate["caption"].__setitem__("txtModeExtra", True), None),
        ):
            with self.subTest(case=label):
                candidate = _wire(payload)
                mutate(candidate)
                self.assertNotEqual([], validate(candidate, document, document))
                with self.assertRaises(ValueError):
                    config_from_dict(candidate)

        for schema_version in range(2, 8):
            with self.subTest(legacy_schema_version=schema_version):
                legacy = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    countReview=None if schema_version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                    schemaVersion=schema_version,
                )
                if schema_version == 2:
                    legacy.nl["promptVersion"] = "nl-default-prompt-v1"
                candidate = legacy.to_dict()
                candidate["caption"] = {
                    **candidate["caption"],
                    "inputTxtMode": "tag",
                    "taggerFallbackOnMissingTxt": True,
                }
                with self.assertRaises(ValueError):
                    config_from_dict(candidate)

    def test_job_config_capabilities_are_bool_safe_and_versioned(self) -> None:
        self.assertEqual(frozenset({3, 4, 5, 6, 7, 8, 9}), COUNT_REVIEW_SCHEMA_VERSIONS)

        expected = {
            2: (False, False, False),
            3: (False, False, False),
            4: (False, False, False),
            5: (True, False, False),
            6: (True, True, True),
            7: (True, True, True),
            8: (True, True, True),
            9: (True, True, True),
        }
        for version, values in expected.items():
            with self.subTest(version=version):
                self.assertEqual(values, (
                    job_config_supports_ocr(version),
                    job_config_supports_nl_v4(version),
                    job_config_supports_token_budget(version),
                ))

        for invalid in (None, True, False, 6.0, "7", 10):
            with self.subTest(invalid=invalid):
                self.assertEqual((False, False, False), (
                    job_config_supports_ocr(invalid),
                    job_config_supports_nl_v4(invalid),
                    job_config_supports_token_budget(invalid),
                ))

    def test_v2_to_v6_default_config_bytes_and_hashes_remain_frozen(self) -> None:
        expected_canonical_configs = {
            2: (1476, "950b32172780dc4857801056aaaa87a3512c08d907e67801480d231a66b9d4c2"),
            3: (1543, "41eab9dc75134098954de0a24cf9f9fa1fc0ad9672be5a4a461d6e965c62157f"),
            4: (1543, "8dbf56d3aede07d317a7e3296fb401c675ecae9d344c89c651bb3c8067e25e85"),
            5: (1657, "76f0d5a380e9ce92eb2c7fe7593d5e58ca9a1467928be18583e33abd7bcd9608"),
            6: (1865, "fd1f9b0b8eb7fea25475834dce223ea3243473d1db0b8ade0415b4bc28eb8cc9"),
        }
        for schema_version, (expected_bytes, expected_hash) in expected_canonical_configs.items():
            with self.subTest(schema_version=schema_version):
                config = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    countReview=None if schema_version == 2 else {"enabled": True, "protocolVersion": "count-review-v1"},
                    schemaVersion=schema_version,
                )
                if schema_version == 2:
                    config.nl["promptVersion"] = "nl-default-prompt-v1"
                self.assertEqual(expected_bytes, len(canonical_json(config.to_dict()).encode("utf-8")))
                self.assertEqual(expected_hash, config.config_hash)

    def test_token_budget_worker_schema_rejects_untrusted_outcomes(self) -> None:
        schema_path = SCHEMAS / "token-budget-worker-v1.schema.json"
        self.assertTrue(schema_path.is_file(), "Token Budget worker schema must exist before worker output is accepted")
        if not schema_path.is_file():
            return
        document = _schema("token-budget-worker-v1")
        valid = {
            "schemaVersion": 1,
            "payloadType": "token_budget_outcome",
            "sampleId": 1,
            "leaseId": "lease-1",
            "status": "trimmed",
            "originalTokens": 8,
            "finalTokens": 4,
            "removed": {"quality": [], "environment": [], "tags": ["tail"], "appearance": []},
            "annotation": {
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["head"], "environment": [], "nl": "caption.",
            },
            "flatTextSha256": "a" * 64,
        }
        self.assertEqual([], validate(valid, document, document))
        for changed in (
            {"status": "prepared"},
            {"finalTokens": True},
            {"originalTokens": math.nan},
            {"removed": {"quality": [], "environment": [], "tags": [], "appearance": [], "extra": []}},
            {"flatTextSha256": "A" * 64},
        ):
            with self.subTest(changed=changed):
                candidate = {**valid, **changed}
                self.assertNotEqual([], validate(candidate, document, document))
        overflow = {key: value for key, value in valid.items() if key not in {"annotation", "flatTextSha256"}}
        overflow.update({"status": "overflow", "finalTokens": 9, "removed": {"quality": [], "environment": [], "tags": ["head", "tail"], "appearance": []}})
        self.assertEqual([], validate(overflow, document, document))

    def test_nl_worker_schema_keeps_legacy_items_and_allows_only_strict_v5_v6_context_shapes(self) -> None:
        document = _schema("nl-worker-v1")
        legacy = {
            "schemaVersion": 1,
            "payloadType": "nl_process_request",
            "httpAttemptAllowance": 1,
            "items": [{
                "schemaVersion": 1,
                "sampleId": 1,
                "leaseId": "lease-1",
                "relativeImagePath": "sample.png",
                "imagePath": None,
                "jsonContext": "{\"nl\":\"\"}",
            }],
        }
        v5 = _wire(legacy)
        v5["items"][0]["ocrContext"] = {"items": [["top-left", "Hello"], ["bottom-right", "Hello"]]}
        self.assertEqual([], validate(legacy, document, document))
        self.assertEqual([], validate(v5, document, document))
        v6 = _wire(v5)
        v6["items"][0].update({
            "captionPreset": "character",
            "lengthTier": "medium",
            "primaryCharacterName": "主角",
            "userSupplement": "bounded supplement",
        })
        self.assertEqual([], validate(v6, document, document))
        for label, mutate in (
            ("metadata", lambda value: value["items"][0]["ocrContext"].__setitem__("confidence", 0.8)),
            ("bad-position", lambda value: value["items"][0]["ocrContext"]["items"][0].__setitem__(0, "corner")),
            ("extra-item-field", lambda value: value["items"][0]["ocrContext"]["items"][0].append("extra")),
        ):
            with self.subTest(case=label):
                malformed = _wire(v5)
                mutate(malformed)
                self.assertNotEqual([], validate(malformed, document, document))
        for label, mutate in (
            ("missing-tier", lambda value: value["items"][0].pop("lengthTier")),
            ("character-name-on-general", lambda value: value["items"][0].update({"captionPreset": "general", "primaryCharacterName": "主角"})),
            ("unknown-v6-item-field", lambda value: value["items"][0].__setitem__("extra", "no")),
        ):
            with self.subTest(case=label):
                malformed = _wire(v6)
                mutate(malformed)
                self.assertNotEqual([], validate(malformed, document, document))

    def test_nl_and_dropout_sections_are_no_longer_unconstrained(self) -> None:
        # F45: both used to be bare {"type": "object"}, so an illegal policy only failed in
        # nl_runner.py after Caption, Classify and Replace had already run.
        document = _schema("job-config-v3")
        base = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\data").to_dict()
        invalid = (
            ("nl-unknown-key", {**base, "nl": {**base["nl"], "typo": True}}),
            ("nl-api-without-context", {**base, "nl": {**base["nl"], "apiEnabled": True, "useImage": False, "useFullJson": False}}),
            ("nl-concurrency-out-of-range", {**base, "nl": {**base["nl"], "apiPolicy": {"concurrency": 64}}}),
            ("nl-unknown-policy-key", {**base, "nl": {**base["nl"], "apiPolicy": {"retries": 3}}}),
            ("nl-frozen-policy-value", {**base, "nl": {**base["nl"], "apiPolicy": {"maxNlBytes": 1}}}),
            ("nl-rpm-not-a-number", {**base, "nl": {**base["nl"], "apiPolicy": {"maxRequestsPerMinute": "fast"}}}),
            ("dropout-unknown-key", {**base, "dropout": {**base["dropout"], "typo": 1}}),
            ("dropout-policy-version", {**base, "dropout": {**base["dropout"], "policyVersion": "v2"}}),
            ("dropout-probability-out-of-range", {
                **base,
                "dropout": {**base["dropout"], "artist": {"enabled": True, "dropoutProbability": 1.5}},
            }),
            ("dropout-batch-size", {
                **base,
                "dropout": {**base["dropout"], "quality": {**base["dropout"]["quality"], "batchSize": 0}},
            }),
            ("dropout-coupled-shape", {
                **base,
                "dropout": {**base["dropout"], "appearanceNl": {**base["dropout"]["appearanceNl"], "solo": {"dropNl": 0.5}}},
            }),
            ("count-review-unknown-key", {**base, "countReview": {**base["countReview"], "typo": True}}),
            ("count-review-protocol-version", {
                **base, "countReview": {**base["countReview"], "protocolVersion": "count-review-v2"},
            }),
            ("count-review-without-classify", {
                **base, "classify": {**base["classify"], "enabled": False},
            }),
            ("export-format", {**base, "export": {"format": "yaml"}}),
        )
        for label, payload in invalid:
            with self.subTest(case=label):
                self.assertNotEqual([], validate(_wire(payload), document, document), f"{label} should be rejected")


if __name__ == "__main__":
    unittest.main()
