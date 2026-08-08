from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "packaging" / "scripts"))

import finalize_danbooru_tagger_resource as finalizer
from anima_core.resource_catalog import RESOURCE_PLACEHOLDERS, ResourcePackage


def _small_spec(key: str) -> finalizer.TaggerSpec:
    return replace(finalizer.SPECS[key], tag_count=3)


def _write_raw_files(library: Path, spec: finalizer.TaggerSpec) -> tuple[Path, dict[str, bytes]]:
    package = library / "tagging-models" / spec.resource_id
    package.mkdir(parents=True)
    if spec.key == "cl":
        names = ("smile", "hatsune_miku", "vocaloid")
        categories = ("General", "Character", "Copyright")
        vocabulary = {
            "idx_to_tag": {str(index): name for index, name in enumerate(names)},
            "tag_to_idx": {name: index for index, name in enumerate(names)},
            "tag_to_category": dict(zip(names, categories, strict=True)),
        }
        payloads = {
            "model.onnx": b"fixture-cl-model",
            "model.onnx.data": b"fixture-cl-model-data",
            "model_metadata.json": b'{"image_size":384,"tag_count":3}\n',
            "model_vocabulary.json": finalizer._json_bytes(vocabulary),
        }
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("tag_id", "name", "category", "count"))
        writer.writerows(((0, "smile", 0, 1), (1, "hatsune_miku", 4, 1), (2, "safe", 9, 1)))
        payloads = {
            "model.onnx": b"fixture-wd-model",
            "selected_tags.csv": output.getvalue().encode("utf-8"),
        }
    for name, data in payloads.items():
        (package / name).write_bytes(data)
    return package, payloads


class DanbooruTaggerFinalizerTests(unittest.TestCase):
    def test_cl_and_wd_finalize_exact_files_and_are_idempotent(self) -> None:
        for key in ("cl", "wd"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                library = Path(temporary) / "resource-library"
                library.mkdir()
                spec = _small_spec(key)
                package_root, original = _write_raw_files(library, spec)

                created = finalizer._finalize_spec(library, spec)
                self.assertEqual("created", created["status"])
                self.assertEqual(set(spec.runtime_files) | {"resource.json"}, {path.name for path in package_root.iterdir()})
                for name, data in original.items():
                    self.assertEqual(data, (package_root / name).read_bytes())

                package = ResourcePackage.load(library, package_root / "resource.json", "tagging-model")
                package.verify_files(verify_hashes=True)
                self.assertEqual(created["resourceFingerprint"], package.fingerprint)
                self.assertEqual(created["vocabularyFingerprint"], package.metadata["vocabularyFingerprint"])
                self.assertEqual("already_valid", finalizer._finalize_spec(library, spec)["status"])

    def test_unknown_or_partial_files_are_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / "resource-library"
            library.mkdir()
            spec = _small_spec("cl")
            package_root, _ = _write_raw_files(library, spec)
            restricted = package_root / "model_tag_metrics.npz"
            restricted.write_bytes(b"must-not-be-packaged")

            with self.assertRaisesRegex(finalizer.TaggerResourceFinalizationError, "unexpected: model_tag_metrics.npz"):
                finalizer._finalize_spec(library, spec)
            self.assertFalse((package_root / "resource.json").exists())
            self.assertFalse((package_root / "thresholds.json").exists())
            self.assertEqual(b"must-not-be-packaged", restricted.read_bytes())

    def test_invalid_vocabulary_fails_before_generating_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / "resource-library"
            library.mkdir()
            spec = _small_spec("cl")
            package_root, _ = _write_raw_files(library, spec)
            vocabulary = json.loads((package_root / "model_vocabulary.json").read_text(encoding="utf-8"))
            vocabulary["idx_to_tag"].pop("2")
            (package_root / "model_vocabulary.json").write_bytes(finalizer._json_bytes(vocabulary))

            with self.assertRaisesRegex(finalizer.TaggerResourceFinalizationError, "idx_to_tag"):
                finalizer._finalize_spec(library, spec)
            self.assertEqual(set(spec.raw_files), {path.name for path in package_root.iterdir()})

    def test_post_write_validation_failure_removes_only_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / "resource-library"
            library.mkdir()
            spec = _small_spec("wd")
            package_root, original = _write_raw_files(library, spec)

            with patch.object(
                finalizer,
                "_load_finalized_package",
                side_effect=finalizer.TaggerResourceFinalizationError("injected validation failure"),
            ):
                with self.assertRaisesRegex(finalizer.TaggerResourceFinalizationError, "injected"):
                    finalizer._finalize_spec(library, spec)

            self.assertEqual(set(spec.raw_files), {path.name for path in package_root.iterdir()})
            for name, data in original.items():
                self.assertEqual(data, (package_root / name).read_bytes())

    def test_fixed_specs_match_the_uninstalled_catalog_placeholders(self) -> None:
        placeholders = {item["resourceId"]: item for item in RESOURCE_PLACEHOLDERS if item["kind"] == "tagging-model"}
        for spec in finalizer.SPECS.values():
            with self.subTest(resource_id=spec.resource_id):
                placeholder = placeholders[spec.resource_id]
                self.assertEqual(spec.resource_version, placeholder["resourceVersion"])
                self.assertEqual(spec.runtime_format, placeholder["runtimeFormat"])
                self.assertEqual(spec.source_url, placeholder["distribution"]["sourceUrl"])
                self.assertEqual(spec.license_url, placeholder["distribution"]["licenseUrl"])
                self.assertEqual(list(spec.adjustable_categories), placeholder["adjustableCategories"])
                self.assertEqual(list(spec.excluded_categories), placeholder["excludedCategories"])
                self.assertEqual(spec.generated_json["thresholds.json"], placeholder["defaultThresholds"])


if __name__ == "__main__":
    unittest.main()
