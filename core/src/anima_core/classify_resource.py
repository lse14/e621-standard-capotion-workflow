from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .contracts import canonical_json
from .path_safety import PathSafetyError, canonicalize, ensure_within, safe_relative_path, sha256_file
from .resource_catalog import ResourceCatalogError, ResourcePackage


MAX_RESOURCE_MANIFEST_BYTES = 1_048_576
CLASSIFY_RESOURCE_FILES = frozenset({"e621_tag_dictionary.json", "e621_count_wiki.sqlite3"})
CLASSIFY_RESOURCE_ID = "classify-e621-20260724-v1"
CLASSIFY_WIKI_DATA_SOURCE_ID = "e621-wiki-count-20260724-v1"
CLASSIFY_DICTIONARY_ENTRY_COUNT = 120_978
CLASSIFY_WIKI_APPLICATION_ID = 1_095_648_562  # ASCII ANM2
CLASSIFY_WIKI_SCHEMA_VERSION = 1
CLASSIFY_REQUIRED_WIKI_PAGE_TITLES = frozenset({
    "anthro_on_anthro", "anthro_on_feral", "crowd", "duo", "feral_on_feral", "group",
    "human_on_anthro", "human_on_feral", "human_on_human", "human_on_humanoid",
    "humanoid_on_anthro", "humanoid_on_feral", "humanoid_on_humanoid", "large_group", "solo", "trio",
})
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ClassifyResourceError(ValueError):
    pass


def missing_required_wiki_titles(page_titles: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Count rules and relationship lower bounds must all have Wiki evidence."""
    return tuple(sorted(CLASSIFY_REQUIRED_WIKI_PAGE_TITLES - set(page_titles)))


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ClassifyResourceError(f"{field} must be a relative path")
    try:
        return safe_relative_path(value)
    except PathSafetyError as exc:
        raise ClassifyResourceError(f"{field} is unsafe: {exc}") from exc


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ClassifyResourceError(f"{field} must be a lowercase SHA-256")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ClassifyResourceError(f"{field} must be a positive integer")
    return value


def _resource_text(value: object, field: str, *, max_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_bytes:
        raise ClassifyResourceError(f"{field} is invalid")
    return value


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def wiki_schema_fingerprint(connection: sqlite3.Connection) -> str:
    tables = {
        str(name): str(sql or "")
        for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    }
    expected_tables = {"resource_metadata", "wiki_catalog"}
    if set(tables) != expected_tables or any("WITHOUT ROWID" not in sql.upper() for sql in tables.values()):
        raise ClassifyResourceError("Wiki projection tables are invalid")
    columns: dict[str, list[tuple[str, str, int]]] = {}
    for table in sorted(expected_tables):
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns[table] = [(str(row[1]), str(row[2]).upper(), int(row[5])) for row in rows]
    expected_columns = {
        "resource_metadata": [("key", "TEXT", 1), ("value", "TEXT", 0)],
        "wiki_catalog": [("title", "TEXT", 1), ("body", "TEXT", 0)],
    }
    if columns != expected_columns:
        raise ClassifyResourceError("Wiki projection columns are invalid")
    payload = {
        "applicationId": connection.execute("PRAGMA application_id").fetchone()[0],
        "userVersion": connection.execute("PRAGMA user_version").fetchone()[0],
        "columns": columns,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClassifyResourceFileV1:
    sizeBytes: int
    sha256: str

    @classmethod
    def from_dict(cls, value: object, field: str) -> "ClassifyResourceFileV1":
        if not isinstance(value, dict) or set(value) != {"sizeBytes", "sha256"}:
            raise ClassifyResourceError(f"{field} must contain sizeBytes and sha256")
        return cls(_positive(value["sizeBytes"], f"{field}.sizeBytes"), _sha256(value["sha256"], f"{field}.sha256"))

    def to_dict(self) -> dict[str, object]:
        return {"sizeBytes": self.sizeBytes, "sha256": self.sha256}


@dataclass(frozen=True)
class ClassifyResourceManifestV1:
    resourceId: str
    resourceVersion: str
    rootRelativePath: str
    dictionaryEntryCount: int
    wikiDataSourceId: str
    wikiApplicationId: int
    wikiSchemaVersion: int
    wikiSchemaFingerprint: str
    wikiPageTitles: tuple[str, ...]
    files: dict[str, ClassifyResourceFileV1]
    fingerprint: str
    owner: str = "classify"
    profile: str = "e621"
    schemaVersion: int = 1

    @classmethod
    def from_dict(cls, value: object) -> "ClassifyResourceManifestV1":
        required = {
            "schemaVersion", "resourceId", "owner", "profile", "resourceVersion", "rootRelativePath",
            "dictionaryEntryCount", "wikiDataSourceId", "wikiApplicationId", "wikiSchemaVersion",
            "wikiSchemaFingerprint", "wikiPageTitles", "files", "fingerprint",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ClassifyResourceError("classify resource manifest fields are invalid")
        if (
            value["schemaVersion"] != 1
            or value["resourceId"] != CLASSIFY_RESOURCE_ID
            or value["owner"] != "classify"
            or value["profile"] != "e621"
            or value["dictionaryEntryCount"] != CLASSIFY_DICTIONARY_ENTRY_COUNT
            or value["wikiDataSourceId"] != CLASSIFY_WIKI_DATA_SOURCE_ID
            or value["wikiApplicationId"] != CLASSIFY_WIKI_APPLICATION_ID
            or value["wikiSchemaVersion"] != CLASSIFY_WIKI_SCHEMA_VERSION
        ):
            raise ClassifyResourceError("classify resource manifest identity is invalid")
        resource_id = value["resourceId"]
        if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
            raise ClassifyResourceError("resourceId is invalid")
        page_titles = value["wikiPageTitles"]
        if not isinstance(page_titles, list) or not 1 <= len(page_titles) <= 64:
            raise ClassifyResourceError("wikiPageTitles is invalid")
        titles = tuple(_resource_text(title, "wiki page title", max_bytes=512) for title in page_titles)
        if tuple(sorted(titles)) != titles or len(set(titles)) != len(titles):
            raise ClassifyResourceError("wikiPageTitles must be sorted and unique")
        missing_titles = missing_required_wiki_titles(titles)
        if missing_titles:
            raise ClassifyResourceError(
                "wikiPageTitles is missing count-rule evidence pages: " + ", ".join(missing_titles)
            )
        raw_files = value["files"]
        if not isinstance(raw_files, dict) or set(raw_files) != CLASSIFY_RESOURCE_FILES:
            raise ClassifyResourceError("classify resource file set is invalid")
        files: dict[str, ClassifyResourceFileV1] = {}
        for relative, record in raw_files.items():
            normalized = _safe_relative(relative, "resource file path")
            if "\\" in normalized:
                raise ClassifyResourceError("classify resource files must be direct children")
            files[normalized] = ClassifyResourceFileV1.from_dict(record, f"files.{normalized}")
        manifest = cls(
            resourceId=resource_id,
            resourceVersion=_resource_text(value["resourceVersion"], "resourceVersion"),
            rootRelativePath=_safe_relative(value["rootRelativePath"], "rootRelativePath"),
            dictionaryEntryCount=CLASSIFY_DICTIONARY_ENTRY_COUNT,
            wikiDataSourceId=CLASSIFY_WIKI_DATA_SOURCE_ID,
            wikiApplicationId=CLASSIFY_WIKI_APPLICATION_ID,
            wikiSchemaVersion=CLASSIFY_WIKI_SCHEMA_VERSION,
            wikiSchemaFingerprint=_sha256(value["wikiSchemaFingerprint"], "wikiSchemaFingerprint"),
            wikiPageTitles=titles,
            files=files,
            fingerprint=_sha256(value["fingerprint"], "fingerprint"),
        )
        if manifest.fingerprint != manifest.calculate_fingerprint():
            raise ClassifyResourceError("classify resource manifest fingerprint mismatch")
        return manifest

    @classmethod
    def load(cls, path: str | Path) -> "ClassifyResourceManifestV1":
        target = Path(path)
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ClassifyResourceError(f"unable to read classify resource manifest: {target}") from exc
        if len(data) > MAX_RESOURCE_MANIFEST_BYTES:
            raise ClassifyResourceError("classify resource manifest exceeds 1 MiB")
        try:
            return cls.from_dict(json.loads(data.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClassifyResourceError("classify resource manifest is not strict UTF-8 JSON") from exc

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "resourceId": self.resourceId,
            "owner": self.owner,
            "profile": self.profile,
            "resourceVersion": self.resourceVersion,
            "rootRelativePath": self.rootRelativePath,
            "dictionaryEntryCount": self.dictionaryEntryCount,
            "wikiDataSourceId": self.wikiDataSourceId,
            "wikiApplicationId": self.wikiApplicationId,
            "wikiSchemaVersion": self.wikiSchemaVersion,
            "wikiSchemaFingerprint": self.wikiSchemaFingerprint,
            "wikiPageTitles": list(self.wikiPageTitles),
            "files": {name: self.files[name].to_dict() for name in sorted(self.files)},
        }

    def calculate_fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.unsigned_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "fingerprint": self.fingerprint}

    def resolve_root(self, install_root: str | Path) -> Path:
        try:
            install = canonicalize(install_root, must_exist=True, directory=True).value
            resource = ensure_within(install, install / Path(self.rootRelativePath.replace("\\", os.sep)))
            return canonicalize(resource, must_exist=True, directory=True).value
        except PathSafetyError as exc:
            raise ClassifyResourceError(f"classify resource root is unsafe: {exc}") from exc

    def verify_files(self, install_root: str | Path, *, verify_hashes: bool = True) -> dict[str, Path]:
        root = self.resolve_root(install_root)
        try:
            with os.scandir(root) as entries:
                actual = {entry.name for entry in entries if entry.is_file(follow_symlinks=False)}
            if actual != CLASSIFY_RESOURCE_FILES or len(list(root.iterdir())) != len(CLASSIFY_RESOURCE_FILES):
                raise ClassifyResourceError("classify resource root must contain exactly two pinned files")
        except OSError as exc:
            raise ClassifyResourceError("unable to enumerate classify resource root") from exc
        resolved: dict[str, Path] = {}
        for relative, expected in self.files.items():
            try:
                target = ensure_within(root, root / relative)
                target = canonicalize(target, must_exist=True, directory=False).value
            except PathSafetyError as exc:
                raise ClassifyResourceError(f"classify resource file is unsafe: {relative}") from exc
            if target.stat().st_size != expected.sizeBytes:
                raise ClassifyResourceError(f"classify resource file size mismatch: {relative}")
            if verify_hashes and sha256_file(target) != expected.sha256:
                raise ClassifyResourceError(f"classify resource file digest mismatch: {relative}")
            resolved[relative] = target
        self._verify_dictionary(resolved["e621_tag_dictionary.json"])
        self._verify_wiki(resolved["e621_count_wiki.sqlite3"])
        return resolved

    def _verify_dictionary(self, path: Path) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ClassifyResourceError("classification dictionary is not strict UTF-8 JSON") from exc
        if not isinstance(value, dict) or set(value) != {"metadata", "entries"}:
            raise ClassifyResourceError("classification dictionary root is invalid")
        metadata = value["metadata"]
        entries = value["entries"]
        if (
            not isinstance(metadata, dict)
            or metadata.get("source") != "e621"
            or metadata.get("entry_count") != self.dictionaryEntryCount
            or not isinstance(entries, dict)
            or len(entries) != self.dictionaryEntryCount
        ):
            raise ClassifyResourceError("classification dictionary source or entry count is invalid")

    def _verify_wiki(self, path: Path) -> None:
        try:
            connection = sqlite3.connect(_file_uri(path), uri=True)
        except sqlite3.Error as exc:
            raise ClassifyResourceError("Wiki projection cannot be opened read-only") from exc
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0] != self.wikiApplicationId
                or connection.execute("PRAGMA user_version").fetchone()[0] != self.wikiSchemaVersion
                or wiki_schema_fingerprint(connection) != self.wikiSchemaFingerprint
            ):
                raise ClassifyResourceError("Wiki projection schema identity is invalid")
            metadata = dict(connection.execute("SELECT key, value FROM resource_metadata"))
            expected_metadata = {
                "resource_id": self.resourceId,
                "wiki_data_source_id": self.wikiDataSourceId,
                "schema_version": str(self.wikiSchemaVersion),
                "schema_fingerprint": self.wikiSchemaFingerprint,
                "page_titles_sha256": hashlib.sha256(canonical_json(list(self.wikiPageTitles)).encode("utf-8")).hexdigest(),
            }
            if metadata != expected_metadata:
                raise ClassifyResourceError("Wiki projection metadata is invalid")
            rows = connection.execute("SELECT title, body FROM wiki_catalog ORDER BY title").fetchall()
            if tuple(title for title, _ in rows) != self.wikiPageTitles or any(not isinstance(body, str) or not body for _, body in rows):
                raise ClassifyResourceError("Wiki projection page set is invalid")
        except sqlite3.Error as exc:
            raise ClassifyResourceError("Wiki projection query failed") from exc
        finally:
            connection.close()


@dataclass(frozen=True)
class CatalogClassifyResourceIdentity:
    """Profile-neutral identity needed by Core before the worker's deep load."""

    resourceId: str
    profile: str
    dictionaryEntryCount: int
    wikiDataSourceId: str
    fingerprint: str
    schemaVersion: int


def load_classify_resource_from_install(
    install_root: str | Path,
    manifest_relative_path: str,
    expected_fingerprint: str,
    *,
    verify_hashes: bool = True,
) -> tuple[ClassifyResourceManifestV1 | CatalogClassifyResourceIdentity, dict[str, Path]]:
    try:
        install = canonicalize(install_root, must_exist=True, directory=True).value
        relative = _safe_relative(manifest_relative_path, "resource manifest path")
        manifest_path = ensure_within(install, install / Path(relative.replace("\\", os.sep)))
    except PathSafetyError as exc:
        raise ClassifyResourceError(f"classify resource manifest path is unsafe: {exc}") from exc
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClassifyResourceError("classify resource manifest is unreadable") from exc
    if isinstance(raw, dict) and raw.get("kind") == "classification-index":
        try:
            package = ResourcePackage.load(install, manifest_path, "classification-index")
            if package.fingerprint != _sha256(expected_fingerprint, "expected resource fingerprint"):
                raise ClassifyResourceError("selected classify resource fingerprint does not match the manifest")
            package.verify_files(verify_hashes=verify_hashes)
        except ResourceCatalogError as exc:
            raise ClassifyResourceError(str(exc)) from exc
        metadata = package.metadata
        if package.profile == "danbooru":
            return CatalogClassifyResourceIdentity(
                resourceId=package.resource_id,
                profile=package.profile,
                dictionaryEntryCount=metadata["dictionaryEntryCount"],
                wikiDataSourceId=metadata["wikiDataSourceId"],
                fingerprint=package.fingerprint,
                schemaVersion=package.schema_version,
            ), {
                role: package.entrypoint(role)
                for role in ("dictionary", "countRules", "countDatabase")
            }
        files = {
            "e621_tag_dictionary.json": ClassifyResourceFileV1(
                package.files[package.entrypoints["dictionary"]].size_bytes,
                package.files[package.entrypoints["dictionary"]].sha256,
            ),
            "e621_count_wiki.sqlite3": ClassifyResourceFileV1(
                package.files[package.entrypoints["countDatabase"]].size_bytes,
                package.files[package.entrypoints["countDatabase"]].sha256,
            ),
        }
        manifest = ClassifyResourceManifestV1(
            resourceId=package.resource_id,
            resourceVersion=package.resource_version,
            rootRelativePath=str(package.package_root.relative_to(install)).replace("/", "\\"),
            dictionaryEntryCount=metadata["dictionaryEntryCount"],
            wikiDataSourceId=metadata["wikiDataSourceId"],
            wikiApplicationId=metadata["wikiApplicationId"],
            wikiSchemaVersion=metadata["wikiSchemaVersion"],
            wikiSchemaFingerprint=metadata["wikiSchemaFingerprint"],
            wikiPageTitles=tuple(metadata["wikiPageTitles"]),
            files=files,
            fingerprint=package.fingerprint,
        )
        paths = {
            "e621_tag_dictionary.json": package.entrypoint("dictionary"),
            "e621_count_wiki.sqlite3": package.entrypoint("countDatabase"),
        }
        manifest._verify_dictionary(paths["e621_tag_dictionary.json"])
        manifest._verify_wiki(paths["e621_count_wiki.sqlite3"])
        return manifest, paths
    manifest = ClassifyResourceManifestV1.load(manifest_path)
    if manifest.fingerprint != _sha256(expected_fingerprint, "expected resource fingerprint"):
        raise ClassifyResourceError("selected classify resource fingerprint does not match the manifest")
    return manifest, manifest.verify_files(install, verify_hashes=verify_hashes)
