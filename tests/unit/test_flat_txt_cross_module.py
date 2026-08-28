from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "anima_caption_format"))
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "token_budget" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "export" / "src"))

from anima_caption_format import serialize_flat_txt
from anima_caption_format.normalizer import CaptionDisplayPolicy, normalize_annotation
from anima_core.contracts import DEFAULT_MODULE_BATCH_SIZE, sha256_json
from anima_core.export_runner import ExportRunner, ExportRunnerError
from anima_export_worker.protocol import parse_process
from anima_export_worker.worker import ExportWorker
from anima_token_budget_worker.worker import TokenBudgetWorker


class _Encoding:
    def __init__(self, text: str) -> None:
        self.ids = list(text)


class _Tokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> _Encoding:
        assert add_special_tokens is False
        return _Encoding(text)


class FlatTxtCrossModuleTests(unittest.TestCase):
    def test_export_runner_rejects_v9_before_worker_transport(self) -> None:
        config = {
            "schemaVersion": 9,
            "moduleBatchSize": dict(DEFAULT_MODULE_BATCH_SIZE),
            "captionFormat": {
                "replaceUnderscoresWithSpaces": True,
                "preserveEscapes": True,
                "triggersEnabled": False,
                "triggerTerms": [],
                "flatTxtLayout": "nl_newline",
            },
            "export": {"format": "both"},
        }
        database = type("Database", (), {"get_job": lambda _self, _job_id: {
            "config_json": json.dumps(config), "config_hash": sha256_json(config), "config_schema_version": 9,
        }})()
        with self.assertRaisesRegex(ExportRunnerError, "invalid"):
            ExportRunner(database, object(), object(), object(), job_id="job", worker_instance_id="worker")._config()

    def test_token_budget_hash_and_export_staged_txt_share_exact_serializer_bytes(self) -> None:
        source = {
            "quality": ["high_quality"], "count": "solo", "character": "Hero", "series": "Series",
            "artist": "", "appearance": ["blue_eyes"], "tags": ["smile"], "environment": [],
            "nl": "Hero smiles.",
        }
        for layout in ("single_line", "nl_newline"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                dataset = root / "dataset"
                overlay = root / "overlay"
                (overlay / "annotations").mkdir(parents=True)
                (overlay / "overlay-manifest.json").write_text(json.dumps({"schemaVersion": 1, "jobId": "job-export", "datasetRoot": str(dataset)}), encoding="utf-8")
                dataset.mkdir()
                (overlay / "annotations" / "sample.json").write_text(json.dumps(source), encoding="utf-8")
                caption_format = {
                    "replaceUnderscoresWithSpaces": True,
                    "preserveEscapes": True,
                    "triggersEnabled": False,
                    "triggerTerms": [],
                    "flatTxtLayout": layout,
                }
                normalized = normalize_annotation(json.dumps(source).encode(), CaptionDisplayPolicy.from_mapping(caption_format))
                self.assertTrue(normalized.valid)
                expected = serialize_flat_txt(normalized.payload, CaptionDisplayPolicy.from_mapping(caption_format))
                budget = TokenBudgetWorker(tokenizer=_Tokenizer())
                budget_outcome = budget.process_item(1, "lease-1", source, caption_format, 10_000)
                worker = ExportWorker()
                worker.initialize({
                    "schemaVersion": 1, "payloadType": "export_hello_request", "jobId": "job-export",
                    "configHash": "a" * 64, "datasetRoot": str(dataset), "overlayRoot": str(overlay),
                    "format": "both", "captionFormat": caption_format,
                })
                item = parse_process({"schemaVersion": 1, "payloadType": "export_process_request", "items": [{
                    "schemaVersion": 1, "sampleId": 1, "leaseId": "lease-1", "relativeImagePath": "sample.png", "annotationKey": "sample",
                }]})
                outcome = worker.process(item)["outcomes"][0]
                staged = overlay / "prepared" / "export" / "lease-1.txt"
                self.assertEqual(expected, staged.read_bytes())
                self.assertEqual(hashlib.sha256(expected).hexdigest(), budget_outcome["flatTextSha256"])


if __name__ == "__main__":
    unittest.main()
