from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.ocr_protocol import (  # noqa: E402
    OcrHelloRequestV1,
    OcrHelloResultV1,
    OcrProcessRequestV1,
    OcrProtocolError,
    parse_ocr_process_result,
    validate_hello_result,
    validate_outcomes_for_items,
    validate_outcome_for_item,
)


INFERENCE = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": True,
    "textRecScoreThresh": 0,
    "textDetLimitSideLen": 1920,
    "textDetLimitType": "max",
}


def _hello() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "ocr_hello_request",
        "jobId": "job-1",
        "configHash": "a" * 64,
        "resourceId": "ocr-ppocrv5-server-paddle-v1",
        "resourceManifestRelativePath": "ocr-models\\ocr-ppocrv5-server-paddle-v1\\resource.json",
        "resourceFingerprint": "b" * 64,
        "inference": copy.deepcopy(INFERENCE),
    }


def _extended_hello(*, device: str = "auto") -> dict[str, object]:
    return {
        **_hello(),
        "requestedDevice": device,
        "expectedRuntimeId": "ocr-paddle-gpu" if device == "cuda" else "ocr-paddle",
        "expectedRuntimeFingerprint": "d" * 64,
    }


def _tuned_hello(*, limit: int = 2560, batch: int = 4) -> dict[str, object]:
    return {
        **_extended_hello(),
        "inference": {**INFERENCE, "textDetLimitSideLen": limit},
        "executionTuning": {
            "textDetLimitSideLen": limit,
            "textBatchSize": batch,
        },
    }


def _request() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "ocr_process_request",
        "items": [
            {
                "schemaVersion": 1,
                "sampleId": 7,
                "leaseId": "lease-7",
                "relativeImagePath": "nested\\image.png",
                "imagePath": "E:\\dataset\\nested\\image.png",
                "imageSize": 123,
                "imageSha256": "c" * 64,
            }
        ],
    }


def _image() -> dict[str, object]:
    return {"width": 100, "height": 80, "sizeBytes": 123, "sha256": "c" * 64}


def _success() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "ocr_process_result",
        "items": [
            {
                "schemaVersion": 1,
                "status": "success",
                "sampleId": 7,
                "leaseId": "lease-7",
                "relativeImagePath": "nested\\image.png",
                "image": _image(),
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
            }
        ],
    }


def _parse_hello_valid(payload: dict[str, object]) -> OcrHelloRequestV1:
    try:
        return OcrHelloRequestV1.from_dict(payload)
    except OcrProtocolError as exc:
        raise AssertionError(f"valid OCR hello request was rejected: {exc}") from exc


def _parse_result_valid(payload: dict[str, object]):
    try:
        return parse_ocr_process_result(payload)
    except OcrProtocolError as exc:
        raise AssertionError(f"valid OCR process result was rejected: {exc}") from exc


def _failed_result(error: dict[str, object], *, width: int = 100, height: int = 80) -> dict[str, object]:
    payload = _success()
    outcome = payload["items"][0]
    outcome["status"] = "failed"
    outcome["image"]["width"] = width
    outcome["image"]["height"] = height
    outcome["items"] = []
    outcome["error"] = error
    return payload


class OcrProtocolTests(unittest.TestCase):
    def test_batch_results_may_be_shuffled_but_must_match_the_exact_request_identities(self) -> None:
        first = _request()["items"][0]
        second = dict(first)
        second.update({"sampleId": 8, "leaseId": "lease-8", "relativeImagePath": "nested\\second.png", "imageSha256": "d" * 64})
        request = OcrProcessRequestV1.from_dict({"schemaVersion": 1, "payloadType": "ocr_process_request", "items": [first, second]})
        first_outcome = _success()["items"][0]
        second_outcome = copy.deepcopy(first_outcome)
        second_outcome.update({"sampleId": 8, "leaseId": "lease-8", "relativeImagePath": "nested\\second.png"})
        second_outcome["image"]["sha256"] = "d" * 64
        shuffled = parse_ocr_process_result({"schemaVersion": 1, "payloadType": "ocr_process_result", "items": [second_outcome, first_outcome]})
        ordered = validate_outcomes_for_items(shuffled, request.items)
        self.assertEqual([7, 8], [outcome.sampleId for outcome in ordered])
        for malformed in (
            (shuffled[0], shuffled[0]),
            (shuffled[0],),
        ):
            with self.subTest(malformed=malformed), self.assertRaises(OcrProtocolError):
                validate_outcomes_for_items(tuple(malformed), request.items)

    def test_hello_execution_tuning_is_optional_complete_bounded_and_preserves_legacy_bytes(self) -> None:
        legacy = OcrHelloRequestV1.from_dict(_hello())
        self.assertEqual(_hello(), legacy.to_dict())
        try:
            tuned = OcrHelloRequestV1.from_dict(_tuned_hello())
        except OcrProtocolError as exc:
            self.fail(f"Task 3.4 execution tuning was rejected: {exc}")
        self.assertEqual(_tuned_hello(), tuned.to_dict())
        self.assertEqual((2560, 4), (tuned.textDetLimitSideLen, tuned.textBatchSize))
        partial = _tuned_hello()
        partial["executionTuning"] = {"textDetLimitSideLen": 2560}
        invalid = (
            _tuned_hello(limit=1919),
            _tuned_hello(limit=2305),
            _tuned_hello(limit=3841),
            _tuned_hello(limit=1920, batch=0),
            _tuned_hello(limit=1920, batch=9),
            partial,
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(OcrProtocolError):
                OcrHelloRequestV1.from_dict(payload)

    def test_hello_device_evidence_is_all_or_none_and_legacy_serialization_is_unchanged(self) -> None:
        legacy = OcrHelloRequestV1.from_dict(_hello())
        self.assertEqual(_hello(), legacy.to_dict())

        try:
            request = OcrHelloRequestV1.from_dict(_extended_hello())
        except OcrProtocolError as exc:
            self.fail(f"valid extended OCR hello request was rejected: {exc}")
        self.assertEqual(_extended_hello(), request.to_dict())
        result_payload = {
            "schemaVersion": 1,
            "payloadType": "ocr_hello_result",
            "ready": True,
            "executable": "E:\\app\\runtimes\\ocr-paddle\\python.exe",
            "pythonVersion": "3.11.15",
            "modelSessionLoads": 1,
            "resourceFingerprint": "b" * 64,
            "requestedDevice": "auto",
            "observedDevice": "cpu",
            "runtimeId": "ocr-paddle",
            "runtimeFingerprint": "d" * 64,
            "paddleVersion": "3.2.2",
            "compiledWithCuda": False,
            "cudaVersion": None,
            "gpuName": None,
            "totalVramBytes": None,
        }
        try:
            result = OcrHelloResultV1.from_dict(result_payload)
        except OcrProtocolError as exc:
            self.fail(f"valid extended OCR hello result was rejected: {exc}")
        self.assertEqual(result_payload, result.to_dict())
        validate_hello_result(result, request)

        cuda_payload = {**result_payload, "requestedDevice": "cuda", "observedDevice": "cuda", "runtimeId": "ocr-paddle-gpu", "compiledWithCuda": True, "cudaVersion": "12.6", "gpuName": "NVIDIA GeForce RTX 4090", "totalVramBytes": 24 * 1024 ** 3}
        validate_hello_result(OcrHelloResultV1.from_dict(cuda_payload), OcrHelloRequestV1.from_dict(_extended_hello(device="cuda")))

        for payload in (
            {**_hello(), "requestedDevice": "auto"},
            {**_extended_hello(), "requestedDevice": "gpu"},
            {**_extended_hello(), "expectedRuntimeFingerprint": "D" * 64},
            {**_extended_hello(device="cuda"), "expectedRuntimeId": "ocr-paddle"},
            {**_extended_hello(device="cpu"), "expectedRuntimeId": "ocr-paddle-gpu"},
        ):
            with self.subTest(payload=payload), self.assertRaises(OcrProtocolError):
                OcrHelloRequestV1.from_dict(payload)

        malformed_result = dict(result_payload)
        malformed_result.pop("gpuName")
        with self.assertRaises(OcrProtocolError):
            OcrHelloResultV1.from_dict(malformed_result)

        mismatch = OcrHelloResultV1.from_dict({**result_payload, "runtimeFingerprint": "e" * 64})
        with self.assertRaises(OcrProtocolError):
            validate_hello_result(mismatch, request)

    def test_hello_freezes_identity_resource_and_result_affecting_settings(self) -> None:
        payload = _hello()
        request = _parse_hello_valid(payload)
        self.assertEqual(payload, request.to_dict())
        self.assertEqual(INFERENCE, request.inference)
        result = OcrHelloResultV1(
            executable="E:\\app\\runtimes\\ocr-paddle\\python.exe",
            resourceFingerprint="b" * 64,
        )
        self.assertEqual(result, OcrHelloResultV1.from_dict(result.to_dict()))
        validate_hello_result(result, request)
        with self.assertRaises(OcrProtocolError):
            validate_hello_result(
                OcrHelloResultV1(
                    executable="E:\\app\\runtimes\\ocr-paddle\\python.exe",
                    resourceFingerprint="c" * 64,
                ),
                request,
            )
        for changed in (
            {**payload, "resourceManifestRelativePath": "..\\resource.json"},
            {**payload, "resourceFingerprint": "B" * 64},
            {**payload, "inference": {**INFERENCE, "textRecScoreThresh": 0.1}},
            {**payload, "device": "cpu"},
        ):
            with self.subTest(changed=changed), self.assertRaises(OcrProtocolError):
                OcrHelloRequestV1.from_dict(changed)

        missing = dict(INFERENCE)
        missing.pop("textDetLimitType")
        for inference in (
            {**INFERENCE, "textDetLimitSideLen": "1920"},
            {**INFERENCE, "textDetLimitSideLen": True},
            {**INFERENCE, "textDetLimitSideLen": 1920.0},
            {**INFERENCE, "textDetLimitSideLen": 960},
            {**INFERENCE, "textDetLimitType": "min"},
            missing,
            {**INFERENCE, "unexpected": 1},
        ):
            with self.subTest(inference=inference), self.assertRaises(OcrProtocolError):
                OcrHelloRequestV1.from_dict({**payload, "inference": inference})

    def test_process_request_preserves_order_and_rejects_relative_or_unbounded_paths(self) -> None:
        payload = _request()
        request = OcrProcessRequestV1.from_dict(payload)
        self.assertEqual(payload, request.to_dict())
        self.assertEqual("E:\\dataset\\nested\\image.png", request.items[0].imagePath)
        too_many = {**payload, "items": payload["items"] * 1025}
        escaped = copy.deepcopy(payload)
        escaped["items"][0]["relativeImagePath"] = "..\\image.png"  # type: ignore[index]
        relative_image = copy.deepcopy(payload)
        relative_image["items"][0]["imagePath"] = "nested\\image.png"  # type: ignore[index]
        for candidate in (too_many, escaped, relative_image):
            with self.subTest(candidate=candidate), self.assertRaises(OcrProtocolError):
                OcrProcessRequestV1.from_dict(candidate)

    def test_outcomes_are_complete_and_core_revalidates_identity_digest_and_geometry(self) -> None:
        request = OcrProcessRequestV1.from_dict(_request())
        outcome = _parse_result_valid(_success())[0]
        validate_outcome_for_item(outcome, request.items[0])
        validate_outcome_for_item(outcome, request.items[0], expected_width=100, expected_height=80)
        self.assertEqual(["Hello", "Hello"], [item.text for item in outcome.items])

        with self.assertRaises(OcrProtocolError):
            validate_outcome_for_item(outcome, request.items[0], expected_width=99, expected_height=80)

        for label, mutate in (
            ("wrong-lease", lambda value: value["items"][0].__setitem__("leaseId", "lease-other")),  # type: ignore[index]
            ("wrong-digest", lambda value: value["items"][0]["image"].__setitem__("sha256", "d" * 64)),  # type: ignore[index]
            ("wrong-size", lambda value: value["items"][0]["image"].__setitem__("sizeBytes", 124)),  # type: ignore[index]
            ("non-finite-confidence", lambda value: value["items"][0]["items"][0].__setitem__("confidence", math.nan)),  # type: ignore[index]
            ("polygon-mismatch", lambda value: value["items"][0]["items"][0]["polygonPixels"].__setitem__(0, [31, 1])),  # type: ignore[index]
        ):
            with self.subTest(case=label):
                candidate = _success()
                mutate(candidate)
                with self.assertRaises(OcrProtocolError):
                    parsed = parse_ocr_process_result(candidate)[0]
                    validate_outcome_for_item(parsed, request.items[0])

    def test_no_text_and_normal_inference_failure_have_complete_empty_shapes(self) -> None:
        request = OcrProcessRequestV1.from_dict(_request())
        for status in ("no_text", "failed"):
            with self.subTest(status=status):
                payload = _success()
                outcome = payload["items"][0]
                outcome["status"] = status
                outcome["items"] = []
                if status == "failed":
                    outcome["error"] = {
                        "code": "ocr_inference_failed",
                        "message": "The OCR engine failed for this image.",
                        "retriable": True,
                    }
                parsed = _parse_result_valid(payload)[0]
                validate_outcome_for_item(parsed, request.items[0])
        malformed = _success()
        malformed["items"][0]["status"] = "failed"
        malformed["items"][0]["items"] = []
        with self.assertRaises(OcrProtocolError):
            parse_ocr_process_result(malformed)

    def test_failed_outcome_is_a_strict_normal_or_oversize_union(self) -> None:
        request = OcrProcessRequestV1.from_dict(_request())
        normal_error = {
            "code": "ocr_inference_failed",
            "message": "The OCR engine failed for this image.",
            "retriable": True,
        }
        normal = _parse_result_valid(_failed_result(normal_error))[0]
        validate_outcome_for_item(normal, request.items[0])
        self.assertEqual(normal_error, normal.error.to_dict())

        oversize_error = {
            "code": "ocr_image_too_large",
            "message": "OCR image dimensions exceed the first-release safety limit.",
            "retriable": False,
            "details": {
                "actualPixels": 40_002_000,
                "maxPixels": 40_000_000,
                "maxSide": 16_384,
            },
        }
        oversize = _parse_result_valid(_failed_result(oversize_error, width=20_001, height=2_000))[0]
        validate_outcome_for_item(oversize, request.items[0], expected_width=20_001, expected_height=2_000)
        self.assertEqual(oversize_error, oversize.error.to_dict())

        invalid: list[tuple[str, dict[str, object]]] = []
        normal_with_details = dict(normal_error)
        normal_with_details["details"] = {"actualPixels": 8_000, "maxPixels": 40_000_000, "maxSide": 16_384}
        invalid.append(("normal-details-forbidden", _failed_result(normal_with_details)))
        normal_absolute_path = dict(normal_error)
        normal_absolute_path["message"] = "OCR failed for C:\\dataset\\image.png"
        invalid.append(("normal-absolute-path-message", _failed_result(normal_absolute_path)))

        for label, details in (
            ("actual-mismatch", {"actualPixels": 40_001_999, "maxPixels": 40_000_000, "maxSide": 16_384}),
            ("actual-negative", {"actualPixels": -1, "maxPixels": 40_000_000, "maxSide": 16_384}),
            ("actual-boolean", {"actualPixels": True, "maxPixels": 40_000_000, "maxSide": 16_384}),
            ("max-pixels-wrong", {"actualPixels": 40_002_000, "maxPixels": 40_000_001, "maxSide": 16_384}),
            ("max-side-wrong", {"actualPixels": 40_002_000, "maxPixels": 40_000_000, "maxSide": 16_383}),
            ("details-extra", {"actualPixels": 40_002_000, "maxPixels": 40_000_000, "maxSide": 16_384, "extra": 1}),
        ):
            error = copy.deepcopy(oversize_error)
            error["details"] = details
            invalid.append((label, _failed_result(error, width=20_001, height=2_000)))

        missing_details = dict(oversize_error)
        missing_details.pop("details")
        invalid.append(("missing-details", _failed_result(missing_details, width=20_001, height=2_000)))
        wrong_retriable = copy.deepcopy(oversize_error)
        wrong_retriable["retriable"] = True
        invalid.append(("oversize-retriable", _failed_result(wrong_retriable, width=20_001, height=2_000)))
        error_extra = copy.deepcopy(oversize_error)
        error_extra["extra"] = 1
        invalid.append(("error-extra", _failed_result(error_extra, width=20_001, height=2_000)))
        oversize_absolute_path = copy.deepcopy(oversize_error)
        oversize_absolute_path["message"] = "OCR rejected C:\\dataset\\image.png"
        invalid.append(("oversize-absolute-path-message", _failed_result(oversize_absolute_path, width=20_001, height=2_000)))
        invalid.append(("in-bounds-image", _failed_result(oversize_error)))

        for label, payload in invalid:
            with self.subTest(case=label), self.assertRaises(OcrProtocolError):
                parse_ocr_process_result(payload)

    def test_nonfailed_outcomes_accept_limits_and_reject_any_oversize_dimension(self) -> None:
        for width, height in ((10_000, 4_000), (16_384, 1)):
            with self.subTest(boundary=(width, height)):
                payload = _success()
                payload["items"][0]["status"] = "no_text"
                payload["items"][0]["image"]["width"] = width
                payload["items"][0]["image"]["height"] = height
                payload["items"][0]["items"] = []
                _parse_result_valid(payload)

        for width, height in ((8_001, 5_000), (16_385, 1)):
            with self.subTest(oversize=(width, height)), self.assertRaises(OcrProtocolError):
                payload = _success()
                payload["items"][0]["status"] = "no_text"
                payload["items"][0]["image"]["width"] = width
                payload["items"][0]["image"]["height"] = height
                payload["items"][0]["items"] = []
                parse_ocr_process_result(payload)


if __name__ == "__main__":
    unittest.main()
