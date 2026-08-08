"""Portable dataset-level provenance for Replace's single replacement round."""
from __future__ import annotations

import itertools
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import utc_now
from .path_safety import canonicalize, ensure_within, safe_relative_path


METADATA_DIRECTORY = ".anima-idg"
DATABASE_FILENAME = "replace-provenance-v1.sqlite3"
APPLICATION_ID = 0x414E494D  # "ANIM"
USER_VERSION = 1
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TABLE = "replace_provenance"
_CREATE_SQL = f"""
CREATE TABLE {_TABLE} (
    annotation_key TEXT NOT NULL PRIMARY KEY,
    resource_fingerprint TEXT NOT NULL,
    json_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_EXPECTED_COLUMNS = (
    (0, "annotation_key", "TEXT", 1, None, 1),
    (1, "resource_fingerprint", "TEXT", 1, None, 0),
    (2, "json_sha256", "TEXT", 1, None, 0),
    (3, "updated_at", "TEXT", 1, None, 0),
)


class ReplaceProvenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplaceProvenanceChange:
    annotation_key: str
    resource_fingerprint: str | None
    json_sha256: str | None

    @classmethod
    def upsert(cls, annotation_key: str, resource_fingerprint: str, json_sha256: str) -> "ReplaceProvenanceChange":
        return cls(annotation_key, resource_fingerprint, json_sha256)

    @classmethod
    def delete(cls, annotation_key: str) -> "ReplaceProvenanceChange":
        return cls(annotation_key, None, None)


def provenance_database_path(dataset_root: str | Path) -> Path:
    dataset = canonicalize(dataset_root, must_exist=True, directory=True).value
    return ensure_within(dataset, dataset / METADATA_DIRECTORY / DATABASE_FILENAME)


def _validate_digest(value: str, name: str) -> None:
    if _HEX_SHA256.fullmatch(value) is None:
        raise ReplaceProvenanceError(f"{name} must be a lowercase SHA-256 digest")


def _validate_change(change: ReplaceProvenanceChange) -> None:
    try:
        safe_relative_path(change.annotation_key)
    except ValueError as exc:
        raise ReplaceProvenanceError("replace provenance annotation key is invalid") from exc
    if change.resource_fingerprint is None and change.json_sha256 is None:
        return
    if not isinstance(change.resource_fingerprint, str) or not isinstance(change.json_sha256, str):
        raise ReplaceProvenanceError("replace provenance change is incomplete")
    _validate_digest(change.resource_fingerprint, "resource fingerprint")
    _validate_digest(change.json_sha256, "JSON digest")


def _reject_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        if path.with_name(path.name + suffix).exists():
            raise ReplaceProvenanceError("replace provenance database has an unresolved SQLite sidecar")


def _validate_connection(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
        raise ReplaceProvenanceError("replace provenance database integrity check failed")
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != APPLICATION_ID or user_version != USER_VERSION:
        raise ReplaceProvenanceError("replace provenance database identity is invalid")
    objects = connection.execute(
        "SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    if objects != [("table", _TABLE)]:
        raise ReplaceProvenanceError("replace provenance database contains an unexpected schema object")
    columns = tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({_TABLE})"))
    if columns != _EXPECTED_COLUMNS:
        raise ReplaceProvenanceError("replace provenance table schema is invalid")


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ReplaceProvenanceError("replace provenance path is not a regular file")
    _reject_sidecars(path)
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        _validate_connection(connection)
        return connection
    except (OSError, sqlite3.Error, ReplaceProvenanceError) as exc:
        if "connection" in locals():
            connection.close()
        if isinstance(exc, ReplaceProvenanceError):
            raise
        raise ReplaceProvenanceError("replace provenance database cannot be read") from exc


class DatasetReplaceProvenance:
    def __init__(self, connection: sqlite3.Connection | None) -> None:
        self._connection = connection

    @classmethod
    def open(cls, dataset_root: str | Path) -> "DatasetReplaceProvenance":
        path = provenance_database_path(dataset_root)
        if path.parent.exists() and not path.parent.is_dir():
            raise ReplaceProvenanceError("replace provenance metadata path is not a directory")
        _reject_sidecars(path)
        if not path.exists():
            return cls(None)
        return cls(_open_readonly(path))

    def matches(self, annotation_key: str, resource_fingerprint: str, json_sha256: str) -> bool:
        _validate_change(ReplaceProvenanceChange.upsert(annotation_key, resource_fingerprint, json_sha256))
        if self._connection is None:
            return False
        try:
            row = self._connection.execute(
                f"SELECT resource_fingerprint,json_sha256 FROM {_TABLE} WHERE annotation_key=?",
                (annotation_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReplaceProvenanceError("replace provenance record cannot be read") from exc
        if row is None:
            return False
        stored_fingerprint, stored_json_sha256 = row
        if not isinstance(stored_fingerprint, str) or not isinstance(stored_json_sha256, str):
            raise ReplaceProvenanceError("replace provenance record is invalid")
        _validate_digest(stored_fingerprint, "stored resource fingerprint")
        _validate_digest(stored_json_sha256, "stored JSON digest")
        return (stored_fingerprint, stored_json_sha256) == (resource_fingerprint, json_sha256)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "DatasetReplaceProvenance":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _create_database(path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={USER_VERSION}")
        connection.execute(_CREATE_SQL)
        connection.commit()
        _validate_connection(connection)
        return connection
    except (OSError, sqlite3.Error, ReplaceProvenanceError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, ReplaceProvenanceError):
            raise
        raise ReplaceProvenanceError("replace provenance database cannot be created") from exc


def _detach_database(path: Path) -> None:
    source = _open_readonly(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.close()
        destination = None
        source.close()
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, sqlite3.Error) as exc:
        raise ReplaceProvenanceError("replace provenance database cannot be detached in staging") from exc
    finally:
        source.close()
        if destination is not None:
            destination.close()
        if temporary.exists():
            temporary.unlink()


def apply_provenance_changes(
    dataset_root: str | Path, changes: Iterable[ReplaceProvenanceChange],
) -> None:
    """Apply changes to a standalone staging copy; an existing hard link is detached first."""
    path = provenance_database_path(dataset_root)
    if path.parent.exists() and not path.parent.is_dir():
        raise ReplaceProvenanceError("replace provenance metadata path is not a directory")
    _reject_sidecars(path)
    iterator = iter(changes)
    if not path.exists():
        first_upsert: ReplaceProvenanceChange | None = None
        for change in iterator:
            _validate_change(change)
            if change.json_sha256 is not None:
                first_upsert = change
                break
        if first_upsert is None:
            return
        connection = _create_database(path)
        iterator = itertools.chain((first_upsert,), iterator)
    else:
        _detach_database(path)
        try:
            connection = sqlite3.connect(path)
            _validate_connection(connection)
        except (sqlite3.Error, ReplaceProvenanceError) as exc:
            if "connection" in locals():
                connection.close()
            if isinstance(exc, ReplaceProvenanceError):
                raise
            raise ReplaceProvenanceError("replace provenance staging database cannot be opened") from exc

    timestamp = utc_now()
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        for change in iterator:
            _validate_change(change)
            if change.json_sha256 is None:
                connection.execute(f"DELETE FROM {_TABLE} WHERE annotation_key=?", (change.annotation_key,))
            else:
                connection.execute(
                    f"""INSERT INTO {_TABLE}(annotation_key,resource_fingerprint,json_sha256,updated_at)
                        VALUES (?,?,?,?)
                        ON CONFLICT(annotation_key) DO UPDATE SET
                          resource_fingerprint=excluded.resource_fingerprint,
                          json_sha256=excluded.json_sha256,
                          updated_at=excluded.updated_at""",
                    (change.annotation_key, change.resource_fingerprint, change.json_sha256, timestamp),
                )
        connection.commit()
        _validate_connection(connection)
    except sqlite3.Error as exc:
        connection.rollback()
        raise ReplaceProvenanceError("replace provenance changes cannot be committed") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _reject_sidecars(path)
