from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "classify" / "src"))

from anima_classify_worker.protocol import (
    ClassifyPayloadError,
    validate_hello_payload,
    validate_process_payload,
)
from anima_core.classify_protocol import (
    CLASSIFY_WIKI_DATA_SOURCE_ID,
    ClassifyCaptionFormatPolicyV1,
    ClassifyCountDecisionV1,
    ClassifyHelloRequestV1,
    ClassifyHelloResultV1,
    ClassifyIssueResultV1,
    ClassifyProcessRequestV1,
    ClassifyProjectionV1,
    ClassifyProtocolError,
    ClassifyResultV1,
    ClassifyWorkItemV1,
    parse_classify_outcome,
    validate_outcome_for_item,
)
from anima_core.contracts import JobConfig, validate_job_config


def _format() -> ClassifyCaptionFormatPolicyV1:
    return ClassifyCaptionFormatPolicyV1(True, True, True, ("anima style",))


def _hello() -> ClassifyHelloRequestV1:
    return ClassifyHelloRequestV1(
        jobId="job-1",
        configHash="a" * 64,
        resourceManifestRelativePath="manifests\\resources\\classify-e621.json",
        resourceFingerprint="b" * 64,
        overwriteCount=False,
        captionFormat=_format(),
    )


def _item() -> ClassifyWorkItemV1:
    return ClassifyWorkItemV1(
        sampleId=1,
        leaseId="lease-1",
        relativeImagePath="nested\\sample.png",
        annotationKey="nested\\sample",
        txtText="anima style, white hair, red eyes",
        txtProvenance="module1_written",
        originalCount="1girl",
    )


def _decision() -> ClassifyCountDecisionV1:
    return ClassifyCountDecisionV1(
        value="solo",
        baseValue="solo",
        selectedSource="original_json",
        originalRaw="1girl",
        originalNormalized="solo",
        wikiValue=None,
        matchedTags=(),
        conflict=False,
        issueCodes=(),
        warnings=(),
        appliedLowerBounds=(),
    )


class ClassifyProtocolTests(unittest.TestCase):
    def test_job_config_and_both_schemas_are_strict(self) -> None:
        job_schema = json.loads((ROOT / "contracts" / "schemas" / "job-config-v9.schema.json").read_text())
        classify_schema = json.loads(
            (ROOT / "contracts" / "schemas" / "classify-worker-v1.schema.json").read_text()
        )
        classify = job_schema["$defs"]["classify"]
        self.assertFalse(classify["additionalProperties"])
        self.assertEqual("#/$defs/resourceId", classify["properties"]["wikiDataSourceId"]["$ref"])
        self.assertEqual("^[a-z0-9][a-z0-9-]{0,127}$", job_schema["$defs"]["resourceId"]["pattern"])
        self.assertEqual("anima://contracts/classify-worker-v1", classify_schema["$id"])
        self.assertEqual(5, len(classify_schema["oneOf"]))

        config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\data")
        validate_job_config(config)
        invalid_values = (
            {"enabled": True, "overwriteJson": False, "overwriteCount": False},
            {**config.classify, "extra": True},
            {**config.classify, "enabled": 1},
            {**config.classify, "overwriteJson": "false"},
            {**config.classify, "wikiDataSourceId": "Invalid Source"},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                changed = JobConfig(
                    profile="e621",
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot="C:\\data",
                    classify=invalid,
                )
                with self.assertRaises(ValueError):
                    validate_job_config(changed)

    def test_hello_roundtrip_and_worker_validator_agree(self) -> None:
        payload = _hello().to_dict()
        self.assertEqual(_hello(), ClassifyHelloRequestV1.from_dict(payload))
        worker = validate_hello_payload(payload)
        self.assertEqual(payload, worker)

        result = ClassifyHelloResultV1("C:\\app\\runtimes\\classify-e621\\python.exe", "b" * 64)
        self.assertEqual(result, ClassifyHelloResultV1.from_dict(result.to_dict()))
        danbooru = {**payload, "profile": "danbooru", "wikiDataSourceId": "danbooru-wiki-test-v1"}
        self.assertEqual("danbooru", ClassifyHelloRequestV1.from_dict(danbooru).profile)
        self.assertEqual("danbooru", validate_hello_payload(danbooru)["profile"])
        for invalid in (
            {**payload, "profile": "other"},
            {**payload, "wikiDataSourceId": "Invalid Source"},
            {**payload, "overwriteCount": 0},
            {**payload, "unknown": True},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ClassifyProtocolError):
                    ClassifyHelloRequestV1.from_dict(invalid)
                with self.assertRaises(ClassifyPayloadError):
                    validate_hello_payload(invalid)

    def test_process_is_single_item_path_safe_and_bounded(self) -> None:
        request = ClassifyProcessRequestV1(_item())
        payload = request.to_dict()
        self.assertEqual(request, ClassifyProcessRequestV1.from_dict(payload))
        self.assertEqual(payload, validate_process_payload(payload))

        invalid_items = (
            {**_item().to_dict(), "relativeImagePath": "..\\sample.png"},
            {**_item().to_dict(), "annotationKey": "other"},
            {**_item().to_dict(), "originalCount": True},
            {**_item().to_dict(), "txtText": " "},
            {**_item().to_dict(), "txtText": "x" * 262_145},
        )
        for invalid in invalid_items:
            value = {"schemaVersion": 1, "payloadType": "classify_process_request", "items": [invalid]}
            with self.subTest(invalid=list(invalid)):
                with self.assertRaises(ClassifyProtocolError):
                    ClassifyProcessRequestV1.from_dict(value)
                with self.assertRaises(ClassifyPayloadError):
                    validate_process_payload(value)
        with self.assertRaises(ClassifyProtocolError):
            ClassifyProcessRequestV1.from_dict(
                {"schemaVersion": 1, "payloadType": "classify_process_request", "items": [_item().to_dict()] * 2}
            )

    def test_result_issue_and_lease_identity_are_strict(self) -> None:
        projection = ClassifyProjectionV1((), "solo", "character_name", "", "", (), ("solo",), (), "")
        result = ClassifyResultV1(1, "lease-1", "nested\\sample.png", projection, _decision(), 2, 2, 0)
        parsed = parse_classify_outcome(result.to_dict())
        self.assertEqual(result, parsed)
        validate_outcome_for_item(parsed, _item())

        bad = result.to_dict()
        bad["projection"] = {**projection.to_dict(), "quality": ["masterpiece"]}
        with self.assertRaises(ClassifyProtocolError):
            parse_classify_outcome(bad)
        mismatched = ClassifyResultV1(2, "lease-1", "nested\\sample.png", projection, _decision(), 2, 2, 0)
        with self.assertRaises(ClassifyProtocolError):
            validate_outcome_for_item(mismatched, _item())

        danbooru_projection = ClassifyProjectionV1(
            (), "solo", "character_name", "series_name", "", (), ("1girl",), (), ""
        )
        danbooru_result = ClassifyResultV1(
            1, "lease-1", "nested\\sample.png", danbooru_projection, _decision(), 3, 3, 0,
            source="danbooru",
        )
        self.assertEqual("series_name", parse_classify_outcome(danbooru_result.to_dict()).projection.series)
        invalid_e621_series = result.to_dict()
        invalid_e621_series["projection"] = danbooru_projection.to_dict()
        with self.assertRaises(ClassifyProtocolError):
            parse_classify_outcome(invalid_e621_series)

        permanent = ClassifyIssueResultV1(
            1, "lease-1", "nested\\sample.png", "count_sheet_multi_conflict", False, "conflict"
        )
        transient = ClassifyIssueResultV1(
            1,
            "lease-1",
            "nested\\sample.png",
            "classify_wiki_io_failed",
            True,
            "temporary failure",
            repairStartModule="classify",
        )
        self.assertEqual(permanent, parse_classify_outcome(permanent.to_dict()))
        # F05 / F35: deterministic worker findings must cross the core boundary as
        # permanent, non-repairable issues.
        for code in ("classify_no_writable_tags", "classify_text_invalid"):
            with self.subTest(code=code):
                deterministic = ClassifyIssueResultV1(1, "lease-1", "nested\\sample.png", code, False, "deterministic")
                self.assertEqual(deterministic, parse_classify_outcome(deterministic.to_dict()))
        self.assertEqual(transient, parse_classify_outcome(transient.to_dict()))
        invalid_retry = permanent.to_dict()
        invalid_retry["retriable"] = True
        invalid_retry["repairStartModule"] = "classify"
        with self.assertRaises(ClassifyProtocolError):
            parse_classify_outcome(invalid_retry)


if __name__ == "__main__":
    unittest.main()
