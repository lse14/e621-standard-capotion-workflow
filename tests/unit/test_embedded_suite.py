"""Contracts for deterministic embedded test-suite selection."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tests" / "run_embedded_suite.py"
WORKER_TEST_COUNT = 9


def _load_runner():
    spec = importlib.util.spec_from_file_location("embedded_suite_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load runner: {RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EmbeddedSuiteCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def test_fast_selects_unit_contract_and_worker_suites_with_isolated_bytecode_free_children(self) -> None:
        commands = self.runner.commands_for_level("fast")

        discoveries = [
            (command.owner, command.start_directory)
            for command in commands
            if command.kind == "discover"
        ]
        self.assertEqual(
            [
                ("core", ROOT / "tests" / "unit"),
                ("core", ROOT / "tests" / "contract"),
            ],
            discoveries,
        )
        self.assertEqual(WORKER_TEST_COUNT, sum(command.kind == "file" for command in commands))
        self.assertTrue(all(command.arguments[:2] == ("-B", "-I") for command in commands))
        self.assertFalse(any(command.start_directory.name in {"integration", "stress"} for command in commands))
        self.assertFalse(hasattr(self.runner, "DECLARED_UNASSEMBLED_WORKER_TESTS"))
        self.assertNotIn("ocr-paddle", self.runner.WORKER_TESTS)
        self.assertEqual(
            ["ocr-paddle"],
            [command.owner for command in self.runner.commands_for_level("fast", ocr_mode="cpu") if command.owner.startswith("ocr")],
        )
        self.assertEqual(
            ["ocr-paddle", "ocr-paddle-gpu"],
            [command.owner for command in self.runner.commands_for_level("fast", ocr_mode="gpu") if command.owner.startswith("ocr")],
        )
        self.assertEqual(("test_token_budget_worker.py",), self.runner.WORKER_TESTS["token-budget"])

    def test_full_discovers_all_non_stress_core_suites_and_worker_suites(self) -> None:
        commands = self.runner.commands_for_level("full")

        discoveries = [command for command in commands if command.kind == "discover"]
        self.assertEqual(
            [
                ("core", ROOT / "tests" / "unit"),
                ("core", ROOT / "tests" / "contract"),
                ("core", ROOT / "tests" / "integration"),
            ],
            [(command.owner, command.start_directory) for command in discoveries],
        )
        self.assertFalse(any(command.start_directory.name == "stress" for command in discoveries))
        self.assertEqual(WORKER_TEST_COUNT, sum(command.kind == "file" for command in commands))
        self.assertTrue(all(command.arguments[:2] == ("-B", "-I") for command in commands))

    def test_optional_ocr_worker_selection_tracks_installed_mode(self) -> None:
        self.assertEqual(
            WORKER_TEST_COUNT,
            sum(command.kind == "file" for command in self.runner.commands_for_level("fast", ocr_mode="none")),
        )
        self.assertEqual(
            WORKER_TEST_COUNT + 1,
            sum(command.kind == "file" for command in self.runner.commands_for_level("fast", ocr_mode="cpu")),
        )
        self.assertEqual(
            WORKER_TEST_COUNT + 2,
            sum(command.kind == "file" for command in self.runner.commands_for_level("fast", ocr_mode="gpu")),
        )

    def test_stress_selects_only_core_stress_discovery(self) -> None:
        commands = self.runner.commands_for_level("stress")

        self.assertEqual(1, len(commands))
        command = commands[0]
        self.assertEqual(("core", "discover", ROOT / "tests" / "stress"), (command.owner, command.kind, command.start_directory))
        self.assertEqual(("-B", "-I", "-m", "unittest", "discover"), command.arguments[:5])

    def test_argument_parser_defaults_to_full(self) -> None:
        self.assertEqual("full", self.runner.parse_arguments([]).level)

    def test_child_exit_code_is_preserved(self) -> None:
        with patch.object(
            self.runner.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["child"], 7),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.runner._run(["child"])
        self.assertEqual(7, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
