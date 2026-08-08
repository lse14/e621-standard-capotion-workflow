from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "classify" / "src"))

from anima_classify_worker.resource import ClassifyResourceError as WorkerResourceError
from anima_classify_worker.resource import load_classify_resource
from anima_core.classify_resource import (
    CLASSIFY_DICTIONARY_ENTRY_COUNT,
    CLASSIFY_RESOURCE_ID,
    CLASSIFY_REQUIRED_WIKI_PAGE_TITLES,
    CLASSIFY_WIKI_APPLICATION_ID,
    CLASSIFY_WIKI_DATA_SOURCE_ID,
    CLASSIFY_WIKI_SCHEMA_VERSION,
    ClassifyResourceError,
    load_classify_resource_from_install,
    missing_required_wiki_titles,
    wiki_schema_fingerprint,
)
from anima_core.contracts import canonical_json


MANIFEST_RELATIVE = "manifests\\resources\\classify-e621.json"
RESOURCE_RELATIVE = "resources\\e621\\classify\\test-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(root: Path, resource_root: Path, *, page_titles: list[str]) -> str:
    files = {
        name: {"sizeBytes": (resource_root / name).stat().st_size, "sha256": _sha256(resource_root / name)}
        for name in ("e621_count_wiki.sqlite3", "e621_tag_dictionary.json")
    }
    connection = sqlite3.connect(resource_root / "e621_count_wiki.sqlite3")
    try:
        schema_fingerprint = wiki_schema_fingerprint(connection)
    finally:
        connection.close()
    unsigned = {
        "schemaVersion": 1,
        "resourceId": CLASSIFY_RESOURCE_ID,
        "owner": "classify",
        "profile": "e621",
        "resourceVersion": "test-v1",
        "rootRelativePath": RESOURCE_RELATIVE,
        "dictionaryEntryCount": CLASSIFY_DICTIONARY_ENTRY_COUNT,
        "wikiDataSourceId": CLASSIFY_WIKI_DATA_SOURCE_ID,
        "wikiApplicationId": CLASSIFY_WIKI_APPLICATION_ID,
        "wikiSchemaVersion": CLASSIFY_WIKI_SCHEMA_VERSION,
        "wikiSchemaFingerprint": schema_fingerprint,
        "wikiPageTitles": page_titles,
        "files": files,
    }
    fingerprint = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    path = root / Path(MANIFEST_RELATIVE.replace("\\", "/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json({**unsigned, "fingerprint": fingerprint}) + "\n", encoding="utf-8")
    return fingerprint


def _build_install(root: Path, *, source: str = "e621", page_titles: list[str] | None = None) -> str:
    resource_root = root / Path(RESOURCE_RELATIVE.replace("\\", "/"))
    resource_root.mkdir(parents=True)
    dictionary = {
        "metadata": {"source": source, "entry_count": CLASSIFY_DICTIONARY_ENTRY_COUNT},
        "entries": {f"tag_{index}": None for index in range(CLASSIFY_DICTIONARY_ENTRY_COUNT)},
    }
    (resource_root / "e621_tag_dictionary.json").write_text(canonical_json(dictionary), encoding="utf-8")
    page_titles = page_titles or sorted(CLASSIFY_REQUIRED_WIKI_PAGE_TITLES)
    database = resource_root / "e621_count_wiki.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(f"PRAGMA application_id={CLASSIFY_WIKI_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={CLASSIFY_WIKI_SCHEMA_VERSION}")
        connection.execute("CREATE TABLE resource_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")
        connection.execute("CREATE TABLE wiki_catalog (title TEXT PRIMARY KEY, body TEXT NOT NULL) WITHOUT ROWID")
        fingerprint = wiki_schema_fingerprint(connection)
        metadata = {
            "resource_id": CLASSIFY_RESOURCE_ID,
            "wiki_data_source_id": CLASSIFY_WIKI_DATA_SOURCE_ID,
            "schema_version": str(CLASSIFY_WIKI_SCHEMA_VERSION),
            "schema_fingerprint": fingerprint,
            "page_titles_sha256": hashlib.sha256(canonical_json(page_titles).encode("utf-8")).hexdigest(),
        }
        connection.executemany("INSERT INTO resource_metadata(key, value) VALUES (?, ?)", sorted(metadata.items()))
        connection.executemany(
            "INSERT INTO wiki_catalog(title, body) VALUES (?, ?)",
            ((title, f"evidence for {title}") for title in page_titles),
        )
        connection.commit()
    finally:
        connection.close()
    return _write_manifest(root, resource_root, page_titles=page_titles)


class ClassifyResourceTests(unittest.TestCase):
    def test_core_and_worker_accept_exact_read_only_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = _build_install(root)
            manifest, files = load_classify_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            worker = load_classify_resource(root, MANIFEST_RELATIVE, fingerprint)
            try:
                self.assertEqual(CLASSIFY_DICTIONARY_ENTRY_COUNT, manifest.dictionaryEntryCount)
                self.assertEqual({"e621_count_wiki.sqlite3", "e621_tag_dictionary.json"}, set(files))
                self.assertEqual(CLASSIFY_WIKI_DATA_SOURCE_ID, worker.wiki_data_source_id)
                self.assertEqual(len(CLASSIFY_REQUIRED_WIKI_PAGE_TITLES), worker.wiki_connection.execute("SELECT COUNT(*) FROM wiki_catalog").fetchone()[0])
                self.assertFalse((files["e621_count_wiki.sqlite3"].with_name("e621_count_wiki.sqlite3-wal")).exists())
                self.assertFalse((files["e621_count_wiki.sqlite3"].with_name("e621_count_wiki.sqlite3-shm")).exists())
            finally:
                worker.close()

    def test_digest_tampering_and_unknown_root_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = _build_install(root)
            dictionary = root / Path(RESOURCE_RELATIVE.replace("\\", "/")) / "e621_tag_dictionary.json"
            dictionary.write_bytes(dictionary.read_bytes() + b" ")
            with self.assertRaises(ClassifyResourceError):
                load_classify_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            with self.assertRaises(WorkerResourceError):
                load_classify_resource(root, MANIFEST_RELATIVE, fingerprint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = _build_install(root)
            resource = root / Path(RESOURCE_RELATIVE.replace("\\", "/"))
            (resource / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(ClassifyResourceError):
                load_classify_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)

    def test_dictionary_source_and_wiki_metadata_mismatch_are_rejected_after_rehash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_install(root, source="danbooru")
            resource = root / Path(RESOURCE_RELATIVE.replace("\\", "/"))
            fingerprint = _write_manifest(root, resource, page_titles=sorted(CLASSIFY_REQUIRED_WIKI_PAGE_TITLES))
            with self.assertRaises(ClassifyResourceError):
                load_classify_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            with self.assertRaises(WorkerResourceError):
                load_classify_resource(root, MANIFEST_RELATIVE, fingerprint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = _build_install(root)
            database = root / Path(RESOURCE_RELATIVE.replace("\\", "/")) / "e621_count_wiki.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("UPDATE resource_metadata SET value='wrong' WHERE key='wiki_data_source_id'")
                connection.commit()
            finally:
                connection.close()
            resource = root / Path(RESOURCE_RELATIVE.replace("\\", "/"))
            fingerprint = _write_manifest(root, resource, page_titles=sorted(CLASSIFY_REQUIRED_WIKI_PAGE_TITLES))
            with self.assertRaises(ClassifyResourceError):
                load_classify_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)

    def test_count_and_relationship_rules_require_matching_wiki_pages(self) -> None:
        missing = sorted(CLASSIFY_REQUIRED_WIKI_PAGE_TITLES - {"crowd"})
        self.assertEqual(("crowd",), missing_required_wiki_titles(missing))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = _build_install(root, page_titles=missing)
            with self.assertRaisesRegex(ClassifyResourceError, "crowd"):
                load_classify_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)


if __name__ == "__main__":
    unittest.main()
