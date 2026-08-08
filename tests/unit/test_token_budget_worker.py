from __future__ import annotations

import importlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKER_SOURCE = ROOT / "workers" / "token_budget" / "src"
PACKAGE_ROOT = WORKER_SOURCE / "anima_token_budget_worker"
SHARED_SOURCE = ROOT / "shared" / "anima_caption_format"
sys.path.insert(0, str(SHARED_SOURCE))

from anima_caption_format import serialize_flat_txt
from anima_caption_format.normalizer import CaptionDisplayPolicy


CAPTION_FORMAT = {
    "replaceUnderscoresWithSpaces": True,
    "preserveEscapes": True,
    "triggersEnabled": False,
    "triggerTerms": [],
}


def annotation() -> dict[str, object]:
    return {
        "quality": [],
        "count": "solo",
        "character": "Protected Character",
        "series": "Protected Series",
        "artist": "Protected Artist",
        "appearance": [],
        "tags": [],
        "environment": [],
        "nl": "Protected NL.",
    }


class FakeEncoding:
    def __init__(self, count: int) -> None:
        self.ids = [0] * count


class FakeTokenizer:
    def __init__(self, count) -> None:
        self.count = count
        self.calls: list[tuple[bytes, bool]] = []

    def encode(self, text: str, *, add_special_tokens: bool) -> FakeEncoding:
        encoded = text.encode("utf-8")
        self.calls.append((encoded, add_special_tokens))
        return FakeEncoding(self.count(encoded))


class TokenBudgetWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        package_init = PACKAGE_ROOT / "__init__.py"
        self.assertTrue(package_init.is_file(), "Token Budget worker source package is missing")
        for name in tuple(sys.modules):
            if name == "anima_token_budget_worker" or name.startswith("anima_token_budget_worker."):
                del sys.modules[name]
        spec = importlib.util.spec_from_file_location(
            "anima_token_budget_worker",
            package_init,
            submodule_search_locations=[str(PACKAGE_ROOT)],
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules["anima_token_budget_worker"] = package
        spec.loader.exec_module(package)
        self.budget = importlib.import_module("anima_token_budget_worker.budget")
        self.worker = importlib.import_module("anima_token_budget_worker.worker")

    def tearDown(self) -> None:
        for name in tuple(sys.modules):
            if name == "anima_token_budget_worker" or name.startswith("anima_token_budget_worker."):
                del sys.modules[name]

    def test_tokenizer_counter_uses_exact_text_without_special_tokens_or_templates(self) -> None:
        tokenizer = FakeTokenizer(lambda text: len(text.split()))

        counts = self.budget.tokenizer_count_many(tokenizer, [b"english", "\u65e5\u672c\u8a9e".encode("utf-8"), "\u4e2d\u6587".encode("utf-8")])

        self.assertEqual(3, len(counts))
        self.assertTrue(tokenizer.calls)
        self.assertTrue(all(add_special_tokens is False for _, add_special_tokens in tokenizer.calls))
        self.assertFalse(hasattr(tokenizer, "apply_chat_template"))

    def test_non_monotonic_candidates_are_all_enumerated_and_keep_the_most_items_that_fit(self) -> None:
        source = annotation()
        source["tags"] = ["tag_one", "tag_two", "tag_three"]
        observed: list[bytes] = []

        def count_many(values: list[bytes]) -> list[int]:
            observed.extend(values)
            result: list[int] = []
            for text in values:
                if b"tag three" in text:
                    result.append(9)
                elif b"tag two" in text:
                    result.append(7)
                elif b"tag one" in text:
                    result.append(3)
                else:
                    result.append(5)
            return result

        result = self.budget.fit(source, CAPTION_FORMAT, 4, count_many)

        self.assertEqual("trimmed", result.status)
        self.assertEqual(["tag_one"], result.annotation["tags"])
        self.assertEqual(["tag_two", "tag_three"], result.removed["tags"])
        candidates = set(observed)
        self.assertGreaterEqual(len(candidates), 4, "every tail-retention candidate must be counted")

    def test_priority_only_removes_the_first_field_that_can_fit(self) -> None:
        fields = ("quality", "environment", "tags", "appearance")
        for field in fields:
            with self.subTest(field=field):
                source = annotation()
                for candidate in fields:
                    source[candidate] = []
                source[field] = [f"{field}_head", f"{field}_tail"]

                def count_many(values: list[bytes]) -> list[int]:
                    return [10 if b"tail" in text else 2 for text in values]

                result = self.budget.fit(source, CAPTION_FORMAT, 2, count_many)
                self.assertEqual("trimmed", result.status)
                self.assertEqual([f"{field}_head"], result.annotation[field])
                self.assertEqual([f"{field}_tail"], result.removed[field])
                for other in fields:
                    if other != field:
                        self.assertEqual([], result.removed[other])

    def test_quality_is_trimmed_before_later_fields(self) -> None:
        source = annotation()
        source.update({"quality": ["quality_tail"], "environment": ["environment_keep"], "tags": ["tag_keep"], "appearance": ["appearance_keep"]})

        def count_many(values: list[bytes]) -> list[int]:
            return [9 if b"quality tail" in text else 3 for text in values]

        result = self.budget.fit(source, CAPTION_FORMAT, 3, count_many)

        self.assertEqual([], result.annotation["quality"])
        self.assertEqual(["environment_keep"], result.annotation["environment"])
        self.assertEqual(["tag_keep"], result.annotation["tags"])
        self.assertEqual(["appearance_keep"], result.annotation["appearance"])

    def test_unicode_duplicates_and_protected_fields_are_preserved(self) -> None:
        source = annotation()
        source["quality"] = ["\u9ad8\u54c1\u8d28", "\u9ad8\u54c1\u8d28"]
        source["tags"] = ["\u732b", "\u732b", "\u9752\u7a7a"]

        result = self.budget.fit(source, CAPTION_FORMAT, 100, lambda values: [1] * len(values))

        self.assertEqual("within_budget", result.status)
        self.assertEqual(["\u9ad8\u54c1\u8d28"], result.annotation["quality"])
        self.assertEqual(["\u732b", "\u9752\u7a7a"], result.annotation["tags"])
        for field in ("count", "character", "series", "artist", "nl"):
            self.assertEqual(source[field], result.annotation[field])

    def test_overflow_never_deletes_protected_fields_or_emits_an_annotation(self) -> None:
        source = annotation()
        source.update({"quality": ["q"], "environment": ["e"], "tags": ["t"], "appearance": ["a"]})

        result = self.budget.fit(source, CAPTION_FORMAT, 1, lambda values: [99] * len(values))

        self.assertEqual("overflow", result.status)
        self.assertIsNone(result.annotation)
        self.assertEqual({"quality": ["q"], "environment": ["e"], "tags": ["t"], "appearance": ["a"]}, result.removed)
        self.assertEqual("Protected NL.", source["nl"])

    def test_worker_hash_is_identical_for_json_only_and_both_because_format_is_not_a_branch(self) -> None:
        tokenizer = FakeTokenizer(lambda text: 1)
        worker = self.worker.TokenBudgetWorker(tokenizer=tokenizer)
        source = annotation()

        json_only = worker.process_item(1, "lease-1", source, CAPTION_FORMAT, 2)
        both = worker.process_item(1, "lease-1", source, CAPTION_FORMAT, 2)

        self.assertEqual("within_budget", json_only["status"])
        self.assertEqual(json_only["flatTextSha256"], both["flatTextSha256"])
        policy = CaptionDisplayPolicy.from_mapping(CAPTION_FORMAT)
        self.assertEqual(serialize_flat_txt(json_only["annotation"], policy), serialize_flat_txt(both["annotation"], policy))

    def test_entry_responses_are_core_protocol_envelopes(self) -> None:
        entry = importlib.import_module("anima_token_budget_worker.entry")
        output = io.BytesIO()

        entry._reply(output, {"messageId": "hello-1", "jobId": "job-1", "configHash": "a" * 64}, "hello", {"ready": True})

        response = json.loads(output.getvalue())
        self.assertEqual("reply-hello-1", response["messageId"])
        self.assertEqual("hello-1", response["replyTo"])
        self.assertEqual("job-1", response["jobId"])
        self.assertEqual("a" * 64, response["configHash"])

    def test_entry_rejects_unbound_or_invalid_protocol_requests_and_bounds_replies(self) -> None:
        entry = importlib.import_module("anima_token_budget_worker.entry")
        valid = {
            "protocolVersion": "1.0", "kind": "request", "messageId": "process-1", "runtimeId": "token-budget",
            "owner": "token-budget", "method": "process_batch", "jobId": "job-1", "configHash": "a" * 64,
            "payload": {},
        }
        self.assertEqual(valid, entry._request(json.dumps(valid).encode("utf-8") + b"\n"))
        for field, value in (("protocolVersion", "2.0"), ("messageId", "bad id"), ("jobId", 1), ("configHash", "short")):
            with self.subTest(field=field):
                malformed = {**valid, field: value}
                with self.assertRaises(entry.TokenBudgetPayloadError):
                    entry._request(json.dumps(malformed).encode("utf-8") + b"\n")
        output = io.BytesIO()
        entry._reply(output, valid, "result", {"value": "x" * entry.MAX_FRAME_BYTES})
        response = json.loads(output.getvalue())
        self.assertEqual("error", response["method"])
        self.assertLessEqual(len(output.getvalue()), entry.MAX_FRAME_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
