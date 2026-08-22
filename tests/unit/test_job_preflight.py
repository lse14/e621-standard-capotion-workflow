from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from PIL import Image

from anima_core.contracts import JobConfig, sha256_json
from anima_core.db import StateDatabase
from anima_core.job_preflight import JobPreflightError, JobPreparationService, config_from_dict
from anima_core.locks import DatasetLockError
from anima_core.overlay import OverlayLayout
from anima_core.path_safety import windows_key
from anima_core.resource_catalog import ResourceCatalog


OCR_RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
TOKENIZER_RESOURCE_ID = "tokenizer-qwen3-0.6b-anima-v1"


def _legacy_resource_metadata(kind: str) -> dict[str, object]:
    if kind == "replacement-index":
        return {
            "ruleCount": 1,
            "actionCounts": {"keep": 1, "replace": 0, "drop": 0},
            "pipeReplacementCount": 0,
            "literalKeepPipeCount": 0,
        }
    if kind == "classification-index":
        return {
            "dictionaryEntryCount": 1,
            "wikiDataSourceId": "wiki-test-v1",
            "wikiApplicationId": 1,
            "wikiSchemaVersion": 1,
            "wikiSchemaFingerprint": "a" * 64,
            "wikiPageTitles": ["solo"],
        }
    if kind == "tagging-model":
        return {"tagCount": 1, "categories": ["general", "character", "species", "rating"]}
    return {}


def _write_test_resource_library(root: Path, *, include_ocr: bool) -> tuple[ResourceCatalog, Path | None]:
    library = root / "resource-library"
    library.mkdir()
    layouts = {
        "replacement-index": (
            "replacement-indexes", "replace-default", "e621-replacement-csv-v1", {"index": "index.csv"},
        ),
        "classification-index": (
            "classification-indexes", "classify-default", "e621-classification-index-v1",
            {"dictionary": "dictionary.json", "countDatabase": "count.sqlite3"},
        ),
        "tagging-model": (
            "tagging-models", "tagger-default", "e621-eva02-onnx-v1",
            {
                "model": "model.onnx",
                "modelData": "model.onnx.data",
                "preprocess": "preprocess.json",
                "tags": "tags.json",
                "thresholds": "thresholds.json",
            },
        ),
        "dropout-model": (
            "dropout-models", "dropout-default", "lse14-scorer-5k-v1",
            {
                "clip": "clip/model.pt",
                "fusion": "fusion/model.safetensors",
                "jtp3": "jtp3/model.safetensors",
                "waifu": "waifu/model.safetensors",
            },
        ),
    }
    defaults = {
        "replacementIndex": "replace-default",
        "classificationIndex": "classify-default",
        "taggingModel": "tagger-default",
        "dropoutModel": "dropout-default",
    }
    (library / "defaults.json").write_text(
        json.dumps({"schemaVersion": 1, "defaults": defaults}), encoding="utf-8",
    )
    for kind, (category, resource_id, runtime_format, entrypoints) in layouts.items():
        package = library / category / resource_id
        package.mkdir(parents=True)
        files: dict[str, dict[str, object]] = {}
        for index, relative in enumerate(entrypoints.values(), start=1):
            target = package / Path(relative.replace("/", os.sep))
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative == "tags.json":
                content = b'{"tag_names":["tag_a"]}'
            elif relative == "dictionary.json":
                content = b'{"entries":{"tag_a":{}}}'
            elif relative == "thresholds.json":
                content = b'{"general":0.5,"character":0.5,"species":0.5,"rating":0.5}'
            else:
                content = f"file-{index}".encode("ascii")
            target.write_bytes(content)
            files[relative.replace("/", "\\")] = {
                "sizeBytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        manifest = {
            "schemaVersion": 1,
            "kind": kind,
            "resourceId": resource_id,
            "resourceVersion": "test-v1",
            "profile": "e621",
            "displayName": {"zh-CN": "Test", "en": "Test"},
            "description": {"zh-CN": "Test resource", "en": "Test resource"},
            "runtimeFormat": runtime_format,
            "entrypoints": entrypoints,
            "files": files,
            "metadata": _legacy_resource_metadata(kind),
            "documentation": [],
        }
        (package / "resource.json").write_text(json.dumps(manifest), encoding="utf-8")

    ocr_manifest: Path | None = None
    if include_ocr:
        package = library / "ocr-models" / OCR_RESOURCE_ID
        contents = {
            r"detection\inference.json": b'{"model":"det"}',
            r"detection\model.pdiparams": b"det-params",
            r"recognition\inference.json": b'{"model":"rec"}',
            r"recognition\model.pdiparams": b"rec-params",
            r"textline-orientation\inference.json": b'{"model":"orientation"}',
            r"textline-orientation\model.pdiparams": b"orientation-params",
        }
        package.mkdir(parents=True)
        files: dict[str, dict[str, object]] = {}
        for relative, content in contents.items():
            target = package / Path(relative.replace("\\", os.sep))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            files[relative] = {
                "sizeBytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        manifest = {
            "schemaVersion": 2,
            "kind": "ocr-model",
            "resourceId": OCR_RESOURCE_ID,
            "resourceVersion": "ppocrv5-server-paddle-v1",
            "profile": "shared",
            "displayName": {"zh-CN": "OCR test", "en": "OCR test"},
            "description": {"zh-CN": "Test OCR resource", "en": "Test OCR resource"},
            "runtimeFormat": "ppocrv5-server-paddle-v1",
            "distribution": {"mode": "bundled"},
            "entrypoints": {
                "detection": r"detection\inference.json",
                "recognition": r"recognition\inference.json",
                "textlineOrientation": r"textline-orientation\inference.json",
            },
            "files": files,
            "metadata": {
                "models": {
                    "detection": "PP-OCRv5_server_det",
                    "recognition": "PP-OCRv5_server_rec",
                    "textlineOrientation": "PP-LCNet_x1_0_textline_ori",
                },
                "inference": {
                    "useDocOrientationClassify": False,
                    "useDocUnwarping": False,
                    "useTextlineOrientation": True,
                    "textRecScoreThresh": 0,
                    "textDetLimitSideLen": 1920,
                    "textDetLimitType": "max",
                },
            },
            "documentation": [],
        }
        ocr_manifest = package / "resource.json"
        ocr_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    return ResourceCatalog(library), ocr_manifest


class JobPreflightTests(unittest.TestCase):
    def test_v9_config_has_no_task_profile_and_rejects_legacy_job_configs(self) -> None:
        legacy = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental",
            sourceRoot="C:\\dataset", schemaVersion=8,
        ).to_dict()
        candidate = {
            **legacy,
            "schemaVersion": 9,
            "classify": {
                "enabled": True,
                "indexMode": "bundled",
                "resourceId": "classify-e621-20260724-v1",
                "overwriteJson": False,
                "overwriteCount": False,
            },
        }
        candidate.pop("profile")
        candidate["nl"]["systemPrompt"] = "Describe the visible image."

        try:
            parsed = config_from_dict(candidate)
        except JobPreflightError as exc:
            self.fail(f"JobConfig v9 without a task profile must be accepted: {exc}")
        self.assertNotIn("profile", parsed.to_dict())
        self.assertEqual("bundled", parsed.classify["indexMode"])

        with self.assertRaisesRegex(JobPreflightError, "incompatible|reinitialize|重新初始化"):
            config_from_dict(legacy)
        candidate["profile"] = "e621"
        with self.assertRaisesRegex(JobPreflightError, "profile|shape"):
            config_from_dict(candidate)

    def test_v9_classify_mode_rejects_mixed_client_inputs_and_frozen_metadata(self) -> None:
        base = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental",
            sourceRoot="C:\\dataset", schemaVersion=8,
        ).to_dict()
        base.pop("profile")
        base["schemaVersion"] = 9
        base["nl"]["systemPrompt"] = "Describe the visible image."
        base["classify"] = {
            "enabled": True,
            "indexMode": "custom",
            "customResourcePath": "C:\\resources\\resource.json",
            "overwriteJson": False,
            "overwriteCount": False,
        }

        mixed = json.loads(json.dumps(base))
        mixed["classify"]["resourceId"] = "classify-e621-20260724-v1"
        with self.assertRaisesRegex(JobPreflightError, "custom|classify"):
            config_from_dict(mixed)

        frozen = json.loads(json.dumps(base))
        frozen["classify"]["resourceManifestRelativePath"] = "classification-indexes\\custom\\resource.json"
        frozen["classify"]["resourceFingerprint"] = "a" * 64
        with self.assertRaisesRegex(JobPreflightError, "assigned by preflight"):
            JobPreparationService._reject_client_frozen_resources(frozen)

    def test_v9_preflight_derives_classify_wiki_data_source_id_in_frozen_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            (catalog.root / "defaults.json").write_text(json.dumps({
                "schemaVersion": 3,
                "defaults": {
                    "replacementIndex": "replace-default",
                    "classificationIndex": "classify-default",
                    "taggingModel": "tagger-default",
                    "dropoutModel": "dropout-default",
                },
            }), encoding="utf-8")
            config = JobConfig(
                profile="e621", workMode="in_place", overwriteMode="incremental",
                sourceRoot=str(source), schemaVersion=8,
            ).to_dict()
            config.pop("profile")
            config["schemaVersion"] = 9
            config["nl"]["systemPrompt"] = "Describe the visible image."
            config["caption"]["resourceId"] = "tagger-default"
            config["classify"] = {
                "enabled": True,
                "indexMode": "bundled",
                "overwriteJson": False,
                "overwriteCount": False,
                "resourceId": "classify-default",
            }
            config["replace"]["resourceId"] = "replace-default"
            config["dropout"]["quality"]["resourceId"] = "dropout-default"
            config["tokenBudget"]["enabled"] = False
            self.assertNotIn("wikiDataSourceId", config["classify"])

            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                summary = service.preflight(config)
                database = StateDatabase.open(root / "state.db")
                try:
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))
                    self.assertEqual("wiki-test-v1", frozen["classify"]["wikiDataSourceId"])
                finally:
                    database.close()
            finally:
                service.close()

    @staticmethod
    def _binding_api():
        spec = importlib.util.find_spec("anima_core.ocr_runtime_binding")
        if spec is None:
            return None
        return importlib.import_module("anima_core.ocr_runtime_binding")

    def test_ocr_execution_request_normalizes_auto_and_rejects_invalid_manual_values(self) -> None:
        binding = self._binding_api()
        self.assertIsNotNone(binding, "Task 3.4 requires a task-owned OCR execution binding module")
        if binding is None:
            return
        self.assertEqual(binding.OcrExecutionRequestV1.auto(), binding.normalize_ocr_execution(None))
        self.assertEqual(
            {
                "textDetLimitSideLen": {"mode": "manual", "value": 2304},
                "textBatchSize": {"mode": "manual", "value": 2},
            },
            binding.normalize_ocr_execution({
                "textDetLimitSideLen": {"mode": "manual", "value": 2304},
                "textBatchSize": {"mode": "manual", "value": 2},
            }).to_dict(),
        )
        invalid_values = (
            {"textDetLimitSideLen": {"mode": "manual", "value": 1919}},
            {"textDetLimitSideLen": {"mode": "manual", "value": 2305}},
            {"textDetLimitSideLen": {"mode": "manual", "value": 3841}},
            {"textDetLimitSideLen": {"mode": "manual", "value": True}},
            {"textDetLimitSideLen": {"mode": "manual", "value": 1920.0}},
            {"textBatchSize": {"mode": "manual", "value": 0}},
            {"textBatchSize": {"mode": "manual", "value": 9}},
            {"textBatchSize": {"mode": "manual", "value": True}},
            {"textBatchSize": {"mode": "manual", "value": 1.0}},
            {"textBatchSize": {"mode": "auto", "value": 1}},
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(binding.OcrExecutionError):
                binding.normalize_ocr_execution(value)

    def test_ocr_execution_recommendations_follow_the_conservative_vram_table(self) -> None:
        binding = self._binding_api()
        self.assertIsNotNone(binding, "Task 3.4 requires conservative OCR tuning recommendations")
        if binding is None:
            return
        gib = 1024 ** 3
        self.assertEqual(binding.RecommendedTuning(1920, 1, "cpu"), binding.recommend_tuning(device="cpu", total_vram_bytes=None))
        self.assertEqual(binding.RecommendedTuning(1920, 1, "unavailable_fallback"), binding.recommend_tuning(device="cuda", total_vram_bytes=None))
        self.assertEqual(binding.RecommendedTuning(1920, 1, "gpu_vram_table"), binding.recommend_tuning(device="cuda", total_vram_bytes=12 * gib - 1))
        self.assertEqual(binding.RecommendedTuning(2304, 2, "gpu_vram_table"), binding.recommend_tuning(device="cuda", total_vram_bytes=12 * gib))
        self.assertEqual(binding.RecommendedTuning(2304, 2, "gpu_vram_table"), binding.recommend_tuning(device="cuda", total_vram_bytes=24 * gib - 1))
        self.assertEqual(binding.RecommendedTuning(2560, 4, "gpu_vram_table"), binding.recommend_tuning(device="cuda", total_vram_bytes=24 * gib))

    def test_ocr_runtime_binding_is_atomic_and_revalidates_an_existing_record(self) -> None:
        binding = self._binding_api()
        self.assertIsNotNone(binding, "Task 3.4 requires an immutable OCR runtime binding record")
        if binding is None:
            return
        record = binding.OcrRuntimeBindingV1.from_dict({
            "schemaVersion": 1,
            "requested": {
                "device": "cpu",
                "textDetLimitSideLen": {"mode": "auto", "value": None},
                "textBatchSize": {"mode": "manual", "value": 2},
            },
            "recommended": {
                "source": "cpu",
                "totalVramBytes": None,
                "textDetLimitSideLen": 1920,
                "textBatchSize": 1,
            },
            "effective": {"textDetLimitSideLen": 1920, "textBatchSize": 2},
            "runtime": {
                "runtimeId": "ocr-paddle",
                "runtimeFingerprint": "a" * 64,
                "observedDevice": "cpu",
                "paddleVersion": "3.2.2",
                "compiledWithCuda": False,
                "cudaVersion": None,
                "gpuName": None,
            },
            "resourceFingerprint": "b" * 64,
        })
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ocr-runtime-binding-v1.json"
            binding.write_runtime_binding(path, record)
            self.assertEqual(record, binding.read_runtime_binding(path))
            binding.write_runtime_binding(path, record)
            changed = binding.OcrRuntimeBindingV1.from_dict({
                **record.to_dict(),
                "runtime": {
                    **record.to_dict()["runtime"],
                    "runtimeFingerprint": "c" * 64,
                },
            })
            with self.assertRaises(binding.OcrExecutionError):
                binding.write_runtime_binding(path, changed)

    def _config(self, source: Path) -> dict[str, object]:
        config = JobConfig(profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(source))
        config.nl["systemPrompt"] = "describe the visible image"
        return config.to_dict()

    def test_dataset_claim_conflict_keeps_new_job_ready(self) -> None:
        transitions = {
            "interrupted": ("preparing_workspace", "interrupted"),
            "failed": ("preparing_workspace", "failed"),
            "cancelled_recoverable": ("preparing_workspace", "cancelling", "cancelled_recoverable"),
        }
        for owner_status, owner_transitions in transitions.items():
            with self.subTest(owner_status=owner_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                dataset = root / "dataset"
                dataset.mkdir()
                Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
                catalog, _ = _write_test_resource_library(root, include_ocr=False)
                service = JobPreparationService(root / "state.db", resource_catalog=catalog)
                try:
                    owner = service.preflight(self._ocr_config(dataset, enabled=False))
                    contender = service.preflight(self._ocr_config(dataset, enabled=False))
                    database = StateDatabase.open(root / "state.db")
                    try:
                        overlay = OverlayLayout.create(dataset, owner.jobId)
                        database.set_workspace_metadata(
                            owner.jobId,
                            dataset_root=str(dataset),
                            dataset_root_key=windows_key(dataset),
                            overlay_root=str(overlay.root),
                        )
                        for status in owner_transitions:
                            database.set_job_status(owner.jobId, status, current_module_id="workspace")
                        database.connection.execute(
                            """INSERT INTO dataset_claims(dataset_root,dataset_root_key,job_id,lock_path,acquired_at)
                               VALUES (?,?,?,?,?)""",
                            (str(dataset), windows_key(dataset), owner.jobId, str(root / ".restart.lock"), "2026-08-17T00:00:00Z"),
                        )

                        with self.assertRaises(DatasetLockError):
                            service.confirm_workspace(contender.jobId, confirmed=True, confirmed_rebuild=False)

                        self.assertEqual(owner_status, database.get_job(owner.jobId)["status"])
                        self.assertEqual("ready", database.get_job(contender.jobId)["status"])
                        claim = database.connection.execute(
                            "SELECT job_id FROM dataset_claims WHERE dataset_root=?", (str(dataset),)
                        ).fetchone()
                        self.assertEqual(owner.jobId, claim["job_id"] if claim is not None else None)
                        self.assertTrue(overlay.root.is_dir())
                    finally:
                        database.close()
                finally:
                    service.close()

    def test_succeeded_dataset_claim_is_released_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                owner = service.preflight(self._ocr_config(dataset, enabled=False))
                contender = service.preflight(self._ocr_config(dataset, enabled=False))
                database = StateDatabase.open(root / "state.db")
                try:
                    overlay = OverlayLayout.create(dataset, owner.jobId)
                    database.set_workspace_metadata(
                        owner.jobId,
                        dataset_root=str(dataset),
                        dataset_root_key=windows_key(dataset),
                        overlay_root=str(overlay.root),
                    )
                    for status in ("preparing_workspace", "running", "reviewing", "exporting", "committing", "succeeded"):
                        database.set_job_status(owner.jobId, status, current_module_id="workspace")
                    database.connection.execute(
                        """INSERT INTO dataset_claims(dataset_root,dataset_root_key,job_id,lock_path,acquired_at)
                           VALUES (?,?,?,?,?)""",
                        (str(dataset), windows_key(dataset), owner.jobId, str(root / ".restart.lock"), "2026-08-17T00:00:00Z"),
                    )
                finally:
                    database.close()

                try:
                    workspace = service.confirm_workspace(contender.jobId, confirmed=True, confirmed_rebuild=False)
                except DatasetLockError:
                    workspace = None
                self.assertIsNotNone(workspace, "a succeeded task's durable claim must be released before confirmation")
                if workspace is not None:
                    self.assertEqual("preparing_workspace", workspace["status"])
                database = StateDatabase.open(root / "state.db")
                try:
                    self.assertEqual("preparing_workspace", database.get_job(contender.jobId)["status"])
                    claims = database.connection.execute("SELECT job_id FROM dataset_claims").fetchall()
                    self.assertEqual([contender.jobId], [row["job_id"] for row in claims])
                finally:
                    database.close()
            finally:
                service.close()

    def test_succeeded_live_dataset_lock_is_released_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            Image.new("RGB", (3, 3), "white").save(dataset / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                owner = service.preflight(self._ocr_config(dataset, enabled=False))
                contender = service.preflight(self._ocr_config(dataset, enabled=False))
                service.confirm_workspace(owner.jobId, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    for status in ("running", "reviewing", "exporting", "committing", "succeeded"):
                        database.set_job_status(owner.jobId, status, current_module_id="workspace")
                finally:
                    database.close()

                try:
                    workspace = service.confirm_workspace(contender.jobId, confirmed=True, confirmed_rebuild=False)
                except DatasetLockError:
                    workspace = None
                self.assertIsNotNone(workspace, "a succeeded task's live dataset lock must be released before confirmation")
                if workspace is not None:
                    self.assertEqual("preparing_workspace", workspace["status"])
            finally:
                service.close()

    def test_minimal_v5_config_selects_v3_without_changing_v2_to_v4_defaults(self) -> None:
        v5 = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\dataset", schemaVersion=5,
        )
        v4 = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot="C:\\dataset", schemaVersion=4,
        )

        self.assertEqual("nl-default-prompt-v3", v5.nl["promptVersion"])
        self.assertEqual("nl-default-prompt-v2", v4.nl["promptVersion"])

    def _ocr_config(self, source: Path, *, enabled: bool) -> dict[str, object]:
        config = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(source), schemaVersion=5,
        )
        config.nl.update({"systemPrompt": "describe the visible image", "promptVersion": "nl-default-prompt-v3"})
        config.caption["resourceId"] = "tagger-default"
        config.classify["resourceId"] = "classify-default"
        config.replace["resourceId"] = "replace-default"
        config.dropout["quality"]["resourceId"] = "dropout-default"
        config.ocr["enabled"] = enabled
        return config.to_dict()

    @staticmethod
    def _write_tokenizer_resource(library: Path, *, context_limit: int = 1) -> Path:
        package = library / "tokenizers" / TOKENIZER_RESOURCE_ID
        package.mkdir(parents=True)
        files = []
        for relative, content in (
            ("config.json", b'{"max_position_embeddings":1}'),
            ("tokenizer.json", b'{"version":"1.0"}'),
        ):
            (package / relative).write_bytes(content)
            files.append({
                "path": relative,
                "sizeBytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        manifest = {
            "schemaVersion": 3,
            "kind": "tokenizer",
            "resourceId": TOKENIZER_RESOURCE_ID,
            "owner": "token-budget",
            "profile": "shared",
            "resourceVersion": "test-v1",
            "officialModelId": "Qwen/Qwen3-0.6B",
            "revision": "a" * 40,
            "tokenizerFamily": "qwen3",
            "contextLimit": context_limit,
            "rootRelativePath": f"tokenizers\\{TOKENIZER_RESOURCE_ID}",
            "files": files,
            "distribution": {
                "mode": "local-only",
                "sourceUrl": "https://huggingface.co/Qwen/Qwen3-0.6B",
                "licenseStatus": "unverified",
            },
        }
        manifest["fingerprint"] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        path = package / "resource.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def _token_budget_config(
        self,
        source: Path,
        *,
        enabled: bool,
        max_tokens: int = 1,
        schema_version: int = 6,
    ) -> dict[str, object]:
        config = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(source), schemaVersion=schema_version,
        )
        config.caption["resourceId"] = "tagger-default"
        config.classify["resourceId"] = "classify-default"
        config.replace["resourceId"] = "replace-default"
        config.dropout["quality"]["resourceId"] = "dropout-default"
        config.nl["systemPrompt"] = "describe the visible image"
        config.tokenBudget["enabled"] = enabled
        config.tokenBudget["maxTokens"] = max_tokens
        return config.to_dict()

    def _v7_ocr_config(self, source: Path, *, enabled: bool) -> dict[str, object]:
        config = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(source), schemaVersion=7,
        )
        config.caption["resourceId"] = "tagger-default"
        config.classify["resourceId"] = "classify-default"
        config.replace["resourceId"] = "replace-default"
        config.dropout["quality"]["resourceId"] = "dropout-default"
        config.nl["systemPrompt"] = "describe the visible image"
        config.tokenBudget["enabled"] = False
        config.ocr["enabled"] = enabled
        return config.to_dict()

    def _v8_config(self, source: Path, *, input_txt_mode: str = "tag") -> JobConfig:
        config = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=str(source), schemaVersion=8,
        )
        config.caption["resourceId"] = "tagger-default"
        config.caption["inputTxtMode"] = input_txt_mode
        config.classify["resourceId"] = "classify-default"
        config.replace["resourceId"] = "replace-default"
        config.dropout["quality"]["resourceId"] = "dropout-default"
        config.nl["systemPrompt"] = "describe the visible image"
        config.tokenBudget["enabled"] = False
        return config

    def test_v8_input_nl_has_no_api_prompt_or_budget_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            config = self._v8_config(source, input_txt_mode="nl")
            config.nl.update({"apiEnabled": True, "systemPrompt": ""})
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                summary = service.preflight(config.to_dict())
                self.assertEqual((0, 0, 0, 0, 0), (
                    summary.api["candidateCount"],
                    summary.api["minRequests"],
                    summary.api["maxPrimaryRequests"],
                    summary.api["maxWithBackupRequests"],
                    summary.api["httpAttemptBudget"],
                ))
                database = StateDatabase.open(root / "state.db")
                try:
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))
                    self.assertNotIn("apiPolicy", frozen["nl"])
                finally:
                    database.close()
            finally:
                service.close()

    def test_v8_ocr_devices_freeze_an_execution_request(self) -> None:
        binding = self._binding_api()
        self.assertIsNotNone(binding, "v8 OCR must reuse the v7 execution request contract")
        if binding is None:
            return
        for device in ("auto", "cuda", "cpu"):
            with self.subTest(device=device), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "dataset"
                source.mkdir()
                Image.new("RGB", (3, 3), "white").save(source / "image.png")
                catalog, _ = _write_test_resource_library(root, include_ocr=True)
                config = self._v8_config(source)
                config.ocr.update({"enabled": True, "device": device})
                service = JobPreparationService(root / "state.db", resource_catalog=catalog)
                try:
                    summary = service.preflight(config.to_dict())
                    workspace = service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                    request_path = Path(str(workspace["overlayRoot"])) / "resources" / "ocr-execution-request-v1.json"
                    self.assertEqual(binding.OcrExecutionRequestV1.auto(), binding.read_execution_request(request_path))
                finally:
                    service.close()

    def test_v7_ocr_execution_request_survives_service_restart_before_workspace_confirmation(self) -> None:
        binding = self._binding_api()
        self.assertIsNotNone(binding, "Task 3.4 requires a durable task-owned OCR execution request")
        if binding is None:
            return
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=True)
            request = binding.normalize_ocr_execution({
                "textDetLimitSideLen": {"mode": "manual", "value": 2304},
                "textBatchSize": {"mode": "manual", "value": 2},
            })
            first = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                summary = first.preflight(self._v7_ocr_config(source, enabled=True), ocr_execution=request)
            finally:
                first.close()
            second = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                try:
                    workspace = second.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                except JobPreflightError:
                    workspace = None
                self.assertIsNotNone(
                    workspace,
                    "the task-owned OCR request must survive a Core restart before workspace confirmation",
                )
                if workspace is not None:
                    request_path = Path(str(workspace["overlayRoot"])) / "resources" / "ocr-execution-request-v1.json"
                    self.assertEqual(request, binding.read_execution_request(request_path))
            finally:
                second.close()

    def test_ocr_disabled_skips_ocr_hash_verification_and_does_not_freeze_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, manifest_path = _write_test_resource_library(root, include_ocr=True)
            assert manifest_path is not None
            target = manifest_path.parent / "recognition" / "model.pdiparams"
            target.write_bytes(b"rec-paramx")
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                summary = service.preflight(self._ocr_config(source, enabled=False))
                database = StateDatabase.open(root / "state.db")
                try:
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))
                    self.assertNotIn("resourceManifestRelativePath", frozen["ocr"])
                    self.assertNotIn("resourceFingerprint", frozen["ocr"])
                finally:
                    database.close()
            finally:
                service.close()

    def test_ocr_enabled_requires_the_fixed_resource_when_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                with self.assertRaisesRegex(
                    JobPreflightError,
                    r"ocr_resource_install_required.*OCR_MODEL_DOWNLOAD\.md.*ocr-model-archives",
                ):
                    service.preflight(self._ocr_config(source, enabled=True))
            finally:
                service.close()

    def test_ocr_enabled_verifies_hashes_and_freezes_the_shared_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, manifest_path = _write_test_resource_library(root, include_ocr=True)
            assert manifest_path is not None
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                summary = service.preflight(self._ocr_config(source, enabled=True))
                database = StateDatabase.open(root / "state.db")
                try:
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))["ocr"]
                    self.assertEqual(OCR_RESOURCE_ID, frozen["resourceId"])
                    self.assertIn("resourceManifestRelativePath", frozen)
                    self.assertIn("resourceFingerprint", frozen)
                    self.assertEqual(
                        r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json",
                        frozen["resourceManifestRelativePath"],
                    )
                    self.assertRegex(frozen["resourceFingerprint"], r"^[0-9a-f]{64}$")
                    self.assertEqual(frozen["resourceFingerprint"], summary.resources["ocr"]["fingerprint"])
                    self.assertEqual(
                        "nl-default-prompt-v3",
                        json.loads(str(database.get_job(summary.jobId)["config_json"]))["nl"]["promptVersion"],
                    )
                finally:
                    database.close()
            finally:
                service.close()

            target = manifest_path.parent / "recognition" / "model.pdiparams"
            target.write_bytes(b"rec-paramx")
            tampered_service = JobPreparationService(root / "tampered.db", resource_catalog=catalog)
            try:
                with self.assertRaisesRegex(
                    JobPreflightError,
                    r"ocr_resource_install_required.*SHA-256 mismatch.*OCR_MODEL_DOWNLOAD\.md.*ocr-model-archives",
                ):
                    tampered_service.preflight(self._ocr_config(source, enabled=True))
            finally:
                tampered_service.close()

    def test_client_cannot_supply_ocr_frozen_resource_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=True)
            config = self._ocr_config(source, enabled=True)
            config["ocr"]["resourceManifestRelativePath"] = r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json"
            config["ocr"]["resourceFingerprint"] = "a" * 64
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                with self.assertRaisesRegex(JobPreflightError, "assigned by preflight"):
                    service.preflight(config)
            finally:
                service.close()

    def test_token_budget_disabled_does_not_require_or_freeze_a_tokenizer_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            scan_calls: list[bool] = []
            original_scan = catalog.scan

            def scan(*, include_tokenizers: bool = True):
                scan_calls.append(include_tokenizers)
                return original_scan(include_tokenizers=include_tokenizers)

            catalog.scan = scan  # type: ignore[method-assign]
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                for schema_version in (6, 7):
                    with self.subTest(schema_version=schema_version):
                        summary = service.preflight(self._token_budget_config(
                            source, enabled=False, schema_version=schema_version,
                        ))
                        database = StateDatabase.open(root / "state.db")
                        try:
                            frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))["tokenBudget"]
                            self.assertNotIn("resourceManifestRelativePath", frozen)
                            self.assertNotIn("resourceFingerprint", frozen)
                            self.assertNotIn("contextLimit", frozen)
                            self.assertNotIn("tokenBudget", summary.resources)
                        finally:
                            database.close()
                self.assertEqual([False, False], scan_calls)
            finally:
                service.close()

    def test_enabled_token_budget_freezes_shared_tokenizer_and_checks_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            manifest_path = self._write_tokenizer_resource(catalog.root, context_limit=1)
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                for schema_version in (6, 7):
                    with self.subTest(schema_version=schema_version):
                        summary = service.preflight(self._token_budget_config(
                            source, enabled=True, max_tokens=1, schema_version=schema_version,
                        ))
                        database = StateDatabase.open(root / "state.db")
                        try:
                            frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))["tokenBudget"]
                            self.assertEqual(TOKENIZER_RESOURCE_ID, frozen["resourceId"])
                            self.assertEqual(r"tokenizers\tokenizer-qwen3-0.6b-anima-v1\resource.json", frozen["resourceManifestRelativePath"])
                            self.assertRegex(frozen["resourceFingerprint"], r"^[0-9a-f]{64}$")
                            self.assertEqual(1, frozen["contextLimit"])
                            self.assertEqual(frozen["resourceFingerprint"], summary.resources["tokenBudget"]["fingerprint"])
                            self.assertEqual(str(manifest_path.relative_to(catalog.root)).replace("/", "\\"), frozen["resourceManifestRelativePath"])
                        finally:
                            database.close()
                        with self.assertRaisesRegex(JobPreflightError, "contextLimit"):
                            service.preflight(self._token_budget_config(
                                source, enabled=True, max_tokens=2, schema_version=schema_version,
                            ))
            finally:
                service.close()

    def test_enabled_token_budget_requires_the_selected_tokenizer_and_rejects_client_frozen_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                with self.assertRaisesRegex(JobPreflightError, "tokenizer_resource_install_required"):
                    service.preflight(self._token_budget_config(source, enabled=True))
                config = self._token_budget_config(source, enabled=False)
                config["tokenBudget"]["contextLimit"] = 1
                with self.assertRaisesRegex(JobPreflightError, "assigned by preflight"):
                    service.preflight(config)
            finally:
                service.close()

    def test_v6_ocr_uses_the_existing_shared_resource_freeze_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=True)
            config = self._token_budget_config(source, enabled=False)
            config["ocr"]["enabled"] = True
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                summary = service.preflight(config)
                database = StateDatabase.open(root / "state.db")
                try:
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))["ocr"]
                    self.assertEqual(
                        r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json",
                        frozen["resourceManifestRelativePath"],
                    )
                    self.assertRegex(frozen["resourceFingerprint"], r"^[0-9a-f]{64}$")
                finally:
                    database.close()
            finally:
                service.close()

    def test_invalid_image_action_is_backward_compatible_and_validated(self) -> None:
        legacy = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=r"C:\dataset",
        ).to_dict()
        legacy["nl"]["systemPrompt"] = "describe the visible image"
        legacy["imageDecode"].pop("invalidImageAction")
        self.assertEqual("block", config_from_dict(legacy).imageDecode.invalidImageAction)

        invalid = JobConfig(
            profile="e621", workMode="in_place", overwriteMode="incremental", sourceRoot=r"C:\dataset",
        ).to_dict()
        invalid["nl"]["systemPrompt"] = "describe the visible image"
        invalid["imageDecode"]["invalidImageAction"] = "ignore"
        with self.assertRaisesRegex(ValueError, "block or skip"):
            config_from_dict(invalid)

    def test_character_preset_validates_every_in_scope_path_and_reports_bounded_relative_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            (source / "001_主角").mkdir(parents=True)
            Image.new("RGB", (3, 3), "white").save(source / "001_主角" / "good.png")
            Image.new("RGB", (3, 3), "white").save(source / "bad.png")
            catalog, _ = _write_test_resource_library(root, include_ocr=False)
            service = JobPreparationService(root / "state.db", resource_catalog=catalog)
            try:
                for schema_version in (6, 7):
                    with self.subTest(schema_version=schema_version):
                        config = self._token_budget_config(source, enabled=False, schema_version=schema_version)
                        config["nl"]["captionPreset"] = "character"
                        with self.assertRaisesRegex(JobPreflightError, r"character.*bad\.png") as raised:
                            service.preflight(config)
                        self.assertNotIn(str(source), str(raised.exception))
                        self.assertNotIn("sourceRoot", str(raised.exception))
            finally:
                service.close()

    def test_v6_user_supplement_is_bounded_before_a_job_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "dataset"
            source.mkdir()
            for schema_version in (6, 7):
                with self.subTest(schema_version=schema_version):
                    config = self._token_budget_config(source, enabled=False, schema_version=schema_version)
                    config["nl"]["systemPrompt"] = "x" * 16_385
                    with self.assertRaisesRegex(JobPreflightError, "supplement"):
                        config_from_dict(config)

    def test_preflight_persists_bounded_manifest_without_overlay_or_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            (source / "image.txt").write_text("cat", encoding="utf-8")
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(self._config(source))
            self.assertEqual((1, 1, 0, 1, 0), (summary.sampleCount, summary.inScopeCount, summary.outOfScopeCount, summary.nonblankTxtCount, summary.nonblankJsonCount))
            self.assertFalse(any(source.parent.glob("*.anima-overlay-*")))
            database = StateDatabase.open(root / "state.db")
            try:
                job = database.get_job(summary.jobId)
                self.assertEqual("ready", job["status"])
                # created_at used to be the frozen "1970-01-01T00:00:00Z" sentinel.
                self.assertNotEqual("1970-01-01T00:00:00Z", str(job["created_at"]))
                self.assertTrue(str(job["created_at"]).startswith("20"))
                self.assertEqual(0, database.connection.execute("SELECT COUNT(*) FROM dataset_claims").fetchone()[0])
                frozen = json.loads(str(job["config_json"]))
                self.assertEqual(2, frozen["nl"]["apiPolicy"]["maxHttpAttempts"])
                self.assertEqual(summary.configHash, job["config_hash"])
            finally:
                database.close()

    def test_confirm_creates_overlay_only_after_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(self._config(source))
            with self.assertRaises(JobPreflightError):
                service.confirm_workspace(summary.jobId, confirmed=False, confirmed_rebuild=False)
            result = service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
            self.assertEqual("preparing_workspace", result["status"])
            self.assertTrue(Path(str(result["overlayRoot"])).is_dir())
            service.close()

    def test_preflight_annotation_presence_matrix(self) -> None:
        cases = (
            ("empty", False, False, False, (0, 0, 0)),
            ("no_annotations", True, False, False, (1, 0, 0)),
            ("txt_only", True, True, False, (1, 1, 0)),
            ("json_only", True, False, True, (1, 0, 1)),
            ("txt_and_json", True, True, True, (1, 1, 1)),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, has_image, has_txt, has_json, expected in cases:
                with self.subTest(name=name):
                    source = root / name
                    source.mkdir()
                    if has_image:
                        Image.new("RGB", (3, 3), "white").save(source / "image.png")
                    if has_txt:
                        (source / "image.txt").write_text("cat", encoding="utf-8")
                    if has_json:
                        (source / "image.json").write_text('{"nl":""}', encoding="utf-8")
                    service = JobPreparationService(root / f"{name}.db")
                    try:
                        summary = service.preflight(self._config(source))
                        self.assertEqual(expected, (summary.sampleCount, summary.nonblankTxtCount, summary.nonblankJsonCount))
                    finally:
                        service.close()

    def test_unusable_image_keeps_the_preflight_and_the_job_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "good.png")
            # A renamed JPEG or a truncated download used to delete the job
            # control record and fail the whole preflight.
            (source / "broken.png").write_bytes(b"not a PNG")
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(self._config(source))
            self.assertEqual((2, 1), (summary.sampleCount, summary.imageIssueCount))
            database = StateDatabase.open(root / "state.db")
            try:
                self.assertEqual("ready", database.get_job(summary.jobId)["status"])
                issues = database.page_issues(summary.jobId, limit=10)
                self.assertEqual(["broken.png"], [str(row["relative_image_path"]) for row in issues])
                self.assertEqual(1, int(issues[0]["blocking"]))
            finally:
                database.close()
            with self.assertRaisesRegex(JobPreflightError, "unusable images"):
                service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
            service.close()

    def test_skip_invalid_image_excludes_all_modules_and_preserves_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "good.png")
            (source / "broken.png").write_bytes(b"not a PNG")
            (source / "broken.txt").write_text("original tags", encoding="utf-8")
            (source / "broken.json").write_text('{"nl":"original description"}', encoding="utf-8")
            before = ((source / "broken.txt").read_bytes(), (source / "broken.json").read_bytes())
            config = self._config(source)
            config["imageDecode"]["invalidImageAction"] = "skip"
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(config)
            self.assertEqual((2, 1, 1, 1), (summary.sampleCount, summary.inScopeCount, summary.outOfScopeCount, summary.imageIssueCount))
            database = StateDatabase.open(root / "state.db")
            try:
                broken = next(row for row in database.page_samples(summary.jobId, limit=10) if row["relative_image_path"] == "broken.png")
                self.assertEqual((0, "nonblank", "nonblank"), (broken["in_processing_scope"], broken["original_txt_state"], broken["original_json_state"]))
                issue = database.page_issues(summary.jobId, limit=10)[0]
                self.assertEqual(("warning", 0), (issue["severity"], issue["blocking"]))
                self.assertEqual(0, database.count_unresolved_blocking_issues(summary.jobId))
                for module_id in ("caption", "classify", "replace", "nl", "dropout", "export"):
                    self.assertEqual(1, database.count_module_samples(summary.jobId, module_id), module_id)
            finally:
                database.close()
            result = service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
            self.assertEqual("preparing_workspace", result["status"])
            self.assertEqual(before, ((source / "broken.txt").read_bytes(), (source / "broken.json").read_bytes()))
            service.close()

    def test_preflight_summary_reports_projection_estimates_and_api_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            for name in ("one", "two"):
                Image.new("RGB", (3, 3), "white").save(source / f"{name}.png")
            (source / "one.txt").write_text("existing caption", encoding="utf-8")
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(self._config(source))
            self.assertEqual((1, 1), (summary.blankTxtCount, summary.nonblankTxtCount))
            self.assertEqual((0, 2), (summary.nonblankJsonCount, summary.blankJsonCount))
            self.assertEqual(0, summary.annotationKeyCollisionCount)
            self.assertEqual("both", summary.projection["format"])
            self.assertEqual((2, 0), (summary.projection["inScopeSamples"], summary.projection["retainedSamples"]))
            self.assertEqual((1, 1, 0), (summary.projection["txtCreate"], summary.projection["txtOverwrite"], summary.projection["txtDelete"]))
            self.assertEqual((2, 0, 0), (summary.projection["jsonCreate"], summary.projection["jsonOverwrite"], summary.projection["jsonDelete"]))
            existing = (source / "one.txt").stat().st_size
            self.assertEqual(
                (1, existing, existing, existing),
                (
                    summary.estimate["existingAnnotationFiles"], summary.estimate["existingAnnotationBytes"],
                    summary.estimate["averageAnnotationBytes"], summary.estimate["backupUpperBoundBytes"],
                ),
            )
            self.assertEqual(4 * existing, summary.estimate["incrementalWriteBytes"])
            # ROADMAP.md:833 budget is candidateCount + ceil(candidateCount * 0.05).
            self.assertEqual((2, 2, 6, 10, 3), (
                summary.api["candidateCount"], summary.api["minRequests"], summary.api["maxPrimaryRequests"],
                summary.api["maxWithBackupRequests"], summary.api["httpAttemptBudget"],
            ))
            self.assertGreater(summary.api["estimatedUploadBytes"], 0)
            database = StateDatabase.open(root / "state.db")
            try:
                job = database.get_job(summary.jobId)
                frozen = json.loads(str(job["config_json"]))
                self.assertEqual(3, frozen["nl"]["apiPolicy"]["maxHttpAttempts"])
                self.assertEqual(summary.configHash, job["config_hash"])
            finally:
                database.close()
            service.close()

    def test_explicit_http_attempt_budget_is_preserved_in_summary_and_frozen_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            config = self._config(source)
            config["nl"]["apiPolicy"] = {"maxHttpAttempts": 17}
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(config)
            self.assertEqual(17, summary.api["httpAttemptBudget"])
            database = StateDatabase.open(root / "state.db")
            try:
                frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))
                self.assertEqual(17, frozen["nl"]["apiPolicy"]["maxHttpAttempts"])
            finally:
                database.close()
                service.close()

    def test_preflight_api_bounds_are_zero_when_the_api_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "one.png")
            config = self._config(source)
            config["nl"]["apiEnabled"] = False
            service = JobPreparationService(root / "state.db")
            summary = service.preflight(config)
            self.assertEqual(
                (0, 0, 0, 0),
                (
                    summary.api["candidateCount"], summary.api["maxWithBackupRequests"],
                    summary.api["httpAttemptBudget"], summary.api["estimatedUploadBytes"],
                ),
            )
            service.close()

    def test_custom_replace_index_is_hashed_at_preflight_and_frozen_in_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            index = root / "replace.csv"
            original = "source_tag,canonical_e621_tag,action,replacement_tags\nold,,replace,new\n"
            index.write_text(original, encoding="utf-8")
            original_bytes = index.read_bytes()
            config = self._config(source)
            config["replace"] = {"enabled": True, "indexMode": "custom", "customIndexPath": str(index)}
            service = JobPreparationService(root / "state.db")
            try:
                summary = service.preflight(config)
                self.assertEqual(("custom", 1), (summary.replaceIndex["mode"], summary.replaceIndex["ruleCount"]))
                result = service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                frozen = Path(str(result["overlayRoot"])) / "resources" / "replace" / "custom-index.csv"
                self.assertEqual(original_bytes, frozen.read_bytes())
                index.write_text("source_tag,canonical_e621_tag,action,replacement_tags\nold,,drop,\n", encoding="utf-8")
                self.assertEqual(original_bytes, frozen.read_bytes())
            finally:
                service.close()

    def test_custom_replace_index_change_between_preflight_and_confirmation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            index = root / "replace.csv"
            index.write_text("source_tag,canonical_e621_tag,action,replacement_tags\nold,,replace,new\n", encoding="utf-8")
            config = self._config(source)
            config["replace"] = {"enabled": True, "indexMode": "custom", "customIndexPath": str(index)}
            service = JobPreparationService(root / "state.db")
            try:
                summary = service.preflight(config)
                index.write_text("source_tag,canonical_e621_tag,action,replacement_tags\nold,,drop,\n", encoding="utf-8")
                with self.assertRaisesRegex(JobPreflightError, "changed after preflight"):
                    service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
            finally:
                service.close()

    def test_custom_classification_change_before_confirmation_leaves_no_workspace_or_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            external_package = root / "external" / "classification"
            shutil.copytree(
                ROOT / "resource-library" / "classification-indexes" / "e621-classify-20260724-v1",
                external_package,
            )
            config = JobConfig(
                workMode="in_place", overwriteMode="incremental", sourceRoot=str(source),
            )
            config.caption["enabled"] = False
            config.classify.update({
                "enabled": True,
                "indexMode": "custom",
                "customResourcePath": str(external_package / "resource.json"),
            })
            config.classify.pop("resourceId", None)
            config.replace["enabled"] = False
            config.ocr["enabled"] = False
            config.nl["enabled"] = False
            config.countReview["enabled"] = False  # type: ignore[index]
            config.dropout["enabled"] = False
            config.dropout["quality"]["enabled"] = False
            assert config.tokenBudget is not None
            config.tokenBudget["enabled"] = False

            service = JobPreparationService(root / "state.db")
            try:
                summary = service.preflight(config.to_dict())
                dictionary = external_package / "e621_tag_dictionary.json"
                dictionary.write_bytes(dictionary.read_bytes() + b"\n")
                with self.assertRaisesRegex(JobPreflightError, "changed after preflight"):
                    service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)

                database = StateDatabase.open(root / "state.db")
                try:
                    job = database.get_job(summary.jobId)
                    self.assertIsNone(job["overlay_root"])
                    self.assertEqual(0, database.connection.execute(
                        "SELECT COUNT(*) FROM dataset_claims WHERE job_id=?", (summary.jobId,),
                    ).fetchone()[0])
                finally:
                    database.close()
                self.assertFalse((source.parent / f".{source.name}.anima-overlay-{summary.jobId}").exists())
            finally:
                service.close()

    def test_confirm_migrates_legacy_frozen_jobs_to_default_resource_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dataset"
            source.mkdir()
            Image.new("RGB", (3, 3), "white").save(source / "image.png")
            service = JobPreparationService(root / "state.db")
            try:
                summary = service.preflight(self._config(source))
                database = StateDatabase.open(root / "state.db")
                try:
                    frozen = json.loads(str(database.get_job(summary.jobId)["config_json"]))
                    for section in (frozen["caption"], frozen["classify"], frozen["replace"], frozen["dropout"]["quality"]):
                        for field in ("resourceId", "resourceManifestRelativePath", "resourceFingerprint"):
                            section.pop(field, None)
                    legacy_hash = sha256_json(frozen)
                    database.connection.execute(
                        "UPDATE jobs SET config_json=?,config_hash=? WHERE job_id=?",
                        (json.dumps(frozen), legacy_hash, summary.jobId),
                    )
                finally:
                    database.close()

                service.confirm_workspace(summary.jobId, confirmed=True, confirmed_rebuild=False)
                database = StateDatabase.open(root / "state.db")
                try:
                    job = database.get_job(summary.jobId)
                    migrated = json.loads(str(job["config_json"]))
                    sections = (
                        migrated["caption"], migrated["classify"], migrated["replace"],
                        migrated["dropout"]["quality"],
                    )
                    self.assertTrue(all(
                        {"resourceId", "resourceManifestRelativePath", "resourceFingerprint"} <= set(section)
                        for section in sections
                    ))
                    self.assertEqual(sha256_json(migrated), job["config_hash"])
                finally:
                    database.close()
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
