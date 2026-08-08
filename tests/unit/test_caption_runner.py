from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.caption_protocol import (
    CaptionHelloResultV1,
    CaptionIssueResultV1,
    CaptionResultV1,
    CaptionTagV1,
    CaptionWorkItemV1,
)
from anima_core.caption_runner import (
    CaptionRunner,
    CaptionRunnerFatalError,
    missing_dictionary_tags,
    stable_caption_issue_id,
    verify_tagger_dictionary_coverage,
)
from anima_core.contracts import JobConfig, ProgressEvent
from anima_core.db import StateDatabase
from anima_core.path_safety import windows_key
from anima_core.raw_e621 import RawE621JsonError, parse_raw_e621_annotation
from anima_core.scheduler import BoundedScheduler
from anima_core.worker_protocol import ProtocolEnvelopeV1


RESOURCE_FINGERPRINT = "a" * 64
PROVIDER = "CPUExecutionProvider"
CAPTION_MANIFEST = "manifests\\resources\\caption-e621.json"
CLASSIFY_MANIFEST = "manifests\\resources\\classify-e621.json"


def _install(root: Path, *, tag_names: list[str], dictionary_tags: list[str]) -> Path:
    """Minimal install tree with only what the coverage check reads."""
    install = root / "install"
    caption_relative = "resources\\e621\\caption\\model"
    classify_relative = "resources\\e621\\classify\\dictionary"
    for manifest_relative, root_relative, name, payload in (
        (CAPTION_MANIFEST, caption_relative, "tags.json", {"tag_names": tag_names}),
        (
            CLASSIFY_MANIFEST,
            classify_relative,
            "e621_tag_dictionary.json",
            {"entries": {tag: {"bucket": "tags"} for tag in dictionary_tags}},
        ),
    ):
        manifest_path = install / Path(manifest_relative.replace("\\", "/"))
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"rootRelativePath": root_relative}), encoding="utf-8")
        resource_path = install / Path(root_relative.replace("\\", "/")) / name
        resource_path.parent.mkdir(parents=True, exist_ok=True)
        resource_path.write_text(json.dumps(payload), encoding="utf-8")
    return install


def _job(
    job_id: str,
    root: Path,
    count: int,
    *,
    overwrite_mode: str = "incremental",
    overwrite_txt: bool = False,
    profile: str = "e621",
    schema_version: int = 3,
    input_txt_mode: str | None = None,
    tagger_fallback_on_missing_txt: bool | None = None,
) -> dict[str, object]:
    config = JobConfig(
        profile=profile,  # type: ignore[arg-type]
        workMode="in_place",
        overwriteMode=overwrite_mode,  # type: ignore[arg-type]
        sourceRoot=str(root),
        schemaVersion=schema_version,
    )
    config.caption["overwriteTxt"] = overwrite_txt
    if input_txt_mode is not None:
        config.caption["inputTxtMode"] = input_txt_mode
    if tagger_fallback_on_missing_txt is not None:
        config.caption["taggerFallbackOnMissingTxt"] = tagger_fallback_on_missing_txt
    return {
        "job_id": job_id,
        "config_schema_version": config.schemaVersion,
        "config_json": json.dumps(config.to_dict()),
        "config_hash": config.config_hash,
        "profile": profile,
        "work_mode": "in_place",
        "overwrite_mode": overwrite_mode,
        "source_root": str(root),
        "output_root": None,
        "dataset_root": str(root),
        "dataset_root_key": windows_key(root),
        "manifest_schema_version": 1,
        "recursive": 0,
        "sample_count": count,
        "manifest_generated_at": "2026-07-24T00:00:00Z",
        "status": "ready",
        "current_module_id": None,
        "last_event_id": 0,
        "pinned": 0,
        "api_budget_extra": 0,
        "api_budget_revision": 0,
        "overlay_root": None,
        "commit_journal_path": None,
        "resume_status": None,
        "created_at": "2026-07-24T00:00:00Z",
        "started_at": None,
        "cancel_requested_at": None,
        "finished_at": None,
    }


def _samples(
    count: int,
    *,
    original_txt_state: str = "missing_or_blank",
    profile: str = "e621",
) -> list[dict[str, object]]:
    return [
        {
            "sample_id": sample_id,
            "relative_image_path": f"nested\\{sample_id}.png",
            "annotation_key": f"nested\\{sample_id}",
            "source": profile,
            "in_processing_scope": True,
            "image_format": "png",
            "image_frame_count": 1,
            "original_txt_state": original_txt_state,
            "original_json_state": "missing_or_blank",
            "image_file_id": f"volume:{sample_id}",
            "image_size": 100 + sample_id,
            "image_mtime_ns": 1_000_000 + sample_id,
        }
        for sample_id in range(1, count + 1)
    ]


def _result(item: CaptionWorkItemV1, *, lease_id: str | None = None) -> dict[str, object]:
    return CaptionResultV1(
        sampleId=item.sampleId,
        leaseId=lease_id or item.leaseId,
        relativeImagePath=item.relativeImagePath,
        tags=(CaptionTagV1("test_tag", 0.75, "general"),),
        formattedTxt="test_tag",
        provider=PROVIDER,
        source=item.source,
    ).to_dict()


class FakeCaptionTransport:
    def __init__(
        self,
        outcome: Callable[[CaptionWorkItemV1], dict[str, object]] = _result,
        *,
        fatal_code: str | None = None,
        on_process: Callable[[CaptionWorkItemV1], None] | None = None,
    ) -> None:
        self.outcome = outcome
        self.fatal_code = fatal_code
        self.on_process = on_process
        self.hello_requests = 0
        self.process_requests = 0
        self.item_counts: list[int] = []

    @staticmethod
    def _response(
        request: ProtocolEnvelopeV1,
        sequence: int,
        method: str,
        payload: dict[str, object],
    ) -> ProtocolEnvelopeV1:
        return ProtocolEnvelopeV1(
            protocolVersion="1.0",
            kind="response",
            messageId=f"response-{sequence}",
            runtimeId="caption-e621",
            owner="caption",
            method=method,
            payload=payload,
            replyTo=request.messageId,
            jobId=request.jobId,
            configHash=request.configHash,
        )

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if request.method == "hello":
            self.hello_requests += 1
            payload = CaptionHelloResultV1(
                executable=r"C:\Anima\runtimes\caption-e621\python.exe",
                provider=PROVIDER,
                resourceFingerprint=RESOURCE_FINGERPRINT,
                tagCount=106_536 if request.payload.get("profile") == "danbooru" else 8_783,
            ).to_dict()
            return self._response(request, self.hello_requests, "hello", payload)
        self.process_requests += 1
        items = request.payload.get("items")
        if not isinstance(items, list):
            raise AssertionError("process request has no item list")
        self.item_counts.append(len(items))
        item = CaptionWorkItemV1.from_dict(items[0])
        if self.on_process is not None:
            self.on_process(item)
        if self.fatal_code is not None:
            return self._response(request, self.process_requests, "error", {"code": self.fatal_code})
        return self._response(request, self.process_requests, "result", self.outcome(item))


class CaptionRunnerTests(unittest.TestCase):
    def _database(
        self,
        root: Path,
        count: int,
        *,
        original_txt_state: str = "missing_or_blank",
        overwrite_mode: str = "incremental",
        overwrite_txt: bool = False,
        profile: str = "e621",
        schema_version: int = 3,
        input_txt_mode: str | None = None,
        tagger_fallback_on_missing_txt: bool | None = None,
    ) -> tuple[StateDatabase, BoundedScheduler]:
        database = StateDatabase.open(root / "state.db")
        database.insert_job(
            _job(
                "job-caption",
                root,
                count,
                overwrite_mode=overwrite_mode,
                overwrite_txt=overwrite_txt,
                profile=profile,
                schema_version=schema_version,
                input_txt_mode=input_txt_mode,
                tagger_fallback_on_missing_txt=tagger_fallback_on_missing_txt,
            )
        )
        database.insert_samples(
            "job-caption",
            _samples(count, original_txt_state=original_txt_state, profile=profile),
        )
        lease_ids = iter(f"lease-{index}" for index in range(1, count + 100))
        scheduler = BoundedScheduler(database, lease_id_factory=lease_ids.__next__)
        scheduler.start_module("job-caption", "caption", enabled=True, profile=profile)
        return database, scheduler

    @staticmethod
    def _runner(
        database: StateDatabase,
        scheduler: BoundedScheduler,
        transport: FakeCaptionTransport,
        *,
        result_consumer: Callable[[CaptionWorkItemV1, CaptionResultV1], None],
        progress_consumer: Callable[[ProgressEvent], None] | None = None,
        install_root: Path | None = None,
        raw_e621_reader: Callable[[str], object] | None = None,
    ) -> CaptionRunner:
        message_ids = iter(f"request-{index}" for index in range(1, 10_000))
        return CaptionRunner(
            database,
            scheduler,
            transport,
            job_id="job-caption",
            worker_instance_id="caption-worker-1",
            resource_manifest_relative_path=CAPTION_MANIFEST,
            resource_fingerprint=RESOURCE_FINGERPRINT,
            result_consumer=result_consumer,
            progress_consumer=progress_consumer,
            install_root=install_root,
            raw_e621_reader=raw_e621_reader,
            clock=lambda: 100.0,
            message_id_factory=message_ids.__next__,
        )

    def test_raw_e621_json_skips_tagger_and_records_conversion_diagnostic(self) -> None:
        raw = parse_raw_e621_annotation(
            b'{"artist":["kannos"],"character":[],"contributor":[],"copyright":[],"general":["solo"],"invalid":[],"lore":[],"meta":[],"species":[]}'
        )
        assert raw is not None
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 1)
            transport = FakeCaptionTransport()
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda *_: self.fail("raw E621 JSON must not produce a caption result"),
                    raw_e621_reader=lambda _: raw,
                ).run()
                self.assertEqual(("completed", 0, 1), (report.status, report.processRequests, report.skipped))
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                self.assertEqual(
                    1,
                    database.module_diagnostic_count("job-caption", "caption", "e621_raw_json_converted"),
                )
            finally:
                database.close()

    def test_malformed_raw_e621_json_creates_issue_without_tagger_fallback(self) -> None:
        def invalid(_: str) -> object:
            raise RawE621JsonError("raw E621 JSON group artist must be a string array")

        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 1)
            transport = FakeCaptionTransport()
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda *_: self.fail("invalid raw E621 JSON must not produce a caption result"),
                    raw_e621_reader=invalid,
                ).run()
                issue = database.page_issues("job-caption", limit=1)[0]
                self.assertEqual("completed_with_issues", report.status)
                self.assertEqual(("caption", "e621_raw_json_invalid", 0), (
                    issue["module_id"], issue["code"], issue["retriable"],
                ))
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
            finally:
                database.close()

    def test_runner_is_bounded_single_item_and_publishes_persisted_aggregate_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 130)
            transport = FakeCaptionTransport()
            consumed = 0
            observed: list[ProgressEvent] = []

            def consume(_item: CaptionWorkItemV1, _result_value: CaptionResultV1) -> None:
                nonlocal consumed
                consumed += 1

            def progress(event: ProgressEvent) -> None:
                persisted = database.event_page("job-caption", event.eventId - 1, limit=1)
                self.assertEqual(event.eventId, persisted[0]["event_id"])
                summary = database.module_summary("job-caption", "caption")
                settled = int(summary["completed"] + summary["failed"] + summary["skipped"])
                self.assertGreaterEqual(settled, event.completed)
                observed.append(event)

            def forbidden_page(*_args: object, **_kwargs: object) -> list[object]:
                raise AssertionError("caption runner must not page or materialize the whole SampleManifest")

            database.page_samples = forbidden_page  # type: ignore[method-assign]
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=consume,
                    progress_consumer=progress,
                ).run()
                self.assertEqual("completed", report.status)
                self.assertEqual((130, 130, 0), (report.total, report.completed, report.failed))
                self.assertEqual(64, report.maxResidentLeases)
                self.assertEqual((1, 130, 130), (report.helloRequests, report.processRequests, consumed))
                self.assertEqual(1, transport.hello_requests)
                self.assertTrue(transport.item_counts)
                self.assertEqual({1}, set(transport.item_counts))
                self.assertEqual([0, 130], [event.completed for event in observed])
                self.assertEqual(2, database.count("event_ring", "job-caption"))
                source = (ROOT / "core" / "src" / "anima_core" / "caption_runner.py").read_text(encoding="utf-8")
                self.assertNotIn("scandir", source)
                self.assertNotIn("rglob", source)
            finally:
                database.close()

    def test_result_identity_mismatch_is_fatal_and_releases_the_bounded_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 2)
            transport = FakeCaptionTransport(lambda item: _result(item, lease_id="wrong-lease"))
            try:
                with self.assertRaises(CaptionRunnerFatalError) as raised:
                    self._runner(
                        database,
                        scheduler,
                        transport,
                        result_consumer=lambda *_: self.fail("mismatched result must not be consumed"),
                    ).run()
                self.assertEqual("caption_protocol_violation", raised.exception.code)
                self.assertEqual(1, transport.process_requests)
                self.assertEqual("failed", database.get_job("job-caption")["status"])
                self.assertEqual("failed", database.module_summary("job-caption", "caption")["status"])
                self.assertEqual("pending", database.get_sample_state("job-caption", 1)["status"])
                self.assertEqual("pending", database.get_sample_state("job-caption", 2)["status"])
                self.assertEqual(0, database.count_in_flight("job-caption"))
            finally:
                database.close()

    def test_retriable_and_non_retriable_issues_are_stable_failed_results(self) -> None:
        def issue(item: CaptionWorkItemV1) -> dict[str, object]:
            if item.sampleId == 1:
                return CaptionIssueResultV1(
                    sampleId=item.sampleId,
                    leaseId=item.leaseId,
                    relativeImagePath=item.relativeImagePath,
                    code="caption_image_decode_failed",
                    retriable=True,
                    message="decode failed",
                    repairStartModule="caption",
                ).to_dict()
            return CaptionIssueResultV1(
                sampleId=item.sampleId,
                leaseId=item.leaseId,
                relativeImagePath=item.relativeImagePath,
                code="caption_no_tags",
                retriable=False,
                message="no tags",
                repairStartModule=None,
            ).to_dict()

        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 2)
            try:
                report = self._runner(
                    database,
                    scheduler,
                    FakeCaptionTransport(issue),
                    result_consumer=lambda *_: self.fail("issue outcomes must not reach the result consumer"),
                ).run()
                self.assertEqual("completed_with_issues", report.status)
                self.assertEqual((0, 2, 2), (report.completed, report.failed, report.issueCount))
                self.assertEqual("failed", database.get_sample_state("job-caption", 1)["status"])
                self.assertEqual("failed", database.get_sample_state("job-caption", 2)["status"])
                issues = database.page_issues("job-caption", limit=10)
                self.assertEqual(2, len(issues))
                self.assertEqual((1, 0), (issues[0]["retriable"], issues[1]["retriable"]))
                self.assertEqual(
                    stable_caption_issue_id("job-caption", 1, "caption_image_decode_failed"),
                    issues[0]["issue_id"],
                )
                final_event = database.event_page("job-caption", 0, limit=10)[-1]
                self.assertEqual(2, final_event["completed"])
            finally:
                database.close()

    def test_fatal_worker_response_stops_without_sending_the_next_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 3)
            transport = FakeCaptionTransport(fatal_code="caption_source_fingerprint_mismatch")
            try:
                with self.assertRaises(CaptionRunnerFatalError) as raised:
                    self._runner(
                        database,
                        scheduler,
                        transport,
                        result_consumer=lambda *_: self.fail("fatal worker response has no result"),
                    ).run()
                self.assertEqual("caption_source_fingerprint_mismatch", raised.exception.code)
                self.assertEqual(1, transport.process_requests)
                self.assertEqual(0, database.count_in_flight("job-caption"))
            finally:
                database.close()

    def test_cancellation_finishes_only_the_in_flight_item_and_starts_no_new_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 70)
            cancellation_requested = False

            def cancel(_item: CaptionWorkItemV1) -> None:
                nonlocal cancellation_requested
                if not cancellation_requested:
                    cancellation_requested = True
                    scheduler.begin_cancellation("job-caption")

            transport = FakeCaptionTransport(on_process=cancel)
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda *_: None,
                ).run()
                self.assertEqual("cancelling", report.status)
                self.assertEqual(1, report.completed)
                self.assertEqual(1, transport.process_requests)
                self.assertEqual("completed", database.get_sample_state("job-caption", 1)["status"])
                self.assertEqual("pending", database.get_sample_state("job-caption", 2)["status"])
                self.assertEqual(0, database.count_in_flight("job-caption"))
                scheduler.settle_cancellation("job-caption")
                self.assertEqual("cancelled_recoverable", database.get_job("job-caption")["status"])
            finally:
                database.close()

    def test_incremental_nonblank_without_overwrite_skips_before_worker_hello(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(
                Path(temporary),
                3,
                original_txt_state="nonblank",
                overwrite_mode="incremental",
                overwrite_txt=False,
            )
            transport = FakeCaptionTransport()
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda *_: self.fail("preserved TXT must skip inference"),
                ).run()
                self.assertEqual("completed", report.status)
                self.assertEqual((0, 3), (report.completed, report.skipped))
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                for sample_id in range(1, 4):
                    state = database.get_sample_state("job-caption", sample_id)
                    self.assertEqual("skipped", state["status"])
                    self.assertEqual("original_preserved", state["txt_provenance"])
            finally:
                database.close()

    def test_rebuild_or_explicit_overwrite_infers_nonblank_txt(self) -> None:
        cases = (("rebuild", False), ("incremental", True))
        for overwrite_mode, overwrite_txt in cases:
            with self.subTest(overwrite_mode=overwrite_mode, overwrite_txt=overwrite_txt), tempfile.TemporaryDirectory() as temporary:
                database, scheduler = self._database(
                    Path(temporary),
                    1,
                    original_txt_state="nonblank",
                    overwrite_mode=overwrite_mode,
                    overwrite_txt=overwrite_txt,
                )
                transport = FakeCaptionTransport()
                try:
                    report = self._runner(
                        database,
                        scheduler,
                        transport,
                        result_consumer=lambda *_: None,
                    ).run()
                    self.assertEqual((1, 0), (report.completed, report.skipped))
                    self.assertEqual((1, 1), (transport.hello_requests, transport.process_requests))
                    state = database.get_sample_state("job-caption", 1)
                    self.assertEqual("completed", state["status"])
                    self.assertEqual("module1_written", state["txt_provenance"])
                finally:
                    database.close()

    def test_v8_tag_nonblank_txt_skips_tagger_even_when_overwrite_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(
                Path(temporary),
                1,
                original_txt_state="nonblank",
                overwrite_mode="rebuild",
                overwrite_txt=True,
                schema_version=8,
                input_txt_mode="tag",
            )
            transport = FakeCaptionTransport()
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda *_: self.fail("v8 Tag TXT must skip inference"),
                ).run()
                self.assertEqual(("completed", 0, 1), (report.status, report.completed, report.skipped))
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                state = database.get_sample_state("job-caption", 1)
                self.assertEqual(("skipped", "original_preserved"), (state["status"], state["txt_provenance"]))
            finally:
                database.close()

    def test_v8_tag_missing_or_blank_txt_uses_tagger_when_fallback_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(
                Path(temporary),
                1,
                original_txt_state="missing_or_blank",
                schema_version=8,
                input_txt_mode="tag",
                tagger_fallback_on_missing_txt=True,
            )
            transport = FakeCaptionTransport()
            try:
                report = self._runner(database, scheduler, transport, result_consumer=lambda *_: None).run()
                self.assertEqual(("completed", 1, 0), (report.status, report.completed, report.skipped))
                self.assertEqual((1, 1), (transport.hello_requests, transport.process_requests))
                state = database.get_sample_state("job-caption", 1)
                self.assertEqual(("completed", "module1_written"), (state["status"], state["txt_provenance"]))
            finally:
                database.close()

    def test_v8_tag_missing_or_blank_txt_without_fallback_warns_and_fails_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(
                Path(temporary),
                1,
                original_txt_state="missing_or_blank",
                schema_version=8,
                input_txt_mode="tag",
                tagger_fallback_on_missing_txt=False,
            )
            transport = FakeCaptionTransport()
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda *_: self.fail("fallback-off must not call the Tagger"),
                ).run()
                self.assertEqual(("completed_with_issues", 0, 1), (report.status, report.completed, report.failed))
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                issue = database.page_issues("job-caption", limit=10)[0]
                self.assertEqual(
                    ("caption_missing_txt_without_tagger_fallback", "warning", 0, 0, None),
                    (issue["code"], issue["severity"], issue["blocking"], issue["retriable"], issue["repair_start_module"]),
                )
                self.assertIn("new task", issue["message"])
                self.assertEqual("failed", database.get_sample_state("job-caption", 1)["status"])
            finally:
                database.close()

    def test_v4_danbooru_job_preserves_profile_through_hello_item_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch("anima_core.scheduler.require_available"),
                mock.patch("anima_core.scheduler.module_availability", return_value="pending"),
            ):
                database, scheduler = self._database(
                    Path(temporary),
                    1,
                    profile="danbooru",
                    schema_version=4,
                )
                observed: list[tuple[str, str]] = []
                try:
                    report = self._runner(
                        database,
                        scheduler,
                        FakeCaptionTransport(),
                        result_consumer=lambda item, result: observed.append((item.source, result.source)),
                    ).run()
                    self.assertEqual("completed", report.status)
                    self.assertEqual([("danbooru", "danbooru")], observed)
                finally:
                    database.close()

    def test_disabled_caption_sets_bounded_provenance_without_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StateDatabase.open(root / "state.db")
            try:
                database.insert_job(_job("job-caption", root, 1_001))
                rows = _samples(1_001)
                rows[0]["original_txt_state"] = "nonblank"
                database.insert_samples("job-caption", rows)
                scheduler = BoundedScheduler(database)
                self.assertEqual(
                    "skipped",
                    scheduler.start_module("job-caption", "caption", enabled=False, profile="e621"),
                )
                first = database.get_sample_state("job-caption", 1)
                second_page = database.get_sample_state("job-caption", 1_001)
                self.assertEqual(("skipped", "original_preserved"), (first["status"], first["txt_provenance"]))
                self.assertEqual(
                    ("skipped", "missing"),
                    (second_page["status"], second_page["txt_provenance"]),
                )
            finally:
                database.close()

    def test_tagger_vocabulary_must_be_covered_by_the_classification_dictionary(self) -> None:
        """F43: 22 shipped tagger labels, including blush, had no dictionary entry."""
        self.assertEqual(("blush",), missing_dictionary_tags(["blush", "solo"], {"solo": {}}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gap = _install(root / "gap", tag_names=["solo", "blush"], dictionary_tags=["solo"])
            with self.assertRaises(CaptionRunnerFatalError) as caught:
                verify_tagger_dictionary_coverage(gap, CAPTION_MANIFEST, CLASSIFY_MANIFEST)
            self.assertEqual("caption_resource_invalid", caught.exception.code)
            self.assertIn("blush", str(caught.exception))
            covered = _install(root / "ok", tag_names=["solo", "blush"], dictionary_tags=["solo", "blush"])
            verify_tagger_dictionary_coverage(covered, CAPTION_MANIFEST, CLASSIFY_MANIFEST)

            database, scheduler = self._database(root, 2)
            transport = FakeCaptionTransport()
            try:
                with self.assertRaises(CaptionRunnerFatalError) as raised:
                    self._runner(
                        database,
                        scheduler,
                        transport,
                        result_consumer=lambda *_: self.fail("an invalid resource pair must not reach a worker"),
                        install_root=gap,
                    ).run()
                self.assertEqual("caption_resource_invalid", raised.exception.code)
                self.assertEqual(0, transport.hello_requests)
                self.assertEqual("failed", database.get_job("job-caption")["status"])
            finally:
                database.close()

    def test_expired_leases_from_a_dead_worker_are_reclaimed_before_claiming(self) -> None:
        """F16: a stale single-worker lease used to deadlock the caption module."""
        with tempfile.TemporaryDirectory() as temporary:
            database, scheduler = self._database(Path(temporary), 2)
            database.connection.execute(
                """UPDATE sample_state SET status='leased',lease_id='dead-lease',
                   worker_instance_id='caption-worker-dead',lease_expires_at='2020-01-01T00:00:00Z'
                   WHERE job_id='job-caption' AND sample_id=1""",
            )
            database.connection.commit()
            transport = FakeCaptionTransport()
            consumed: list[int] = []
            try:
                report = self._runner(
                    database,
                    scheduler,
                    transport,
                    result_consumer=lambda item, _outcome: consumed.append(item.sampleId),
                ).run()
                self.assertEqual("completed", report.status)
                self.assertEqual([1, 2], sorted(consumed))
                self.assertIsNone(database.get_sample_state("job-caption", 1)["lease_expires_at"])
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
