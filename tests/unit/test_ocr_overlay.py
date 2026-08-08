from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.overlay import OverlayError, OverlayLayout  # noqa: E402


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("/", "\\"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class OcrOverlayTests(unittest.TestCase):
    def test_ocr_sidecar_tree_is_independent_and_keeps_original_image_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            (dataset / "cats").mkdir()
            (dataset / "cats" / "poster.jpg").write_bytes(b"jpg")
            (dataset / "cats" / "poster.png").write_bytes(b"png")
            before = _hash_tree(dataset)

            layout = OverlayLayout.create(dataset, "job-ocr-overlay")
            try:
                self.assertTrue(hasattr(layout, "ocr_sidecar_path"), "OCR sidecar path API is missing")
                jpg_path = layout.ocr_sidecar_path("cats\\poster.jpg")
                png_path = layout.ocr_sidecar_path("cats\\poster.png")

                self.assertEqual(layout.root / "ocr_annotations" / "cats" / "poster.jpg.ocr.json", jpg_path)
                self.assertEqual(layout.root / "ocr_annotations" / "cats" / "poster.png.ocr.json", png_path)
                self.assertNotEqual(jpg_path, png_path)
                self.assertEqual(before, _hash_tree(dataset))

                with self.assertRaises(OverlayError):
                    layout.annotation_path("ocr_annotations\\cats\\poster.jpg.ocr", ".json")
            finally:
                layout.discard()

    def test_ocr_sidecar_rejects_unsafe_or_non_image_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            layout = OverlayLayout.create(dataset, "job-ocr-overlay")
            try:
                self.assertTrue(hasattr(layout, "ocr_sidecar_path"), "OCR sidecar path API is missing")
                for value in ("..\\poster.jpg", "C:\\dataset\\poster.jpg", "\\server\\share\\poster.jpg", "poster.txt", "poster.jpg\x00"):
                    with self.subTest(value=value), self.assertRaises(OverlayError):
                        layout.ocr_sidecar_path(value)
            finally:
                layout.discard()

    def test_ocr_prepared_commit_uses_atomic_digest_discipline_without_business_annotation_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            before = _hash_tree(dataset)
            layout = OverlayLayout.create(dataset, "job-ocr-overlay")
            try:
                self.assertTrue(hasattr(layout, "write_ocr_prepared"), "OCR prepared artifact API is missing")
                payload = b'{"schemaVersion":1,"status":"no_text"}\n'
                prepared, digest = layout.write_ocr_prepared("lease-ocr-1", payload)
                relative = str(prepared.relative_to(layout.root)).replace("/", "\\")

                committed = layout.commit_ocr_prepared(relative, digest, "cats\\poster.jpg")

                self.assertEqual(layout.ocr_sidecar_path("cats\\poster.jpg"), committed)
                self.assertEqual(payload, committed.read_bytes())
                self.assertFalse(prepared.exists())
                self.assertFalse((layout.root / "annotations" / "cats" / "poster.json").exists())
                self.assertEqual(before, _hash_tree(dataset))
            finally:
                layout.discard()

    def test_working_sidecar_view_reads_task_overlay_before_dataset_ocr_annotations(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("anima_core.ocr_overlay"),
            "OCR working sidecar view module is missing",
        )
        from anima_core.ocr_overlay import OcrWorkingSidecarView

        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            dataset_sidecar = dataset / "ocr_annotations" / "cats"
            dataset_sidecar.mkdir(parents=True)
            (dataset_sidecar / "poster.jpg.ocr.json").write_bytes(b"formal-dataset")
            layout = OverlayLayout.create(dataset, "job-ocr-overlay")
            try:
                view = OcrWorkingSidecarView(dataset, layout)
                self.assertEqual(b"formal-dataset", view.read_bytes("cats\\poster.jpg"))

                layout.write_ocr_sidecar("cats\\poster.jpg", b"task-overlay")

                self.assertEqual(b"task-overlay", view.read_bytes("cats\\poster.jpg"))
                self.assertIsNone(view.read_bytes("cats\\missing.jpg"))
            finally:
                layout.discard()


if __name__ == "__main__":
    unittest.main()
