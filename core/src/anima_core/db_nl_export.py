"""NL and export ownership for the state database."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .contracts import COUNT_REVIEW_SCHEMA_VERSIONS, utc_now
from .db_primitives import (
    get_job_row,
    insert_count_observation_consistent,
    validate_page_limit,
)
from .db_schema import MAX_PAGE_SIZE


class NlExportDatabaseMixin:
    """NL request and response staging plus export artifact operations."""

    def record_nl_outcome(self, job_id: str, *, succeeded: bool) -> tuple[int, int]:
        with self.transaction(immediate=True):
            self.connection.execute(
                "INSERT INTO nl_outcome_window(job_id,succeeded) VALUES (?,?)", (job_id, int(succeeded))
            )
            self.connection.execute(
                "DELETE FROM nl_outcome_window WHERE job_id=? AND outcome_id NOT IN (SELECT outcome_id FROM nl_outcome_window WHERE job_id=? ORDER BY outcome_id DESC LIMIT 100)",
                (job_id, job_id),
            )
            row = self.connection.execute(
                "SELECT COUNT(*) AS processed, COALESCE(SUM(1-succeeded),0) AS failed FROM nl_outcome_window WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return int(row["processed"]), int(row["failed"])

    def add_api_budget_extra(self, job_id: str, amount: int) -> int:
        if type(amount) is not int or amount < 1:
            raise ValueError("API budget extra must be a positive integer")
        with self.transaction(immediate=True):
            self.connection.execute(
                "UPDATE jobs SET api_budget_extra=api_budget_extra+?,api_budget_revision=api_budget_revision+1 WHERE job_id=?",
                (amount, job_id),
            )
            row = get_job_row(self.connection, job_id)
        return int(row["api_budget_revision"])

    def stage_export_artifacts(
        self,
        job_id: str,
        sample_id: int,
        *,
        lease_id: str,
        artifacts: Mapping[str, tuple[str, str]],
    ) -> None:
        """Persist only verified Export artifact identities before completion."""
        if not artifacts or set(artifacts) - {"json", "txt"}:
            raise ValueError("export artifacts are invalid")
        for relative_path, digest in artifacts.values():
            if not isinstance(relative_path, str) or not isinstance(digest, str) or len(digest) != 64:
                raise ValueError("export artifact identity is invalid")
        with self.transaction(immediate=True):
            row = self.connection.execute(
                """SELECT 1 FROM sample_state WHERE job_id=? AND sample_id=?
                   AND current_module_id='export' AND status='leased' AND lease_id=?""",
                (job_id, sample_id, lease_id),
            ).fetchone()
            if row is None:
                raise ValueError("export artifacts do not belong to an active lease")
            self.connection.execute(
                "DELETE FROM export_artifacts WHERE job_id=? AND sample_id=?",
                (job_id, sample_id),
            )
            self.connection.executemany(
                """INSERT INTO export_artifacts(job_id,sample_id,kind,relative_path,sha256)
                   VALUES (?,?,?,?,?)""",
                [(job_id, sample_id, kind, relative_path, digest) for kind, (relative_path, digest) in artifacts.items()],
            )

    def page_export_artifacts(
        self,
        job_id: str,
        *,
        after_sample_id: int | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Keyset-page manifest rows with their module-6 artifact identities."""
        limit = validate_page_limit(limit)
        predicate = "" if after_sample_id is None else " AND s.sample_id>?"
        parameters: list[Any] = [job_id]
        if after_sample_id is not None:
            parameters.append(after_sample_id)
        parameters.append(limit)
        return list(self.connection.execute(
            """SELECT s.sample_id,s.annotation_key,s.in_processing_scope,e.kind,e.relative_path,e.sha256
               FROM samples AS s JOIN export_artifacts AS e
                 ON e.job_id=s.job_id AND e.sample_id=s.sample_id
               WHERE s.job_id=?""" + predicate + " ORDER BY s.sample_id,e.kind LIMIT ?",
            parameters,
        ))

    def page_export_artifact_groups(
        self,
        job_id: str,
        *,
        after_sample_id: int | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Page whole sample groups so a JSON/TXT pair can never split pages.

        Samples explicitly skipped by Export own no artifact and must keep
        their existing annotation, so they never enter the commit projection.
        """
        limit = validate_page_limit(limit)
        predicate = "" if after_sample_id is None else " AND s.sample_id>?"
        parameters: list[Any] = [job_id]
        if after_sample_id is not None:
            parameters.append(after_sample_id)
        parameters.append(limit)
        return list(self.connection.execute(
            """WITH page AS (
                   SELECT s.sample_id AS sample_id,s.annotation_key AS annotation_key
                   FROM samples AS s JOIN sample_state AS st
                     ON st.job_id=s.job_id AND st.sample_id=s.sample_id
                   WHERE s.job_id=? AND s.in_processing_scope=1
                     AND NOT (st.current_module_id='export' AND st.status='skipped')"""
            + predicate + " ORDER BY s.sample_id LIMIT ?"
            + """ )
               SELECT page.sample_id,page.annotation_key,e.kind,e.relative_path,e.sha256
               FROM page LEFT JOIN export_artifacts AS e
                 ON e.job_id=? AND e.sample_id=page.sample_id
               ORDER BY page.sample_id,e.kind""",
            (*parameters, job_id),
        ))

    def mark_nl_request_started(self, job_id: str, sample_id: int, *, lease_id: str) -> None:
        result = self.connection.execute(
            """UPDATE sample_state SET status='request_started',updated_at=?
               WHERE job_id=? AND sample_id=? AND current_module_id='nl' AND status='leased' AND lease_id=?""",
            (utc_now(), job_id, sample_id, lease_id),
        )
        if result.rowcount != 1:
            raise ValueError("NL request does not belong to an active lease")

    def return_unsubmitted_nl_request(self, job_id: str, sample_id: int, *, lease_id: str) -> None:
        result = self.connection.execute(
            """UPDATE sample_state SET status='pending',lease_id=NULL,worker_instance_id=NULL,lease_expires_at=NULL,updated_at=?
               WHERE job_id=? AND sample_id=? AND current_module_id='nl' AND status='request_started' AND lease_id=?""",
            (utc_now(), job_id, sample_id, lease_id),
        )
        if result.rowcount != 1:
            raise ValueError("unsubmitted NL request does not belong to an active lease")

    def confirm_nl_unknown_requests(self, job_id: str, *, limit: int = MAX_PAGE_SIZE) -> int:
        """Return only crash-uncertain API requests after explicit user confirmation."""
        limit = validate_page_limit(limit)
        with self.transaction(immediate=True):
            rows = list(self.connection.execute(
                """SELECT sample_id FROM sample_state WHERE job_id=? AND current_module_id='nl'
                   AND status='request_started' ORDER BY sample_id LIMIT ?""",
                (job_id, limit),
            ))
            if not rows:
                return 0
            ids = [int(row["sample_id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            self.connection.execute(
                f"""UPDATE sample_state SET status='pending',lease_id=NULL,worker_instance_id=NULL,
                    lease_expires_at=NULL,updated_at=? WHERE job_id=? AND sample_id IN ({placeholders})
                    AND current_module_id='nl' AND status='request_started'""",
                (utc_now(), job_id, *ids),
            )
        return len(ids)

    def stage_nl_response(
        self,
        job_id: str,
        sample_id: int,
        *,
        lease_id: str,
        nl: str,
        sha256: str,
        observation: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(nl, str) or len(nl.encode("utf-8")) > 16_384 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("staged NL is invalid")
        job = get_job_row(self.connection, job_id)
        config_schema_version = int(job["config_schema_version"])
        if config_schema_version in COUNT_REVIEW_SCHEMA_VERSIONS:
            from .count_review_protocol import CountObservationV1

            parsed_observation = CountObservationV1.from_dict(observation)
            if parsed_observation.status not in {"observed", "invalid"}:
                raise ValueError("requested NL response must contain an observed or invalid observation")
        elif config_schema_version == 2:
            if observation is not None:
                raise ValueError("legacy NL response must not contain a count observation")
            parsed_observation = None
        else:
            raise ValueError("NL task configuration schema is invalid")
        with self.transaction(immediate=True):
            now = utc_now()
            result = self.connection.execute(
                """UPDATE sample_state SET status='response_staged',updated_at=?
                   WHERE job_id=? AND sample_id=? AND current_module_id='nl' AND status='request_started' AND lease_id=?""",
                (now, job_id, sample_id, lease_id),
            )
            if result.rowcount != 1:
                raise ValueError("NL response does not belong to a started request")
            if parsed_observation is not None:
                insert_count_observation_consistent(
                    self.connection, job_id, sample_id, parsed_observation, now=now
                )
            self.connection.execute(
                """INSERT INTO staged_nl(job_id,sample_id,lease_id,nl,sha256,staged_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(job_id,sample_id) DO UPDATE SET lease_id=excluded.lease_id,nl=excluded.nl,sha256=excluded.sha256,staged_at=excluded.staged_at""",
                (job_id, sample_id, lease_id, nl, sha256, now),
            )

    def staged_nl(self, job_id: str, sample_id: int, *, lease_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM staged_nl WHERE job_id=? AND sample_id=? AND lease_id=?", (job_id, sample_id, lease_id),
        ).fetchone()
        if row is None:
            raise KeyError("staged NL does not exist for the current lease")
        return row

    def delete_staged_nl(self, job_id: str, sample_id: int, *, lease_id: str) -> None:
        result = self.connection.execute(
            "DELETE FROM staged_nl WHERE job_id=? AND sample_id=? AND lease_id=?", (job_id, sample_id, lease_id),
        )
        if result.rowcount != 1:
            raise ValueError("staged NL does not belong to the current lease")
