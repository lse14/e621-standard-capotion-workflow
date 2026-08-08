from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.contracts import SampleIssue
from anima_core.classify_overlay import serialize_annotation_json
from anima_core.db import StateDatabase
from anima_core.nl_protocol import NlOutcomeV1
from anima_core.ocr_sidecar import parse_ocr_sidecar, serialize_ocr_sidecar
from anima_core.overlay import OverlayLayout
from anima_core.path_safety import windows_key
from anima_core.token_budget_review import (
    TokenBudgetReviewConflictError,
    TokenBudgetReviewError,
    TokenBudgetReviewService,
    parse_overflow_review,
)
from anima_core.token_budget_overlay import TokenBudgetRecord
from anima_caption_format import flat_txt_sha256
from anima_caption_format.normalizer import CaptionDisplayPolicy


def _ocr_context_sidecar(relative_image_path: str) -> bytes:
    image_bytes = b"review-ocr-image"
    value = {
        "schemaVersion": 1,
        "relativeImagePath": relative_image_path,
        "image": {"width": 10, "height": 8, "sizeBytes": len(image_bytes), "sha256": hashlib.sha256(image_bytes).hexdigest()},
        "status": "success",
        "engine": {"backend": "paddle", "resourceId": "ocr-ppocrv5-server-paddle-v1", "resourceFingerprint": "b" * 64},
        "settings": {
            "llmMinConfidence": 0.5,
            "inference": {
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useTextlineOrientation": True,
                "textRecScoreThresh": 0,
                "textDetLimitSideLen": 1920,
                "textDetLimitType": "max",
            },
        },
        "items": [
            {
                "index": 0, "text": "Visible sign", "confidence": 0.9,
                "polygonPixels": [[0, 0], [4, 0], [4, 4], [0, 4]],
                "polygon": [[0, 0], [0.4, 0], [0.4, 0.5], [0, 0.5]],
                "bboxPixels": [0, 0, 4, 4], "bbox": [0, 0, 0.4, 0.5],
                "position": "top-left", "textlineOrientationDegrees": 0, "includedForLlm": True,
            },
            {
                "index": 1, "text": "Low confidence", "confidence": 0.7,
                "polygonPixels": [[0, 0], [4, 0], [4, 4], [0, 4]],
                "polygon": [[0, 0], [0.4, 0], [0.4, 0.5], [0, 0.5]],
                "bboxPixels": [0, 0, 4, 4], "bbox": [0, 0, 0.4, 0.5],
                "position": "top-left", "textlineOrientationDegrees": 0, "includedForLlm": True,
            },
        ],
        "error": None,
    }
    return serialize_ocr_sidecar(parse_ocr_sidecar(json.dumps(value).encode("utf-8")))


class TokenBudgetReviewServiceContractTests(unittest.TestCase):
    @staticmethod
    def _within_budget_counter(policy: CaptionDisplayPolicy):
        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
            }
        return counter

    def _review_service(
        self, root: Path, *, schema_version: int = 6,
    ) -> tuple[StateDatabase, TokenBudgetReviewService]:
        dataset = root / "dataset"
        dataset.mkdir()
        baseline = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["baseline"], "environment": [], "nl": "baseline caption",
        }
        (dataset / "safe").mkdir()
        (dataset / "safe" / "image.json").write_text(json.dumps(baseline), encoding="utf-8")
        layout = OverlayLayout.create(dataset, "job-review")
        database = StateDatabase.open(root / "state.db")
        frozen_config = {
            "schemaVersion": schema_version,
            "captionFormat": {
                "replaceUnderscoresWithSpaces": False, "preserveEscapes": True,
                "triggersEnabled": False, "triggerTerms": [],
            },
            "tokenBudget": {
                "enabled": True, "maxTokens": 512, "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
                "resourceManifestRelativePath": "resources\\tokenizer.json", "resourceFingerprint": "a" * 64,
                "contextLimit": 40960,
            },
            "nl": {
                "captionPreset": "general", "systemPrompt": "", "lengthDistribution": {"short": 100, "medium": 0, "long": 0},
            },
        }
        database.insert_job({
            "job_id": "job-review", "config_schema_version": schema_version, "config_json": json.dumps(frozen_config), "config_hash": "a" * 64,
            "profile": "e621", "work_mode": "in_place", "overwrite_mode": "incremental", "source_root": str(dataset),
            "output_root": None, "dataset_root": str(dataset), "dataset_root_key": windows_key(dataset),
            "manifest_schema_version": 1, "recursive": 0, "sample_count": 1, "manifest_generated_at": "now",
            "status": "reviewing", "current_module_id": "token_budget", "last_event_id": 0, "pinned": 0,
            "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": str(layout.root), "commit_journal_path": None,
            "resume_status": None, "created_at": "now", "started_at": "now", "cancel_requested_at": None, "finished_at": None,
        })
        database.insert_samples("job-review", [{
            "sample_id": 1, "relative_image_path": "safe\\image.png", "annotation_key": "safe\\image", "source": "e621",
            "in_processing_scope": True, "image_format": "png", "image_frame_count": 1,
            "original_txt_state": "missing_or_blank", "original_json_state": "missing_or_blank",
        }])
        database.upsert_issue(SampleIssue(
            issueId="issue-overflow", jobId="job-review", sampleId=1, relativeImagePath="safe\\image.png",
            moduleId="token_budget", code="token_budget_overflow", severity="error", blocking=True, retriable=True,
            message="Token Budget exceeded the frozen maximum", attempt=1, repairStartModule="token_budget",
        ))
        layout.write_resource("token-budget\\reviews\\1.json", json.dumps({
            "schemaVersion": 1, "sampleId": 1, "leaseId": "lease-1", "status": "overflow",
            "originalTokens": 600, "finalTokens": 550,
            "removed": {"quality": ["high quality"], "environment": [], "tags": [], "appearance": []},
            "maxTokens": 512, "resourceId": "tokenizer-qwen3-0.6b-anima-v1", "resourceFingerprint": "a" * 64,
        }).encode("utf-8"))
        try:
            return database, TokenBudgetReviewService(database, "job-review")
        except Exception:
            database.close()
            raise

    def test_service_exposes_durable_review_cas_boundary(self) -> None:
        """Task 2.7 needs a Core-owned review service, not an API-only mutation."""
        self.assertTrue(callable(TokenBudgetReviewService))

    def test_v1_overflow_review_keeps_frozen_resource_identity(self) -> None:
        review = parse_overflow_review({
            "schemaVersion": 1,
            "sampleId": 7,
            "leaseId": "lease-7",
            "status": "overflow",
            "originalTokens": 600,
            "finalTokens": 550,
            "removed": {"quality": ["high quality"], "environment": [], "tags": [], "appearance": []},
            "maxTokens": 512,
            "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
            "resourceFingerprint": "a" * 64,
        })
        self.assertEqual(
            (7, "overflow", 600, 550, 512, "tokenizer-qwen3-0.6b-anima-v1", "a" * 64),
            (review.sampleId, review.status, review.originalTokens, review.finalTokens, review.maxTokens, review.resourceId, review.resourceFingerprint),
        )

    def test_page_uses_bounded_sample_keyset_and_does_not_expose_overlay_paths(self) -> None:
        for schema_version in (6, 7):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as temporary:
                database, service = self._review_service(Path(temporary), schema_version=schema_version)
                try:
                    page = service.page(after_sample_id=None, limit=500)
                finally:
                    database.close()
                self.assertEqual(1, page["targetCount"])
                self.assertEqual("safe\\image.png", page["items"][0]["relativeImagePath"])
                annotation = page["items"][0]["annotation"]
                self.assertEqual(
                    {"quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl"},
                    set(annotation),
                )
                self.assertEqual("baseline caption", annotation["nl"])
                serialized = json.dumps(page)
                self.assertNotIn("overlay", serialized)
                self.assertNotIn("dataset_root", serialized)
                self.assertNotIn("systemPrompt", serialized)

    def test_recount_preserves_missing_sample_identity_for_the_api_404_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                service = TokenBudgetReviewService(database, "job-review", counter=lambda *_: {})
                with self.assertRaises(KeyError):
                    service.recount(2, expected_version=1, annotation={})
            finally:
                database.close()

    def test_recount_creates_a_versioned_proposal_without_changing_working_annotation(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "A person wears a red dress.",
        }
        policy = CaptionDisplayPolicy(False, True, False, ())

        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
            }

        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                service = TokenBudgetReviewService(database, "job-review", counter=counter)
                proposal = service.recount(1, expected_version=1, annotation=annotation)
                with self.assertRaises(Exception):
                    service.recount(1, expected_version=1, annotation=annotation)
                job = database.get_job("job-review")
                layout = OverlayLayout.open_existing(str(job["overlay_root"]), "job-review")
                self.assertFalse(layout.annotation_path("safe\\image", ".json").exists())
            finally:
                database.close()
        self.assertEqual(2, proposal["version"])
        self.assertEqual("within_budget", proposal["status"])
        self.assertEqual(
            hashlib.sha256(json.dumps({
                "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                "appearance": [], "tags": ["baseline"], "environment": [], "nl": "baseline caption",
            }).encode("utf-8")).hexdigest(),
            proposal["baseAnnotationSha256"],
        )

    def test_page_exposes_the_saved_proposal_and_its_current_version(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "A person wears a red dress.",
        }
        policy = CaptionDisplayPolicy(False, True, False, ())

        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
            }

        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                service = TokenBudgetReviewService(database, "job-review", counter=counter)
                proposal = service.recount(1, expected_version=1, annotation=annotation)
                page = service.page(limit=1)
            finally:
                database.close()
        self.assertEqual(int(proposal["version"]), page["items"][0]["review"]["version"])
        self.assertEqual(proposal, page["items"][0]["proposal"])

    def test_apply_recounts_then_resolves_only_the_matching_overflow_issue(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "A person wears a red dress.",
        }
        policy = CaptionDisplayPolicy(False, True, False, ())

        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
            }

        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                database.connection.execute(
                    "UPDATE sample_state SET current_module_id='token_budget',status='failed' WHERE job_id='job-review' AND sample_id=1"
                )
                service = TokenBudgetReviewService(database, "job-review", counter=counter)
                service.recount(1, expected_version=1, annotation=annotation)
                record = service.apply(1, expected_version=2)
                state = database.get_sample_state("job-review", 1)
                issue = database.connection.execute(
                    "SELECT resolved_at FROM issues WHERE job_id='job-review' AND sample_id=1 AND code='token_budget_overflow'"
                ).fetchone()
            finally:
                database.close()
        self.assertEqual(("within_budget", 7), (record["status"], record["finalTokens"]))
        self.assertEqual(("pending", "export"), (state["status"], state["current_module_id"]))
        self.assertIsNotNone(issue["resolved_at"])

    def test_apply_accepts_within_budget_confirmation_of_a_trimmed_proposal(self) -> None:
        original = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress", "blue shoes"], "environment": [],
            "nl": "A person wears a red dress and blue shoes.",
        }
        trimmed = {**original, "tags": ["red dress"]}
        policy = CaptionDisplayPolicy(False, True, False, ())

        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            if lease_id.startswith("review-apply-"):
                return {
                    "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                    "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                    "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                    "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
                }
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "trimmed", "originalTokens": 600, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": ["blue shoes"], "appearance": []},
                "annotation": trimmed, "flatTextSha256": flat_txt_sha256(trimmed, policy),
            }

        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                database.connection.execute(
                    "UPDATE sample_state SET current_module_id='token_budget',status='failed' WHERE job_id='job-review' AND sample_id=1"
                )
                service = TokenBudgetReviewService(database, "job-review", counter=counter)
                proposal = service.recount(1, expected_version=1, annotation=original)
                record = service.apply(1, expected_version=int(proposal["version"]))
            finally:
                database.close()
        self.assertEqual(("trimmed", 600, 7), (record["status"], record["originalTokens"], record["finalTokens"]))

    def test_apply_rejects_a_proposal_when_the_working_annotation_changed(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "A person wears a red dress.",
        }
        policy = CaptionDisplayPolicy(False, True, False, ())

        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._review_service(root)
            try:
                database.connection.execute(
                    "UPDATE sample_state SET current_module_id='token_budget',status='failed' WHERE job_id='job-review' AND sample_id=1"
                )
                service = TokenBudgetReviewService(database, "job-review", counter=counter)
                proposal = service.recount(1, expected_version=1, annotation=annotation)
                (root / "dataset" / "safe" / "image.json").write_text(json.dumps({**annotation, "nl": "manual edit"}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed before apply"):
                    service.apply(1, expected_version=int(proposal["version"]))
            finally:
                database.close()

    def test_review_recovers_a_prepared_apply_before_exposing_the_review_page(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "A person wears a red dress.",
        }
        policy = CaptionDisplayPolicy(False, True, False, ())
        lease_id = "review-apply-1-2"
        with tempfile.TemporaryDirectory() as temporary:
            database, service = self._review_service(Path(temporary))
            try:
                job = database.get_job("job-review")
                layout = OverlayLayout.open_existing(str(job["overlay_root"]), "job-review")
                prepared, digest = layout.write_prepared("token_budget", lease_id, ".json", serialize_annotation_json(annotation))
                relative = str(prepared.relative_to(layout.root)).replace("/", "\\")
                database.connection.execute(
                    """UPDATE sample_state SET current_module_id='token_budget',status='leased',lease_id=?,
                       worker_instance_id='token-budget-review' WHERE job_id='job-review' AND sample_id=1""",
                    (lease_id,),
                )
                database.stage_prepared_artifact("job-review", 1, lease_id=lease_id, relative_path=relative, sha256=digest)
                record = TokenBudgetRecord(
                    sample_id=1, lease_id=lease_id, status="within_budget", original_tokens=7, final_tokens=7,
                    removed={"quality": [], "environment": [], "tags": [], "appearance": []},
                    flat_text_sha256=flat_txt_sha256(annotation, policy),
                    annotation_relative_path="annotations\\safe\\image.json", max_tokens=512,
                ).to_dict()
                layout.write_resource("token-budget\\records\\1.json", (json.dumps(record) + "\n").encode("utf-8"))
                layout.write_resource(f"token-budget\\prepared\\{lease_id}.json", (json.dumps(record) + "\n").encode("utf-8"))
                service.page(limit=1)
                state = database.get_sample_state("job-review", 1)
                issue = database.connection.execute(
                    "SELECT resolved_at FROM issues WHERE job_id='job-review' AND sample_id=1 AND code='token_budget_overflow'"
                ).fetchone()
            finally:
                database.close()
        self.assertEqual(("pending", "export"), (state["status"], state["current_module_id"]))
        self.assertIsNotNone(issue["resolved_at"])

    def test_rewrite_short_uses_exact_selection_and_stages_only_a_proposal(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "long original caption",
        }
        policy = CaptionDisplayPolicy(False, True, False, ())

        def counter(sample_id: int, lease_id: str, value: dict[str, object], _: object) -> dict[str, object]:
            return {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id,
                "leaseId": lease_id, "status": "within_budget", "originalTokens": 7, "finalTokens": 7,
                "removed": {"quality": [], "environment": [], "tags": [], "appearance": []},
                "annotation": value, "flatTextSha256": flat_txt_sha256(value, policy),
            }

        sent: list[dict[str, object]] = []

        def rewriter(item: dict[str, object]) -> str:
            sent.append(item)
            return "short replacement caption"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._review_service(root)
            try:
                (root / "dataset" / "safe").mkdir(exist_ok=True)
                (root / "dataset" / "safe" / "image.json").write_text(json.dumps(annotation), encoding="utf-8")
                service = TokenBudgetReviewService(database, "job-review", counter=counter, rewriter=rewriter)
                result = service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                replay = service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                job = database.get_job("job-review")
                layout = OverlayLayout.open_existing(str(job["overlay_root"]), "job-review")
                self.assertFalse(layout.annotation_path("safe\\image", ".json").exists())
                page = service.page(limit=1)
            finally:
                database.close()
        self.assertEqual("short", sent[0]["lengthTier"])
        self.assertEqual("review-rewrite-1-1", sent[0]["leaseId"])
        self.assertEqual(1, len(sent))
        self.assertEqual([1], result["sampleIds"])
        self.assertEqual(result, replay)
        self.assertEqual("within_budget", result["proposals"][0]["status"])
        self.assertEqual(result["proposals"][0], page["items"][0]["rewriteProposal"]["proposal"])

    def test_rewrite_short_does_not_send_when_the_frozen_nl_budget_is_exhausted(self) -> None:
        """A user-triggered rewrite shares the job's bounded NL request budget."""
        policy = CaptionDisplayPolicy(False, True, False, ())
        calls: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                job = database.get_job("job-review")
                config = json.loads(str(job["config_json"]))
                config["nl"]["apiPolicy"] = {"maxHttpAttempts": 1, "mainAttempts": 1}
                database.connection.execute(
                    "UPDATE jobs SET config_json=? WHERE job_id='job-review'", (json.dumps(config),)
                )
                database.increment_module_diagnostic(
                    "job-review", "nl", "nl_http_attempts", severity="info", amount=1,
                )
                service = TokenBudgetReviewService(
                    database,
                    "job-review",
                    counter=self._within_budget_counter(policy),
                    rewriter=lambda item: calls.append(item) or "must not be sent",
                )
                with self.assertRaisesRegex(TokenBudgetReviewError, "budget"):
                    service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
            finally:
                database.close()
        self.assertEqual([], calls)

    def test_rewrite_short_settles_only_the_worker_reported_http_attempts(self) -> None:
        policy = CaptionDisplayPolicy(False, True, False, ())
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                job = database.get_job("job-review")
                config = json.loads(str(job["config_json"]))
                config["nl"]["apiPolicy"] = {"maxHttpAttempts": 2, "mainAttempts": 1}
                database.connection.execute(
                    "UPDATE jobs SET config_json=? WHERE job_id='job-review'", (json.dumps(config),)
                )
                database.increment_module_diagnostic(
                    "job-review", "nl", "nl_http_attempts", severity="info", amount=1,
                )

                def rewriter(item: dict[str, object]) -> NlOutcomeV1:
                    return NlOutcomeV1(
                        sampleId=int(item["sampleId"]), leaseId=str(item["leaseId"]),
                        relativeImagePath=str(item["relativeImagePath"]), nl="short replacement caption",
                        code=None, retriable=False, httpAttempts=1,
                    )

                service = TokenBudgetReviewService(
                    database, "job-review", counter=self._within_budget_counter(policy), rewriter=rewriter,
                )
                service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                used = database.module_diagnostic_count("job-review", "nl", "nl_http_attempts")
                reserved = database.module_diagnostic_count("job-review", "nl", "nl_review_http_reserved")
            finally:
                database.close()
        self.assertEqual((2, 0), (used, reserved))

    def test_rewrite_rejects_an_unknown_sample_before_creating_a_started_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                service = TokenBudgetReviewService(database, "job-review", rewriter=lambda _: "unused")
                with self.assertRaises(KeyError):
                    service.rewrite_short(sample_ids=[2], expected_versions={"2": 1})
                layout = OverlayLayout.open_existing(str(database.get_job("job-review")["overlay_root"]), "job-review")
                self.assertFalse((layout.resource_path("token-budget\\rewrites")).exists())
            finally:
                database.close()

    def test_rewrite_marks_an_outbound_failure_as_outcome_unknown_without_retry(self) -> None:
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["red dress"], "environment": [], "nl": "long original caption",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._review_service(root)
            try:
                (root / "dataset" / "safe" / "image.json").write_text(json.dumps(annotation), encoding="utf-8")
                service = TokenBudgetReviewService(database, "job-review", rewriter=lambda _: (_ for _ in ()).throw(RuntimeError("lost response")))
                with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
                    service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                layout = OverlayLayout.open_existing(str(database.get_job("job-review")["overlay_root"]), "job-review")
                operation = next(layout.resource_path("token-budget\\rewrites").glob("*.json"))
                state = json.loads(operation.read_text(encoding="utf-8"))
                self.assertEqual(("outcome_unknown", 2), (state["status"], state["httpAttempts"]))
                with self.assertRaisesRegex(RuntimeError, "will not be retried"):
                    service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
            finally:
                database.close()

    def test_rewrite_rejects_a_response_after_the_working_annotation_changes(self) -> None:
        policy = CaptionDisplayPolicy(False, True, False, ())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._review_service(root)
            try:
                def rewriter(_: dict[str, object]) -> str:
                    (root / "dataset" / "safe" / "image.json").write_text(json.dumps({
                        "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                        "appearance": [], "tags": ["concurrent"], "environment": [], "nl": "manual edit",
                    }), encoding="utf-8")
                    return "short replacement caption"

                service = TokenBudgetReviewService(
                    database, "job-review", counter=self._within_budget_counter(policy), rewriter=rewriter,
                )
                with self.assertRaises(TokenBudgetReviewConflictError):
                    service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                layout = OverlayLayout.open_existing(str(database.get_job("job-review")["overlay_root"]), "job-review")
                operation = json.loads(next(layout.resource_path("token-budget\\rewrites").glob("*.json")).read_text(encoding="utf-8"))
                self.assertEqual("conflict", operation["status"])
                self.assertIn("baseAnnotationSha256", operation)
                self.assertFalse(layout.resource_path("token-budget\\proposals\\1.json").exists())
            finally:
                database.close()

    def test_rewrite_rejects_an_edit_between_response_check_and_recount(self) -> None:
        """The candidate must retain the digest captured before the outbound request."""
        policy = CaptionDisplayPolicy(False, True, False, ())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._review_service(root)
            try:
                service = TokenBudgetReviewService(
                    database, "job-review", counter=self._within_budget_counter(policy),
                    rewriter=lambda _: "short replacement caption",
                )
                original_current_annotation = service._current_annotation
                calls = 0

                def mutate_before_recount(*args: object) -> tuple[dict[str, object], str]:
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        (root / "dataset" / "safe" / "image.json").write_text(json.dumps({
                            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
                            "appearance": [], "tags": ["manual edit"], "environment": [], "nl": "manual caption",
                        }), encoding="utf-8")
                    return original_current_annotation(*args)

                service._current_annotation = mutate_before_recount  # type: ignore[method-assign]
                with self.assertRaises(TokenBudgetReviewConflictError):
                    service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                layout = OverlayLayout.open_existing(str(database.get_job("job-review")["overlay_root"]), "job-review")
                operation = json.loads(next(layout.resource_path("token-budget\\rewrites").glob("*.json")).read_text(encoding="utf-8"))
                self.assertEqual("conflict", operation["status"])
                self.assertFalse(layout.resource_path("token-budget\\proposals\\1.json").exists())
            finally:
                database.close()

    def test_rewrite_reuses_bounded_working_ocr_sidecar_context(self) -> None:
        policy = CaptionDisplayPolicy(False, True, False, ())
        sent: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._review_service(root)
            try:
                job = database.get_job("job-review")
                config = json.loads(str(job["config_json"]))
                config["ocr"] = {"enabled": True, "llmMinConfidence": 0.8}
                database.connection.execute("UPDATE jobs SET config_json=? WHERE job_id='job-review'", (json.dumps(config),))
                layout = OverlayLayout.open_existing(str(job["overlay_root"]), "job-review")
                layout.write_ocr_sidecar("safe\\image.png", _ocr_context_sidecar("safe\\image.png"))

                def rewriter(item: dict[str, object]) -> str:
                    sent.append(item)
                    return "short replacement caption"

                service = TokenBudgetReviewService(
                    database, "job-review", counter=self._within_budget_counter(policy), rewriter=rewriter,
                )
                service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
            finally:
                database.close()
        self.assertEqual({"items": [["top-left", "Visible sign"]]}, sent[0]["ocrContext"])

    def test_rewrite_records_the_worker_outcome_http_attempts_and_keeps_string_callbacks_compatible(self) -> None:
        policy = CaptionDisplayPolicy(False, True, False, ())
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                job = database.get_job("job-review")
                config = json.loads(str(job["config_json"]))
                config["nl"]["apiPolicy"] = {"maxHttpAttempts": 3, "mainAttempts": 3}
                database.connection.execute(
                    "UPDATE jobs SET config_json=? WHERE job_id='job-review'", (json.dumps(config),)
                )
                def rewriter(item: dict[str, object]) -> NlOutcomeV1:
                    return NlOutcomeV1(
                        sampleId=int(item["sampleId"]), leaseId=str(item["leaseId"]),
                        relativeImagePath=str(item["relativeImagePath"]), nl="short replacement caption",
                        code=None, retriable=False, httpAttempts=3,
                    )

                service = TokenBudgetReviewService(
                    database, "job-review", counter=self._within_budget_counter(policy), rewriter=rewriter,
                )
                service.rewrite_short(sample_ids=[1], expected_versions={"1": 1})
                layout = OverlayLayout.open_existing(str(database.get_job("job-review")["overlay_root"]), "job-review")
                operation = json.loads(next(layout.resource_path("token-budget\\rewrites").glob("*.json")).read_text(encoding="utf-8"))
            finally:
                database.close()
        self.assertEqual(("completed", 3), (operation["status"], operation["httpAttempts"]))

    def test_recount_without_an_initial_overflow_is_a_bad_request_but_a_race_is_a_conflict(self) -> None:
        policy = CaptionDisplayPolicy(False, True, False, ())
        annotation = {
            "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
            "appearance": [], "tags": ["candidate"], "environment": [], "nl": "candidate caption",
        }
        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                database.connection.execute("DELETE FROM issues WHERE job_id='job-review' AND sample_id=1")
                calls: list[int] = []
                service = TokenBudgetReviewService(
                    database,
                    "job-review",
                    counter=lambda *_: calls.append(1) or self._within_budget_counter(policy)(*_),
                )
                with self.assertRaises(TokenBudgetReviewError):
                    service.recount(1, expected_version=1, annotation=annotation)
                self.assertEqual([], calls)
            finally:
                database.close()

        with tempfile.TemporaryDirectory() as temporary:
            database, _ = self._review_service(Path(temporary))
            try:
                def resolves_during_count(*args: object) -> dict[str, object]:
                    database.connection.execute("UPDATE issues SET resolved_at=datetime('now') WHERE job_id='job-review' AND sample_id=1")
                    return self._within_budget_counter(policy)(*args)

                service = TokenBudgetReviewService(database, "job-review", counter=resolves_during_count)
                with self.assertRaises(TokenBudgetReviewConflictError):
                    service.recount(1, expected_version=1, annotation=annotation)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
