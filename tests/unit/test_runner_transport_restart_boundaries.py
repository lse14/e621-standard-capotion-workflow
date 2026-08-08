from __future__ import annotations

import sys
import unittest
import importlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.export_runner import ExportRunner
from anima_core.nl_runner import NlRunner
from anima_core.policy_runner import PolicyRunner
from anima_core.replace_runner import ReplaceRunner
from anima_core.stdio_transport import StdioJsonlTransportError


class _CrashingTransport:
    def __init__(self, failure: StdioJsonlTransportError) -> None:
        self.failure = failure

    def exchange(self, _request):
        raise self.failure


class RunnerTransportRestartBoundaryTests(unittest.TestCase):
    def test_stdio_transport_error_is_rethrown_unchanged_by_restartable_runners(self) -> None:
        spec = importlib.util.find_spec("anima_core.ocr_runner")
        if spec is None:
            self.fail("OCR runner module is missing")
        ocr_module = importlib.import_module("anima_core.ocr_runner")
        ocr_runner = ocr_module.OcrRunner
        for runner_type in (ReplaceRunner, NlRunner, PolicyRunner, ExportRunner, ocr_runner):
            with self.subTest(runner=runner_type.__name__):
                failure = StdioJsonlTransportError("worker exited before request")
                runner = object.__new__(runner_type)
                runner.transport = _CrashingTransport(failure)
                runner.job_id = "job-transport-boundary"
                if runner_type is ocr_runner:
                    runner.runtime_id = ocr_module.OCR_RUNTIME_ID

                with self.assertRaises(StdioJsonlTransportError) as caught:
                    runner._exchange("hello", {}, "0" * 64)

                self.assertIs(failure, caught.exception)


if __name__ == "__main__":
    unittest.main()
