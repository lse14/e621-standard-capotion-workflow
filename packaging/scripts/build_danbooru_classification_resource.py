"""Build a deterministic Danbooru classification/count resource from local snapshots."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "core" / "src"))

from anima_core.classify_resource import wiki_schema_fingerprint
from anima_core.contracts import canonical_json
from anima_core.path_safety import sha256_file
from anima_core.resource_catalog import ResourcePackage


DEFAULT_RESOURCE_ID = "danbooru-classify-20260727-v1"
RUNTIME_FORMAT = "danbooru-classification-index-v1"
WIKI_APPLICATION_ID = int.from_bytes(b"AND2", "big")
WIKI_SCHEMA_VERSION = 1
RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

CL_CATEGORY_ROUTES: dict[str, str | None] = {
    "general": None,
    "character": "character",
    "copyright": "series",
    "meta": "drop",
    "rating": "drop",
    "quality": "drop",
}
WD_CATEGORY_ROUTES: dict[str, str | None] = {
    "0": None,
    "4": "character",
    "9": "drop",
}
SITE_CATEGORY_ROUTES = {
    0: "tags",
    1: "drop",
    3: "series",
    4: "character",
    5: "drop",
}
OVERLAY_BUCKETS = frozenset({"appearance", "environment", "tags"})


def _family(prefix: str, singular: str, plural: str) -> dict[str, dict[str, int | None]]:
    return {
        f"1{singular}": {"min": 1, "max": 1},
        **{
            f"{value}{plural}": {"min": value, "max": value}
            for value in range(2, 6)
        },
        f"6+{plural}": {"min": 6, "max": None},
    }


COUNT_RULES: dict[str, object] = {
    "schemaVersion": 1,
    "profile": "danbooru",
    "families": {
        "girl": _family("girl", "girl", "girls"),
        "boy": _family("boy", "boy", "boys"),
        "other": _family("other", "other", "others"),
    },
    "lowerBounds": {
        "multiple_girls": {"family": "girl", "min": 2},
        "multiple_boys": {"family": "boy", "min": 2},
        "multiple_others": {"family": "other", "min": 2},
    },
    "fallbacks": {"solo": "solo"},
    "nonDecisive": ["solo_focus"],
}
EXACT_COUNTERS = frozenset(
    tag
    for family in COUNT_RULES["families"].values()  # type: ignore[union-attr]
    for tag in family
)
LOWER_BOUND_TAGS = frozenset(COUNT_RULES["lowerBounds"])  # type: ignore[arg-type]
FALLBACK_TAGS = frozenset(COUNT_RULES["fallbacks"])  # type: ignore[arg-type]
NON_DECISIVE_TAGS = frozenset(COUNT_RULES["nonDecisive"])  # type: ignore[arg-type]
COUNT_TAGS = EXACT_COUNTERS | LOWER_BOUND_TAGS | FALLBACK_TAGS | NON_DECISIVE_TAGS
REQUIRED_WIKI_TITLES = tuple(sorted(COUNT_TAGS))


class DanbooruResourceBuildError(ValueError):
    pass


def _read_bytes(path: Path, field: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DanbooruResourceBuildError(f"unable to read {field}: {path}") from exc
    if not data:
        raise DanbooruResourceBuildError(f"{field} is empty")
    return data


def _read_json(path: Path, field: str) -> tuple[object, bytes]:
    data = _read_bytes(path, field)
    try:
        return json.loads(data.decode("utf-8-sig")), data
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DanbooruResourceBuildError(f"{field} is not strict UTF-8 JSON") from exc


def _text(value: object, field: str, *, max_bytes: int = 2_048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise DanbooruResourceBuildError(f"{field} is invalid")
    return value


def _tag(value: object, field: str) -> str:
    result = _text(value, field, max_bytes=512)
    if any(character in result for character in ",\r\n"):
        raise DanbooruResourceBuildError(f"{field} contains a caption delimiter")
    return result


def _https_url(value: object, field: str) -> str:
    result = _text(value, field)
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise DanbooruResourceBuildError(f"{field} must be an absolute HTTPS URL without credentials")
    return result


def _snapshot_header(value: object, field: str, collection: str) -> tuple[dict[str, object], list[object]]:
    required = {"schemaVersion", "source", "snapshotId", "sourceUrl", collection}
    if not isinstance(value, dict) or set(value) != required:
        raise DanbooruResourceBuildError(f"{field} fields are invalid")
    if value["schemaVersion"] != 1 or value["source"] != "danbooru":
        raise DanbooruResourceBuildError(f"{field} identity is invalid")
    rows = value[collection]
    if not isinstance(rows, list) or not rows:
        raise DanbooruResourceBuildError(f"{field}.{collection} must be a non-empty array")
    return {
        "snapshotId": _text(value["snapshotId"], f"{field}.snapshotId", max_bytes=512),
        "sourceUrl": _https_url(value["sourceUrl"], f"{field}.sourceUrl"),
    }, rows


def _catalog(
    path: Path,
) -> tuple[dict[str, int], dict[str, str], dict[str, object], bytes]:
    value, data = _read_json(path, "Danbooru catalog snapshot")
    required = {"schemaVersion", "source", "snapshotId", "sourceUrl", "tags", "aliases"}
    if not isinstance(value, dict) or set(value) != required:
        raise DanbooruResourceBuildError("Danbooru catalog snapshot fields are invalid")
    header, rows = _snapshot_header(
        {key: value[key] for key in required - {"aliases"}},
        "Danbooru catalog snapshot",
        "tags",
    )
    categories: dict[str, int] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise DanbooruResourceBuildError(f"catalog tag {index} is invalid")
        name = _tag(raw.get("name"), f"catalog tag {index}.name")
        category = raw.get("category")
        if type(category) is not int or category not in SITE_CATEGORY_ROUTES:
            raise DanbooruResourceBuildError(f"catalog tag {name} has an unsupported category")
        if name in categories:
            raise DanbooruResourceBuildError(f"catalog contains duplicate tag: {name}")
        categories[name] = category

    raw_aliases = value["aliases"]
    if not isinstance(raw_aliases, list):
        raise DanbooruResourceBuildError("catalog aliases must be an array")
    aliases: dict[str, str] = {}
    for index, raw in enumerate(raw_aliases):
        if not isinstance(raw, dict):
            raise DanbooruResourceBuildError(f"catalog alias {index} is invalid")
        status = _text(raw.get("status"), f"catalog alias {index}.status", max_bytes=64)
        if status != "active":
            continue
        antecedent = _tag(raw.get("antecedent_name"), f"catalog alias {index}.antecedent_name")
        consequent = _tag(raw.get("consequent_name"), f"catalog alias {index}.consequent_name")
        if consequent not in categories:
            raise DanbooruResourceBuildError(f"catalog alias target is missing: {consequent}")
        previous = aliases.setdefault(antecedent, consequent)
        if previous != consequent:
            raise DanbooruResourceBuildError(f"catalog alias has multiple active targets: {antecedent}")
    return categories, aliases, header, data


def _wiki_pages(path: Path) -> tuple[dict[str, str], dict[str, object], bytes]:
    value, data = _read_json(path, "Danbooru Wiki snapshot")
    header, rows = _snapshot_header(value, "Danbooru Wiki snapshot", "pages")
    pages: dict[str, str] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise DanbooruResourceBuildError(f"Wiki page {index} is invalid")
        title = _tag(raw.get("title"), f"Wiki page {index}.title")
        body = _text(raw.get("body"), f"Wiki page {title}.body", max_bytes=4_194_304)
        if title in pages:
            raise DanbooruResourceBuildError(f"Wiki snapshot contains duplicate page: {title}")
        pages[title] = body
    missing = sorted(set(REQUIRED_WIKI_TITLES) - set(pages))
    if missing:
        raise DanbooruResourceBuildError("Wiki snapshot is missing count pages: " + ", ".join(missing))
    return {title: pages[title] for title in REQUIRED_WIKI_TITLES}, header, data


def _overlay(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    value, _ = _read_json(path, "Danbooru classification overlay")
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "source", "entries"}
        or value["schemaVersion"] != 1
        or value["source"] != "audited-danbooru-general-overlay"
        or not isinstance(value["entries"], dict)
    ):
        raise DanbooruResourceBuildError("Danbooru classification overlay fields are invalid")
    result: dict[str, str] = {}
    for raw_tag, raw in value["entries"].items():
        name = _tag(raw_tag, "overlay tag")
        if not isinstance(raw, dict) or set(raw) != {"bucket", "evidence"}:
            raise DanbooruResourceBuildError(f"overlay entry is invalid: {name}")
        bucket = raw["bucket"]
        if bucket not in OVERLAY_BUCKETS:
            raise DanbooruResourceBuildError(f"overlay bucket is invalid: {name}")
        _text(raw["evidence"], f"overlay evidence for {name}", max_bytes=4_096)
        result[name] = str(bucket)
    return result


def _indexed_names(value: object, expected_count: int) -> tuple[str, ...]:
    if isinstance(value, list):
        raw_names = value
    elif isinstance(value, dict) and set(value) == {str(index) for index in range(expected_count)}:
        raw_names = [value[str(index)] for index in range(expected_count)]
    else:
        raise DanbooruResourceBuildError("CL vocabulary idx_to_tag is invalid")
    names = tuple(_tag(name, "CL vocabulary tag") for name in raw_names)
    if len(names) != expected_count or len(set(names)) != len(names):
        raise DanbooruResourceBuildError("CL vocabulary tag count or uniqueness is invalid")
    return names


def _cl_vocabulary(path: Path, expected_count: int) -> dict[str, str | None]:
    value, _ = _read_json(path, "CL vocabulary")
    if not isinstance(value, dict) or not {"idx_to_tag", "tag_to_idx", "tag_to_category"}.issubset(value):
        raise DanbooruResourceBuildError("CL vocabulary fields are incomplete")
    names = _indexed_names(value["idx_to_tag"], expected_count)
    indexes = value["tag_to_idx"]
    categories = value["tag_to_category"]
    if not isinstance(indexes, dict) or set(indexes) != set(names):
        raise DanbooruResourceBuildError("CL vocabulary tag_to_idx is invalid")
    if any(type(indexes[name]) is not int or indexes[name] != index for index, name in enumerate(names)):
        raise DanbooruResourceBuildError("CL vocabulary indices are inconsistent")
    if not isinstance(categories, dict) or set(categories) != set(names):
        raise DanbooruResourceBuildError("CL vocabulary tag_to_category is invalid")
    routes: dict[str, str | None] = {}
    for name in names:
        category = categories[name]
        if not isinstance(category, str) or category.lower() not in CL_CATEGORY_ROUTES:
            raise DanbooruResourceBuildError(f"CL vocabulary category is invalid: {name}")
        routes[name] = CL_CATEGORY_ROUTES[category.lower()]
    return routes


def _wd_vocabulary(path: Path, expected_count: int) -> dict[str, str | None]:
    routes: dict[str, str | None] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not {"name", "category"}.issubset(reader.fieldnames):
                raise DanbooruResourceBuildError("WD selected_tags.csv columns are incomplete")
            for row in reader:
                name = _tag(row.get("name"), "WD vocabulary tag")
                category = row.get("category")
                if category not in WD_CATEGORY_ROUTES or name in routes:
                    raise DanbooruResourceBuildError(f"WD vocabulary row is invalid: {name}")
                routes[name] = WD_CATEGORY_ROUTES[category]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DanbooruResourceBuildError("WD selected_tags.csv is unreadable") from exc
    if len(routes) != expected_count:
        raise DanbooruResourceBuildError("WD vocabulary tag count is invalid")
    return routes


def _tagger_vocabulary(
    resource_library: Path,
    manifest_path: Path,
) -> tuple[str, str, dict[str, str | None]]:
    package = ResourcePackage.load(resource_library, manifest_path.resolve(), "tagging-model")
    if package.profile != "danbooru" or package.runtime_format not in {
        "cl-tagger-v2-onnx-v1",
        "wd-eva02-large-tagger-v3-onnx-v1",
    }:
        raise DanbooruResourceBuildError(f"unsupported Danbooru tagger: {package.resource_id}")
    role = "vocabulary" if package.runtime_format == "cl-tagger-v2-onnx-v1" else "selectedTags"
    vocabulary = package.entrypoint(role)
    fingerprint = sha256_file(vocabulary)
    if fingerprint != package.metadata["vocabularyFingerprint"]:
        raise DanbooruResourceBuildError(f"tagger vocabulary digest mismatch: {package.resource_id}")
    count = int(package.metadata["tagCount"])
    routes = (
        _cl_vocabulary(vocabulary, count)
        if package.runtime_format == "cl-tagger-v2-onnx-v1"
        else _wd_vocabulary(vocabulary, count)
    )
    return package.runtime_format, fingerprint, routes


def _merge_model_routes(
    sources: list[tuple[str, str, dict[str, str | None]]],
) -> tuple[dict[str, str | None], list[str]]:
    by_tag: dict[str, set[str]] = {}
    all_tags: set[str] = set()
    fingerprints: list[str] = []
    runtimes: set[str] = set()
    for runtime, fingerprint, routes in sources:
        if runtime in runtimes:
            raise DanbooruResourceBuildError(f"duplicate tagger runtime: {runtime}")
        runtimes.add(runtime)
        fingerprints.append(fingerprint)
        for tag, route in routes.items():
            all_tags.add(tag)
            if route is not None:
                by_tag.setdefault(tag, set()).add(route)
    conflicts = {tag: routes for tag, routes in by_tag.items() if len(routes) > 1}
    if conflicts:
        first = sorted(conflicts)[0]
        raise DanbooruResourceBuildError(
            f"tagger category routes conflict for {first}: {', '.join(sorted(conflicts[first]))}"
        )
    return {tag: next(iter(by_tag.get(tag, ())), None) for tag in all_tags}, sorted(fingerprints)


def _dictionary(
    categories: dict[str, int],
    aliases: dict[str, str],
    model_routes: dict[str, str | None],
    overlay: dict[str, str],
    *,
    snapshot: str,
    source_url: str,
    source_size: int,
    source_sha256: str,
    vocabulary_fingerprints: list[str],
) -> dict[str, object]:
    all_tags = set(categories) | set(aliases)
    missing_vocabulary = sorted(set(model_routes) - all_tags)
    if missing_vocabulary:
        preview = ", ".join(missing_vocabulary[:10])
        raise DanbooruResourceBuildError(
            f"catalog snapshot is missing {len(missing_vocabulary)} tagger labels: {preview}"
        )
    missing_rules = sorted(COUNT_TAGS - all_tags)
    if missing_rules:
        raise DanbooruResourceBuildError("catalog snapshot is missing count tags: " + ", ".join(missing_rules))
    unknown_overlay = sorted(set(overlay) - all_tags)
    if unknown_overlay:
        raise DanbooruResourceBuildError("overlay contains unknown tags: " + ", ".join(unknown_overlay))

    entries: dict[str, dict[str, str]] = {}
    for tag in sorted(all_tags):
        canonical = aliases.get(tag, tag)
        category = categories.get(tag, categories[canonical])
        site_route = SITE_CATEGORY_ROUTES[category]
        model_route = model_routes.get(tag)
        if tag in COUNT_TAGS:
            if model_route not in {None, "tags"} or site_route != "tags":
                raise DanbooruResourceBuildError(f"count tag category is inconsistent: {tag}")
            bucket, method = "tags", "count_rule"
        elif model_route is not None:
            if site_route != "tags" and site_route != model_route:
                raise DanbooruResourceBuildError(f"model/site category routes conflict: {tag}")
            bucket, method = model_route, "model_category"
        elif tag in overlay:
            if site_route != "tags":
                raise DanbooruResourceBuildError(f"overlay may only classify General tags: {tag}")
            bucket, method = overlay[tag], "audited_overlay"
        elif site_route == "tags":
            bucket, method = "tags", "general_fallback"
        else:
            bucket = site_route
            method = "site_alias_category" if tag in aliases else "site_category"
        entries[tag] = {
            "canonical": canonical,
            "bucket": bucket,
            "output": tag,
            "method": method,
        }
    return {
        "metadata": {
            "schemaVersion": 1,
            "source": "danbooru",
            "entryCount": len(entries),
            "catalogSnapshot": snapshot,
            "catalogSourceUrl": source_url,
            "catalogSourceSizeBytes": source_size,
            "catalogSourceSha256": source_sha256,
            "supportedVocabularyFingerprints": vocabulary_fingerprints,
        },
        "entries": entries,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8", newline="\n")


def _write_wiki_database(
    path: Path,
    pages: dict[str, str],
    *,
    resource_id: str,
    wiki_data_source_id: str,
) -> str:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA application_id={WIKI_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={WIKI_SCHEMA_VERSION}")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE resource_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE wiki_catalog (title TEXT PRIMARY KEY, body TEXT NOT NULL) WITHOUT ROWID"
        )
        schema_fingerprint = wiki_schema_fingerprint(connection)
        titles = sorted(pages)
        metadata = {
            "resource_id": resource_id,
            "wiki_data_source_id": wiki_data_source_id,
            "schema_version": str(WIKI_SCHEMA_VERSION),
            "schema_fingerprint": schema_fingerprint,
            "page_titles_sha256": hashlib.sha256(canonical_json(titles).encode("utf-8")).hexdigest(),
        }
        connection.executemany(
            "INSERT INTO resource_metadata(key, value) VALUES (?, ?)", sorted(metadata.items())
        )
        connection.executemany(
            "INSERT INTO wiki_catalog(title, body) VALUES (?, ?)",
            ((title, pages[title]) for title in titles),
        )
        connection.commit()
        connection.execute("VACUUM")
        return schema_fingerprint
    except sqlite3.Error as exc:
        raise DanbooruResourceBuildError("unable to build the Wiki projection") from exc
    finally:
        connection.close()


def _file_record(path: Path) -> dict[str, object]:
    return {"sizeBytes": path.stat().st_size, "sha256": sha256_file(path)}


def build_resource(
    resource_library: Path,
    catalog_snapshot: Path,
    wiki_snapshot: Path,
    tagger_manifests: list[Path],
    *,
    resource_id: str = DEFAULT_RESOURCE_ID,
    resource_version: str,
    wiki_data_source_id: str,
    overlay_path: Path | None = None,
) -> dict[str, object]:
    if not RESOURCE_ID.fullmatch(resource_id) or not RESOURCE_ID.fullmatch(wiki_data_source_id):
        raise DanbooruResourceBuildError("resource or Wiki data source ID is invalid")
    _text(resource_version, "resourceVersion", max_bytes=512)
    root = resource_library.resolve(strict=True)
    if not root.is_dir():
        raise DanbooruResourceBuildError("resource library is not a directory")
    if not tagger_manifests:
        raise DanbooruResourceBuildError("at least one installed Danbooru tagger manifest is required")

    categories, aliases, catalog_header, catalog_data = _catalog(catalog_snapshot)
    pages, wiki_header, wiki_data = _wiki_pages(wiki_snapshot)
    overlay = _overlay(overlay_path)
    sources = [_tagger_vocabulary(root, path) for path in tagger_manifests]
    model_routes, vocabulary_fingerprints = _merge_model_routes(sources)
    catalog_digest = hashlib.sha256(catalog_data).hexdigest()
    wiki_digest = hashlib.sha256(wiki_data).hexdigest()
    dictionary = _dictionary(
        categories,
        aliases,
        model_routes,
        overlay,
        snapshot=str(catalog_header["snapshotId"]),
        source_url=str(catalog_header["sourceUrl"]),
        source_size=len(catalog_data),
        source_sha256=catalog_digest,
        vocabulary_fingerprints=vocabulary_fingerprints,
    )

    category_root = root / "classification-indexes"
    category_root.mkdir(exist_ok=True)
    target = category_root / resource_id
    if target.exists():
        raise DanbooruResourceBuildError(f"resource destination already exists: {target}")
    staging = Path(tempfile.mkdtemp(prefix=f".{resource_id}-", dir=category_root))
    try:
        dictionary_path = staging / "danbooru_tag_dictionary.json"
        rules_path = staging / "danbooru_count_rules.json"
        database_path = staging / "danbooru_count_wiki.sqlite3"
        _write_json(dictionary_path, dictionary)
        _write_json(rules_path, COUNT_RULES)
        schema_fingerprint = _write_wiki_database(
            database_path,
            pages,
            resource_id=resource_id,
            wiki_data_source_id=wiki_data_source_id,
        )
        files = {
            path.name: _file_record(path)
            for path in (dictionary_path, rules_path, database_path)
        }
        manifest = {
            "schemaVersion": 2,
            "kind": "classification-index",
            "resourceId": resource_id,
            "resourceVersion": resource_version,
            "profile": "danbooru",
            "displayName": {
                "zh-CN": "Danbooru 分类与 Count 索引",
                "en": "Danbooru Classification and Count Index",
            },
            "description": {
                "zh-CN": "按 Danbooru 类别路由标签，并用独立规则生成可复核 Count 证据。",
                "en": "Routes Danbooru tags and provides independent, reviewable count evidence.",
            },
            "runtimeFormat": RUNTIME_FORMAT,
            "distribution": {"mode": "bundled"},
            "entrypoints": {
                "dictionary": dictionary_path.name,
                "countRules": rules_path.name,
                "countDatabase": database_path.name,
            },
            "files": files,
            "metadata": {
                "dictionaryEntryCount": len(dictionary["entries"]),  # type: ignore[arg-type]
                "wikiDataSourceId": wiki_data_source_id,
                "wikiApplicationId": WIKI_APPLICATION_ID,
                "wikiSchemaVersion": WIKI_SCHEMA_VERSION,
                "wikiSchemaFingerprint": schema_fingerprint,
                "wikiPageTitles": list(REQUIRED_WIKI_TITLES),
                "catalogSnapshot": catalog_header["snapshotId"],
                "catalogSourceUrl": catalog_header["sourceUrl"],
                "catalogSourceSizeBytes": len(catalog_data),
                "catalogSourceSha256": catalog_digest,
                "wikiSourceUrl": wiki_header["sourceUrl"],
                "wikiSourceSizeBytes": len(wiki_data),
                "wikiSourceSha256": wiki_digest,
                "supportedVocabularyFingerprints": vocabulary_fingerprints,
            },
            "documentation": [],
        }
        _write_json(staging / "resource.json", manifest)
        package = ResourcePackage.load(root, staging / "resource.json", "classification-index")
        package.verify_files(verify_hashes=True)
        os.replace(staging, target)
        final_package = ResourcePackage.load(root, target / "resource.json", "classification-index")
        final_package.verify_files(verify_hashes=True)
        return {
            "schemaVersion": 1,
            "resourceId": final_package.resource_id,
            "resourceVersion": final_package.resource_version,
            "resourceManifest": str((target / "resource.json").resolve()),
            "resourceFingerprint": final_package.fingerprint,
            "dictionaryEntryCount": len(dictionary["entries"]),  # type: ignore[arg-type]
            "wikiPageCount": len(REQUIRED_WIKI_TITLES),
            "supportedVocabularyFingerprints": vocabulary_fingerprints,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-library", type=Path, required=True)
    parser.add_argument("--catalog-snapshot", type=Path, required=True)
    parser.add_argument("--wiki-snapshot", type=Path, required=True)
    parser.add_argument("--tagger-manifest", type=Path, action="append", required=True)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--resource-id", default=DEFAULT_RESOURCE_ID)
    parser.add_argument("--resource-version", required=True)
    parser.add_argument("--wiki-data-source-id", required=True)
    arguments = parser.parse_args()
    result = build_resource(
        arguments.resource_library,
        arguments.catalog_snapshot,
        arguments.wiki_snapshot,
        arguments.tagger_manifest,
        resource_id=arguments.resource_id,
        resource_version=arguments.resource_version,
        wiki_data_source_id=arguments.wiki_data_source_id,
        overlay_path=arguments.overlay,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
