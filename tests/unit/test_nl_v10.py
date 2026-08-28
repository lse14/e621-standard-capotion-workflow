from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.contracts import DEFAULT_MODULE_BATCH_SIZE, sha256_json
from anima_core.nl_runner import NlApiCredentials, NlRunner, NlRunnerError


def _config(*, nl_batch: int = 7, include_concurrency: bool = False) -> dict[str, object]:
    policy: dict[str, object] = {"maxRequestsPerMinute": 60}
    if include_concurrency:
        policy["concurrency"] = 2
    return {
        "schemaVersion": 10,
        "workMode": "in_place",
        "overwriteMode": "incremental",
        "sourceRoot": "C:\\dataset",
        "annotationBackup": "required",
        "recursive": False,
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": False,
            "triggerTerms": [],
            "flatTxtLayout": "nl_newline",
        },
        "caption": {"enabled": False, "thresholdMode": "model_default", "overwriteTxt": False, "inputTxtMode": "tag", "taggerFallbackOnMissingTxt": True},
        "ocr": {"enabled": False, "llmMinConfidence": 0.5, "forceReprocess": False, "resourceId": "ocr-model", "device": "auto"},
        "nl": {
            "enabled": True, "reuseOriginalNl": False, "apiEnabled": True, "useImage": True,
            "useFullJson": False, "systemPrompt": "", "promptVersion": "nl-default-prompt-v4",
            "captionPreset": "general", "lengthDistribution": {"short": 33, "medium": 34, "long": 33},
            "lengthSeed": "seed", "apiPolicy": policy,
        },
        "moduleBatchSize": {**DEFAULT_MODULE_BATCH_SIZE, "nl": nl_batch},
    }


class _Database:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config

    def get_job(self, _job_id: str) -> dict[str, object]:
        return {
            "config_json": json.dumps(self.config),
            "config_hash": sha256_json(self.config),
            "config_schema_version": 10,
            "dataset_root": "C:\\dataset",
            "sample_count": 1,
        }


class NlV10Tests(unittest.TestCase):
    def test_policy_concurrency_comes_only_from_module_batch_size(self) -> None:
        config = _config(nl_batch=7)
        runner = NlRunner(_Database(config), object(), object(), object(), object(), job_id="job", worker_instance_id="worker", credentials=NlApiCredentials("https://example.test", "model", "key"))
        _hash, nl, _root, _version, _ocr, _mode = runner._config()
        self.assertEqual(7, runner._policy(nl)["concurrency"])

    def test_v10_rejects_api_policy_concurrency(self) -> None:
        config = _config(include_concurrency=True)
        runner = NlRunner(_Database(config), object(), object(), object(), object(), job_id="job", worker_instance_id="worker")
        with self.assertRaisesRegex(NlRunnerError, "concurrency"):
            runner._config()


if __name__ == "__main__":
    unittest.main()
