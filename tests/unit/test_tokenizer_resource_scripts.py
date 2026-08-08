"""Preview-only lifecycle contracts for tokenizer resources."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "scripts" / "tokenizer_resource.py"
IMPORT_PS1 = ROOT / "packaging" / "scripts" / "Import-TokenizerResources.ps1"
RESET_PS1 = ROOT / "packaging" / "scripts" / "Reset-TokenBudgetRuntime.ps1"
CLEAN_PS1 = ROOT / "packaging" / "scripts" / "Clean-TokenBudgetArtifacts.ps1"


def _load_module():
    spec = importlib.util.spec_from_file_location("tokenizer_resource_for_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("tokenizer resource driver is missing")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            del sys.modules[spec.name]
        else:
            sys.modules[spec.name] = previous
    return module


class TokenizerResourceScriptTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        (root / "packaging" / "requirements").mkdir(parents=True)
        (root / "packaging" / "requirements" / "token-budget.in").write_text("tokenizers==0.21.4\n", encoding="ascii")
        (root / "resource-library").mkdir()
        (root / ".runtime-build" / "runtimes").mkdir(parents=True)
        (root / ".runtime-build" / "manifests" / "runtimes").mkdir(parents=True)
        (root / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64").mkdir(parents=True)
        (root / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64" / "python.exe").write_bytes(b"fixture")

    def test_source_identities_are_exact_and_preview_is_read_only(self) -> None:
        module = _load_module()
        self.assertEqual(
            {
                "tokenizer-qwen3-0.6b-anima-v1": {
                    "model_id": "Qwen/Qwen3-0.6B",
                    "context_path": ("max_position_embeddings",),
                },
                "tokenizer-qwen3-vl-4b-krea2-v1": {
                    "model_id": "Qwen/Qwen3-VL-4B-Instruct",
                    "context_path": ("text_config", "max_position_embeddings"),
                },
            },
            module.TOKENIZER_SOURCES,
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._project(project)
            before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))
            preview = module.plan_import(project)
            after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual("preview", preview["mode"])
        self.assertEqual("ImportTokenizerResources", preview["action"])
        self.assertEqual(["tokenizers==0.21.4"], preview["requirements"])
        self.assertEqual(set(module.TOKENIZER_SOURCES), set(preview["resources"]))
        self.assertIn("token-budget", preview["targets"]["runtime"])
        self.assertIn("download tokenizer files", preview["applyChanges"])

    def test_import_preview_reports_existing_verified_resource_packages_without_writing(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._project(project)
            for resource_id in module.TOKENIZER_SOURCES:
                package = project / "resource-library" / "tokenizers" / resource_id
                package.mkdir(parents=True)
                (package / "resource.json").write_text(
                    json.dumps({"fingerprint": "a" * 64}), encoding="utf-8"
                )
            before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))
            preview = module.plan_import(project)
            after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))

        self.assertEqual(before, after)
        self.assertEqual("installed", preview["resources"]["tokenizer-qwen3-0.6b-anima-v1"]["installation"])
        self.assertEqual("a" * 64, preview["resources"]["tokenizer-qwen3-0.6b-anima-v1"]["fingerprint"])

    def test_context_limit_requires_exactly_one_declared_path_and_weight_files_are_rejected(self) -> None:
        module = _load_module()
        self.assertEqual(17, module.context_limit({"max_position_embeddings": 17}, ("max_position_embeddings",)))
        with self.assertRaisesRegex(module.TokenizerResourceError, "conflicting context limit"):
            module.context_limit({"max_position_embeddings": 17, "text_config": {"max_position_embeddings": 18}}, ("max_position_embeddings",))
        with self.assertRaisesRegex(module.TokenizerResourceError, "context limit"):
            module.context_limit({}, ("max_position_embeddings",))
        for filename in ("model.safetensors", "pytorch_model.bin", "model.onnx", "unknown.txt"):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(module.TokenizerResourceError, "allowlist"):
                    module.validate_downloaded_files({filename: (1, "a" * 64)})

    def test_official_head_preserves_immutable_revision_from_first_redirect(self) -> None:
        module = _load_module()
        url = "https://huggingface.co/Qwen/Qwen3-0.6B/resolve/main/tokenizer.json"
        headers = Message()
        headers["x-repo-commit"] = "a" * 40

        class RedirectingOpener:
            def open(self, request, timeout):
                raise HTTPError(url, 307, "Temporary Redirect", headers, None)

        original_urlopen = module.urllib.request.urlopen
        original_build_opener = module.urllib.request.build_opener
        try:
            module.urllib.request.urlopen = lambda request, timeout: (_ for _ in ()).throw(
                HTTPError(url, 307, "Temporary Redirect", headers, None)
            )
            module.urllib.request.build_opener = lambda *handlers: RedirectingOpener()
            status, final_url, result_headers = module._read_official_head(url)
        finally:
            module.urllib.request.urlopen = original_urlopen
            module.urllib.request.build_opener = original_build_opener

        self.assertEqual(307, status)
        self.assertEqual(url, final_url)
        self.assertEqual("a" * 40, result_headers["x-repo-commit"])

    def test_preview_lifecycle_scripts_and_root_entrypoints_keep_apply_explicit(self) -> None:
        for script in (IMPORT_PS1, RESET_PS1, CLEAN_PS1):
            self.assertTrue(script.is_file(), script)
            source = script.read_text(encoding="utf-8")
            self.assertIn("-B', '-I'", source)
            self.assertIn("--apply", source)
            self.assertIn("tokenizer_resource.py", source)
        for name in ("Import-TokenizerResources.bat", "Reset-TokenBudgetRuntime.bat", "Clean-TokenBudgetArtifacts.bat"):
            source = (ROOT / name).read_text(encoding="ascii")
            self.assertIn("powershell.exe", source)
            self.assertIn(name.replace(".bat", ".ps1"), source)

    def test_reset_and_clean_previews_only_name_project_local_token_budget_targets(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._project(project)
            reset = module.plan_reset(project)
            clean = module.plan_clean(project)
        self.assertEqual("preview", reset["mode"])
        self.assertEqual("preview", clean["mode"])
        self.assertEqual([], clean["targets"])
        self.assertIn("token-budget", reset["targets"]["runtime"])
        self.assertIn("token-budget", reset["targets"]["manifest"])

    def test_apply_source_contract_resolves_immutable_commits_and_stages_only_allowlisted_files(self) -> None:
        module = _load_module()
        commits = {
            "Qwen/Qwen3-0.6B": "a" * 40,
            "Qwen/Qwen3-VL-4B-Instruct": "b" * 40,
        }
        requests: list[str] = []

        def fetch_head(url: str) -> object:
            requests.append(url)
            self.assertNotIn("/api/", url)
            prefix = "https://huggingface.co/"
            model_id, _, filename = url.removeprefix(prefix).partition("/resolve/main/")
            self.assertIn(model_id, commits)
            if filename not in {"config.json", "tokenizer.json"}:
                return (404, url, {})
            return (200, f"https://huggingface.co/{model_id}/resolve/{commits[model_id]}/{filename}", {"x-repo-commit": commits[model_id]})

        sources = module.resolve_official_tokenizer_sources(fetch_head=fetch_head)
        self.assertTrue(requests)
        self.assertTrue(all("/resolve/main/" in url for url in requests))
        self.assertEqual(("config.json", "tokenizer.json"), sources["tokenizer-qwen3-0.6b-anima-v1"].files)
        self.assertEqual("a" * 40, sources["tokenizer-qwen3-0.6b-anima-v1"].revision)

        tokenizer_json = b'{"version":"1.0","truncation":null,"padding":null,"added_tokens":[],"normalizer":null,"pre_tokenizer":null,"post_processor":null,"decoder":null,"model":{"type":"WordLevel","vocab":{},"unk_token":"[UNK]"}}'

        def fetch_bytes(url: str) -> bytes:
            self.assertIn("/resolve/", url)
            self.assertNotIn("model.safetensors", url)
            filename = url.rsplit("/", 1)[1]
            if filename == "tokenizer.json":
                return tokenizer_json
            return b'{"text_config":{"max_position_embeddings":32768}}' if "Qwen3-VL-4B-Instruct" in url else b'{"max_position_embeddings":32768}'

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._project(project)
            layout = module.project_layout(project)
            stage = project / ".runtime-build" / "tokenizer-import" / "stage"
            staged = module.stage_tokenizer_resources(layout, stage, sources, fetch_bytes=fetch_bytes)
            self.assertFalse(layout.resource_target.exists(), "formal resources must not be written before publish")
            manifest = json.loads((staged["tokenizer-qwen3-0.6b-anima-v1"] / "resource.json").read_text(encoding="utf-8"))

        self.assertEqual("Qwen/Qwen3-0.6B", manifest["officialModelId"])
        self.assertEqual("a" * 40, manifest["revision"])
        self.assertEqual(32768, manifest["contextLimit"])
        self.assertEqual(["config.json", "tokenizer.json"], [item["path"] for item in manifest["files"]])


if __name__ == "__main__":
    unittest.main()
