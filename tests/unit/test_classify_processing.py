from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "classify" / "src"))

from anima_classify_worker.count import WikiCountResolver, decide_count, normalize_original_count
from anima_classify_worker.dictionary import DictionaryEntry, E621Dictionary
from anima_classify_worker.parsing import parse_tag_text
from anima_classify_worker.worker import ClassifyWorker


RESOURCE_ROOT = ROOT / "resource-library"
RESOURCE_MANIFEST = r"classification-indexes\e621-classify-20260724-v1\resource.json"
RESOURCE_FINGERPRINT = "530323a5d1ca5c3f903c0d57b04d6f1014cdcc0ca01b8de5dc0a41e27e1d2baf"


def _resolver(*pages: str) -> WikiCountResolver:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE wiki_catalog (title TEXT PRIMARY KEY, body TEXT NOT NULL)")
    connection.executemany("INSERT INTO wiki_catalog(title, body) VALUES (?, ?)", [(page, "verified") for page in pages])
    return WikiCountResolver(connection)


def _dictionary(entries: dict[str, DictionaryEntry]) -> E621Dictionary:
    result = object.__new__(E621Dictionary)
    result.entries = entries
    return result


def _format(*, triggers: list[str] | None = None) -> dict[str, object]:
    return {
        "replaceUnderscoresWithSpaces": True,
        "preserveEscapes": True,
        "triggersEnabled": bool(triggers),
        "triggerTerms": list(triggers or []),
    }


def _offline_worker(entries: dict[str, DictionaryEntry], *pages: str) -> ClassifyWorker:
    worker = object.__new__(ClassifyWorker)
    worker.hello = {"captionFormat": _format(), "overwriteCount": False, "profile": "e621"}
    worker.resource = None
    worker.dictionary = _dictionary(entries)
    worker.count_rules = None
    worker.resolver = _resolver(*pages)
    return worker


def _work_item(text: str, provenance: str = "original_preserved") -> dict[str, object]:
    return {
        "schemaVersion": 1, "sampleId": 3, "leaseId": "lease-3", "source": "e621",
        "relativeImagePath": "sample.png", "annotationKey": "sample", "txtText": text,
        "txtProvenance": provenance, "originalCount": None,
    }


class ClassifyProcessingTests(unittest.TestCase):
    def test_real_resource_initialization_and_processing(self) -> None:
        manifest = json.loads((RESOURCE_ROOT / Path(RESOURCE_MANIFEST.replace("\\", "/"))).read_text(encoding="utf-8"))
        worker = ClassifyWorker()
        try:
            hello = worker.initialize({
                "schemaVersion": 1,
                "payloadType": "classify_hello_request",
                "jobId": "job-classify",
                "configHash": "a" * 64,
                "profile": "e621",
                "resourceManifestRelativePath": RESOURCE_MANIFEST,
                "resourceFingerprint": RESOURCE_FINGERPRINT,
                "wikiDataSourceId": manifest["metadata"]["wikiDataSourceId"],
                "overwriteCount": False,
                "captionFormat": {
                    "replaceUnderscoresWithSpaces": True,
                    "preserveEscapes": True,
                    "triggersEnabled": False,
                    "triggerTerms": [],
                },
            }, install_root=RESOURCE_ROOT)
            self.assertEqual((True, 1, 1, 120_978), (hello["ready"], hello["dictionaryLoads"], hello["wikiConnectionLoads"], hello["entryCount"]))
            result = worker.process({
                "schemaVersion": 1,
                "sampleId": 7,
                "leaseId": "lease-7",
                "source": "e621",
                "relativeImagePath": "nested\\sample.png",
                "annotationKey": "nested\\sample",
                "txtText": "solo, blue eyes",
                "txtProvenance": "module1_written",
                "originalCount": None,
            })
            self.assertEqual(("classify_result", 7, "lease-7", "solo"), (
                result["payloadType"], result["sampleId"], result["leaseId"], result["projection"]["count"],
            ))
        finally:
            worker.close()

    def test_txt_is_comma_only_and_trigger_prefix_requires_provenance(self) -> None:
        format_policy = {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": True,
            "triggerTerms": ["anima style", "project\\)"],
        }
        self.assertEqual(
            ["white_hair", "red_eyes"],
            parse_tag_text("anima style, project\\), white hair, red eyes", format_policy, "module1_written"),
        )
        self.assertEqual(
            ["anima_style", "project)", "white_hair"],
            parse_tag_text("anima style, project\\), white hair", format_policy, "original_preserved"),
        )
        self.assertEqual(
            ["project)", "anima_style", "white_hair"],
            parse_tag_text("project\\), anima style, white hair", format_policy, "module1_written"),
        )
        self.assertEqual(["white_hair", "red_eyes"], parse_tag_text("white hair, red eyes", format_policy, "missing"))

    def test_dictionary_canonicalizes_aliases_without_changing_count_evidence(self) -> None:
        dictionary = _dictionary({
            "named_feral": DictionaryEntry("named_feral", "character", "named_feral"),
            "anthro/feral": DictionaryEntry("anthro_on_feral", "appearance", "anthro_on_feral"),
            "fox/wolf": DictionaryEntry("hybrid", "appearance", "hybrid"),
            "character_sheet": DictionaryEntry("model_sheet", "drop", "model_sheet"),
            "solo": DictionaryEntry("solo", "count", "solo"),
        })
        projection = dictionary.classify(["named_feral", "anthro/feral", "fox/wolf", "character_sheet", "solo"])
        self.assertEqual("named_feral", projection.character)
        self.assertEqual(("anthro_on_feral", "hybrid"), projection.appearance)
        self.assertEqual(("solo",), projection.tags)
        self.assertEqual(("named_feral",), projection.canonical_character_ids)
        self.assertIn("anthro_on_feral", projection.evidence_tags)
        self.assertIn("model_sheet", projection.evidence_tags)
        self.assertEqual(1, projection.dropped_tag_count)

    def test_original_count_whitelist_and_wiki_priority_follow_the_frozen_matrix(self) -> None:
        self.assertEqual("duo", normalize_original_count("1boy, 1girl"))
        self.assertEqual("solo", normalize_original_count("solo, 1girl"))
        self.assertIsNone(normalize_original_count("duo, 1girl"))
        self.assertIsNone(normalize_original_count("multiple animals"))
        resolver = _resolver("solo", "duo", "trio", "group")
        tags = ["duo"]
        normal = decide_count("solo", tags, (), tags, resolver, False)
        overwrite = decide_count("solo", tags, (), tags, resolver, True)
        self.assertEqual(("solo", "original_json", True), (normal.value, normal.selected_source, normal.conflict))
        self.assertEqual(("duo", "wiki_tags", True), (overwrite.value, overwrite.selected_source, overwrite.conflict))

    def test_character_and_named_nonhuman_lower_bounds_are_mandatory(self) -> None:
        resolver = _resolver()
        for amount, expected in ((1, "solo"), (2, "duo"), (3, "trio"), (4, "group"), (9, "group")):
            with self.subTest(amount=amount):
                identities = tuple(f"character_{index}" for index in range(amount))
                decision = decide_count(None, (), identities, (), resolver, False)
                self.assertEqual(expected, decision.value)
                self.assertEqual(("character",), decision.applied_lower_bounds)
        unnamed_species = decide_count(None, ("hybrid", "wolf", "fox"), (), ("hybrid", "wolf", "fox"), resolver, False)
        self.assertEqual("", unnamed_species.value)
        self.assertEqual((), unnamed_species.applied_lower_bounds)

    def test_existing_e621_relationship_canonical_tags_only_establish_duo_lower_bound(self) -> None:
        relations = (
            "anthro_on_anthro", "anthro_on_feral", "feral_on_feral", "human_on_anthro", "human_on_feral",
            "human_on_human", "human_on_humanoid", "humanoid_on_anthro", "humanoid_on_feral", "humanoid_on_humanoid",
        )
        resolver = _resolver(*relations)
        for relation in relations:
            with self.subTest(relation=relation):
                decision = decide_count(None, (relation,), (), (relation,), resolver, False)
                self.assertEqual("duo", decision.value)
                self.assertEqual(("e621_relationship",), decision.applied_lower_bounds)
        high = decide_count("trio", ("anthro_on_feral",), (), ("anthro_on_feral",), resolver, False)
        self.assertEqual("trio", high.value)
        missing = decide_count(None, ("anthro_on_feral",), (), ("anthro_on_feral",), _resolver(), False)
        self.assertEqual("", missing.value)
        self.assertIn("wiki_missing:e621:anthro_on_feral", missing.warnings)

    def test_sheet_guard_does_not_turn_repeated_views_into_multiple_people(self) -> None:
        resolver = _resolver("duo")
        protected = decide_count(None, (), ("named_character",), ("model_sheet",), resolver, False)
        self.assertEqual("solo", protected.value)
        self.assertEqual(("character",), protected.applied_lower_bounds)
        conflict_one = decide_count(None, ("duo",), ("named_character",), ("character_sheet", "model_sheet", "duo"), resolver, False)
        self.assertEqual("count_sheet_multi_conflict", conflict_one.blocking_code)
        # M2-02: e621 posts whose cast is expressed through species carry no character tag at
        # all, so 0 canonical identities is normal and must not block a verified duo.
        zero_identity = decide_count(None, ("duo",), (), ("multiple_views", "multiple_angles", "duo"), resolver, False)
        self.assertEqual(("duo", None), (zero_identity.value, zero_identity.blocking_code))
        normal = decide_count(None, (), ("a", "b"), ("multiple_poses",), resolver, False)
        self.assertEqual("duo", normal.value)

    def test_invalid_original_cannot_be_masked_by_character_or_pair_evidence(self) -> None:
        resolver = _resolver()
        decision = decide_count("multiple characters", (), ("a", "b"), (), resolver, False)
        self.assertEqual("classify_original_count_unresolved", decision.blocking_code)
        relation = decide_count("not a count", ("anthro_on_feral",), (), ("anthro_on_feral",), resolver, False)
        self.assertEqual("classify_original_count_unresolved", relation.blocking_code)

    def test_count_resolution_only_changes_count_payload_member(self) -> None:
        dictionary = _dictionary({
            "named_character": DictionaryEntry("named_character", "character", "named_character"),
            "blue_fur": DictionaryEntry("blue_fur", "appearance", "blue_fur"),
            "forest": DictionaryEntry("forest", "environment", "forest"),
            "solo": DictionaryEntry("solo", "count", "solo"),
        })
        projection = dictionary.classify(["named_character", "blue_fur", "solo", "forest"])
        before = projection.to_json_projection("")
        decision = decide_count(None, projection.evidence_tags, projection.canonical_character_ids, projection.evidence_tags, _resolver("solo"), False)
        after = projection.to_json_projection(decision.value)
        for field in ("quality", "character", "series", "artist", "appearance", "tags", "environment", "nl"):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual("solo", after["count"])

    def test_zero_writable_tags_blocks_instead_of_writing_an_empty_projection(self) -> None:
        # F05: a preserved space-separated dump parses to one unknown tag, so every tag is
        # dropped; the previous build shipped a nine-field empty JSON to Export.
        worker = _offline_worker({"solo": DictionaryEntry("solo", "count", "solo")})
        outcome = worker.process(_work_item("1girl solo standing in a forest at sunset"))
        self.assertEqual(("classify_issue", "classify_no_writable_tags", True, False), (
            outcome["payloadType"], outcome["code"], outcome["blocking"], outcome["retriable"],
        ))
        self.assertNotIn("repairStartModule", outcome)
        self.assertEqual("classify_result", worker.process(_work_item("solo"))["payloadType"])

    def test_deterministic_caption_text_errors_are_not_retriable(self) -> None:
        # F35: an oversized tag repeats on every attempt, so it must not enter the retry queue.
        worker = _offline_worker({"solo": DictionaryEntry("solo", "count", "solo")})
        outcome = worker.process(_work_item("solo, " + "a" * 600))
        self.assertEqual(("classify_text_invalid", False), (outcome["code"], outcome["retriable"]))
        self.assertNotIn("repairStartModule", outcome)

    def test_strong_layout_suppresses_the_relationship_duo_lower_bound(self) -> None:
        # F06: multiple_poses + anthro_on_anthro + one identity used to be forced to duo.
        resolver = _resolver("anthro_on_anthro", "duo")
        evidence = ("multiple_poses", "anthro_on_anthro")
        single = decide_count(None, evidence, ("named_character",), evidence, resolver, False)
        self.assertEqual(("solo", ("character",), None), (
            single.value, single.applied_lower_bounds, single.blocking_code,
        ))
        pair = decide_count(None, evidence, ("a", "b"), evidence, resolver, False)
        self.assertEqual(("duo", ("character", "e621_relationship")), (pair.value, pair.applied_lower_bounds))

    def test_lore_and_non_identity_character_tags_are_dropped_at_the_projection_layer(self) -> None:
        # F46 + M2-01: lore is invisible setting text and fan_character/anon/unnamed_character
        # are not individual identities, so neither may be written nor counted.
        dictionary = _dictionary({
            "incest": DictionaryEntry("incest_(lore)", "tags", "incest_(lore)"),
            "brother_(lore)": DictionaryEntry("brother_(lore)", "tags", "brother_(lore)"),
            "fan_character": DictionaryEntry("fan_character", "character", "fan_character"),
            "anon": DictionaryEntry("anon", "character", "anon"),
            "unnamed_character": DictionaryEntry("unnamed_character", "character", "unnamed_character"),
            "named_character": DictionaryEntry("named_character", "character", "named_character"),
            "big_(anatomy)": DictionaryEntry("big_(anatomy)", "appearance", "big_(anatomy)"),
        })
        projection = dictionary.classify([
            "incest", "brother_(lore)", "fan_character", "anon", "unnamed_character",
            "named_character", "big_(anatomy)",
        ])
        self.assertEqual((), projection.tags)
        self.assertEqual("named_character", projection.character)
        self.assertEqual(("named_character",), projection.canonical_character_ids)
        self.assertEqual(("big_(anatomy)",), projection.appearance)
        self.assertEqual(5, projection.dropped_tag_count)
        decision = decide_count(None, projection.evidence_tags, projection.canonical_character_ids, projection.evidence_tags, _resolver(), False)
        self.assertEqual("solo", decision.value)

    def test_trigger_prefix_removal_keeps_a_genuine_duplicate_model_tag(self) -> None:
        # M2-05: Caption does not de-duplicate its trigger prefix against the model tags.
        self.assertEqual(
            ["blue_fur", "anthro"],
            parse_tag_text("anthro, blue fur, anthro", _format(triggers=["anthro"]), "module1_written"),
        )

    def test_consecutive_underscores_survive_the_display_round_trip(self) -> None:
        # M2-11: Caption maps one underscore to one space, so the inverse must not collapse.
        self.assertEqual(["colonel__klink"], parse_tag_text("colonel  klink", _format(), "original_preserved"))

    def test_original_count_subjects_are_gender_symmetric(self) -> None:
        # M2-12: 2males normalized while 2 females / 1 woman blocked the whole sample.
        for value, expected in (
            ("2males", "duo"), ("2 females", "duo"), ("1 woman", "solo"), ("3 ladies", "trio"),
            ("1 man, 1 woman", "duo"), ("female solo", "solo"), ("male solo", "solo"),
        ):
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_original_count(value))


if __name__ == "__main__":
    unittest.main()
