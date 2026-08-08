from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.replace_provenance import (
    APPLICATION_ID,
    USER_VERSION,
    DatasetReplaceProvenance,
    ReplaceProvenanceChange,
    ReplaceProvenanceError,
    apply_provenance_changes,
    provenance_database_path,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ReplaceProvenanceTests(unittest.TestCase):
    def test_create_match_update_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            first_json = _digest(b"first")
            second_json = _digest(b"second")
            first_resource = _digest(b"resource-1")
            second_resource = _digest(b"resource-2")

            with DatasetReplaceProvenance.open(dataset) as provenance:
                self.assertFalse(provenance.matches("nested\\sample", first_resource, first_json))
            apply_provenance_changes(dataset, [
                ReplaceProvenanceChange.upsert("nested\\sample", first_resource, first_json),
                ReplaceProvenanceChange.upsert("other", first_resource, first_json),
            ])
            with DatasetReplaceProvenance.open(dataset) as provenance:
                self.assertTrue(provenance.matches("nested\\sample", first_resource, first_json))
                self.assertTrue(provenance.matches("other", first_resource, first_json))

            apply_provenance_changes(dataset, [
                ReplaceProvenanceChange.upsert("nested\\sample", second_resource, second_json),
                ReplaceProvenanceChange.delete("other"),
            ])
            with DatasetReplaceProvenance.open(dataset) as provenance:
                self.assertTrue(provenance.matches("nested\\sample", second_resource, second_json))
                self.assertFalse(provenance.matches("nested\\sample", first_resource, first_json))
                self.assertFalse(provenance.matches("other", first_resource, first_json))

            with closing(sqlite3.connect(provenance_database_path(dataset))) as connection:
                self.assertEqual(APPLICATION_ID, connection.execute("PRAGMA application_id").fetchone()[0])
                self.assertEqual(USER_VERSION, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_staging_write_detaches_a_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            staging = root / "staging"
            dataset.mkdir()
            staging.mkdir()
            old_resource, old_json = _digest(b"old-resource"), _digest(b"old-json")
            new_resource, new_json = _digest(b"new-resource"), _digest(b"new-json")
            apply_provenance_changes(dataset, [
                ReplaceProvenanceChange.upsert("sample", old_resource, old_json),
            ])
            source_database = provenance_database_path(dataset)
            staging_database = provenance_database_path(staging)
            staging_database.parent.mkdir()
            os.link(source_database, staging_database)
            self.assertTrue(os.path.samefile(source_database, staging_database))

            apply_provenance_changes(staging, [
                ReplaceProvenanceChange.upsert("sample", new_resource, new_json),
            ])
            self.assertFalse(os.path.samefile(source_database, staging_database))
            with DatasetReplaceProvenance.open(dataset) as provenance:
                self.assertTrue(provenance.matches("sample", old_resource, old_json))
                self.assertFalse(provenance.matches("sample", new_resource, new_json))
            with DatasetReplaceProvenance.open(staging) as provenance:
                self.assertTrue(provenance.matches("sample", new_resource, new_json))

    def test_database_survives_a_complete_dataset_copy_without_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "source-dataset"
            copied = root / "copied-dataset"
            dataset.mkdir()
            resource, json_digest = _digest(b"resource"), _digest(b"json")
            apply_provenance_changes(dataset, [
                ReplaceProvenanceChange.upsert("nested\\sample", resource, json_digest),
            ])
            shutil.copytree(dataset, copied)
            with DatasetReplaceProvenance.open(copied) as provenance:
                self.assertTrue(provenance.matches("nested\\sample", resource, json_digest))

    def test_corrupt_schema_sidecar_and_record_are_rejected(self) -> None:
        cases = ("corrupt", "schema", "sidecar", "orphan_sidecar", "record")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                dataset = Path(temporary) / "dataset"
                dataset.mkdir()
                path = provenance_database_path(dataset)
                path.parent.mkdir()
                resource, json_digest = _digest(b"resource"), _digest(b"json")
                if case == "corrupt":
                    path.write_bytes(b"not a SQLite database")
                elif case == "schema":
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version={USER_VERSION}")
                        connection.execute("CREATE TABLE replace_provenance(annotation_key TEXT PRIMARY KEY)")
                        connection.commit()
                elif case == "orphan_sidecar":
                    path.with_name(path.name + "-wal").write_bytes(b"unresolved")
                else:
                    apply_provenance_changes(dataset, [
                        ReplaceProvenanceChange.upsert("sample", resource, json_digest),
                    ])
                    if case == "sidecar":
                        path.with_name(path.name + "-wal").write_bytes(b"unresolved")
                    else:
                        with closing(sqlite3.connect(path)) as connection:
                            connection.execute(
                                "UPDATE replace_provenance SET json_sha256='invalid' WHERE annotation_key='sample'",
                            )
                            connection.commit()
                with self.assertRaises(ReplaceProvenanceError):
                    with DatasetReplaceProvenance.open(dataset) as provenance:
                        provenance.matches("sample", resource, json_digest)

    def test_metadata_directory_collision_is_rejected_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            (dataset / ".anima-idg").write_bytes(b"path collision")
            with self.assertRaises(ReplaceProvenanceError):
                DatasetReplaceProvenance.open(dataset)
            with self.assertRaises(ReplaceProvenanceError):
                apply_provenance_changes(dataset, [
                    ReplaceProvenanceChange.upsert("sample", _digest(b"resource"), _digest(b"json")),
                ])

    def test_invalid_change_does_not_create_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            with self.assertRaises(ReplaceProvenanceError):
                apply_provenance_changes(dataset, [
                    ReplaceProvenanceChange.upsert("sample", "bad", _digest(b"json")),
                ])
            self.assertFalse(provenance_database_path(dataset).exists())


if __name__ == "__main__":
    unittest.main()
