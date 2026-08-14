"""Guards against the two silent drifts that broke the 2026-07-26 fix round.

Both defects were invisible to the rest of the suite because unit tests inject the
source tree onto ``sys.path`` while the real pipeline spawns the assembled copies
under ``.runtime-build\\runtimes``:

1. The classification resource backfilled 22 tagger labels into the installed
   dictionary (120,956 -> 120,978 entries) while four source constants and two test
   expectations still pinned 120,956, so every classify hello failed against the
   real resource.
2. Eleven parallel edits landed in ``core/src`` and ``workers/*/src`` without
   re-running the packaging assembly, so the assembled runtimes kept executing the
   pre-fix code and their manifest hashes described stale files.
"""
from __future__ import annotations

import ast
import argparse
import filecmp
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.classify_protocol import ClassifyHelloResultV1
from anima_core.classify_resource import CLASSIFY_DICTIONARY_ENTRY_COUNT
from anima_core.runtime_manifest import RuntimeManifestError, inspect_optional_ocr_runtime_state

INSTALL_ROOT = Path(os.environ.get("ANIMA_INSTALL_ROOT", ROOT / ".runtime-build"))
RESOURCE_ROOT = Path(os.environ.get("ANIMA_RESOURCE_ROOT", ROOT / "resource-library"))
CLASSIFY_RESOURCE_MANIFEST = (
    RESOURCE_ROOT / "classification-indexes" / "e621-classify-20260724-v1" / "resource.json"
)
RUNTIME_PACKAGES = {
    "core": ("core/src/anima_core", "anima_core"),
    "caption-e621": ("workers/caption/src/anima_caption_worker", "anima_caption_worker"),
    "classify-e621": ("workers/classify/src/anima_classify_worker", "anima_classify_worker"),
    "replace-e621": ("workers/replace/src/anima_replace_worker", "anima_replace_worker"),
    "nl": ("workers/nl/src/anima_nl_worker", "anima_nl_worker"),
    "policy": ("workers/policy/src/anima_policy_worker", "anima_policy_worker"),
    "export": ("workers/export/src/anima_export_worker", "anima_export_worker"),
    "token-budget": ("workers/token_budget/src/anima_token_budget_worker", "anima_token_budget_worker"),
    "ocr-paddle": ("workers/ocr/src/anima_ocr_worker", "anima_ocr_worker"),
}
OPTIONAL_RUNTIME_PACKAGES = {
    "ocr-paddle-gpu": ("workers/ocr/src/anima_ocr_worker", "anima_ocr_worker"),
}
SHARED_CAPTION_SOURCE = ROOT / "shared" / "anima_caption_format" / "anima_caption_format"
SHARED_CAPTION_RUNTIMES = ("core", "export", "token-budget")
OCR_ONLY_IMPORTS = frozenset({"paddle", "paddleocr", "paddlex", "anima_ocr_worker"})


def _gpu_formal_targets(project_root: Path, install_root: Path) -> tuple[tuple[Path, bool], ...]:
    return (
        (install_root / "runtimes" / "ocr-paddle-gpu", True),
        (install_root / "manifests" / "runtimes" / "ocr-paddle-gpu.json", False),
        (install_root / "manifests" / "requirements" / "ocr-paddle-gpu.lock", False),
        (project_root / "packaging" / "requirements" / "ocr-paddle-gpu.lock", False),
    )


def _active_runtime_packages(project_root: Path = ROOT, install_root: Path = INSTALL_ROOT) -> dict[str, tuple[str, str]]:
    requested = os.environ.get("ANIMA_OCR_MODE", "auto")
    if requested != "auto" and install_root.resolve() == INSTALL_ROOT.resolve():
        if requested not in {"none", "cpu", "gpu"}:
            raise AssertionError("ANIMA_OCR_MODE is invalid")
        try:
            actual = inspect_optional_ocr_runtime_state(install_root)
        except RuntimeManifestError as exc:
            raise AssertionError("optional OCR runtime state is partial") from exc
        if actual != requested:
            raise AssertionError(f"requested OCR mode {requested} does not match installed state {actual}")
        packages = dict(RUNTIME_PACKAGES)
        packages.pop("ocr-paddle")
        if requested in {"cpu", "gpu"}:
            packages["ocr-paddle"] = RUNTIME_PACKAGES["ocr-paddle"]
        if requested == "gpu":
            packages.update(OPTIONAL_RUNTIME_PACKAGES)
        return packages
    packages = dict(RUNTIME_PACKAGES)
    targets = _gpu_formal_targets(project_root, install_root)
    present = tuple(path.exists() for path, _ in targets)
    if not any(present):
        return packages
    if not all(present):
        raise AssertionError("GPU optional formal artifacts are partial")
    if any(path.is_dir() != is_directory for path, is_directory in targets):
        raise AssertionError("GPU optional formal artifacts have invalid types")
    packages.update(OPTIONAL_RUNTIME_PACKAGES)
    return packages


def _select_runtime_packages(
    selected: str | None,
    *,
    project_root: Path = ROOT,
    install_root: Path = INSTALL_ROOT,
) -> dict[str, tuple[str, str]]:
    if selected is None:
        return _active_runtime_packages(project_root, install_root)
    runtime_ids = tuple(item.strip() for item in selected.split(","))
    if not runtime_ids or any(not item for item in runtime_ids) or len(set(runtime_ids)) != len(runtime_ids):
        raise AssertionError("ANIMA_DRIFT_RUNTIME_IDS is invalid")
    packages = _active_runtime_packages(project_root, install_root)
    unknown = set(runtime_ids) - set(packages)
    if unknown:
        raise AssertionError(f"ANIMA_DRIFT_RUNTIME_IDS contains unknown runtimes: {sorted(unknown)}")
    return {runtime_id: packages[runtime_id] for runtime_id in runtime_ids}


def _selected_runtime_packages() -> dict[str, tuple[str, str]]:
    return _select_runtime_packages(os.environ.get("ANIMA_DRIFT_RUNTIME_IDS"))


def _selected_shared_caption_runtimes() -> tuple[str, ...]:
    selected = _selected_runtime_packages()
    return tuple(runtime_id for runtime_id in SHARED_CAPTION_RUNTIMES if runtime_id in selected)


def _module_constant(relative_path: str, name: str) -> int:
    """Read a module-level int constant without importing the worker package."""
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not defined in {relative_path}")


@unittest.skipUnless(CLASSIFY_RESOURCE_MANIFEST.is_file(), "classify E621 resource has not been assembled")
class ClassifyEntryCountContractTests(unittest.TestCase):
    def test_e621_pins_match_and_wire_contract_accepts_profile_resource_count(self) -> None:
        manifest = json.loads(CLASSIFY_RESOURCE_MANIFEST.read_text(encoding="utf-8"))
        dictionary_path = CLASSIFY_RESOURCE_MANIFEST.parent / manifest["entrypoints"]["dictionary"]
        dictionary = json.loads(dictionary_path.read_text(encoding="utf-8"))
        installed = len(dictionary["entries"])

        self.assertEqual(installed, dictionary["metadata"]["entry_count"])
        self.assertEqual(installed, manifest["metadata"]["dictionaryEntryCount"])
        # core: resource validation and the hello identity check.
        self.assertEqual(installed, CLASSIFY_DICTIONARY_ENTRY_COUNT)
        self.assertEqual(installed, ClassifyHelloResultV1.entryCount)
        # classify worker: manifest pin and the two dictionary load guards.
        self.assertEqual(
            installed,
            _module_constant("workers/classify/src/anima_classify_worker/resource.py", "DICTIONARY_ENTRY_COUNT"),
        )
        worker_dictionary = (ROOT / "workers/classify/src/anima_classify_worker/dictionary.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(2, worker_dictionary.count(f"{installed:_}"), "dictionary.py load guards are stale")
        # The shared wire schema accepts either profile's resource count. Core
        # still compares the result with the exact count frozen from that resource.
        schema = json.loads((ROOT / "contracts/schemas/classify-worker-v1.schema.json").read_text(encoding="utf-8"))
        branches = [
            branch
            for branch in schema["oneOf"]
            if branch.get("properties", {}).get("payloadType", {}).get("const") == "classify_hello_result"
        ]
        self.assertEqual(1, len(branches), "classify_hello_result branch is missing from the schema")
        entry_count = branches[0]["properties"]["entryCount"]
        self.assertEqual("integer", entry_count["type"])
        self.assertLessEqual(entry_count["minimum"], installed)
        self.assertGreaterEqual(entry_count["maximum"], installed)
        self.assertNotIn("const", entry_count)


class OptionalGpuRuntimeDriftContractTests(unittest.TestCase):
    def test_optional_gpu_filter_rejects_empty_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            install = project / ".runtime-build"
            for path, is_directory in _gpu_formal_targets(project, install):
                path.parent.mkdir(parents=True, exist_ok=True)
                if is_directory:
                    path.mkdir()
                else:
                    path.write_bytes(b"fixture")
            for selected in ("", " ", "ocr-paddle,,ocr-paddle-gpu", ",ocr-paddle"):
                with self.subTest(selected=selected):
                    with self.assertRaisesRegex(AssertionError, "invalid"):
                        _select_runtime_packages(selected, project_root=project, install_root=install)

    def test_optional_gpu_formal_artifacts_are_all_or_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            install = project / ".runtime-build"
            self.assertNotIn("ocr-paddle-gpu", _active_runtime_packages(project, install))
            with self.assertRaisesRegex(AssertionError, "unknown runtimes"):
                _select_runtime_packages("ocr-paddle-gpu", project_root=project, install_root=install)

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            install = project / ".runtime-build"
            runtime, _ = _gpu_formal_targets(project, install)[0]
            runtime.mkdir(parents=True)
            with self.assertRaisesRegex(AssertionError, "partial"):
                _active_runtime_packages(project, install)

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            install = project / ".runtime-build"
            for path, is_directory in _gpu_formal_targets(project, install):
                path.parent.mkdir(parents=True, exist_ok=True)
                if is_directory:
                    path.mkdir()
                else:
                    path.write_bytes(b"fixture")
            packages = _active_runtime_packages(project, install)
            self.assertEqual(OPTIONAL_RUNTIME_PACKAGES["ocr-paddle-gpu"], packages["ocr-paddle-gpu"])
            self.assertEqual(
                OPTIONAL_RUNTIME_PACKAGES,
                _select_runtime_packages("ocr-paddle-gpu", project_root=project, install_root=install),
            )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            install = project / ".runtime-build"
            for path, is_directory in _gpu_formal_targets(project, install):
                path.parent.mkdir(parents=True, exist_ok=True)
                if is_directory:
                    path.mkdir()
                else:
                    path.write_bytes(b"fixture")
            runtime, _ = _gpu_formal_targets(project, install)[0]
            runtime.rmdir()
            runtime.write_bytes(b"wrong type")
            with self.assertRaisesRegex(AssertionError, "invalid types"):
                _active_runtime_packages(project, install)


@unittest.skipUnless((INSTALL_ROOT / "manifests" / "runtimes").is_dir(), "embedded release tree has not been built")
@unittest.skipUnless((ROOT / "core" / "src" / "anima_core").is_dir(), "source tree is not present")
class AssembledRuntimeDriftTests(unittest.TestCase):
    def test_shared_caption_format_is_byte_identical_and_manifested_for_each_owner(self) -> None:
        self.assertTrue(SHARED_CAPTION_SOURCE.is_dir(), "shared caption format source is missing")
        source_modules = {path.relative_to(SHARED_CAPTION_SOURCE).as_posix() for path in SHARED_CAPTION_SOURCE.rglob("*.py")}
        for runtime_id in _selected_shared_caption_runtimes():
            with self.subTest(runtime_id=runtime_id):
                assembled_root = INSTALL_ROOT / "runtimes" / runtime_id / "Lib" / "site-packages" / "anima_caption_format"
                self.assertTrue(assembled_root.is_dir(), f"{runtime_id} shared caption format is not assembled")
                assembled_modules = {path.relative_to(assembled_root).as_posix() for path in assembled_root.rglob("*.py")}
                self.assertEqual(source_modules, assembled_modules)
                manifest = json.loads((INSTALL_ROOT / "manifests" / "runtimes" / f"{runtime_id}.json").read_text(encoding="utf-8"))
                critical = manifest["runtime"]["criticalFilesSha256"]
                for relative in source_modules:
                    source = SHARED_CAPTION_SOURCE / relative
                    assembled = assembled_root / relative
                    self.assertTrue(filecmp.cmp(source, assembled, shallow=False), relative)
                    manifest_relative = str(assembled.relative_to(INSTALL_ROOT)).replace("/", "\\")
                    self.assertEqual(hashlib.sha256(assembled.read_bytes()).hexdigest(), critical.get(manifest_relative))

    def test_paddle_and_ocr_worker_imports_remain_owned_by_the_ocr_runtime(self) -> None:
        source_roots = [ROOT / "core" / "src", *(ROOT / "workers").glob("*/src")]
        for source_root in source_roots:
            is_ocr_owner = source_root == ROOT / "workers" / "ocr" / "src"
            for path in source_root.rglob("*.py"):
                imported: set[str] = set()
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".", 1)[0])
                forbidden = imported & OCR_ONLY_IMPORTS
                if is_ocr_owner:
                    self.assertNotIn("anima_ocr_worker", forbidden, path)
                else:
                    self.assertEqual(set(), forbidden, path)

    def test_assembled_runtimes_are_byte_identical_to_the_source_tree(self) -> None:
        for runtime_id, (source_relative, package) in _selected_runtime_packages().items():
            with self.subTest(runtime_id=runtime_id):
                source = ROOT / source_relative
                assembled = INSTALL_ROOT / "runtimes" / runtime_id / "Lib" / "site-packages" / package
                self.assertTrue(assembled.is_dir(), f"{runtime_id} is not assembled")
                source_modules = {
                    path.relative_to(source).as_posix() for path in source.rglob("*.py")
                }
                assembled_modules = {
                    path.relative_to(assembled).as_posix()
                    for path in assembled.rglob("*.py")
                    if "__pycache__" not in path.parts
                }
                self.assertEqual(source_modules, assembled_modules, f"{runtime_id} module set drifted")
                stale = [
                    relative
                    for relative in sorted(source_modules)
                    if not filecmp.cmp(source / relative, assembled / relative, shallow=False)
                ]
                self.assertEqual([], stale, f"{runtime_id} needs re-assembly")

    def test_runtime_manifest_hashes_describe_the_assembled_files(self) -> None:
        for runtime_id in _selected_runtime_packages():
            with self.subTest(runtime_id=runtime_id):
                manifest = json.loads(
                    (INSTALL_ROOT / "manifests" / "runtimes" / f"{runtime_id}.json").read_text(encoding="utf-8")
                )
                for relative, expected in manifest["runtime"]["criticalFilesSha256"].items():
                    path = INSTALL_ROOT / relative.replace("\\", os.sep)
                    self.assertTrue(path.is_file(), f"{runtime_id}: {relative} is missing")
                    self.assertEqual(
                        expected,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        f"{runtime_id}: {relative} does not match its manifest hash",
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ocr-mode", choices=("auto", "none", "cpu", "gpu"), default="auto")
    arguments, remaining = parser.parse_known_args()
    os.environ["ANIMA_OCR_MODE"] = arguments.ocr_mode
    unittest.main(argv=[sys.argv[0], *remaining])
