from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.classify_overlay import ClassifyOverlayWriter
from anima_core.classify_protocol import (
    ClassifyCountDecisionV1,
    ClassifyHelloResultV1,
    ClassifyIssueResultV1,
    ClassifyProjectionV1,
    ClassifyResultV1,
    ClassifyWorkItemV1,
)
from anima_core.classify_runner import ClassifyRunner, stable_classify_issue_id
from anima_core.contracts import JobConfig
from anima_core.db import StateDatabase
from anima_core.overlay import BaselineView, OverlayLayout, WorkingAnnotationView
from anima_core.path_safety import windows_key
from anima_core.raw_e621 import parse_raw_e621_annotation
from anima_core.scheduler import BoundedScheduler
from anima_core.worker_protocol import ProtocolEnvelopeV1


RESOURCE_ROOT = ROOT / "resource-library"
RESOURCE_MANIFEST = r"classification-indexes\e621-classify-20260724-v1\resource.json"
FINGERPRINT = "530323a5d1ca5c3f903c0d57b04d6f1014cdcc0ca01b8de5dc0a41e27e1d2baf"
RESOURCE_ENTRY_COUNT = 120_978
WIKI_DATA_SOURCE_ID = "e621-wiki-count-20260724-v1"


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("/", "\\"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


CLEAN_DECISION = ClassifyCountDecisionV1(
    value="solo", baseValue="solo", selectedSource="wiki_tags", originalRaw=None,
    originalNormalized=None, wikiValue="solo", matchedTags=("solo",), conflict=False,
    issueCodes=(), warnings=(), appliedLowerBounds=(),
)


class FakeClassifyTransport:
    def __init__(self, decision: ClassifyCountDecisionV1 = CLEAN_DECISION) -> None:
        self.hello_requests = 0
        self.process_requests = 0
        self.decision = decision
        self.items: list[ClassifyWorkItemV1] = []

    @staticmethod
    def _response(request: ProtocolEnvelopeV1, method: str, payload: dict[str, object]) -> ProtocolEnvelopeV1:
        return ProtocolEnvelopeV1(
            protocolVersion="1.0", kind="response", messageId=f"response-{request.messageId}",
            runtimeId="classify-e621", owner="classify", method=method, payload=payload,
            replyTo=request.messageId, jobId=request.jobId, configHash=request.configHash,
        )

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if request.method == "hello":
            self.hello_requests += 1
            return self._response(request, "hello", ClassifyHelloResultV1(
                executable=r"C:\Anima\runtimes\classify-e621\python.exe", resourceFingerprint=FINGERPRINT,
            ).to_dict())
        self.process_requests += 1
        raw_items = request.payload.get("items")
        if not isinstance(raw_items, list):
            raise AssertionError("classify batch request has no items")
        items = [ClassifyWorkItemV1.from_dict(raw) for raw in raw_items]
        self.items.extend(items)
        outcomes = [ClassifyResultV1(
            sampleId=item.sampleId, leaseId=item.leaseId, relativeImagePath=item.relativeImagePath,
            projection=ClassifyProjectionV1(
                quality=(), count="solo", character="named_character", series="", artist="",
                appearance=("blue_fur",), tags=("solo",), environment=("forest",), nl="",
            ),
            countDecision=self.decision,
            inputTagCount=4, outputTagCount=4, droppedTagCount=0,
        ).to_dict() for item in items]
        return self._response(request, "result", {
            "schemaVersion": 1,
            "payloadType": "classify_process_result",
            "outcomes": outcomes,
        })


class FakeBatchClassifyTransport(FakeClassifyTransport):
    def __init__(self, *, issue_sample_id: int) -> None:
        super().__init__()
        self.issue_sample_id = issue_sample_id
        self.batch_sizes: list[int] = []

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if request.method == "hello":
            return super().exchange(request)
        self.process_requests += 1
        raw_items = request.payload.get("items")
        if not isinstance(raw_items, list):
            raise AssertionError("classify batch request has no items")
        items = [ClassifyWorkItemV1.from_dict(raw) for raw in raw_items]
        self.batch_sizes.append(len(items))
        self.items.extend(items)
        outcomes: list[dict[str, object]] = []
        for item in items:
            if item.sampleId == self.issue_sample_id:
                outcomes.append(ClassifyIssueResultV1(
                    item.sampleId,
                    item.leaseId,
                    item.relativeImagePath,
                    "classify_no_writable_tags",
                    False,
                    "caption produced no writable classification tag",
                ).to_dict())
                continue
            outcomes.append(ClassifyResultV1(
                sampleId=item.sampleId, leaseId=item.leaseId, relativeImagePath=item.relativeImagePath,
                projection=ClassifyProjectionV1(
                    quality=(), count="solo", character="named_character", series="", artist="",
                    appearance=("blue_fur",), tags=("solo",), environment=("forest",), nl="",
                ),
                countDecision=self.decision,
                inputTagCount=4, outputTagCount=4, droppedTagCount=0,
            ).to_dict())
        return self._response(request, "result", {
            "schemaVersion": 1,
            "payloadType": "classify_process_result",
            "outcomes": list(reversed(outcomes)),
        })


class ClassifyRecoveryFixture:
    def __init__(
        self,
        root: Path,
        *,
        input_txt_mode: str | None = None,
        baseline_txt: bytes | None = b"solo, blue eyes",
        baseline_json: bytes | None = None,
        overlay_txt: bytes | None = None,
    ) -> None:
        self.dataset = root / "dataset"
        self.dataset.mkdir()
        (self.dataset / "sample.png").write_bytes(b"immutable-image")
        if baseline_txt is not None:
            (self.dataset / "sample.txt").write_bytes(baseline_txt)
        if baseline_json is not None:
            (self.dataset / "sample.json").write_bytes(baseline_json)
        self.layout = OverlayLayout.create(self.dataset, "job-classify-recovery")
        if overlay_txt is not None:
            self.layout.write_annotation("sample", ".txt", overlay_txt)
        self.database = StateDatabase.open(root / "state.db")
        self.config = JobConfig(
            workMode="in_place",
            overwriteMode="incremental",
            sourceRoot=str(self.dataset),
        )
        self.config.caption["enabled"] = False
        if input_txt_mode is not None:
            self.config.caption["inputTxtMode"] = input_txt_mode
        self.config.classify.update({
            "wikiDataSourceId": WIKI_DATA_SOURCE_ID,
            "dictionaryEntryCount": RESOURCE_ENTRY_COUNT,
            "resourceProfile": "e621",
            "resourceManifestRelativePath": RESOURCE_MANIFEST,
            "resourceFingerprint": FINGERPRINT,
        })
        self.database.insert_job({
            "job_id": "job-classify-recovery", "config_schema_version": self.config.schemaVersion,
            "config_json": json.dumps(self.config.to_dict()),
            "config_hash": self.config.config_hash, "profile": "e621", "work_mode": "in_place",
            "overwrite_mode": "incremental", "source_root": str(self.dataset), "output_root": None,
            "dataset_root": str(self.dataset), "dataset_root_key": windows_key(self.dataset), "manifest_schema_version": 1,
            "recursive": 0, "sample_count": 1, "manifest_generated_at": "2026-07-24T00:00:00Z",
            "status": "ready", "current_module_id": None, "last_event_id": 0, "pinned": 0,
            "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": str(self.layout.root),
            "commit_journal_path": None, "resume_status": None, "created_at": "2026-07-24T00:00:00Z",
            "started_at": None, "cancel_requested_at": None, "finished_at": None,
        })
        self.database.insert_samples("job-classify-recovery", [{
            "sample_id": 1, "relative_image_path": "sample.png", "annotation_key": "sample", "source": "e621",
            "in_processing_scope": True, "image_format": "png", "image_frame_count": 1,
            "original_txt_state": "nonblank" if baseline_txt and baseline_txt.strip() else "missing_or_blank",
            "original_json_state": "nonblank" if baseline_json else "missing_or_blank", "image_file_id": "volume:1",
            "image_size": len(b"immutable-image"), "image_mtime_ns": 1_000_000,
        }])
        self.scheduler = BoundedScheduler(self.database, lease_id_factory=lambda: "lease-classify-recovery")
        self.scheduler.start_module("job-classify-recovery", "caption", enabled=False, profile="e621")
        self.scheduler.start_module("job-classify-recovery", "classify", enabled=True, profile="e621")
        self.lease = self.scheduler.claim_batch(
            "job-classify-recovery", "classify", "classify-worker-1", self.config.config_hash, limit=1,
        )[0]
        self.item = ClassifyWorkItemV1(
            sampleId=1, leaseId=str(self.lease.leaseId), relativeImagePath="sample.png", annotationKey="sample",
            txtText="solo, blue eyes", txtProvenance="original_preserved", originalCount=None,
        )
        self.writer = ClassifyOverlayWriter(self.database, self.layout, "job-classify-recovery")

    def write_prepared(self) -> tuple[str, str]:
        prepared, digest = self.layout.write_prepared("classify", self.item.leaseId, ".json", b'{"count":"solo"}\n')
        return os.path.relpath(prepared, self.layout.root).replace("/", "\\"), digest

    def recover(self):
        self.database.mark_interrupted("job-classify-recovery")
        return self.scheduler.recover(
            "job-classify-recovery", confirmed=True, expected_config_hash=self.config.config_hash,
            manifest_schema_version=1, protocol_version="1.0", verify_source_fingerprints=lambda: True,
            commit_prepared=self.writer.recover_prepared,
        )

    def close(self) -> None:
        self.database.close()
        if self.layout.root.exists():
            self.layout.discard()


class ClassifyRunnerTests(unittest.TestCase):
    @staticmethod
    def _runner(fixture: ClassifyRecoveryFixture, transport: FakeClassifyTransport) -> ClassifyRunner:
        return ClassifyRunner(
            fixture.database,
            fixture.scheduler,
            transport,
            WorkingAnnotationView(BaselineView(fixture.dataset), fixture.layout),
            fixture.writer,
            job_id="job-classify-recovery",
            worker_instance_id="classify-worker-1",
            install_root=RESOURCE_ROOT,
            resource_manifest_relative_path=RESOURCE_MANIFEST,
            resource_fingerprint=FINGERPRINT,
        )

    def test_v10_runner_sends_the_frozen_batch_and_settles_shuffled_item_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            layout = OverlayLayout.create(dataset, "job-classify-batch")
            database = StateDatabase.open(root / "state.db")
            try:
                config = JobConfig(
                    workMode="in_place",
                    overwriteMode="incremental",
                    sourceRoot=str(dataset),
                )
                config.caption["enabled"] = False
                config.moduleBatchSize["classify"] = 3
                config.classify.update({
                    "wikiDataSourceId": WIKI_DATA_SOURCE_ID,
                    "dictionaryEntryCount": RESOURCE_ENTRY_COUNT,
                    "resourceProfile": "e621",
                    "resourceManifestRelativePath": RESOURCE_MANIFEST,
                    "resourceFingerprint": FINGERPRINT,
                })
                database.insert_job({
                    "job_id": "job-classify-batch", "config_schema_version": 10,
                    "config_json": json.dumps(config.to_dict()), "config_hash": config.config_hash,
                    "profile": "e621", "work_mode": "in_place", "overwrite_mode": "incremental",
                    "source_root": str(dataset), "output_root": None, "dataset_root": str(dataset),
                    "dataset_root_key": windows_key(dataset), "manifest_schema_version": 1,
                    "recursive": 0, "sample_count": 3, "manifest_generated_at": "2026-08-27T00:00:00Z",
                    "status": "ready", "current_module_id": None, "last_event_id": 0, "pinned": 0,
                    "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": str(layout.root),
                    "commit_journal_path": None, "resume_status": None, "created_at": "2026-08-27T00:00:00Z",
                    "started_at": None, "cancel_requested_at": None, "finished_at": None,
                })
                samples: list[dict[str, object]] = []
                for sample_id in range(1, 4):
                    annotation_key = f"sample-{sample_id}"
                    (dataset / f"{annotation_key}.png").write_bytes(b"immutable-image")
                    (dataset / f"{annotation_key}.txt").write_text("solo, blue eyes", encoding="utf-8")
                    samples.append({
                        "sample_id": sample_id,
                        "relative_image_path": f"{annotation_key}.png",
                        "annotation_key": annotation_key,
                        "source": "e621",
                        "in_processing_scope": True,
                        "image_format": "png",
                        "image_frame_count": 1,
                        "original_txt_state": "nonblank",
                        "original_json_state": "missing_or_blank",
                        "image_file_id": f"volume:{sample_id}",
                        "image_size": len(b"immutable-image"),
                        "image_mtime_ns": 1_000_000,
                    })
                database.insert_samples("job-classify-batch", samples)
                lease_ids = iter(("lease-batch-1", "lease-batch-2", "lease-batch-3"))
                scheduler = BoundedScheduler(database, lease_id_factory=lease_ids.__next__)
                scheduler.start_module("job-classify-batch", "caption", enabled=False, profile="e621")
                scheduler.start_module("job-classify-batch", "classify", enabled=True, profile="e621")
                transport = FakeBatchClassifyTransport(issue_sample_id=2)

                report = ClassifyRunner(
                    database,
                    scheduler,
                    transport,
                    WorkingAnnotationView(BaselineView(dataset), layout),
                    ClassifyOverlayWriter(database, layout, "job-classify-batch"),
                    job_id="job-classify-batch",
                    worker_instance_id="classify-worker-batch",
                    install_root=RESOURCE_ROOT,
                    resource_manifest_relative_path=RESOURCE_MANIFEST,
                    resource_fingerprint=FINGERPRINT,
                ).run()

                self.assertEqual(
                    ("completed_with_issues", 2, 1, 1, 1),
                    (report.status, report.completed, report.failed, report.helloRequests, report.processRequests),
                )
                self.assertEqual([3], transport.batch_sizes)
                self.assertEqual([1, 2, 3], [item.sampleId for item in transport.items])
                self.assertEqual("failed", database.get_sample_state("job-classify-batch", 2)["status"])
                self.assertEqual(0, database.count_in_flight("job-classify-batch"))
            finally:
                database.close()
                if layout.root.exists():
                    layout.discard()

    def test_raw_e621_json_reuses_worker_and_preserves_artist_and_character(self) -> None:
        raw = parse_raw_e621_annotation(
            b'{"artist":["kannos"],"character":["raw_character"],"contributor":["ignored"],"copyright":[],"general":["solo","blue_fur"],"invalid":["bad"],"lore":["lore_tag"],"meta":[],"species":[]}'
        )
        assert raw is not None
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClassifyRecoveryFixture(Path(temporary))
            fixture.scheduler.release_unstarted(fixture.lease)
            transport = FakeClassifyTransport()
            try:
                report = ClassifyRunner(
                    fixture.database,
                    fixture.scheduler,
                    transport,
                    WorkingAnnotationView(BaselineView(fixture.dataset), fixture.layout),
                    fixture.writer,
                    job_id="job-classify-recovery",
                    worker_instance_id="classify-worker-1",
                    install_root=RESOURCE_ROOT,
                    resource_manifest_relative_path=RESOURCE_MANIFEST,
                    resource_fingerprint=FINGERPRINT,
                    raw_e621_reader=lambda _: raw,
                ).run()
                output = json.loads(fixture.layout.annotation_path("sample", ".json").read_text(encoding="utf-8"))
                self.assertEqual(("completed", 1), (report.status, transport.process_requests))
                self.assertEqual("solo, blue_fur, raw_character", transport.items[0].txtText)
                self.assertEqual("kannos", output["artist"])
                self.assertEqual("raw_character", output["character"])
                self.assertEqual(("blue_fur",), tuple(output["appearance"]))
                self.assertEqual(("solo",), tuple(output["tags"]))
                self.assertEqual("solo", output["count"])
            finally:
                fixture.close()

    def test_worker_issue_is_mapped_to_a_stable_current_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClassifyRecoveryFixture(Path(temporary))
            try:
                runner = ClassifyRunner(
                    fixture.database, fixture.scheduler, FakeClassifyTransport(),
                    WorkingAnnotationView(BaselineView(fixture.dataset), fixture.layout), fixture.writer,
                    job_id="job-classify-recovery", worker_instance_id="classify-worker-1",
                    install_root=RESOURCE_ROOT, resource_manifest_relative_path=RESOURCE_MANIFEST,
                    resource_fingerprint=FINGERPRINT,
                )
                row = fixture.database.get_leased_sample(
                    "job-classify-recovery", "classify", 1, lease_id=str(fixture.lease.leaseId),
                    worker_instance_id="classify-worker-1",
                )
                runner._issue(  # The worker's validated issue outcome reaches this single core mapping point.
                    fixture.lease, row, "count_sheet_multi_conflict", "role sheet conflicts with duo", retriable=False,
                )
                self.assertEqual("failed", fixture.database.get_sample_state("job-classify-recovery", 1)["status"])
                issue = fixture.database.page_issues("job-classify-recovery", limit=10)[0]
                self.assertEqual(
                    (stable_classify_issue_id("job-classify-recovery", 1, "count_sheet_multi_conflict"), 0, None),
                    (issue["issue_id"], issue["retriable"], issue["repair_start_module"]),
                )
            finally:
                fixture.close()

    def test_count_diagnostics_are_persisted_for_completed_samples(self) -> None:
        # F31: the worker's count findings used to vanish; only write + complete ran.
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClassifyRecoveryFixture(Path(temporary))
            try:
                fixture.scheduler.release_unstarted(fixture.lease)
                transport = FakeClassifyTransport(ClassifyCountDecisionV1(
                    value="solo", baseValue="", selectedSource="none", originalRaw=None,
                    originalNormalized=None, wikiValue=None, matchedTags=(), conflict=False,
                    issueCodes=("count_source_conflict", "count_character_lower_bound"),
                    warnings=("count_source_conflict", "wiki_missing:e621:duo"),
                    appliedLowerBounds=("character",),
                ))
                report = ClassifyRunner(
                    fixture.database, fixture.scheduler, transport,
                    WorkingAnnotationView(BaselineView(fixture.dataset), fixture.layout), fixture.writer,
                    job_id="job-classify-recovery", worker_instance_id="classify-worker-1",
                    install_root=RESOURCE_ROOT,
                    resource_manifest_relative_path=RESOURCE_MANIFEST,
                    resource_fingerprint=FINGERPRINT,
                ).run()
                self.assertEqual(("completed", 1), (report.status, report.completed))
                self.assertEqual(
                    {"count_source_conflict": 1, "count_character_lower_bound": 1, "wiki_missing": 1},
                    {
                        code: fixture.database.module_diagnostic_count("job-classify-recovery", "classify", code)
                        for code in ("count_source_conflict", "count_character_lower_bound", "wiki_missing")
                    },
                )
                evidence = fixture.database.get_count_evidence("job-classify-recovery", 1)
                self.assertEqual("solo", evidence["value"])
                self.assertEqual(
                    ["count_source_conflict", "count_character_lower_bound", "wiki_missing"],
                    json.loads(evidence["review_warning_codes_json"]),
                )
            finally:
                fixture.close()

    def test_v10_nl_uses_baseline_txt_without_replacing_tagger_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClassifyRecoveryFixture(
                Path(temporary),
                input_txt_mode="nl",
                baseline_txt=b"baseline natural-language caption",
                baseline_json=b'{"nl":"old NL"}',
                overlay_txt=b"generated, tag",
            )
            fixture.scheduler.release_unstarted(fixture.lease)
            transport = FakeClassifyTransport()
            try:
                report = self._runner(fixture, transport).run()
                committed = json.loads(fixture.layout.annotation_path("sample", ".json").read_text(encoding="utf-8"))
                self.assertEqual(("completed", 1, 1), (report.status, transport.hello_requests, transport.process_requests))
                self.assertEqual("generated, tag", transport.items[0].txtText)
                self.assertEqual("baseline natural-language caption", committed["nl"])
            finally:
                fixture.close()

    def test_v10_nl_missing_or_blank_baseline_txt_writes_an_empty_nl(self) -> None:
        for baseline_txt in (None, b" \t\r\n"):
            with self.subTest(baseline_txt=baseline_txt), tempfile.TemporaryDirectory() as temporary:
                fixture = ClassifyRecoveryFixture(
                    Path(temporary),
                    input_txt_mode="nl",
                    baseline_txt=baseline_txt,
                    baseline_json=b'{"nl":"old NL"}',
                    overlay_txt=b"generated, tag",
                )
                fixture.scheduler.release_unstarted(fixture.lease)
                transport = FakeClassifyTransport()
                try:
                    report = self._runner(fixture, transport).run()
                    committed = json.loads(fixture.layout.annotation_path("sample", ".json").read_text(encoding="utf-8"))
                    self.assertEqual(("completed", 1), (report.status, transport.process_requests))
                    self.assertEqual("generated, tag", transport.items[0].txtText)
                    self.assertEqual("", committed["nl"])
                finally:
                    fixture.close()

    def test_v10_nl_rejects_invalid_baseline_txt_before_worker_process(self) -> None:
        for label, baseline_txt in (
            ("invalid-utf8", b"\xff"),
            ("nul", b"caption\x00text"),
            ("too-large", b"x" * 16_385),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = ClassifyRecoveryFixture(
                    Path(temporary),
                    input_txt_mode="nl",
                    baseline_txt=baseline_txt,
                    overlay_txt=b"generated, tag",
                )
                fixture.scheduler.release_unstarted(fixture.lease)
                transport = FakeClassifyTransport()
                try:
                    report = self._runner(fixture, transport).run()
                    issue = fixture.database.page_issues("job-classify-recovery", limit=10)[0]
                    self.assertEqual(("completed_with_issues", 0, 0), (
                        report.status, transport.hello_requests, transport.process_requests,
                    ))
                    self.assertEqual(("classify_input_invalid", "error", 1), (
                        issue["code"], issue["severity"], issue["blocking"],
                    ))
                    self.assertFalse(fixture.layout.annotation_path("sample", ".json").exists())
                finally:
                    fixture.close()

    def test_recovery_handles_all_classify_prepared_crash_windows(self) -> None:
        for window in ("orphan_before_state", "prepared_before_move", "target_before_complete", "invalid_prepared"):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as temporary:
                fixture = ClassifyRecoveryFixture(Path(temporary))
                before = _hashes(fixture.dataset)
                try:
                    relative, digest = fixture.write_prepared()
                    if window != "orphan_before_state":
                        fixture.database.stage_classify_prepared_artifact(
                            "job-classify-recovery", 1, lease_id=fixture.item.leaseId,
                            relative_path=relative, sha256=digest,
                            count_decision=CLEAN_DECISION.to_dict(),
                        )
                    if window == "target_before_complete":
                        fixture.layout.commit_prepared(relative, digest, "sample", ".json")
                    if window == "invalid_prepared":
                        fixture.layout.resolve_prepared(relative).write_bytes(b"tampered")
                    report = fixture.recover()
                    state = fixture.database.get_sample_state("job-classify-recovery", 1)
                    if window == "orphan_before_state":
                        self.assertEqual((1, 0, 0, "pending"), (
                            report.returnedLeases, report.committedPrepared, report.repeatedPrepared, state["status"],
                        ))
                        self.assertFalse(fixture.layout.annotation_path("sample", ".json").exists())
                    elif window == "invalid_prepared":
                        self.assertEqual((0, 0, 1, "pending"), (
                            report.returnedLeases, report.committedPrepared, report.repeatedPrepared, state["status"],
                        ))
                        self.assertFalse(fixture.layout.annotation_path("sample", ".json").exists())
                    else:
                        self.assertEqual((0, 1, 0, "completed"), (
                            report.returnedLeases, report.committedPrepared, report.repeatedPrepared, state["status"],
                        ))
                        self.assertEqual(b'{"count":"solo"}\n', fixture.layout.annotation_path("sample", ".json").read_bytes())
                    self.assertEqual(before, _hashes(fixture.dataset))
                finally:
                    fixture.close()

    def test_count_evidence_retry_is_idempotent_and_rolls_back_prepared_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClassifyRecoveryFixture(Path(temporary))
            try:
                relative, digest = fixture.write_prepared()
                conflicting = ClassifyCountDecisionV1(
                    value="duo", baseValue="duo", selectedSource="wiki_tags", originalRaw=None,
                    originalNormalized=None, wikiValue="duo", matchedTags=("duo",), conflict=False,
                    issueCodes=(), warnings=(), appliedLowerBounds=(),
                )
                fixture.database.connection.execute(
                    """INSERT INTO count_evidence(
                           job_id,sample_id,schema_version,value,decision_json,
                           review_warning_codes_json,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        "job-classify-recovery", 1, 1, "duo",
                        json.dumps(conflicting.to_dict(), sort_keys=True, separators=(",", ":")),
                        "[]", "2026-07-26T00:00:00Z", "2026-07-26T00:00:00Z",
                    ),
                )
                with self.assertRaisesRegex(ValueError, "does not match persisted evidence"):
                    fixture.database.stage_classify_prepared_artifact(
                        "job-classify-recovery", 1, lease_id=fixture.item.leaseId,
                        relative_path=relative, sha256=digest, count_decision=CLEAN_DECISION.to_dict(),
                    )
                self.assertEqual("leased", fixture.database.get_sample_state("job-classify-recovery", 1)["status"])

                fixture.database.connection.execute(
                    "DELETE FROM count_evidence WHERE job_id=? AND sample_id=?",
                    ("job-classify-recovery", 1),
                )
                for _ in range(2):
                    fixture.database.stage_classify_prepared_artifact(
                        "job-classify-recovery", 1, lease_id=fixture.item.leaseId,
                        relative_path=relative, sha256=digest, count_decision=CLEAN_DECISION.to_dict(),
                    )
                self.assertEqual("prepared", fixture.database.get_sample_state("job-classify-recovery", 1)["status"])
                self.assertEqual(1, len(fixture.database.page_count_evidence("job-classify-recovery", limit=500)))
                with self.assertRaises(ValueError):
                    fixture.database.page_count_evidence("job-classify-recovery", limit=501)
            finally:
                fixture.close()

    def test_runner_writes_only_overlay_and_finishes_the_bounded_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            (dataset / "sample.png").write_bytes(b"immutable-image")
            (dataset / "sample.txt").write_text("solo, blue eyes", encoding="utf-8")
            (dataset / "sample.json").write_text('{"count":"duo","nl":"preserve"}', encoding="utf-8")
            before = _hashes(dataset)
            layout = OverlayLayout.create(dataset, "job-classify")
            database = StateDatabase.open(root / "state.db")
            try:
                config = JobConfig(workMode="in_place", overwriteMode="incremental", sourceRoot=str(dataset))
                config.caption["enabled"] = False
                config.classify.update({
                    "wikiDataSourceId": WIKI_DATA_SOURCE_ID,
                    "dictionaryEntryCount": RESOURCE_ENTRY_COUNT,
                    "resourceProfile": "e621",
                    "resourceManifestRelativePath": RESOURCE_MANIFEST,
                    "resourceFingerprint": FINGERPRINT,
                })
                database.insert_job({
                    "job_id": "job-classify", "config_schema_version": config.schemaVersion,
                    "config_json": json.dumps(config.to_dict()),
                    "config_hash": config.config_hash, "profile": "e621", "work_mode": "in_place",
                    "overwrite_mode": "incremental", "source_root": str(dataset), "output_root": None,
                    "dataset_root": str(dataset), "dataset_root_key": windows_key(dataset), "manifest_schema_version": 1,
                    "recursive": 0, "sample_count": 1, "manifest_generated_at": "2026-07-24T00:00:00Z",
                    "status": "ready", "current_module_id": None, "last_event_id": 0, "pinned": 0,
                    "api_budget_extra": 0, "api_budget_revision": 0, "overlay_root": str(layout.root),
                    "commit_journal_path": None, "resume_status": None, "created_at": "2026-07-24T00:00:00Z",
                    "started_at": None, "cancel_requested_at": None, "finished_at": None,
                })
                database.insert_samples("job-classify", [{
                    "sample_id": 1, "relative_image_path": "sample.png", "annotation_key": "sample", "source": "e621",
                    "in_processing_scope": True, "image_format": "png", "image_frame_count": 1,
                    "original_txt_state": "nonblank", "original_json_state": "nonblank", "image_file_id": "volume:1",
                    "image_size": len(b"immutable-image"), "image_mtime_ns": 1_000_000,
                }])
                scheduler = BoundedScheduler(database, lease_id_factory=lambda: "lease-classify-1")
                scheduler.start_module("job-classify", "caption", enabled=False, profile="e621")
                scheduler.start_module("job-classify", "classify", enabled=True, profile="e621")
                transport = FakeClassifyTransport()
                report = ClassifyRunner(
                    database, scheduler, transport, WorkingAnnotationView(BaselineView(dataset), layout),
                    ClassifyOverlayWriter(database, layout, "job-classify"), job_id="job-classify",
                    worker_instance_id="classify-worker-1", install_root=RESOURCE_ROOT,
                    resource_manifest_relative_path=RESOURCE_MANIFEST,
                    resource_fingerprint=FINGERPRINT, message_id_factory=iter(("hello-1", "process-1")).__next__,
                ).run()
                self.assertEqual(("completed", 1, 0, 1, 1), (
                    report.status, report.completed, report.failed, transport.hello_requests, transport.process_requests,
                ))
                self.assertEqual(before, _hashes(dataset))
                self.assertEqual(
                    {"count": "solo", "nl": "preserve"},
                    json.loads(layout.annotation_path("sample", ".json").read_text(encoding="utf-8")),
                )
                self.assertEqual("completed", database.get_sample_state("job-classify", 1)["status"])
                self.assertEqual("solo", database.get_count_evidence("job-classify", 1)["value"])
            finally:
                database.close()
                if layout.root.exists():
                    layout.discard()


if __name__ == "__main__":
    unittest.main()
