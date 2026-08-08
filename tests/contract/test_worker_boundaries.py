from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.worker_protocol import ProtocolEnvelopeV1, decode_frame, encode_frame
from anima_core.launcher import WorkerLauncher
from tests.worker_test_support import test_config_hash, worker_hello_payload

INSTALL_ROOT = ROOT / ".runtime-build"
RESOURCE_ROOT = ROOT / "resource-library"


ASSEMBLED_WORKERS = {
    "caption": ("caption-e621", "anima_caption_worker", 64),
    "classify": ("classify-e621", "anima_classify_worker", 500),
    "replace": ("replace-e621", "anima_replace_worker", 500),
    "nl": ("nl", "anima_nl_worker", 32),
    "policy": ("policy", "anima_policy_worker", 16),
    "export": ("export", "anima_export_worker", 500),
    "ocr": ("ocr-paddle", "anima_ocr_worker", 1024),
}
WORKERS = dict(ASSEMBLED_WORKERS)
WORKER_PACKAGES = {package for _, package, _ in WORKERS.values()}
PADDLE_PACKAGES = {"paddle", "paddleocr", "paddlex"}


def _embedded_worker_command(owner: str, runtime_id: str) -> tuple[list[str], dict[str, str]]:
    launch = WorkerLauncher.from_install_root(INSTALL_ROOT, resource_root=RESOURCE_ROOT).resolve(
        runtime_id, expected_owner=owner
    )
    return list(launch.command), dict(launch.environment)


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _top_level_absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _package_import_graph(package_root: Path, package_name: str) -> dict[str, set[str]]:
    modules = {path.stem: path for path in package_root.glob("*.py")}
    graph = {module: set() for module in modules}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            target: str | None = None
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    target = node.module.split(".", 1)[0] if node.module else "__init__"
                elif node.module and node.module.startswith(package_name + "."):
                    target = node.module[len(package_name) + 1:].split(".", 1)[0]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(package_name + "."):
                        imported = alias.name[len(package_name) + 1:].split(".", 1)[0]
                        if imported in modules:
                            graph[module].add(imported)
            if target in modules:
                graph[module].add(target)
    return graph


def _find_cycle(graph: dict[str, set[str]]) -> tuple[str, ...] | None:
    visited: set[str] = set()
    active: list[str] = []

    def visit(module: str) -> tuple[str, ...] | None:
        if module in active:
            start = active.index(module)
            return tuple((*active[start:], module))
        if module in visited:
            return None
        active.append(module)
        for dependency in graph[module]:
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        active.pop()
        visited.add(module)
        return None

    for module in graph:
        cycle = visit(module)
        if cycle is not None:
            return cycle
    return None


class WorkerBoundaryTests(unittest.TestCase):
    def test_token_budget_source_isolated_from_core_and_other_worker_owners(self) -> None:
        package_root = ROOT / "workers" / "token_budget" / "src" / "anima_token_budget_worker"
        self.assertTrue(package_root.is_dir(), "Token Budget worker source package is missing")
        if not package_root.is_dir():
            return
        expected = {"__init__.py", "protocol.py", "resource.py", "budget.py", "worker.py", "entry.py"}
        self.assertEqual(expected, {path.name for path in package_root.glob("*.py")})
        forbidden = {"anima_core", *WORKER_PACKAGES, *PADDLE_PACKAGES, "torch", "torchvision", "onnxruntime"}
        for source in package_root.rglob("*.py"):
            imported = _absolute_imports(source)
            self.assertFalse(imported & forbidden, f"token-budget imports another owner or inference stack: {source}")

    def test_ast_import_boundaries_and_each_module_has_its_own_runtime(self) -> None:
        core_package = ROOT / "core" / "src" / "anima_core"
        for source in core_package.rglob("*.py"):
            imported = _absolute_imports(source)
            self.assertFalse(imported & WORKER_PACKAGES, f"core imports worker code: {source}")
            self.assertFalse(imported & PADDLE_PACKAGES, f"core imports Paddle code: {source}")
        self.assertIsNone(
            _find_cycle(_package_import_graph(core_package, "anima_core")),
            "core contains a circular import dependency",
        )

        workers_root = ROOT / "workers"
        for owner, (_, own_package, _) in WORKERS.items():
            package_root = workers_root / owner / "src" / own_package
            self.assertTrue(package_root.is_dir(), f"declared worker source is missing: {package_root}")
            for source in package_root.rglob("*.py"):
                imported = _absolute_imports(source)
                self.assertFalse(
                    any(module == "anima_core" or module.startswith("anima_core.") for module in imported),
                    f"worker imports core: {source}",
                )
                other_packages = WORKER_PACKAGES - {own_package}
                self.assertFalse(
                    any(
                        module == package or module.startswith(package + ".")
                        for module in imported
                        for package in other_packages
                    ),
                    f"worker imports another worker: {source}",
                )
                if owner != "ocr":
                    self.assertFalse(imported & PADDLE_PACKAGES, f"non-OCR worker imports Paddle: {source}")
                else:
                    self.assertFalse(
                        _top_level_absolute_imports(source) & {"PIL", "numpy", *PADDLE_PACKAGES},
                        f"OCR worker imports optional inference dependencies eagerly: {source}",
                    )


    def test_frontend_is_rendering_and_api_only(self) -> None:
        forbidden = (
            "node:fs", "from \"fs\"", "from 'fs'", "showdirectorypicker(", "webkitdirectory",
            "readdir(", "readfile(", "writefile(", "sqlite", "onnxruntime", "process_batch",
            "claim_batch", "anima_core", "anima_caption_worker", "anima_classify_worker",
            "anima_replace_worker", "anima_nl_worker", "anima_policy_worker", "anima_export_worker", "anima_ocr_worker",
        )
        for source in (ROOT / "frontend" / "src").rglob("*"):
            if source.suffix not in {".ts", ".tsx"}:
                continue
            text = source.read_text(encoding="utf-8").lower()
            for token in forbidden:
                self.assertNotIn(token, text, f"frontend contains backend business/file logic: {source}")
            if source.name != "api.ts":
                self.assertNotIn("fetch(", text, f"network access must stay in the frontend API adapter: {source}")

    def test_worker_rejects_an_unbounded_input_line(self) -> None:
        runtime_id, _, _ = WORKERS["caption"]
        command, environment = _embedded_worker_command("caption", runtime_id)
        completed = subprocess.run(
            command,
            input=b"x" * (1_048_576 + 2),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=10,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn(b"unterminated", completed.stderr)

    def test_each_worker_speaks_its_own_identity(self) -> None:
        for owner, (runtime_id, _, _) in ASSEMBLED_WORKERS.items():
            with self.subTest(owner=owner):
                with worker_hello_payload(runtime_id, INSTALL_ROOT) as (mode, payload):
                    command, environment = _embedded_worker_command(owner, runtime_id)
                    if mode == "transport_only":
                        environment["ANIMA_TEST_TRANSPORT_ONLY"] = "1"
                    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=environment, cwd=str(INSTALL_ROOT))
                    assert process.stdin is not None and process.stdout is not None
                    job_id = None if mode == "transport_only" else "worker-test"
                    config_hash = None if mode == "transport_only" else test_config_hash()
                    hello = ProtocolEnvelopeV1("1.0", "request", "hello-1", runtime_id, owner, "hello", payload, jobId=job_id, configHash=config_hash)
                    process.stdin.write(encode_frame(hello))
                    process.stdin.flush()
                    decoded = decode_frame(process.stdout.readline(), runtime_id=runtime_id, owner=owner)
                    self.assertEqual("hello", decoded.method)
                    self.assertEqual("response", decoded.kind)
                    shutdown = ProtocolEnvelopeV1("1.0", "request", "shutdown-1", runtime_id, owner, "shutdown", {})
                    process.stdin.write(encode_frame(shutdown))
                    process.stdin.flush()
                    decoded = decode_frame(process.stdout.readline(), runtime_id=runtime_id, owner=owner)
                    self.assertEqual("result", decoded.method)
                    process.stdin.close()
                    stderr = process.stderr.read().decode("utf-8", errors="replace")
                    self.assertEqual(0, process.wait(timeout=30), stderr)
                    process.stdout.close()
                    process.stderr.close()

    def test_profile_files_preserve_frozen_danbooru_boundary(self) -> None:
        e621 = json.loads((ROOT / "profiles" / "e621.profile.json").read_text(encoding="utf-8"))
        danbooru = json.loads((ROOT / "profiles" / "danbooru.profile.json").read_text(encoding="utf-8"))
        self.assertTrue(e621["available"])
        self.assertEqual("available", e621["dropoutStatus"])
        self.assertEqual("policy", e621["policyRuntimeId"])
        self.assertTrue(danbooru["available"])
        self.assertEqual("direct_to_classify", danbooru["taggerOutputRoute"])
        self.assertEqual("none", danbooru["tagNormalization"])
        self.assertEqual("skipped_by_profile", danbooru["replacement"])
        self.assertNotIn("replace-e621", danbooru["runtimeIds"])
        self.assertEqual(
            ["caption-e621", "classify-e621", "nl", "policy", "export"],
            danbooru["runtimeIds"],
        )


if __name__ == "__main__":
    unittest.main()
