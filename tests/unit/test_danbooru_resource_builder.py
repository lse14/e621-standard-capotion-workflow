from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "packaging" / "scripts"))

from anima_core.resource_catalog import ResourcePackage, verify_tagger_dictionary_compatibility
from build_danbooru_classification_resource import (
    COUNT_RULES,
    COUNT_TAGS,
    EXACT_COUNTERS,
    REQUIRED_WIKI_TITLES,
    WIKI_APPLICATION_ID,
    DanbooruResourceBuildError,
    build_resource,
)


CL_TAGS = {
    "hatsune_miku": "Character",
    "vocaloid": "Copyright",
    "blue_eyes": "General",
    "smile": "General",
    "best_quality": "Quality",
    "tagme": "Meta",
    "1girl": "General",
    "solo_focus": "General",
}
WD_TAGS = {
    "hatsune_miku": "4",
    "forest": "0",
    "smile": "0",
    "rating_safe": "9",
    "1girl": "0",
}


def _write_json(path: Path, value: object) -> bytes:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    path.write_bytes(data)
    return data


def _record(data: bytes) -> dict[str, object]:
    return {"sizeBytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _write_tagger(root: Path, runtime: str, tags: dict[str, str]) -> Path:
    resource_id = "fixture-cl" if runtime == "cl-tagger-v2-onnx-v1" else "fixture-wd"
    package = root / "tagging-models" / resource_id
    package.mkdir(parents=True)
    if runtime == "cl-tagger-v2-onnx-v1":
        names = list(tags)
        vocabulary = {
            "idx_to_tag": {str(index): name for index, name in enumerate(names)},
            "tag_to_idx": {name: index for index, name in enumerate(names)},
            "tag_to_category": tags,
        }
        payloads = {
            "model.onnx": b"fixture-cl-model",
            "model.onnx.data": b"fixture-cl-data",
            "model_metadata.json": b"{}\n",
            "model_vocabulary.json": json.dumps(
                vocabulary, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n",
            "thresholds.json": b'{"character":0.55,"copyright":0.55,"general":0.55}\n',
        }
        entrypoints = {
            "model": "model.onnx",
            "modelData": "model.onnx.data",
            "metadata": "model_metadata.json",
            "vocabulary": "model_vocabulary.json",
            "thresholds": "thresholds.json",
        }
        model_categories = ["General", "Character", "Copyright", "Meta", "Rating", "Quality"]
        adjustable = ["general", "character", "copyright"]
        excluded = ["meta", "rating", "quality"]
        vocabulary_name = "model_vocabulary.json"
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("tag_id", "name", "category", "count"))
        for index, (name, category) in enumerate(tags.items()):
            writer.writerow((index, name, category, 1))
        payloads = {
            "model.onnx": b"fixture-wd-model",
            "selected_tags.csv": output.getvalue().encode("utf-8"),
            "preprocess.json": b"{}\n",
            "thresholds.json": b'{"character":0.5,"general":0.5}\n',
        }
        entrypoints = {
            "model": "model.onnx",
            "selectedTags": "selected_tags.csv",
            "preprocess": "preprocess.json",
            "thresholds": "thresholds.json",
        }
        model_categories = ["General", "Character", "Rating"]
        adjustable = ["general", "character"]
        excluded = ["rating"]
        vocabulary_name = "selected_tags.csv"
    for name, data in payloads.items():
        (package / name).write_bytes(data)
    manifest = {
        "schemaVersion": 2,
        "kind": "tagging-model",
        "resourceId": resource_id,
        "resourceVersion": "fixture-v1",
        "profile": "danbooru",
        "displayName": {"zh-CN": "Fixture", "en": "Fixture"},
        "description": {"zh-CN": "测试资源", "en": "Test resource"},
        "runtimeFormat": runtime,
        "distribution": {
            "mode": "local-only",
            "sourceUrl": "https://huggingface.co/example/model",
            "licenseUrl": "https://huggingface.co/example/model/blob/main/LICENSE",
        },
        "entrypoints": entrypoints,
        "files": {name: _record(data) for name, data in payloads.items()},
        "metadata": {
            "tagCount": len(tags),
            "modelCategories": model_categories,
            "adjustableCategories": adjustable,
            "excludedCategories": excluded,
            "vocabularyFingerprint": _record(payloads[vocabulary_name])["sha256"],
        },
        "documentation": [],
    }
    _write_json(package / "resource.json", manifest)
    return package / "resource.json"


def _write_sources(root: Path, *, omit_tag: str | None = None) -> tuple[Path, Path, Path]:
    categories = {tag: 0 for tag in COUNT_TAGS}
    categories.update({
        "hatsune_miku": 4,
        "vocaloid": 3,
        "blue_eyes": 0,
        "forest": 0,
        "smile": 0,
        "best_quality": 0,
        "rating_safe": 0,
        "tagme": 5,
        "fixture_artist": 1,
    })
    if omit_tag is not None:
        categories.pop(omit_tag, None)
    catalog = {
        "schemaVersion": 1,
        "source": "danbooru",
        "snapshotId": "danbooru-tags-fixture-v1",
        "sourceUrl": "https://danbooru.donmai.us/tags.json",
        "tags": [
            {"name": name, "category": category}
            for name, category in sorted(categories.items())
        ],
        "aliases": [{
            "antecedent_name": "miku",
            "consequent_name": "hatsune_miku",
            "status": "active",
        }],
    }
    wiki = {
        "schemaVersion": 1,
        "source": "danbooru",
        "snapshotId": "danbooru-wiki-fixture-v1",
        "sourceUrl": "https://danbooru.donmai.us/wiki_pages.json",
        "pages": [
            {"title": title, "body": f"fixture evidence for {title}"}
            for title in REQUIRED_WIKI_TITLES
        ],
    }
    overlay = {
        "schemaVersion": 1,
        "source": "audited-danbooru-general-overlay",
        "entries": {
            "blue_eyes": {"bucket": "appearance", "evidence": "fixture audit"},
            "forest": {"bucket": "environment", "evidence": "fixture audit"},
        },
    }
    catalog_path = root / "catalog.json"
    wiki_path = root / "wiki.json"
    overlay_path = root / "overlay.json"
    _write_json(catalog_path, catalog)
    _write_json(wiki_path, wiki)
    _write_json(overlay_path, overlay)
    return catalog_path, wiki_path, overlay_path


def _build(root: Path) -> tuple[dict[str, object], list[Path]]:
    library = root / "resource-library"
    library.mkdir()
    cl = _write_tagger(library, "cl-tagger-v2-onnx-v1", CL_TAGS)
    wd = _write_tagger(library, "wd-eva02-large-tagger-v3-onnx-v1", WD_TAGS)
    catalog, wiki, overlay = _write_sources(root)
    result = build_resource(
        library,
        catalog,
        wiki,
        [cl, wd],
        resource_version="fixture-v1",
        wiki_data_source_id="danbooru-wiki-fixture-v1",
        overlay_path=overlay,
    )
    return result, [cl, wd]


class DanbooruResourceBuilderTests(unittest.TestCase):
    def test_builds_deterministic_dictionary_rules_wiki_and_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first_root, second_root = Path(first_temp), Path(second_temp)
            first, first_taggers = _build(first_root)
            second, _ = _build(second_root)
            self.assertEqual(first["resourceFingerprint"], second["resourceFingerprint"])
            self.assertEqual(18, len(EXACT_COUNTERS))
            self.assertEqual(23, len(REQUIRED_WIKI_TITLES))

            resource_root = (
                first_root / "resource-library" / "classification-indexes" / "danbooru-classify-20260727-v1"
            )
            manifest = json.loads((resource_root / "resource.json").read_text(encoding="utf-8"))
            dictionary = json.loads(
                (resource_root / "danbooru_tag_dictionary.json").read_text(encoding="utf-8")
            )
            rules = json.loads((resource_root / "danbooru_count_rules.json").read_text(encoding="utf-8"))
            entries = dictionary["entries"]
            self.assertEqual("character", entries["hatsune_miku"]["bucket"])
            self.assertEqual(
                {"canonical": "hatsune_miku", "bucket": "character", "output": "miku", "method": "site_alias_category"},
                entries["miku"],
            )
            self.assertEqual("series", entries["vocaloid"]["bucket"])
            self.assertEqual("appearance", entries["blue_eyes"]["bucket"])
            self.assertEqual("environment", entries["forest"]["bucket"])
            self.assertEqual("general_fallback", entries["smile"]["method"])
            self.assertEqual("drop", entries["best_quality"]["bucket"])
            self.assertEqual("drop", entries["rating_safe"]["bucket"])
            self.assertEqual("drop", entries["fixture_artist"]["bucket"])
            self.assertEqual("count_rule", entries["1girl"]["method"])
            self.assertNotIn("count", {entry["bucket"] for entry in entries.values()})
            self.assertEqual(COUNT_RULES, rules)
            self.assertEqual(2, len(manifest["metadata"]["supportedVocabularyFingerprints"]))

            connection = sqlite3.connect(
                (resource_root / "danbooru_count_wiki.sqlite3").resolve().as_uri() + "?mode=ro&immutable=1",
                uri=True,
            )
            try:
                self.assertEqual(WIKI_APPLICATION_ID, connection.execute("PRAGMA application_id").fetchone()[0])
                self.assertEqual(
                    len(REQUIRED_WIKI_TITLES),
                    connection.execute("SELECT COUNT(*) FROM wiki_catalog").fetchone()[0],
                )
            finally:
                connection.close()

            library = first_root / "resource-library"
            classification = ResourcePackage.load(
                library, resource_root / "resource.json", "classification-index"
            )
            for tagger_manifest in first_taggers:
                tagger = ResourcePackage.load(library, tagger_manifest, "tagging-model")
                verify_tagger_dictionary_compatibility(tagger, classification)

            first_files = {
                name: hashlib.sha256((resource_root / name).read_bytes()).hexdigest()
                for name in manifest["files"]
            }
            second_resource = (
                second_root / "resource-library" / "classification-indexes" / "danbooru-classify-20260727-v1"
            )
            second_manifest = json.loads((second_resource / "resource.json").read_text(encoding="utf-8"))
            second_files = {
                name: hashlib.sha256((second_resource / name).read_bytes()).hexdigest()
                for name in second_manifest["files"]
            }
            self.assertEqual(first_files, second_files)

    def test_missing_vocabulary_label_fails_without_partial_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / "resource-library"
            library.mkdir()
            cl = _write_tagger(library, "cl-tagger-v2-onnx-v1", CL_TAGS)
            catalog, wiki, overlay = _write_sources(root, omit_tag="smile")
            with self.assertRaisesRegex(DanbooruResourceBuildError, "missing 1 tagger labels: smile"):
                build_resource(
                    library,
                    catalog,
                    wiki,
                    [cl],
                    resource_version="fixture-v1",
                    wiki_data_source_id="danbooru-wiki-fixture-v1",
                    overlay_path=overlay,
                )
            self.assertFalse(
                (library / "classification-indexes" / "danbooru-classify-20260727-v1").exists()
            )

    def test_model_category_cannot_override_a_site_identity_category(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / "resource-library"
            library.mkdir()
            conflicting = dict(CL_TAGS)
            conflicting["hatsune_miku"] = "Copyright"
            cl = _write_tagger(library, "cl-tagger-v2-onnx-v1", conflicting)
            catalog, wiki, overlay = _write_sources(root)
            with self.assertRaisesRegex(DanbooruResourceBuildError, "model/site category routes conflict"):
                build_resource(
                    library,
                    catalog,
                    wiki,
                    [cl],
                    resource_version="fixture-v1",
                    wiki_data_source_id="danbooru-wiki-fixture-v1",
                    overlay_path=overlay,
                )


if __name__ == "__main__":
    unittest.main()
