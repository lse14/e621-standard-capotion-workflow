from __future__ import annotations

import hashlib
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.contracts import JobConfig, SampleRunState
from anima_core.custom_replace_index import CustomReplaceIndexError, inspect_custom_replace_index
from anima_core.db import StateDatabase
from anima_core.overlay import BaselineView, OverlayLayout, WorkingAnnotationView
from anima_core.path_safety import windows_key
from anima_core.replace_overlay import ReplaceOverlayWriter
from anima_core.replace_provenance import (
    ReplaceProvenanceChange,
    apply_provenance_changes,
    provenance_database_path,
)
from anima_core.replace_runner import ReplaceRunner, ReplaceRunnerError
from anima_core.scheduler import BoundedScheduler
from anima_core.worker_protocol import ProtocolEnvelopeV1


RESOURCE_ROOT = ROOT / "resource-library"
RESOURCE_MANIFEST = r"replacement-indexes\e621-replace-20260726-v2\resource.json"
FINGERPRINT = "3cabbeeffd379a893a0b53d427c3dbb26ea6c587f474ae761b21afde4ee4c47b"


def _projection(tag: str = "!") -> dict[str, object]:
    return {
        "quality": [], "count": "solo", "character": "", "series": "", "artist": "",
        "appearance": [], "tags": [tag], "environment": [], "nl": "",
    }


class ReplaceTransport:
    def __init__(self, database: StateDatabase | None = None, *, cancel_after_first: bool = False, fingerprint: str = FINGERPRINT, rule_count: int = 86_922, keep_non_canonical: int = 0, direction_conflicts: int = 0) -> None:
        self.database = database
        self.cancel_after_first = cancel_after_first
        self.fingerprint = fingerprint
        self.rule_count = rule_count
        self.keep_non_canonical = keep_non_canonical
        self.direction_conflicts = direction_conflicts
        self.hello_requests = 0
        self.process_requests = 0
        self.batch_sizes: list[int] = []

    @staticmethod
    def _response(request: ProtocolEnvelopeV1, method: str, payload: dict[str, object]) -> ProtocolEnvelopeV1:
        return ProtocolEnvelopeV1(
            protocolVersion="1.0", kind="response", messageId=f"reply-{request.messageId}", runtimeId="replace-e621",
            owner="replace", method=method, payload=payload, replyTo=request.messageId,
            jobId=request.jobId, configHash=request.configHash,
        )

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if request.method == "hello":
            self.hello_requests += 1
            return self._response(request, "hello", {
                "schemaVersion": 1, "payloadType": "replace_hello_result", "ready": True,
                "indexLoads": 1, "ruleCount": self.rule_count, "resourceFingerprint": self.fingerprint,
                "keepNonCanonical": self.keep_non_canonical, "canonicalDirectionConflict": self.direction_conflicts,
            })
        self.process_requests += 1
        items = request.payload["items"]
        assert isinstance(items, list)
        self.batch_sizes.append(len(items))
        if self.cancel_after_first and self.process_requests == 1 and self.database is not None:
            self.database.begin_cancellation(str(request.jobId))
        return self._response(request, "result", {
            "schemaVersion": 1, "payloadType": "replace_process_result", "outcomes": [{
                "schemaVersion": 1, "payloadType": "replace_result", "sampleId": item["sampleId"],
                "leaseId": item["leaseId"], "source": "e621", "relativeImagePath": item["relativeImagePath"],
                "projection": _projection("exclamation_point"), "replaced": 1, "dropped": 0, "passthrough": 1,
                "keepRewritten": 2,
            } for item in items],
        })


class BatchReplaceTransport(ReplaceTransport):
    def __init__(self) -> None:
        super().__init__()
        self.batch_sizes: list[int] = []

    def exchange(self, request: ProtocolEnvelopeV1) -> ProtocolEnvelopeV1:
        if request.method == "hello":
            return super().exchange(request)
        self.process_requests += 1
        items = request.payload["items"]
        assert isinstance(items, list)
        self.batch_sizes.append(len(items))
        outcomes: list[dict[str, object]] = []
        for item in reversed(items):
            assert isinstance(item, dict)
            if item["sampleId"] == 2:
                outcomes.append({
                    "schemaVersion": 1, "payloadType": "replace_issue", "sampleId": item["sampleId"],
                    "leaseId": item["leaseId"], "source": "e621", "relativeImagePath": item["relativeImagePath"],
                    "code": "replace_json_invalid", "severity": "error", "blocking": True, "retriable": False,
                    "message": "projection rejected",
                })
            else:
                outcomes.append({
                    "schemaVersion": 1, "payloadType": "replace_result", "sampleId": item["sampleId"],
                    "leaseId": item["leaseId"], "source": "e621", "relativeImagePath": item["relativeImagePath"],
                    "projection": _projection("exclamation_point"), "replaced": 1, "dropped": 0,
                    "passthrough": 1, "keepRewritten": 2,
                })
        return self._response(request, "result", {
            "schemaVersion": 1, "payloadType": "replace_process_result", "outcomes": outcomes,
        })


class ReplaceRunnerFixture:
    def __init__(self, root: Path, count: int, *, valid_json: bool = True) -> None:
        self.dataset = root / "dataset"
        self.dataset.mkdir()
        self.layout = OverlayLayout.create(self.dataset, "job-replace-runner")
        self.database = StateDatabase.open(root / "state.db")
        self.config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(self.dataset))
        self.config.caption["enabled"] = False
        self.config.classify["enabled"] = False
        self.config.countReview["enabled"] = False  # type: ignore[index]
        self.database.insert_job({
            "job_id": "job-replace-runner", "config_schema_version": self.config.schemaVersion, "config_json": json.dumps(self.config.to_dict()),
            "config_hash": self.config.config_hash, "profile": "e621", "work_mode": "in_place", "overwrite_mode": "incremental",
            "source_root": str(self.dataset), "output_root": None, "dataset_root": str(self.dataset), "dataset_root_key": windows_key(self.dataset),
            "manifest_schema_version": 1, "recursive": 0, "sample_count": count, "manifest_generated_at": "2026-07-24T00:00:00Z",
            "status": "ready", "current_module_id": None, "last_event_id": 0, "pinned": 0, "api_budget_extra": 0, "api_budget_revision": 0,
            "overlay_root": str(self.layout.root), "commit_journal_path": None, "resume_status": None, "created_at": "2026-07-24T00:00:00Z",
            "started_at": None, "cancel_requested_at": None, "finished_at": None,
        })
        records = []
        for sample_id in range(1, count + 1):
            key = f"sample-{sample_id:04d}"
            (self.dataset / f"{key}.png").write_bytes(b"immutable-image")
            (self.dataset / f"{key}.json").write_text(json.dumps(_projection()) if valid_json else "{bad", encoding="utf-8")
            records.append({
                "sample_id": sample_id, "relative_image_path": f"{key}.png", "annotation_key": key, "source": "e621",
                "in_processing_scope": True, "image_format": "png", "image_frame_count": 1, "original_txt_state": "missing_or_blank",
                "original_json_state": "nonblank", "image_file_id": f"volume:{sample_id}", "image_size": 15, "image_mtime_ns": sample_id,
            })
        self.database.insert_samples("job-replace-runner", records)
        lease_sequence = itertools.count(1)
        self.scheduler = BoundedScheduler(self.database, lease_id_factory=lambda: f"lease-{next(lease_sequence)}")
        self.scheduler.start_module("job-replace-runner", "caption", enabled=False, profile="e621")
        self.scheduler.start_module("job-replace-runner", "classify", enabled=False, profile="e621")
        self.scheduler.start_module("job-replace-runner", "replace", enabled=True, profile="e621")

    def runner(self, transport: ReplaceTransport, *, fingerprint: str = FINGERPRINT, custom: tuple[str, str, int] | None = None) -> ReplaceRunner:
        args: dict[str, object] = {"resource_manifest_relative_path": RESOURCE_MANIFEST, "resource_fingerprint": fingerprint}
        if custom is not None:
            path, digest, count = custom
            args = {"custom_index_path": path, "custom_index_overlay_root": str(self.layout.root), "custom_index_sha256": digest, "custom_index_rule_count": count}
        return ReplaceRunner(
            self.database, self.scheduler, transport, WorkingAnnotationView(BaselineView(self.dataset), self.layout),
            ReplaceOverlayWriter(self.database, self.layout, "job-replace-runner"), job_id="job-replace-runner",
            worker_instance_id="replace-worker-1", install_root=str(RESOURCE_ROOT),
            **args,
        )

    def close(self) -> None:
        self.database.close()
        if self.layout.root.exists():
            self.layout.discard()


class ReplaceRunnerTests(unittest.TestCase):
    def test_v10_runner_sends_one_batch_and_settles_shuffled_item_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 3)
            try:
                fixture.config.moduleBatchSize["replace"] = 3
                fixture.database.connection.execute(
                    "UPDATE jobs SET config_json=?, config_hash=? WHERE job_id=?",
                    (json.dumps(fixture.config.to_dict()), fixture.config.config_hash, "job-replace-runner"),
                )
                transport = BatchReplaceTransport()

                self.assertEqual("completed_with_issues", fixture.runner(transport).run())
                self.assertEqual([3], transport.batch_sizes)
                self.assertEqual("failed", fixture.database.get_sample_state("job-replace-runner", 2)["status"])
                self.assertEqual("completed", fixture.database.get_sample_state("job-replace-runner", 3)["status"])
                self.assertEqual(0, fixture.database.count_in_flight("job-replace-runner"))
            finally:
                fixture.close()

    def test_runner_processes_500_leases_with_overlay_only_and_aggregate_info(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 500)
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.dataset.glob("*")}
            try:
                transport = ReplaceTransport()
                self.assertEqual("completed", fixture.runner(transport).run())
                self.assertEqual((1, 4), (transport.hello_requests, transport.process_requests))
                self.assertEqual([128, 128, 128, 116], transport.batch_sizes)
                self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.dataset.glob("*")})
                self.assertEqual("completed", fixture.database.get_sample_state("job-replace-runner", 500)["status"])
                self.assertEqual("exclamation_point", json.loads(fixture.layout.annotation_path("sample-0500", ".json").read_text(encoding="utf-8"))["tags"][0])
                # F34: replaced / dropped / keep-rewritten used to be dropped on the floor.
                self.assertEqual(
                    {"replace_keep_rewritten": 1_000, "replace_passthrough": 500, "replace_replaced": 500},
                    {row["code"]: row["count"] for row in fixture.database.module_diagnostics("job-replace-runner", "replace")},
                )
                self.assertEqual([], fixture.database.page_issues("job-replace-runner", limit=10))
            finally:
                fixture.close()

    def test_invalid_json_is_a_per_sample_issue_without_starting_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1, valid_json=False)
            try:
                transport = ReplaceTransport()
                self.assertEqual("completed_with_issues", fixture.runner(transport).run())
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                issue = fixture.database.page_issues("job-replace-runner", limit=10)[0]
                self.assertEqual(("replace_json_invalid", 0), (issue["code"], issue["retriable"]))
            finally:
                fixture.close()

    def test_cancellation_releases_unstarted_leases_without_more_worker_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 3)
            try:
                fixture.config.moduleBatchSize["replace"] = 1
                fixture.database.connection.execute(
                    "UPDATE jobs SET config_json=?, config_hash=? WHERE job_id=?",
                    (json.dumps(fixture.config.to_dict()), fixture.config.config_hash, "job-replace-runner"),
                )
                transport = ReplaceTransport(fixture.database, cancel_after_first=True)
                self.assertEqual("cancelling", fixture.runner(transport).run())
                self.assertEqual(1, transport.process_requests)
                self.assertEqual("pending", fixture.database.get_sample_state("job-replace-runner", 2)["status"])
            finally:
                fixture.close()

    def test_resource_manifest_mismatch_blocks_before_worker_hello(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1)
            try:
                transport = ReplaceTransport()
                with self.assertRaisesRegex(ReplaceRunnerError, "fingerprint"):
                    fixture.runner(transport, fingerprint="0" * 64).run()
                self.assertEqual(0, transport.hello_requests)
            finally:
                fixture.close()

    def test_custom_frozen_index_is_sent_to_worker_instead_of_the_bundled_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1)
            try:
                data = b"source_tag,canonical_e621_tag,action,replacement_tags\nold,,replace,new\n"
                frozen = fixture.layout.write_resource("replace\\custom-index.csv", data)
                digest = hashlib.sha256(data).hexdigest()
                fixture.config.replace.clear()
                fixture.config.replace.update({"enabled": True, "indexMode": "custom", "customIndexPath": str(Path(temporary) / "source.csv"), "customIndexSha256": digest, "customIndexRuleCount": 1})
                fixture.database.connection.execute("UPDATE jobs SET config_json=?,config_hash=? WHERE job_id=?", (json.dumps(fixture.config.to_dict()), fixture.config.config_hash, "job-replace-runner"))
                transport = ReplaceTransport(fingerprint=digest, rule_count=1)
                self.assertEqual("completed", fixture.runner(transport, custom=(str(frozen), digest, 1)).run())
                self.assertEqual((1, 1), (transport.hello_requests, transport.process_requests))
            finally:
                fixture.close()

    def test_index_audit_counts_are_reported_once_as_module_diagnostics(self) -> None:
        # M3-07: canonical_e621_tag is read at load time and surfaced instead of being ignored.
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1)
            try:
                transport = ReplaceTransport(keep_non_canonical=32, direction_conflicts=3)
                self.assertEqual("completed", fixture.runner(transport).run())
                diagnostics = {row["code"]: (row["severity"], row["count"]) for row in fixture.database.module_diagnostics("job-replace-runner", "replace")}
                self.assertEqual(("info", 32), diagnostics["replace_keep_non_canonical"])
                self.assertEqual(("warning", 3), diagnostics["replace_canonical_direction_conflict"])
            finally:
                fixture.close()

    def test_a_second_replace_pass_over_the_same_sample_is_a_no_op(self) -> None:
        # Replace is a single non-recursive round, but the overlay JSON it wrote is the input of
        # any re-run, so without provenance the tags were replaced a second time.
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1)
            try:
                transport = ReplaceTransport()
                self.assertEqual("completed", fixture.runner(transport).run())
                replaced = fixture.layout.annotation_path("sample-0001", ".json").read_bytes()
                fixture.database.set_sample_state("job-replace-runner", 1, SampleRunState(sampleId=1, currentModuleId="replace"))
                fixture.database.set_module_summary("job-replace-runner", "replace", status="running", completed=0)
                fixture.database.set_job_status("job-replace-runner", "running", current_module_id="replace")
                self.assertEqual("completed", fixture.runner(transport).run())
                self.assertEqual(1, transport.process_requests)
                self.assertEqual(replaced, fixture.layout.annotation_path("sample-0001", ".json").read_bytes())
                self.assertEqual(1, fixture.database.module_diagnostic_count("job-replace-runner", "replace", "replace_already_applied"))
            finally:
                fixture.close()

    def test_matching_dataset_provenance_skips_replace_across_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1)
            try:
                raw_json = (fixture.dataset / "sample-0001.json").read_bytes()
                apply_provenance_changes(fixture.dataset, [
                    ReplaceProvenanceChange.upsert(
                        "sample-0001", FINGERPRINT, hashlib.sha256(raw_json).hexdigest(),
                    ),
                ])
                transport = ReplaceTransport()
                self.assertEqual("completed", fixture.runner(transport).run())
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                writer = ReplaceOverlayWriter(fixture.database, fixture.layout, "job-replace-runner")
                self.assertEqual(FINGERPRINT, writer.provenance("sample-0001"))
                self.assertEqual(
                    1,
                    fixture.database.module_diagnostic_count(
                        "job-replace-runner", "replace", "replace_already_applied",
                    ),
                )
            finally:
                fixture.close()

    def test_changed_json_or_resource_fingerprint_runs_replace_again(self) -> None:
        for change in ("json", "fingerprint"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                fixture = ReplaceRunnerFixture(Path(temporary), 1)
                try:
                    original = (fixture.dataset / "sample-0001.json").read_bytes()
                    recorded_fingerprint = "0" * 64 if change == "fingerprint" else FINGERPRINT
                    apply_provenance_changes(fixture.dataset, [
                        ReplaceProvenanceChange.upsert(
                            "sample-0001", recorded_fingerprint, hashlib.sha256(original).hexdigest(),
                        ),
                    ])
                    if change == "json":
                        (fixture.dataset / "sample-0001.json").write_text(
                            json.dumps(_projection("question_mark")), encoding="utf-8",
                        )
                    transport = ReplaceTransport()
                    self.assertEqual("completed", fixture.runner(transport).run())
                    self.assertEqual((1, 1), (transport.hello_requests, transport.process_requests))
                finally:
                    fixture.close()

    def test_invalid_dataset_provenance_fails_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ReplaceRunnerFixture(Path(temporary), 1)
            try:
                path = provenance_database_path(fixture.dataset)
                path.parent.mkdir()
                path.write_bytes(b"not a SQLite database")
                transport = ReplaceTransport()
                with self.assertRaises(ReplaceRunnerError) as caught:
                    fixture.runner(transport).run()
                self.assertEqual("replace_provenance_invalid", caught.exception.code)
                self.assertEqual((0, 0), (transport.hello_requests, transport.process_requests))
                self.assertEqual("failed", fixture.database.get_job("job-replace-runner")["status"])
            finally:
                fixture.close()


class CustomReplaceIndexTests(unittest.TestCase):
    def test_keep_rows_are_validated_against_the_on_disk_tag_constraint_with_a_line_number(self) -> None:
        # F24: `foo,,keep,"bar, baz"` used to pass preflight and only fail as a "protocol
        # violation" once Replace hit the first matching sample, discarding Caption/Classify work.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.csv"
            for row, expected in (
                (b'foo,,keep,"bar, baz"', "line 3"),
                (b"foo,,keep, bar", "line 3"),
                (b"foo,,keep,", "line 3"),
            ):
                with self.subTest(row=row):
                    path.write_bytes(b"source_tag,canonical_e621_tag,action,replacement_tags\nok,,keep,ok\n" + row + b"\n")
                    with self.assertRaises(CustomReplaceIndexError) as caught:
                        inspect_custom_replace_index(str(path))
                    self.assertIn(expected, str(caught.exception))
            path.write_bytes(b"source_tag,canonical_e621_tag,action,replacement_tags\nfoo,,keep,bar_baz\n")
            self.assertEqual(1, inspect_custom_replace_index(str(path)).rule_count)


if __name__ == "__main__":
    unittest.main()
