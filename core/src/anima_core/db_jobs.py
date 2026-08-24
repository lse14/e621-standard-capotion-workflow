"""Job metadata ownership for the state database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import ProgressEvent, SampleIssue, pipeline_module_ids, utc_now
from .db_primitives import (
    get_job_row,
    increment_module_counts as increment_module_counts_primitive,
    repair_target_clause,
    set_job_status as set_job_status_primitive,
    upsert_issue as upsert_issue_primitive,
    validate_count_page_limit,
    validate_page_limit,
)
from .db_schema import (
    DEFAULT_PAGE_SIZE,
    MAX_EVENT_RING,
    MAX_PAGE_SIZE,
    NON_INTERRUPTIBLE_JOB_STATUSES,
)


class JobDatabaseMixin:
    """Job, manifest, dataset claim, summary, diagnostic, issue, and event operations."""

    def insert_job(self, job: Mapping[str, Any]) -> None:
        columns = [
            "job_id", "config_schema_version", "config_json", "config_hash", "work_mode",
            "overwrite_mode", "source_root", "output_root", "dataset_root", "dataset_root_key",
            "manifest_schema_version", "recursive", "sample_count", "manifest_generated_at", "status",
            "current_module_id", "last_event_id", "pinned", "api_budget_extra", "api_budget_revision",
            "overlay_root", "commit_journal_path", "resume_status", "created_at", "started_at",
            "cancel_requested_at", "finished_at",
        ]
        values = [job.get(column) for column in columns]
        placeholders = ",".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO jobs({','.join(columns)}) VALUES ({placeholders})", values
        )

    def update_preflight_config(
        self,
        job_id: str,
        *,
        expected_config_hash: str,
        config_json: str,
        config_hash: str,
    ) -> None:
        """Freeze derived config fields before any workspace or worker activity."""
        with self.transaction(immediate=True):
            job = self.connection.execute(
                "SELECT status,last_event_id,config_hash,overlay_root FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"job does not exist: {job_id}")
            if (
                job["status"] not in {"preflighting", "ready"}
                or int(job["last_event_id"]) != 0
                or job["overlay_root"] is not None
            ):
                raise ValueError("job configuration can only be frozen before workspace preparation")
            if str(job["config_hash"]) != expected_config_hash:
                raise ValueError("preflight configuration changed concurrently")
            result = self.connection.execute(
                "UPDATE jobs SET config_json=?,config_hash=? WHERE job_id=? AND config_hash=?",
                (config_json, config_hash, job_id, expected_config_hash),
            )
            if result.rowcount != 1:
                raise ValueError("preflight configuration could not be frozen")

    def get_job(self, job_id: str) -> sqlite3.Row:
        return get_job_row(self.connection, job_id)

    def set_pinned(self, job_id: str, pinned: bool) -> None:
        result = self.connection.execute("UPDATE jobs SET pinned=? WHERE job_id=?", (int(pinned), job_id))
        if result.rowcount != 1:
            raise KeyError(f"job does not exist: {job_id}")

    def has_repair_children(self, parent_job_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM repair_jobs WHERE parent_job_id=? LIMIT 1", (parent_job_id,)
        ).fetchone() is not None

    def delete_job_control_record(self, job_id: str) -> None:
        """Delete only control-plane rows. Backup paths are never tracked/deleted here."""
        result = self.connection.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        if result.rowcount != 1:
            raise KeyError(f"job does not exist: {job_id}")

    def insert_samples(self, job_id: str, records: Iterable[Mapping[str, Any]], *, batch_size: int = 500) -> int:
        count = 0
        batch: list[tuple[Any, ...]] = []
        for record in records:
            batch.append((
                job_id, record["sample_id"], record["relative_image_path"], record["annotation_key"],
                record["source"], int(record["in_processing_scope"]), record["image_format"],
                record["image_frame_count"], record["original_txt_state"], record["original_json_state"],
                record.get("image_file_id"), record.get("image_size"), record.get("image_mtime_ns"),
                record.get("original_txt_sha256"), record.get("original_json_sha256"),
            ))
            if len(batch) >= batch_size:
                count += self._insert_sample_batch(batch)
                batch.clear()
        if batch:
            count += self._insert_sample_batch(batch)
        return count

    def _insert_sample_batch(self, batch: list[tuple[Any, ...]]) -> int:
        with self.transaction():
            self.connection.executemany(
                """INSERT INTO samples(
                    job_id,sample_id,relative_image_path,annotation_key,source,in_processing_scope,
                    image_format,image_frame_count,original_txt_state,original_json_state,
                    image_file_id,image_size,image_mtime_ns,original_txt_sha256,original_json_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            self.connection.executemany(
                "INSERT INTO sample_state(job_id,sample_id,txt_provenance,status,updated_at) VALUES (?,?,?,'pending',?)",
                [(row[0], row[1], "missing", utc_now()) for row in batch],
            )
        return len(batch)

    def clear_manifest_rows(self, job_id: str) -> None:
        """Remove a partial scan without touching the immutable job config."""
        with self.transaction(immediate=True):
            self.connection.execute("DELETE FROM samples WHERE job_id=?", (job_id,))
            result = self.connection.execute(
                "UPDATE jobs SET sample_count=0,manifest_generated_at=NULL WHERE job_id=?", (job_id,)
            )
            if result.rowcount != 1:
                raise KeyError(f"job does not exist: {job_id}")

    def initialize_module_summary(self, job_id: str, module_id: str, *, total: int, status: str = "pending") -> None:
        if total < 0:
            raise ValueError("module total must not be negative")
        self.connection.execute(
            """INSERT INTO module_summary(job_id,module_id,status,total) VALUES (?,?,?,?)
               ON CONFLICT(job_id,module_id) DO NOTHING""",
            (job_id, module_id, status, total),
        )

    def module_summary(self, job_id: str, module_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM module_summary WHERE job_id=? AND module_id=?", (job_id, module_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"module summary does not exist: {job_id}/{module_id}")
        return row

    def set_module_summary(
        self,
        job_id: str,
        module_id: str,
        *,
        status: str,
        completed: int | None = None,
        failed: int | None = None,
        skipped: int | None = None,
        issue_count: int | None = None,
        finished: bool = False,
    ) -> None:
        result = self.connection.execute(
            """UPDATE module_summary
               SET status=?,completed=COALESCE(?,completed),failed=COALESCE(?,failed),
                   skipped=COALESCE(?,skipped),issue_count=COALESCE(?,issue_count),
                   started_at=COALESCE(started_at,?),finished_at=CASE WHEN ? THEN ? ELSE finished_at END
               WHERE job_id=? AND module_id=?""",
            (
                status, completed, failed, skipped, issue_count,
                utc_now() if status == "running" else None, int(finished), utc_now() if finished else None,
                job_id, module_id,
            ),
        )
        if result.rowcount != 1:
            raise KeyError(f"module summary does not exist: {job_id}/{module_id}")

    def increment_worker_restart(self, job_id: str, module_id: str) -> int:
        with self.transaction(immediate=True):
            result = self.connection.execute(
                "UPDATE module_summary SET worker_restart_count=worker_restart_count+1 WHERE job_id=? AND module_id=?",
                (job_id, module_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"module summary does not exist: {job_id}/{module_id}")
            return int(self.connection.execute(
                "SELECT worker_restart_count FROM module_summary WHERE job_id=? AND module_id=?",
                (job_id, module_id),
            ).fetchone()[0])

    @staticmethod
    def _limit(value: int | None) -> int:
        return validate_page_limit(value)

    @staticmethod
    def _count_limit(value: int | None) -> int:
        return validate_count_page_limit(value)

    def page_samples(self, job_id: str, *, after_sample_id: int | None = None, limit: int = DEFAULT_PAGE_SIZE) -> list[sqlite3.Row]:
        limit = self._limit(limit)
        if after_sample_id is None:
            return list(self.connection.execute(
                "SELECT * FROM samples WHERE job_id=? ORDER BY sample_id LIMIT ?", (job_id, limit)
            ))
        return list(self.connection.execute(
            "SELECT * FROM samples WHERE job_id=? AND sample_id>? ORDER BY sample_id LIMIT ?",
            (job_id, after_sample_id, limit),
        ))

    def count_processing_samples(self, job_id: str) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM samples WHERE job_id=? AND in_processing_scope=1", (job_id,)
        ).fetchone()[0])

    def _repair_target_clause(self, job_id: str, module_id: str, *, sample_alias: str = "s") -> tuple[str, list[Any]]:
        return repair_target_clause(
            self.connection, job_id, module_id, sample_alias=sample_alias,
        )

    def count_module_samples(self, job_id: str, module_id: str) -> int:
        clause, parameters = self._repair_target_clause(job_id, module_id)
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM samples AS s WHERE s.job_id=? AND s.in_processing_scope=1" + clause,
            (job_id, *parameters),
        ).fetchone()[0])

    def increment_module_counts(
        self,
        job_id: str,
        module_id: str,
        *,
        completed: int = 0,
        failed: int = 0,
        skipped: int = 0,
        issues: int = 0,
    ) -> None:
        increment_module_counts_primitive(
            self.connection,
            job_id,
            module_id,
            completed=completed,
            failed=failed,
            skipped=skipped,
            issues=issues,
        )

    def page_issues(
        self,
        job_id: str,
        *,
        after_sample_id: int | None = None,
        after_issue_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> list[sqlite3.Row]:
        """Page sample issues by the full (sample_id, issue_id) ordering keyset.

        A sample whose issues straddle a page boundary keeps its remaining rows
        visible; the caller must carry both cursor parts forward.
        """
        limit = self._limit(limit)
        if after_sample_id is None:
            return list(self.connection.execute(
                """SELECT * FROM issues WHERE job_id=? AND sample_id IS NOT NULL
                   ORDER BY sample_id,issue_id LIMIT ?""",
                (job_id, limit),
            ))
        if after_issue_id is None:
            return list(self.connection.execute(
                """SELECT * FROM issues WHERE job_id=? AND sample_id>?
                   ORDER BY sample_id,issue_id LIMIT ?""",
                (job_id, after_sample_id, limit),
            ))
        return list(self.connection.execute(
            """SELECT * FROM issues WHERE job_id=?
                 AND (sample_id>? OR (sample_id=? AND issue_id>?))
               ORDER BY sample_id,issue_id LIMIT ?""",
            (job_id, after_sample_id, after_sample_id, after_issue_id, limit),
        ))

    def count_unresolved_blocking_issues(self, job_id: str, *, exclude_module_id: str | None = None) -> int:
        """Count the issue-review gate's open blocking issues."""
        predicate = "" if exclude_module_id is None else " AND module_id<>?"
        parameters: list[Any] = [job_id]
        if exclude_module_id is not None:
            parameters.append(exclude_module_id)
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM issues WHERE job_id=? AND resolved_at IS NULL AND blocking=1" + predicate,
            parameters,
        ).fetchone()[0])

    def create_repair_link(self, repair_job_id: str, parent_job_id: str) -> None:
        result = self.connection.execute(
            """INSERT INTO repair_jobs(repair_job_id,parent_job_id,created_at)
               SELECT ?,?,? WHERE EXISTS (
                   SELECT 1 FROM jobs WHERE job_id=? AND status<>'discarded'
               )""",
            (repair_job_id, parent_job_id, utc_now(), parent_job_id),
        )
        if result.rowcount != 1:
            raise ValueError("parent task is unavailable for repair")

    def repair_parent_job_id(self, repair_job_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT parent_job_id FROM repair_jobs WHERE repair_job_id=?", (repair_job_id,)
        ).fetchone()
        return None if row is None else str(row["parent_job_id"])

    def repair_children(self, parent_job_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT jobs.job_id,jobs.status,jobs.current_module_id,jobs.sample_count,jobs.created_at,jobs.finished_at,
                      COUNT(repair_targets.sample_id) AS target_count
                 FROM repair_jobs
                 JOIN jobs ON jobs.job_id=repair_jobs.repair_job_id
                 LEFT JOIN repair_targets ON repair_targets.repair_job_id=repair_jobs.repair_job_id
                WHERE repair_jobs.parent_job_id=?
                GROUP BY jobs.job_id,jobs.status,jobs.current_module_id,jobs.sample_count,jobs.created_at,jobs.finished_at
                ORDER BY jobs.created_at DESC, jobs.job_id DESC""",
            (parent_job_id,),
        ))

    def count_repair_targets(self, repair_job_id: str) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM repair_targets WHERE repair_job_id=?", (repair_job_id,)
        ).fetchone()[0])

    def repair_candidate_summary(self, parent_job_id: str) -> tuple[int, int]:
        """Return bounded UI counts from current, retriable parent issues only."""
        row = self.connection.execute(
            """SELECT COUNT(DISTINCT sample_id) AS targets,
                      COUNT(DISTINCT CASE WHEN repair_start_module IN ('caption','classify','replace','ocr','nl')
                                          THEN sample_id END) AS reaches_nl
                 FROM issues
                WHERE job_id=? AND resolved_at IS NULL AND retriable=1
                  AND severity IN ('warning','error') AND sample_id IS NOT NULL
                  AND repair_start_module IN ('caption','classify','replace','ocr','nl','dropout','token_budget','export')""",
            (parent_job_id,),
        ).fetchone()
        return int(row["targets"]), int(row["reaches_nl"])

    def resolve_repaired_parent_issues(self, repair_job_id: str) -> int:
        """Close fixed issues only after a repair job's directory commit succeeds."""
        with self.transaction(immediate=True):
            parent = self.repair_parent_job_id(repair_job_id)
            if parent is None:
                return 0
            linked = list(self.connection.execute(
                """SELECT parent_issue.issue_id,parent_issue.module_id,parent_issue.code,target.sample_id
                     FROM repair_target_issues AS link
                     JOIN issues AS parent_issue ON parent_issue.issue_id=link.parent_issue_id
                     JOIN repair_targets AS target
                       ON target.repair_job_id=link.repair_job_id AND target.sample_id=link.sample_id
                    WHERE link.repair_job_id=? AND parent_issue.resolved_at IS NULL""",
                (repair_job_id,),
            ))
            resolved = 0
            for row in linked:
                current = self.connection.execute(
                    """SELECT severity,blocking,retriable,repair_start_module,message,field_errors_json
                         FROM issues WHERE job_id=? AND sample_id=? AND module_id=? AND code=?
                           AND resolved_at IS NULL""",
                    (repair_job_id, row["sample_id"], row["module_id"], row["code"]),
                ).fetchone()
                if current is None:
                    self.resolve_issue(str(row["issue_id"]))
                    resolved += 1
                    continue
                self.connection.execute(
                    """UPDATE issues SET severity=?,blocking=?,retriable=?,repair_start_module=?,message=?,
                           field_errors_json=?,attempt=attempt+1,resolved_at=NULL,updated_at=? WHERE issue_id=?""",
                    (
                        current["severity"], current["blocking"], current["retriable"], current["repair_start_module"],
                        current["message"], current["field_errors_json"], utc_now(), row["issue_id"],
                    ),
                )
            return resolved

    def repair_parent_cursor(self, repair_job_id: str) -> int | None:
        row = self.connection.execute(
            """SELECT MAX(parent_issue.sample_id) AS sample_id
               FROM repair_target_issues AS target_issue
               JOIN issues AS parent_issue ON parent_issue.issue_id=target_issue.parent_issue_id
               WHERE target_issue.repair_job_id=?""",
            (repair_job_id,),
        ).fetchone()
        return None if row is None or row["sample_id"] is None else int(row["sample_id"])

    def stage_repair_target_page(
        self, repair_job_id: str, parent_job_id: str, *, after_sample_id: int | None, limit: int = 500,
    ) -> list[int]:
        """Persist one bounded, deduplicated page of eligible original issues."""
        limit = self._limit(limit)
        repair_job = self.get_job(repair_job_id)
        parent_job = self.get_job(parent_job_id)
        if int(repair_job["config_schema_version"]) != int(parent_job["config_schema_version"]):
            raise ValueError("repair task configuration version does not match its parent")
        try:
            pipeline_order = pipeline_module_ids(int(repair_job["config_schema_version"]))
        except ValueError as exc:
            raise ValueError("repair task configuration schema is invalid") from exc
        eligible_set = {"caption", "classify", "replace", "ocr", "nl", "dropout", "token_budget", "export"}
        repair_order = tuple(module_id for module_id in pipeline_order if module_id in eligible_set)
        rank_sql = "CASE parent_issue.repair_start_module " + " ".join(
            f"WHEN ? THEN {rank}" for rank, _ in enumerate(repair_order)
        ) + " END"
        eligible_sql = ",".join("?" for _ in repair_order)
        predicate = "" if after_sample_id is None else " AND parent_issue.sample_id>?"
        params: list[Any] = [*repair_order, repair_job_id, parent_job_id, *repair_order]
        if after_sample_id is not None:
            params.append(after_sample_id)
        params.append(limit)
        rows = list(self.connection.execute(
            """SELECT repair_sample.sample_id AS repair_sample_id,parent_issue.sample_id AS parent_sample_id,
                       MIN(""" + rank_sql + """
                       ) AS start_rank
                 FROM issues AS parent_issue
                 JOIN samples AS parent_sample ON parent_sample.job_id=parent_issue.job_id
                   AND parent_sample.sample_id=parent_issue.sample_id
                 JOIN samples AS repair_sample ON repair_sample.job_id=?
                   AND repair_sample.relative_image_path=parent_sample.relative_image_path
                 WHERE parent_issue.job_id=? AND parent_issue.resolved_at IS NULL AND parent_issue.retriable=1
                   AND parent_issue.severity IN ('warning','error') AND parent_issue.sample_id IS NOT NULL
                   AND parent_issue.repair_start_module IN (""" + eligible_sql + ")"
            + predicate + " GROUP BY repair_sample.sample_id,parent_issue.sample_id ORDER BY parent_issue.sample_id LIMIT ?",
            params,
        ))
        if not rows:
            return []
        sample_ids = [int(row["repair_sample_id"]) for row in rows]
        parent_sample_ids = [int(row["parent_sample_id"]) for row in rows]
        with self.transaction(immediate=True):
            self.connection.executemany(
                "INSERT INTO repair_targets(repair_job_id,sample_id,repair_start_module) VALUES (?,?,?)",
                [
                    (repair_job_id, sample_id, repair_order[int(row["start_rank"])])
                    for sample_id, row in zip(sample_ids, rows, strict=True)
                ],
            )
            placeholders = ",".join("?" for _ in parent_sample_ids)
            issue_rows = self.connection.execute(
                """SELECT sample_id,issue_id FROM issues WHERE job_id=? AND resolved_at IS NULL AND retriable=1
                   AND severity IN ('warning','error')
                   AND repair_start_module IN ('caption','classify','replace','ocr','nl','dropout','token_budget','export')
                   AND sample_id IN (""" + placeholders + ")",
                (parent_job_id, *parent_sample_ids),
            )
            repair_by_parent = dict(zip(parent_sample_ids, sample_ids, strict=True))
            self.connection.executemany(
                "INSERT INTO repair_target_issues(repair_job_id,sample_id,parent_issue_id) VALUES (?,?,?)",
                [(repair_job_id, repair_by_parent[int(row["sample_id"])], str(row["issue_id"])) for row in issue_rows],
            )
        return sample_ids

    def page_overlay_jobs(self, *, after_job_id: str | None = None, limit: int = DEFAULT_PAGE_SIZE) -> list[sqlite3.Row]:
        """Bounded startup scan for task-owned commit journals only.

        `resume_status` travels with the row so a job frozen by startup recovery
        can still be resolved against the module it was interrupted in.
        """
        limit = self._limit(limit)
        if after_job_id is None:
            return list(self.connection.execute(
                """SELECT job_id,overlay_root,status,resume_status,current_module_id FROM jobs
                   WHERE overlay_root IS NOT NULL ORDER BY job_id LIMIT ?""", (limit,),
            ))
        return list(self.connection.execute(
            """SELECT job_id,overlay_root,status,resume_status,current_module_id FROM jobs
               WHERE overlay_root IS NOT NULL AND job_id>? ORDER BY job_id LIMIT ?""",
            (after_job_id, limit),
        ))

    def page_active_jobs(self, *, after_job_id: str | None = None, limit: int = DEFAULT_PAGE_SIZE) -> list[sqlite3.Row]:
        """Keyset page of jobs a crashed backend could have left mid-flight."""
        limit = self._limit(limit)
        placeholders = ",".join("?" for _ in NON_INTERRUPTIBLE_JOB_STATUSES)
        predicate = "" if after_job_id is None else " AND job_id>?"
        parameters: list[Any] = [*NON_INTERRUPTIBLE_JOB_STATUSES]
        if after_job_id is not None:
            parameters.append(after_job_id)
        parameters.append(limit)
        return list(self.connection.execute(
            f"""SELECT job_id,status,resume_status,current_module_id FROM jobs
                WHERE status NOT IN ({placeholders}){predicate} ORDER BY job_id LIMIT ?""",
            parameters,
        ))

    def clear_stale_dataset_claims(self) -> int:
        """Drop claims left by successful jobs; recoverable tasks retain ownership."""
        result = self.connection.execute(
            """DELETE FROM dataset_claims WHERE job_id IN (
                    SELECT job_id FROM jobs WHERE status='succeeded')""",
        )
        return result.rowcount

    def release_dataset_claim(self, job_id: str) -> None:
        """Release one job's dataset claim without depending on in-process locks."""
        self.connection.execute("DELETE FROM dataset_claims WHERE job_id=?", (job_id,))

    def clear_workspace_metadata(self, job_id: str) -> None:
        """Forget an overlay that no longer exists so recovery stops revisiting it."""
        result = self.connection.execute(
            "UPDATE jobs SET overlay_root=NULL,commit_journal_path=NULL WHERE job_id=?", (job_id,)
        )
        if result.rowcount != 1:
            raise KeyError(f"job does not exist: {job_id}")

    def get_runtime_evidence(self, job_id: str) -> dict[str, object]:
        raw = self.get_job(job_id)["runtime_evidence_json"]
        if raw is None:
            return {}
        try:
            value = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("runtime evidence is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("runtime evidence must be an object")
        return value

    def set_runtime_evidence(self, job_id: str, module_id: str, evidence: Mapping[str, object]) -> None:
        if module_id not in {"ocr", "dropout"} or not isinstance(evidence, Mapping):
            raise ValueError("runtime evidence is invalid")
        current = self.get_runtime_evidence(job_id)
        current[module_id] = dict(evidence)
        serialized = json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 262_144:
            raise ValueError("runtime evidence is too large")
        result = self.connection.execute(
            "UPDATE jobs SET runtime_evidence_json=? WHERE job_id=?", (serialized, job_id),
        )
        if result.rowcount != 1:
            raise KeyError(f"job does not exist: {job_id}")

    def clear_cancellation_metadata(self, job_id: str) -> None:
        """Clear only cancellation timestamps after a recovery is accepted."""
        result = self.connection.execute(
            "UPDATE jobs SET cancel_requested_at=NULL,finished_at=NULL WHERE job_id=?", (job_id,)
        )
        if result.rowcount != 1:
            raise KeyError(f"job does not exist: {job_id}")

    def preflight_projection_counts(self, job_id: str) -> dict[str, Any]:
        """Project module-6 file effects from manifest state and the frozen format.

        Keys: ``format``, ``inScopeSamples``, ``retainedSamples`` (out of scope,
        never touched), and ``jsonCreate`` / ``jsonOverwrite`` / ``jsonDelete``
        plus the same three ``txt`` counts.
        """
        job = self.get_job(job_id)
        try:
            format_value = json.loads(str(job["config_json"]))["export"]["format"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("frozen export configuration is invalid") from exc
        if format_value not in {"json", "flat_txt", "both"}:
            raise ValueError("frozen export format is invalid")
        row = self.connection.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(in_processing_scope),0) AS in_scope,
                      COALESCE(SUM(CASE WHEN in_processing_scope=1 AND original_json_state='nonblank' THEN 1 END),0) AS json_present,
                      COALESCE(SUM(CASE WHEN in_processing_scope=1 AND original_txt_state='nonblank' THEN 1 END),0) AS txt_present
                 FROM samples WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        total, in_scope = int(row["total"]), int(row["in_scope"])
        json_present, txt_present = int(row["json_present"]), int(row["txt_present"])
        writes_json = format_value in {"json", "both"}
        writes_txt = format_value in {"flat_txt", "both"}
        return {
            "format": str(format_value),
            "inScopeSamples": in_scope,
            "retainedSamples": total - in_scope,
            "jsonCreate": in_scope - json_present if writes_json else 0,
            "jsonOverwrite": json_present if writes_json else 0,
            "jsonDelete": 0 if writes_json else json_present,
            "txtCreate": in_scope - txt_present if writes_txt else 0,
            "txtOverwrite": txt_present if writes_txt else 0,
            "txtDelete": 0 if writes_txt else txt_present,
        }

    def module_summaries(self, job_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM module_summary WHERE job_id=?", (job_id,)
        ))

    def increment_module_diagnostic(self, job_id: str, module_id: str, code: str, *, severity: str, amount: int) -> None:
        if severity not in {"info", "warning", "error"} or not code or not 1 <= amount <= MAX_PAGE_SIZE:
            raise ValueError("module diagnostic is invalid")
        self.connection.execute(
            """INSERT INTO module_diagnostics(job_id,module_id,code,severity,count,updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id,module_id,code) DO UPDATE SET count=count+excluded.count,updated_at=excluded.updated_at""",
            (job_id, module_id, code, severity, amount, utc_now()),
        )

    def module_diagnostics(self, job_id: str, module_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM module_diagnostics WHERE job_id=? AND module_id=? ORDER BY code", (job_id, module_id)
        ))

    def module_diagnostic_count(self, job_id: str, module_id: str, code: str) -> int:
        row = self.connection.execute(
            "SELECT count FROM module_diagnostics WHERE job_id=? AND module_id=? AND code=?",
            (job_id, module_id, code),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def set_module_diagnostic_count(self, job_id: str, module_id: str, code: str, *, severity: str, count: int) -> None:
        if severity not in {"info", "warning", "error"} or type(count) is not int or count < 0:
            raise ValueError("module diagnostic is invalid")
        self.connection.execute(
            """INSERT INTO module_diagnostics(job_id,module_id,code,severity,count,updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id,module_id,code) DO UPDATE SET severity=excluded.severity,count=excluded.count,updated_at=excluded.updated_at""",
            (job_id, module_id, code, severity, count, utc_now()),
        )

    def upsert_issue(self, issue: SampleIssue) -> None:
        upsert_issue_primitive(self.connection, issue)

    def resolve_issue(self, issue_id: str) -> None:
        self.connection.execute("UPDATE issues SET resolved_at=?,updated_at=? WHERE issue_id=?", (utc_now(), utc_now(), issue_id))

    def append_event(self, event: ProgressEvent) -> None:
        with self.transaction(immediate=True):
            job = self.connection.execute(
                "SELECT config_hash,last_event_id FROM jobs WHERE job_id=?", (event.jobId,)
            ).fetchone()
            if job is None:
                raise KeyError(f"job does not exist: {event.jobId}")
            if job["config_hash"] != event.configHash:
                raise ValueError("event config hash does not match immutable job configuration")
            if event.eventId <= int(job["last_event_id"]):
                raise ValueError("eventId must be strictly increasing for a job")
            self.connection.execute(
                "UPDATE jobs SET last_event_id=? WHERE job_id=?",
                (event.eventId, event.jobId),
            )
            self.connection.execute(
                """INSERT INTO event_ring(job_id,event_id,module_id,status,completed,total,sample_id,issue_code,attempt,config_hash,occurred_at,message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.jobId, event.eventId, event.moduleId, event.status, event.completed, event.total,
                    event.sampleId, event.issueCode, event.attempt, event.configHash, event.occurredAt, event.message,
                ),
            )
            self.connection.execute(
                "DELETE FROM event_ring WHERE job_id=? AND event_id <= (SELECT COALESCE(MAX(event_id),0)-? FROM event_ring WHERE job_id=?)",
                (event.jobId, MAX_EVENT_RING, event.jobId),
            )

    def event_page(self, job_id: str, after_event_id: int, limit: int = 200) -> list[sqlite3.Row]:
        limit = self._limit(limit)
        return list(self.connection.execute(
            "SELECT * FROM event_ring WHERE job_id=? AND event_id>? ORDER BY event_id LIMIT ?",
            (job_id, after_event_id, limit),
        ))

    def event_snapshot_required(self, job_id: str, after_event_id: int) -> bool:
        oldest = self.connection.execute(
            "SELECT MIN(event_id) FROM event_ring WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        return oldest is not None and after_event_id < int(oldest) - 1

    def set_job_status(self, job_id: str, status: str, *, current_module_id: str | None = None, resume_status: str | None = None) -> None:
        set_job_status_primitive(
            self.connection,
            job_id,
            status,
            current_module_id=current_module_id,
            resume_status=resume_status,
        )

    def set_workspace_metadata(self, job_id: str, *, dataset_root: str, dataset_root_key: str, overlay_root: str) -> None:
        result = self.connection.execute(
            "UPDATE jobs SET dataset_root=?,dataset_root_key=?,overlay_root=? WHERE job_id=?",
            (dataset_root, dataset_root_key, overlay_root, job_id),
        )
        if result.rowcount != 1:
            raise KeyError(f"job does not exist: {job_id}")
