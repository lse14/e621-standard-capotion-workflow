from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "caption" / "src"))

from anima_caption_worker.protocol import CaptionPayloadError, validate_hello_payload, validate_process_payload
from anima_core.caption_protocol import (
    CaptionFormatPolicyV1,
    CaptionHelloRequestV1,
    CaptionHelloResultV1,
    CaptionIssueResultV1,
    CaptionProcessRequestV1,
    CaptionProtocolError,
    CaptionResultV1,
    CaptionTagV1,
    CaptionThresholdPolicyV1,
    CaptionWorkItemV1,
    ImageDecodePolicyV1,
    parse_caption_outcome,
    validate_outcome_for_item,
)
from anima_core.contracts import CaptionFormatPolicy, ImageDecodePolicy, JobConfig, validate_job_config


def _hello() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "caption_hello_request",
        "jobId": "job-1",
        "configHash": "a" * 64,
        "profile": "e621",
        "datasetRoot": "E:\\dataset",
        "resourceManifestRelativePath": "manifests\\resources\\caption-e621.json",
        "resourceFingerprint": "b" * 64,
        "thresholdPolicy": {"mode": "model_default"},
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": True,
            "triggerTerms": ["anima_style"],
        },
        "imageDecode": {
            "extensions": [".jpg", ".jpeg", ".png", ".webp", ".bmp"],
            "rejectMultiFrame": True,
            "applyExifTranspose": True,
            "alphaBackground": "#FFFFFF",
        },
    }


def _work_item() -> CaptionWorkItemV1:
    return CaptionWorkItemV1(
        sampleId=7,
        leaseId="lease-7",
        relativeImagePath="nested\\image.png",
        annotationKey="nested\\image",
        imageFormat="png",
        imageSize=123,
        imageMtimeNs=456,
        imageFileId="1:2",
    )


class CaptionProtocolTests(unittest.TestCase):
    def test_hello_roundtrip_and_worker_validator_agree(self) -> None:
        payload = _hello()
        parsed = CaptionHelloRequestV1.from_dict(payload)
        self.assertEqual(payload, parsed.to_dict())
        self.assertEqual(payload, validate_hello_payload(payload))
        self.assertEqual("model_default", parsed.thresholdPolicy.mode)

        hello_result = CaptionHelloResultV1(
            executable="E:\\app\\runtimes\\caption-e621\\python.exe",
            provider="CPUExecutionProvider",
            resourceFingerprint="b" * 64,
        )
        self.assertEqual(hello_result, CaptionHelloResultV1.from_dict(hello_result.to_dict()))
        fallback_result = CaptionHelloResultV1(
            executable="E:\\app\\runtimes\\caption-e621\\python.exe",
            provider="CPUExecutionProvider",
            resourceFingerprint="b" * 64,
            gpuFallback=True,
        )
        self.assertTrue(CaptionHelloResultV1.from_dict(fallback_result.to_dict()).gpuFallback)

        danbooru = {**payload, "profile": "danbooru"}
        self.assertEqual(danbooru, CaptionHelloRequestV1.from_dict(danbooru).to_dict())
        self.assertEqual(danbooru, validate_hello_payload(danbooru))
        large_result = CaptionHelloResultV1(
            executable="E:\\app\\runtimes\\caption-e621\\python.exe",
            provider="CPUExecutionProvider",
            resourceFingerprint="b" * 64,
            tagCount=106_536,
        )
        self.assertEqual(large_result, CaptionHelloResultV1.from_dict(large_result.to_dict()))

        for changed in (
            {**payload, "profile": "unknown"},
            {**payload, "configHash": "A" * 64},
            {**payload, "resourceManifestRelativePath": "..\\caption.json"},
            {**payload, "datasetRoot": "relative\\dataset"},
            {**payload, "unknown": True},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(CaptionProtocolError):
                    CaptionHelloRequestV1.from_dict(changed)
                with self.assertRaises(CaptionPayloadError):
                    validate_hello_payload(changed)

    def test_threshold_policy_is_strict_and_finite(self) -> None:
        self.assertEqual(
            0.0,
            CaptionThresholdPolicyV1.from_dict({"mode": "uniform", "uniformThreshold": 0}).uniformThreshold,
        )
        categories = {"general": 0, "character": 1, "species": 0.5, "rating": 0.25}
        self.assertEqual(
            categories,
            CaptionThresholdPolicyV1.from_dict({"mode": "per_category", "categoryThresholds": categories}).categoryThresholds,
        )
        self.assertEqual(
            {"general": 0.5},
            CaptionThresholdPolicyV1.from_dict(
                {"mode": "per_category", "categoryThresholds": {"general": 0.5}}
            ).categoryThresholds,
        )
        for invalid in (
            {"mode": "uniform", "uniformThreshold": True},
            {"mode": "uniform", "uniformThreshold": math.nan},
            {"mode": "uniform", "uniformThreshold": math.inf},
            {"mode": "uniform", "uniformThreshold": 1.01},
            {"mode": "model_default", "uniformThreshold": 0.5},
            {"mode": "per_category", "categoryThresholds": {}},
            {"mode": "per_category", "categoryThresholds": {"General": 0.5}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(CaptionProtocolError):
                CaptionThresholdPolicyV1.from_dict(invalid)

    def test_format_image_and_job_config_constraints(self) -> None:
        caption_format = CaptionFormatPolicyV1.from_dict(_hello()["captionFormat"])
        image_decode = ImageDecodePolicyV1.from_dict(_hello()["imageDecode"])
        self.assertTrue(caption_format.triggersEnabled)
        self.assertEqual("#FFFFFF", image_decode.alphaBackground)

        invalid_format = dict(_hello()["captionFormat"])
        invalid_format["triggerTerms"] = ["one,two"]
        with self.assertRaises(CaptionProtocolError):
            CaptionFormatPolicyV1.from_dict(invalid_format)

        config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="E:\\dataset")
        validate_job_config(config)
        config.caption["uniformThreshold"] = True
        config.caption["thresholdMode"] = "uniform"
        with self.assertRaises(ValueError):
            validate_job_config(config)

        bad_image = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="E:\\dataset",
            imageDecode=ImageDecodePolicy(alphaBackground="#000000"),
        )
        with self.assertRaises(ValueError):
            validate_job_config(bad_image)
        bad_trigger = JobConfig(
            profile="e621",
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="E:\\dataset",
            captionFormat=CaptionFormatPolicy(triggersEnabled=True, triggerTerms=("bad,term",)),
        )
        with self.assertRaises(ValueError):
            validate_job_config(bad_trigger)

    def test_trigger_terms_are_validated_after_the_display_transform(self) -> None:
        """D16: preflight used to accept terms that the worker always rejects."""
        for term in ("my_style_", "_trigger", "___"):
            blocked = JobConfig(
                profile="e621",
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot="E:\\dataset",
                captionFormat=CaptionFormatPolicy(triggersEnabled=True, triggerTerms=(term,)),
            )
            with self.subTest(term=term), self.assertRaises(ValueError):
                validate_job_config(blocked)
            allowed = JobConfig(
                profile="e621",
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot="E:\\dataset",
                captionFormat=CaptionFormatPolicy(
                    replaceUnderscoresWithSpaces=False,
                    triggersEnabled=True,
                    triggerTerms=(term,),
                ),
            )
            validate_job_config(allowed)

    def test_process_request_is_single_item_and_path_safe(self) -> None:
        request = CaptionProcessRequestV1(_work_item())
        payload = request.to_dict()
        self.assertEqual(payload, CaptionProcessRequestV1.from_dict(payload).to_dict())
        self.assertEqual(payload, validate_process_payload(payload))

        two = {**payload, "items": payload["items"] * 2}
        with self.assertRaises(CaptionProtocolError):
            CaptionProcessRequestV1.from_dict(two)
        with self.assertRaises(CaptionPayloadError):
            validate_process_payload(two)

        escaped_item = dict(payload["items"][0])
        escaped_item["relativeImagePath"] = "..\\image.png"
        escaped = {**payload, "items": [escaped_item]}
        with self.assertRaises(CaptionProtocolError):
            CaptionProcessRequestV1.from_dict(escaped)
        with self.assertRaises(CaptionPayloadError):
            validate_process_payload(escaped)

        mismatched_item = dict(payload["items"][0])
        mismatched_item["annotationKey"] = "nested\\other"
        mismatched = {**payload, "items": [mismatched_item]}
        with self.assertRaises(CaptionProtocolError):
            CaptionProcessRequestV1.from_dict(mismatched)
        with self.assertRaises(CaptionPayloadError):
            validate_process_payload(mismatched)

    def test_result_issue_and_lease_identity_validation(self) -> None:
        item = _work_item()
        result = CaptionResultV1(
            sampleId=item.sampleId,
            leaseId=item.leaseId,
            relativeImagePath=item.relativeImagePath,
            tags=(CaptionTagV1("blue_eyes", 0.8, "general"),),
            formattedTxt="blue eyes",
            provider="CPUExecutionProvider",
        )
        parsed = parse_caption_outcome(result.to_dict())
        self.assertEqual(result, parsed)
        validate_outcome_for_item(parsed, item)
        with self.assertRaises(CaptionProtocolError):
            CaptionTagV1.from_dict({"rawTag": "high_logit", "score": 1.5, "category": "general"})
        with self.assertRaises(CaptionProtocolError):
            CaptionTagV1.from_dict({"rawTag": "bad", "score": math.nan, "category": "general"})

        issue = CaptionIssueResultV1(
            sampleId=item.sampleId,
            leaseId=item.leaseId,
            relativeImagePath=item.relativeImagePath,
            code="caption_no_tags",
            retriable=False,
            message="No model tags matched the frozen thresholds.",
            repairStartModule=None,
        )
        self.assertEqual(issue, parse_caption_outcome(issue.to_dict()))
        validate_outcome_for_item(issue, item)

        wrong = CaptionResultV1(**{**result.__dict__, "leaseId": "lease-other"})
        with self.assertRaises(CaptionProtocolError):
            validate_outcome_for_item(wrong, item)
        invalid_issue = {**issue.to_dict(), "retriable": True}
        with self.assertRaises(CaptionProtocolError):
            parse_caption_outcome(invalid_issue)

    def test_caption_schema_and_job_schema_are_parseable_and_strict(self) -> None:
        caption_schema = json.loads((ROOT / "contracts" / "schemas" / "caption-worker-v1.schema.json").read_text(encoding="utf-8"))
        job_schema = json.loads((ROOT / "contracts" / "schemas" / "job-config-v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("anima://contracts/caption-worker-v1", caption_schema["$id"])
        self.assertEqual(5, len(caption_schema["oneOf"]))
        self.assertFalse(job_schema["properties"]["caption"]["additionalProperties"])
        self.assertFalse(job_schema["properties"]["captionFormat"]["additionalProperties"])
        self.assertFalse(job_schema["properties"]["imageDecode"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
