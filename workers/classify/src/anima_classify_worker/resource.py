from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse


MAX_MANIFEST_BYTES = 1_048_576
DEFAULT_RESOURCE_ID = "classify-e621-20260724-v1"
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
WIKI_DATA_SOURCE_ID = "e621-wiki-count-20260724-v1"
DICTIONARY_ENTRY_COUNT = 120_978
WIKI_APPLICATION_ID = 1_095_648_562
WIKI_SCHEMA_VERSION = 1
REQUIRED_FILES = frozenset({"e621_tag_dictionary.json", "e621_count_wiki.sqlite3"})
DANBOORU_RUNTIME_FORMAT = "danbooru-classification-index-v1"
DANBOORU_WIKI_APPLICATION_ID = int.from_bytes(b"AND2", "big")
DANBOORU_REQUIRED_ROLES = frozenset({"dictionary", "countRules", "countDatabase"})
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ClassifyResourceError(ValueError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ClassifyResourceError(f"{field} is invalid")
    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or any(part in {"", ".", ".."} for part in path.parts):
        raise ClassifyResourceError(f"{field} is unsafe")
    return str(path)


def _text(value: object, field: str, *, max_bytes: int = 2_048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise ClassifyResourceError(f"{field} is invalid")
    return value


def _https_url(value: object, field: str) -> str:
    result = _text(value, field)
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ClassifyResourceError(f"{field} is invalid")
    return result


def _is_reparse(path: Path) -> bool:
    info = os.lstat(path)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _resolve_within(root: Path, relative: str, *, directory: bool) -> Path:
    install = Path(os.path.abspath(root))
    if not install.is_dir() or _is_reparse(install):
        raise ClassifyResourceError("install root is missing or is a reparse point")
    target = install / Path(relative.replace("\\", os.sep))
    absolute = Path(os.path.abspath(target))
    try:
        if os.path.commonpath((str(install), str(absolute))) != str(install):
            raise ClassifyResourceError("resource path escapes the install root")
    except ValueError as exc:
        raise ClassifyResourceError("resource path is on another volume") from exc
    current = install
    for part in absolute.relative_to(install).parts:
        current = current / part
        if not current.exists() or _is_reparse(current):
            raise ClassifyResourceError(f"resource path is missing or unsafe: {relative}")
    if directory and not absolute.is_dir():
        raise ClassifyResourceError(f"resource directory is missing: {relative}")
    if not directory and not absolute.is_file():
        raise ClassifyResourceError(f"resource file is missing: {relative}")
    return absolute


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    tables = {
        str(name): str(sql or "")
        for name, sql in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
    }
    expected = {"resource_metadata", "wiki_catalog"}
    if set(tables) != expected or any("WITHOUT ROWID" not in sql.upper() for sql in tables.values()):
        raise ClassifyResourceError("Wiki projection tables are invalid")
    columns = {
        table: [(str(row[1]), str(row[2]).upper(), int(row[5])) for row in connection.execute(f"PRAGMA table_info({table})")]
        for table in sorted(expected)
    }
    if columns != {
        "resource_metadata": [("key", "TEXT", 1), ("value", "TEXT", 0)],
        "wiki_catalog": [("title", "TEXT", 1), ("body", "TEXT", 0)],
    }:
        raise ClassifyResourceError("Wiki projection columns are invalid")
    payload = {
        "applicationId": connection.execute("PRAGMA application_id").fetchone()[0],
        "userVersion": connection.execute("PRAGMA user_version").fetchone()[0],
        "columns": columns,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _manifest(value: object) -> dict[str, object]:
    required = {
        "schemaVersion", "resourceId", "owner", "profile", "resourceVersion", "rootRelativePath",
        "dictionaryEntryCount", "wikiDataSourceId", "wikiApplicationId", "wikiSchemaVersion",
        "wikiSchemaFingerprint", "wikiPageTitles", "files", "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ClassifyResourceError("classify resource manifest fields are invalid")
    if (
        value["schemaVersion"] != 1
        or value["owner"] != "classify"
        or value["profile"] != "e621"
        or value["wikiApplicationId"] != WIKI_APPLICATION_ID
        or value["wikiSchemaVersion"] != WIKI_SCHEMA_VERSION
    ):
        raise ClassifyResourceError("classify resource manifest identity is invalid")
    if (
        not isinstance(value["resourceId"], str) or not RESOURCE_ID.fullmatch(value["resourceId"])
        or not isinstance(value["resourceVersion"], str) or not value["resourceVersion"]
        or type(value["dictionaryEntryCount"]) is not int or value["dictionaryEntryCount"] < 1
        or not isinstance(value["wikiDataSourceId"], str) or not RESOURCE_ID.fullmatch(value["wikiDataSourceId"])
    ):
        raise ClassifyResourceError("resourceVersion is invalid")
    _relative(value["rootRelativePath"], "rootRelativePath")
    if not isinstance(value["wikiSchemaFingerprint"], str) or not SHA256.fullmatch(value["wikiSchemaFingerprint"]):
        raise ClassifyResourceError("wikiSchemaFingerprint is invalid")
    titles = value["wikiPageTitles"]
    if (
        not isinstance(titles, list)
        or not 1 <= len(titles) <= 64
        or titles != sorted(titles)
        or len(titles) != len(set(titles))
        or not all(isinstance(title, str) and title for title in titles)
    ):
        raise ClassifyResourceError("wikiPageTitles is invalid")
    files = value["files"]
    if not isinstance(files, dict) or set(files) != REQUIRED_FILES:
        raise ClassifyResourceError("classify resource file set is invalid")
    for name, record in files.items():
        if "\\" in _relative(name, "resource file path") or not isinstance(record, dict) or set(record) != {"sizeBytes", "sha256"}:
            raise ClassifyResourceError("resource file record is invalid")
        if type(record["sizeBytes"]) is not int or record["sizeBytes"] < 1 or not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"]):
            raise ClassifyResourceError("resource file size or digest is invalid")
    fingerprint = value["fingerprint"]
    if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint):
        raise ClassifyResourceError("resource fingerprint is invalid")
    unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
    if hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest() != fingerprint:
        raise ClassifyResourceError("resource manifest fingerprint is invalid")
    return value


def _catalog_manifest(value: dict[str, object], expected_fingerprint: str) -> tuple[dict[str, object], dict[str, str]]:
    common = {
        "schemaVersion", "kind", "resourceId", "resourceVersion", "profile", "displayName",
        "description", "runtimeFormat", "entrypoints", "files", "metadata", "documentation",
    }
    schema_version = value.get("schemaVersion")
    required = common if schema_version == 1 else common | {"distribution"}
    if set(value) != required or value.get("kind") != "classification-index":
        raise ClassifyResourceError("catalog classification resource identity is invalid")
    profile = value.get("profile")
    runtime_format = value.get("runtimeFormat")
    if schema_version == 1:
        if profile != "e621" or runtime_format != "e621-classification-index-v1":
            raise ClassifyResourceError("catalog classification resource identity is invalid")
        required_roles = frozenset({"dictionary", "countDatabase"})
        metadata_fields = {
            "dictionaryEntryCount", "wikiDataSourceId", "wikiApplicationId", "wikiSchemaVersion",
            "wikiSchemaFingerprint", "wikiPageTitles",
        }
        distribution = None
    elif schema_version == 2:
        if profile != "danbooru" or runtime_format != DANBOORU_RUNTIME_FORMAT:
            raise ClassifyResourceError("catalog classification resource identity is invalid")
        required_roles = DANBOORU_REQUIRED_ROLES
        metadata_fields = {
            "dictionaryEntryCount", "wikiDataSourceId", "wikiApplicationId", "wikiSchemaVersion",
            "wikiSchemaFingerprint", "wikiPageTitles", "catalogSnapshot", "catalogSourceUrl",
            "catalogSourceSizeBytes", "catalogSourceSha256", "wikiSourceUrl", "wikiSourceSizeBytes",
            "wikiSourceSha256", "supportedVocabularyFingerprints",
        }
        distribution = value.get("distribution")
        if distribution != {"mode": "bundled"}:
            raise ClassifyResourceError("Danbooru classification resource distribution is invalid")
    else:
        raise ClassifyResourceError("catalog classification resource identity is invalid")
    resource_id, resource_version = value.get("resourceId"), value.get("resourceVersion")
    entrypoints, records, metadata = value.get("entrypoints"), value.get("files"), value.get("metadata")
    if (
        not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id)
        or not isinstance(resource_version, str) or not resource_version or resource_version != resource_version.strip()
        or not isinstance(entrypoints, dict) or set(entrypoints) != required_roles
        or len(set(entrypoints.values())) != len(entrypoints)
        or not isinstance(records, dict) or set(entrypoints.values()) != set(records)
        or not isinstance(metadata, dict) or set(metadata) != metadata_fields
        or value.get("documentation") != []
    ):
        raise ClassifyResourceError("catalog classification resource metadata is invalid")
    normalized_entrypoints = {
        role: _relative(entrypoints[role], f"entrypoints.{role}") for role in sorted(entrypoints)
    }
    normalized_records: dict[str, dict[str, object]] = {}
    for name, record in records.items():
        normalized = _relative(name, "resource file path")
        if (
            normalized != str(name).replace("/", "\\")
            or normalized in normalized_records
            or not isinstance(record, dict)
            or set(record) != {"sizeBytes", "sha256"}
        ):
            raise ClassifyResourceError("catalog classification file record is invalid")
        if type(record["sizeBytes"]) is not int or record["sizeBytes"] < 1 or not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"]):
            raise ClassifyResourceError("catalog classification file size or digest is invalid")
        normalized_records[normalized] = {"sizeBytes": record["sizeBytes"], "sha256": record["sha256"]}
    titles = metadata.get("wikiPageTitles")
    if (
        type(metadata.get("dictionaryEntryCount")) is not int or metadata["dictionaryEntryCount"] < 1
        or not isinstance(metadata.get("wikiDataSourceId"), str)
        or not RESOURCE_ID.fullmatch(metadata["wikiDataSourceId"])
        or metadata.get("wikiSchemaVersion") != WIKI_SCHEMA_VERSION
        or not isinstance(metadata.get("wikiSchemaFingerprint"), str)
        or not SHA256.fullmatch(metadata["wikiSchemaFingerprint"])
        or not isinstance(titles, list) or not 1 <= len(titles) <= 64
        or titles != sorted(titles) or len(titles) != len(set(titles))
        or not all(isinstance(title, str) and title for title in titles)
    ):
        raise ClassifyResourceError("catalog classification type metadata is invalid")
    expected_application_id = WIKI_APPLICATION_ID if profile == "e621" else DANBOORU_WIKI_APPLICATION_ID
    if metadata.get("wikiApplicationId") != expected_application_id:
        raise ClassifyResourceError("catalog classification Wiki application identity is invalid")
    if profile == "danbooru":
        fingerprints = metadata.get("supportedVocabularyFingerprints")
        if (
            not isinstance(fingerprints, list) or not 1 <= len(fingerprints) <= 16
            or fingerprints != sorted(set(fingerprints))
            or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in fingerprints)
            or type(metadata.get("catalogSourceSizeBytes")) is not int or metadata["catalogSourceSizeBytes"] < 1
            or type(metadata.get("wikiSourceSizeBytes")) is not int or metadata["wikiSourceSizeBytes"] < 1
            or not isinstance(metadata.get("catalogSourceSha256"), str)
            or not SHA256.fullmatch(metadata["catalogSourceSha256"])
            or not isinstance(metadata.get("wikiSourceSha256"), str)
            or not SHA256.fullmatch(metadata["wikiSourceSha256"])
        ):
            raise ClassifyResourceError("Danbooru classification provenance metadata is invalid")
        _text(metadata.get("catalogSnapshot"), "metadata.catalogSnapshot", max_bytes=512)
        _https_url(metadata.get("catalogSourceUrl"), "metadata.catalogSourceUrl")
        _https_url(metadata.get("wikiSourceUrl"), "metadata.wikiSourceUrl")
    unsigned: dict[str, object] = {
        "schemaVersion": schema_version, "kind": "classification-index", "resourceId": resource_id,
        "resourceVersion": resource_version, "profile": profile, "runtimeFormat": runtime_format,
        "entrypoints": normalized_entrypoints,
        "files": {name: normalized_records[name] for name in sorted(normalized_records)},
        "metadata": metadata,
    }
    if distribution is not None:
        unsigned["distribution"] = distribution
    fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if fingerprint != expected_fingerprint:
        raise ClassifyResourceError("catalog classification fingerprint does not match hello")
    manifest = {
        "schemaVersion": schema_version, "resourceId": resource_id, "owner": "classify", "profile": profile,
        "runtimeFormat": runtime_format,
        "resourceVersion": resource_version, "rootRelativePath": ".",
        "dictionaryEntryCount": metadata.get("dictionaryEntryCount"),
        "wikiDataSourceId": metadata.get("wikiDataSourceId"),
        "wikiApplicationId": metadata.get("wikiApplicationId"),
        "wikiSchemaVersion": metadata.get("wikiSchemaVersion"),
        "wikiSchemaFingerprint": metadata.get("wikiSchemaFingerprint"),
        "wikiPageTitles": metadata.get("wikiPageTitles"), "files": normalized_records, "fingerprint": fingerprint,
    }
    if profile == "danbooru":
        manifest["catalogMetadata"] = metadata
    return manifest, normalized_entrypoints


@dataclass
class WorkerClassifyResource:
    resource_id: str
    root: Path
    fingerprint: str
    profile: str
    runtime_format: str
    dictionary: dict[str, object]
    count_rules: dict[str, object] | None
    wiki_connection: sqlite3.Connection
    wiki_data_source_id: str
    wiki_schema_version: int
    wiki_schema_fingerprint: str
    wiki_page_titles: tuple[str, ...]

    def close(self) -> None:
        self.wiki_connection.close()


def load_classify_resource(install_root: Path, manifest_relative_path: str, expected_fingerprint: str) -> WorkerClassifyResource:
    manifest_path = _resolve_within(install_root, _relative(manifest_relative_path, "resource manifest path"), directory=False)
    data = manifest_path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise ClassifyResourceError("classify resource manifest exceeds 1 MiB")
    try:
        raw_manifest = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ClassifyResourceError("classify resource manifest is not strict UTF-8 JSON") from exc
    catalog_manifest = isinstance(raw_manifest, dict) and raw_manifest.get("kind") == "classification-index"
    if catalog_manifest:
        manifest, logical_files = _catalog_manifest(raw_manifest, expected_fingerprint)
        root = manifest_path.parent
    else:
        manifest = _manifest(raw_manifest)
        if manifest["fingerprint"] != expected_fingerprint:
            raise ClassifyResourceError("resource fingerprint does not match hello")
        root = _resolve_within(install_root, _relative(manifest["rootRelativePath"], "rootRelativePath"), directory=True)
        manifest["runtimeFormat"] = "e621-classification-index-v1"
        logical_files = {
            "dictionary": "e621_tag_dictionary.json",
            "countDatabase": "e621_count_wiki.sqlite3",
        }
    try:
        with os.scandir(root) as entries:
            found: set[str] = set()
            for entry in entries:
                path = Path(entry.path)
                if _is_reparse(path) or not entry.is_file(follow_symlinks=False):
                    raise ClassifyResourceError(f"classify resource root contains an unsafe entry: {entry.name}")
                found.add(entry.name)
    except OSError as exc:
        raise ClassifyResourceError("unable to enumerate classify resource root") from exc
    expected_root_files = set(logical_files.values()) | ({"resource.json"} if catalog_manifest else set())
    if found != expected_root_files:
        raise ClassifyResourceError("classify resource root does not match its pinned file set")

    files = manifest["files"]
    assert isinstance(files, dict)
    resolved: dict[str, Path] = {}
    for name, record in files.items():
        assert isinstance(record, dict)
        path = _resolve_within(root, _relative(name, "resource file path"), directory=False)
        if path.stat().st_size != record["sizeBytes"] or _sha256(path) != record["sha256"]:
            raise ClassifyResourceError(f"classify resource file does not match manifest: {name}")
        resolved[_relative(name, "resource file path")] = path
    paths = {role: resolved[relative] for role, relative in logical_files.items()}
    try:
        dictionary = json.loads(paths["dictionary"].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClassifyResourceError("classification dictionary is not strict UTF-8 JSON") from exc
    profile = str(manifest["profile"])
    if not isinstance(dictionary, dict) or set(dictionary) != {"metadata", "entries"}:
        raise ClassifyResourceError("classification dictionary root is invalid")
    dictionary_metadata = dictionary["metadata"]
    entries = dictionary["entries"]
    if profile == "e621":
        dictionary_valid = (
            isinstance(dictionary_metadata, dict)
            and dictionary_metadata.get("source") == "e621"
            and dictionary_metadata.get("entry_count") == manifest["dictionaryEntryCount"]
        )
        count_rules: dict[str, object] | None = None
    else:
        catalog_metadata = manifest.get("catalogMetadata")
        dictionary_valid = (
            isinstance(dictionary_metadata, dict)
            and set(dictionary_metadata) == {
                "schemaVersion", "source", "entryCount", "catalogSnapshot", "catalogSourceUrl",
                "catalogSourceSizeBytes", "catalogSourceSha256", "supportedVocabularyFingerprints",
            }
            and dictionary_metadata.get("schemaVersion") == 1
            and dictionary_metadata.get("source") == "danbooru"
            and dictionary_metadata.get("entryCount") == manifest["dictionaryEntryCount"]
            and isinstance(catalog_metadata, dict)
            and dictionary_metadata.get("catalogSnapshot") == catalog_metadata.get("catalogSnapshot")
            and dictionary_metadata.get("catalogSourceUrl") == catalog_metadata.get("catalogSourceUrl")
            and dictionary_metadata.get("catalogSourceSizeBytes") == catalog_metadata.get("catalogSourceSizeBytes")
            and dictionary_metadata.get("catalogSourceSha256") == catalog_metadata.get("catalogSourceSha256")
            and dictionary_metadata.get("supportedVocabularyFingerprints")
            == catalog_metadata.get("supportedVocabularyFingerprints")
        )
        try:
            raw_rules = json.loads(paths["countRules"].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ClassifyResourceError("Danbooru count rules are not strict UTF-8 JSON") from exc
        if not isinstance(raw_rules, dict):
            raise ClassifyResourceError("Danbooru count rules root is invalid")
        count_rules = raw_rules
    if (
        not dictionary_valid
        or not isinstance(entries, dict)
        or len(entries) != manifest["dictionaryEntryCount"]
    ):
        raise ClassifyResourceError("classification dictionary source or entry count is invalid")
    titles = tuple(manifest["wikiPageTitles"])
    try:
        connection = sqlite3.connect(_file_uri(paths["countDatabase"]), uri=True)
        if (
            connection.execute("PRAGMA application_id").fetchone()[0] != manifest["wikiApplicationId"]
            or connection.execute("PRAGMA user_version").fetchone()[0] != manifest["wikiSchemaVersion"]
            or _schema_fingerprint(connection) != manifest["wikiSchemaFingerprint"]
        ):
            raise ClassifyResourceError("Wiki projection schema identity is invalid")
        metadata = dict(connection.execute("SELECT key, value FROM resource_metadata"))
        expected_metadata = {
            "resource_id": manifest["resourceId"],
            "wiki_data_source_id": manifest["wikiDataSourceId"],
            "schema_version": str(manifest["wikiSchemaVersion"]),
            "schema_fingerprint": manifest["wikiSchemaFingerprint"],
            "page_titles_sha256": hashlib.sha256(_canonical(list(titles)).encode("utf-8")).hexdigest(),
        }
        rows = connection.execute("SELECT title, body FROM wiki_catalog ORDER BY title").fetchall()
        if metadata != expected_metadata or tuple(title for title, _ in rows) != titles or any(not body for _, body in rows):
            raise ClassifyResourceError("Wiki projection metadata or page set is invalid")
    except (sqlite3.Error, ClassifyResourceError):
        if "connection" in locals():
            connection.close()
        raise
    return WorkerClassifyResource(
        resource_id=str(manifest["resourceId"]),
        root=root,
        fingerprint=str(manifest["fingerprint"]),
        profile=profile,
        runtime_format=str(manifest["runtimeFormat"]),
        dictionary=dictionary,
        count_rules=count_rules,
        wiki_connection=connection,
        wiki_data_source_id=str(manifest["wikiDataSourceId"]),
        wiki_schema_version=int(manifest["wikiSchemaVersion"]),
        wiki_schema_fingerprint=str(manifest["wikiSchemaFingerprint"]),
        wiki_page_titles=titles,
    )
