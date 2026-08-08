from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "classify" / "src"))
sys.path.insert(0, str(ROOT / "packaging" / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from anima_classify_worker.worker import ClassifyWorker
from anima_core.classify_protocol import ClassifyCountDecisionV1, ClassifyResultV1
from anima_core.count_review_protocol import CountEvidenceV1
from test_danbooru_resource_builder import _build


class DanbooruClassifyProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.result, _ = _build(self.root)
        self.library = self.root / "resource-library"
        self.manifest_relative = (
            "classification-indexes\\danbooru-classify-20260727-v1\\resource.json"
        )
        manifest = json.loads(
            (self.library / Path(self.manifest_relative.replace("\\", "/"))).read_text(encoding="utf-8")
        )
        self.worker = ClassifyWorker()
        hello = self.worker.initialize({
            "schemaVersion": 1,
            "payloadType": "classify_hello_request",
            "jobId": "job-danbooru",
            "configHash": "a" * 64,
            "profile": "danbooru",
            "resourceManifestRelativePath": self.manifest_relative,
            "resourceFingerprint": self.result["resourceFingerprint"],
            "wikiDataSourceId": manifest["metadata"]["wikiDataSourceId"],
            "overwriteCount": False,
            "captionFormat": {
                "replaceUnderscoresWithSpaces": True,
                "preserveEscapes": True,
                "triggersEnabled": False,
                "triggerTerms": [],
            },
        }, install_root=self.library)
        self.assertEqual(self.result["dictionaryEntryCount"], hello["entryCount"])
        self.sample_id = 0

    def tearDown(self) -> None:
        self.worker.close()
        self.temporary.cleanup()

    def _process(self, text: str, *, original_count: str | int | None = None) -> dict[str, object]:
        self.sample_id += 1
        result = self.worker.process({
            "schemaVersion": 1,
            "sampleId": self.sample_id,
            "leaseId": f"lease-{self.sample_id}",
            "source": "danbooru",
            "relativeImagePath": f"sample-{self.sample_id}.png",
            "annotationKey": f"sample-{self.sample_id}",
            "txtText": text,
            "txtProvenance": "module1_written",
            "originalCount": original_count,
        })
        self.assertEqual("classify_result", result["payloadType"])
        ClassifyResultV1.from_dict(result)
        return result

    def test_routes_character_copyright_general_and_excluded_categories(self) -> None:
        result = self._process(
            "hatsune miku, miku, vocaloid, blue eyes, forest, smile, "
            "best quality, tagme, rating safe, 1girl"
        )
        projection = result["projection"]
        self.assertEqual("hatsune_miku, miku", projection["character"])
        self.assertEqual("vocaloid", projection["series"])
        self.assertEqual(["blue_eyes"], projection["appearance"])
        self.assertEqual(["smile", "1girl"], projection["tags"])
        self.assertEqual(["forest"], projection["environment"])
        self.assertEqual([], projection["quality"])
        self.assertEqual("", projection["artist"])
        self.assertEqual("solo", projection["count"])
        self.assertEqual(3, result["droppedTagCount"])

    def test_all_exact_counters_and_cross_family_sums(self) -> None:
        for family, singular, plural in (
            ("girl", "girl", "girls"),
            ("boy", "boy", "boys"),
            ("other", "other", "others"),
        ):
            for amount in range(1, 7):
                tag = f"1{singular}" if amount == 1 else f"{amount}{plural}" if amount < 6 else f"6+{plural}"
                expected = ("solo", "duo", "trio")[amount - 1] if amount <= 3 else "group"
                with self.subTest(family=family, tag=tag):
                    self.assertEqual(expected, self._process(tag)["projection"]["count"])
        for text, expected in (
            ("1girl, 1boy", "duo"),
            ("2girls, 1boy", "trio"),
            ("1girl, 1boy, 1other", "trio"),
            ("3girls, 1boy", "group"),
            ("1girl, 1boy, 2others", "group"),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, self._process(text)["projection"]["count"])

    def test_conflicts_lower_bounds_and_fallbacks_are_reviewable(self) -> None:
        cases = (
            ("1girl, 2girls", "", "count_conflict:danbooru:girl:1girl,2girls", ()),
            ("multiple girls", "", "count_lower_bound:danbooru:multiple_girls", ("danbooru_girl",)),
            (
                "1girl, multiple girls",
                "",
                "count_conflict:danbooru:girl:1girl,multiple_girls",
                ("danbooru_girl",),
            ),
            ("2girls, multiple girls", "duo", "count_lower_bound:danbooru:multiple_girls", ("danbooru_girl",)),
            ("solo focus", "", "count_non_decisive:danbooru:solo_focus", ()),
            ("solo", "solo", None, ()),
            ("2girls, solo", "", "count_conflict:danbooru:solo:solo,2girls", ()),
        )
        for text, expected, warning, bounds in cases:
            with self.subTest(text=text):
                result = self._process(text)
                decision = result["countDecision"]
                self.assertEqual(expected, decision["value"])
                self.assertEqual(bounds, tuple(decision["appliedLowerBounds"]))
                if warning is None:
                    self.assertEqual([], decision["warnings"])
                else:
                    self.assertIn(warning, decision["warnings"])
                    evidence = CountEvidenceV1.from_decision(
                        ClassifyCountDecisionV1.from_dict(decision)
                    )
                    self.assertIn("count_conflict", evidence.reviewWarningCodes)

    def test_character_labels_and_layout_tags_never_infer_count(self) -> None:
        result = self._process(
            "hatsune miku, miku, multiple views, character sheet, unnamed creature"
        )
        projection = result["projection"]
        self.assertEqual("hatsune_miku, miku", projection["character"])
        self.assertEqual("", projection["count"])
        self.assertEqual(
            ["multiple_views", "character_sheet", "unnamed_creature"], projection["tags"]
        )
        self.assertEqual([], result["countDecision"]["appliedLowerBounds"])

    def test_original_count_arbitration_does_not_hide_danbooru_warnings(self) -> None:
        preserved = self._process("2girls, multiple girls", original_count="solo")
        decision = preserved["countDecision"]
        self.assertEqual(("solo", "original_json", True), (
            decision["value"], decision["selectedSource"], decision["conflict"],
        ))
        self.assertIn("count_source_conflict", decision["issueCodes"])
        self.assertIn("count_lower_bound:danbooru:multiple_girls", decision["warnings"])
        invalid = self._process("unknown creature", original_count="not a count")
        self.assertEqual("", invalid["countDecision"]["value"])
        self.assertIn("original_count_invalid", invalid["countDecision"]["issueCodes"])

    def test_count_arbitration_changes_no_other_projection_field(self) -> None:
        text = "hatsune miku, vocaloid, blue eyes, forest, smile, 1girl"
        inferred = self._process(text)["projection"]
        preserved = self._process(text, original_count="trio")["projection"]
        self.assertEqual("solo", inferred["count"])
        self.assertEqual("trio", preserved["count"])
        for field in (
            "quality", "character", "series", "artist", "appearance", "tags", "environment", "nl",
        ):
            self.assertEqual(inferred[field], preserved[field], field)


if __name__ == "__main__":
    unittest.main()
