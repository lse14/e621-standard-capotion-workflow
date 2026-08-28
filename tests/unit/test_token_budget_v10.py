from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "token_budget" / "src"))

from anima_core.contracts import sha256_json
from anima_core.token_budget_runner import TokenBudgetRunner, TokenBudgetRunnerError
from anima_token_budget_worker.protocol import parse_process


class _Database:
    def __init__(self, schema_version: int) -> None:
        config = {
            "schemaVersion": schema_version,
            "profile": "e621",
            "tokenBudget": {
                "enabled": True,
                "maxTokens": 128,
                "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
                "resourceManifestRelativePath": "tokenizers\\test\\resource.json",
                "resourceFingerprint": "a" * 64,
                "contextLimit": 512,
            },
            "captionFormat": {
                "replaceUnderscoresWithSpaces": True,
                "preserveEscapes": True,
                "triggersEnabled": False,
                "triggerTerms": [],
                "flatTxtLayout": "nl_newline",
            },
        }
        self.job = {
            "config_schema_version": schema_version,
            "config_hash": sha256_json(config),
            "config_json": json.dumps(config),
        }

    def get_job(self, _job_id: str) -> dict[str, object]:
        return self.job


class TokenBudgetV10Tests(unittest.TestCase):
    def test_runner_rejects_v9_before_claiming_or_starting_worker(self) -> None:
        runner = TokenBudgetRunner(_Database(9), object(), object(), object(), job_id="job", worker_instance_id="worker")
        with self.assertRaisesRegex(TokenBudgetRunnerError, "invalid"):
            runner._config()

    def test_worker_process_accepts_the_frozen_flat_txt_layout(self) -> None:
        payload = {
            "schemaVersion": 1,
            "payloadType": "token_budget_process_request",
            "captionFormat": {
                "replaceUnderscoresWithSpaces": True,
                "preserveEscapes": True,
                "triggersEnabled": False,
                "triggerTerms": [],
                "flatTxtLayout": "nl_newline",
            },
            "items": [{
                "schemaVersion": 1,
                "sampleId": 1,
                "leaseId": "lease-1",
                "annotation": {"quality": [], "count": "solo", "character": "", "series": "", "artist": "", "appearance": [], "tags": [], "environment": [], "nl": "caption"},
            }],
        }
        request = parse_process(payload)
        self.assertEqual("nl_newline", request.caption_format["flatTxtLayout"])


if __name__ == "__main__":
    unittest.main()
