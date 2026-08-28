from __future__ import annotations

import io
import importlib
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
    CaptionProcessResultV1,
    CaptionProtocolError,
    CaptionResultV1,
    CaptionTagV1,
    CaptionThresholdPolicyV1,
    CaptionWorkItemV1,
    ImageDecodePolicyV1,
    parse_caption_outcome,
    validate_outcome_for_item,
    validate_outcomes_for_items,
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


def _reply_result(sample_id: int, lease_id: str, *, tag_count: int = 1) -> dict[str, object]:
    return CaptionResultV1(
        sampleId=sample_id,
        leaseId=lease_id,
        relativeImagePath=f"sample-{sample_id}.png",
        tags=tuple(CaptionTagV1(f"tag-{index}", 0.5, "general") for index in range(tag_count)),
        formattedTxt="tag",
        provider="CPUExecutionProvider",
    ).to_dict()


class CaptionProtocolTests(unittest.TestCase):
    def test_caption_reply_converts_oversize_result_to_bounded_protocol_error(self) -> None:
        entry = importlib.import_module("anima_caption_worker.entry")
        request = {
            "messageId": "process-1",
            "jobId": "job-1",
            "configHash": "a" * 64,
        }
        output = io.BytesIO()

        entry._reply(request, "result", {"value": "x" * entry.MAX_FRAME_BYTES}, output=output)

        response = json.loads(output.getvalue())
        self.assertEqual("error", response["method"])
        self.assertEqual({"code": "caption_protocol_violation"}, response["payload"])
        self.assertLessEqual(len(output.getvalue()), entry.MAX_FRAME_BYTES + 1)

    def test_caption_reply_isolates_one_oversized_item_and_keeps_other_outcomes(self) -> None:
        entry = importlib.import_module("anima_caption_worker.entry")
        request = {"messageId": "process-1", "jobId": "job-1", "configHash": "a" * 64}
        payload = {
            "schemaVersion": 1,
            "payloadType": "caption_process_result",
            "outcomes": [
                _reply_result(1, "lease-1", tag_count=30_000),
                _reply_result(2, "lease-2"),
            ],
        }
        output = io.BytesIO()

        entry._reply(request, "result", payload, output=output)

        frame = output.getvalue()
        self.assertLessEqual(len(frame), entry.MAX_FRAME_BYTES + 1)
        response = json.loads(frame)
        self.assertEqual("result", response["method"])
        parsed = CaptionProcessResultV1.from_dict(response["payload"])
        self.assertEqual(2, len(parsed.outcomes))
        self.assertEqual("caption_result_too_large", parsed.outcomes[0].code)
        self.assertFalse(parsed.outcomes[0].retriable)
        self.assertIsInstance(parsed.outcomes[1], CaptionResultV1)
        self.assertEqual(2, parsed.outcomes[1].sampleId)

    def test_caption_reply_returns_an_item_issue_when_the_only_item_exceeds_frame_limit(self) -> None:
        entry = importlib.import_module("anima_caption_worker.entry")
        request = {"messageId": "process-1", "jobId": "job-1", "configHash": "a" * 64}
        payload = {
            "schemaVersion": 1,
            "payloadType": "caption_process_result",
            "outcomes": [_reply_result(1, "lease-1", tag_count=30_000)],
        }
        output = io.BytesIO()

        entry._reply(request, "result", payload, output=output)

        response = json.loads(output.getvalue())
        self.assertEqual("result", response["method"])
        parsed = CaptionProcessResultV1.from_dict(response["payload"])
        self.assertEqual(1, len(parsed.outcomes))
        self.assertEqual("caption_result_too_large", parsed.outcomes[0].code)
        self.assertEqual((1, "lease-1"), (parsed.outcomes[0].sampleId, parsed.outcomes[0].leaseId))

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

        config = JobConfig(workMode="in_place", overwriteMode="incremental", sourceRoot="E:\\dataset")
        validate_job_config(config)
        config.caption["uniformThreshold"] = True
        config.caption["thresholdMode"] = "uniform"
        with self.assertRaises(ValueError):
            validate_job_config(config)

        bad_image = JobConfig(
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot="E:\\dataset",
            imageDecode=ImageDecodePolicy(alphaBackground="#000000"),
        )
        with self.assertRaises(ValueError):
            validate_job_config(bad_image)
        bad_trigger = JobConfig(
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
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot="E:\\dataset",
                captionFormat=CaptionFormatPolicy(triggersEnabled=True, triggerTerms=(term,)),
            )
            with self.subTest(term=term), self.assertRaises(ValueError):
                validate_job_config(blocked)
            allowed = JobConfig(
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

    def test_process_request_supports_bounded_unique_multi_items_and_path_safety(self) -> None:
        first = _work_item()
        second = CaptionWorkItemV1(
            **{**first.__dict__, "sampleId": 8, "leaseId": "lease-8", "relativeImagePath": "nested\\image-8.png", "annotationKey": "nested\\image-8"}
        )
        payload = {
            "schemaVersion": 1,
            "payloadType": "caption_process_request",
            "items": [first.to_dict(), second.to_dict()],
        }
        try:
            request = CaptionProcessRequestV1.from_dict(payload)
        except CaptionProtocolError:
            request = None
        self.assertIsNotNone(request, "caption v1 must accept a bounded multi-item request")
        assert request is not None
        self.assertEqual((first, second), request.items)
        self.assertEqual(payload, request.to_dict())

        try:
            worker_validated = validate_process_payload(payload)
        except CaptionPayloadError:
            worker_validated = None
        self.assertEqual(payload, worker_validated)

        duplicate = {**payload, "items": [first.to_dict(), first.to_dict()]}
        with self.assertRaises(CaptionProtocolError):
            CaptionProcessRequestV1.from_dict(duplicate)
        with self.assertRaises(CaptionPayloadError):
            validate_process_payload(duplicate)

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

    def test_batch_outcomes_require_exact_unique_leased_identities(self) -> None:
        first = _work_item()
        second = CaptionWorkItemV1(
            **{**first.__dict__, "sampleId": 8, "leaseId": "lease-8", "relativeImagePath": "nested\\image-8.png", "annotationKey": "nested\\image-8"}
        )
        first_result = CaptionResultV1(
            sampleId=first.sampleId,
            leaseId=first.leaseId,
            relativeImagePath=first.relativeImagePath,
            tags=(CaptionTagV1("blue_eyes", 0.8, "general"),),
            formattedTxt="blue eyes",
            provider="CPUExecutionProvider",
        )
        second_issue = CaptionIssueResultV1(
            sampleId=second.sampleId,
            leaseId=second.leaseId,
            relativeImagePath=second.relativeImagePath,
            code="caption_no_tags",
            retriable=False,
            message="No model tags matched the frozen thresholds.",
            repairStartModule=None,
        )
        parsed = CaptionProcessResultV1.from_dict({
            "schemaVersion": 1,
            "payloadType": "caption_process_result",
            "outcomes": [second_issue.to_dict(), first_result.to_dict()],
        })
        self.assertEqual((first_result, second_issue), validate_outcomes_for_items(parsed.outcomes, (first, second)))

        duplicate = {**parsed.to_dict(), "outcomes": [first_result.to_dict(), first_result.to_dict()]}
        with self.assertRaises(CaptionProtocolError):
            CaptionProcessResultV1.from_dict(duplicate)
        missing = {**parsed.to_dict(), "outcomes": [first_result.to_dict()]}
        with self.assertRaises(CaptionProtocolError):
            validate_outcomes_for_items(CaptionProcessResultV1.from_dict(missing).outcomes, (first, second))
        with self.assertRaises(CaptionProtocolError):
            CaptionProcessResultV1.from_dict({
                "schemaVersion": 1,
                "payloadType": "caption_process_result",
                "outcomes": "not-a-list",
            })

    def test_caption_schema_and_job_schema_are_parseable_and_strict(self) -> None:
        caption_schema = json.loads((ROOT / "contracts" / "schemas" / "caption-worker-v1.schema.json").read_text(encoding="utf-8"))
        job_schema = json.loads((ROOT / "contracts" / "schemas" / "job-config-v10.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("anima://contracts/caption-worker-v1", caption_schema["$id"])
        self.assertEqual(6, len(caption_schema["oneOf"]))
        self.assertFalse(job_schema["$defs"]["caption"]["additionalProperties"])
        self.assertFalse(job_schema["$defs"]["captionFormat"]["additionalProperties"])
        self.assertFalse(job_schema["$defs"]["imageDecode"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
