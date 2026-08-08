"""Worker dispatch ownership for the pipeline service."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .caption_overlay import CaptionOverlayWriter
from .caption_runner import CaptionRunner
from .classify_overlay import ClassifyOverlayWriter
from .classify_runner import ClassifyRunner
from .contracts import job_config_supports_caption_input_txt_mode, job_config_supports_ocr_device
from .count_review_overlay import CountReviewOverlayWriter
from .count_review_runner import CountReviewRunner
from .credentials import CredentialStoreError
from .db import StateDatabase
from .export_runner import ExportRunner
from .nl_overlay import NlOverlayWriter
from .nl_profiles import NlProfileError
from .nl_runner import NlApiCredentials, NlRunner
from .ocr_overlay import OcrWorkingSidecarView
from .ocr_runner import OcrRunner
from .ocr_runtime_binding import (
    OcrExecutionError,
    read_runtime_binding,
    write_runtime_binding,
)
from .overlay import BaselineView, OverlayError, OverlayLayout, WorkingAnnotationView
from .policy_runner import PolicyRunner
from .token_budget_runner import TokenBudgetRunner
from .raw_e621 import parse_raw_e621_annotation
from .replace_overlay import ReplaceOverlayWriter
from .replace_runner import ReplaceRunner
from .resource_catalog import (
    ResourceCatalogError,
    ResourceKind,
    danbooru_resource_install_message,
)
from .scheduler import BoundedScheduler
from .stdio_transport import StdioJsonlTransport
from .runtime_manifest import sha256_path


_RUNTIMES = {
    "caption": ("caption-e621", "caption"),
    "classify": ("classify-e621", "classify"),
    "replace": ("replace-e621", "replace"),
    "ocr": ("ocr-paddle", "ocr"),
    "nl": ("nl", "nl"),
    "dropout": ("policy", "policy"),
    "token_budget": ("token-budget", "token-budget"),
    "export": ("export", "export"),
}


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class _OcrRuntimeSelection:
    runtime_id: str
    runtime_fingerprint: str
    total_vram_bytes: int | None
    startup_reason: str | None


class _NoExchangeTransport:
    def exchange(self, _: object) -> object:
        raise PipelineError("NL transport was used while API calls are disabled")


def _is_input_txt_nl(config: dict[str, object]) -> bool:
    caption = config.get("caption")
    return (
        job_config_supports_caption_input_txt_mode(config.get("schemaVersion"))
        and isinstance(caption, dict)
        and caption.get("inputTxtMode") == "nl"
    )


class PipelineDispatchMixin:
    """Runtime selection, resource validation, credentials, and runner dispatch."""

    def _spawn_transport(self, module_id: str) -> StdioJsonlTransport:
        runtime_id, owner = _RUNTIMES[module_id]
        process = self.launcher_factory(self.install_root).spawn(runtime_id, expected_owner=owner)
        return StdioJsonlTransport(process)

    def _spawn_ocr_transport(self, runtime_id: str) -> StdioJsonlTransport:
        process = self.launcher_factory(self.install_root).spawn(runtime_id, expected_owner="ocr")
        return StdioJsonlTransport(process)

    def _ocr_binding_path(self, database: StateDatabase, job_id: str, layout: OverlayLayout) -> Path:
        target = layout.resource_path("ocr-runtime-binding-v1.json")
        parent_job_id = database.repair_parent_job_id(job_id)
        if parent_job_id is None:
            return target
        try:
            parent = database.get_job(parent_job_id)
            parent_overlay_root = parent["overlay_root"]
            if not isinstance(parent_overlay_root, str) or not parent_overlay_root:
                raise OcrExecutionError("repair parent OCR binding is unavailable")
            parent_layout = OverlayLayout.open_existing(parent_overlay_root, parent_job_id)
            parent_binding = read_runtime_binding(parent_layout.resource_path("ocr-runtime-binding-v1.json"))
            write_runtime_binding(target, parent_binding)
        except (KeyError, OcrExecutionError, OverlayError) as exc:
            raise PipelineError("repair parent OCR binding is unavailable or invalid") from exc
        return target

    def _resolve_ocr_runtime(self, runtime_id: str) -> tuple[object, str]:
        try:
            launcher = self.launcher_factory(self.install_root)
            launch = launcher.resolve(runtime_id, expected_owner="ocr")
            return launch, sha256_path(launcher.manifest_path(runtime_id))
        except Exception as exc:
            raise PipelineError("OCR runtime is unavailable") from exc

    @staticmethod
    def _probe_ocr_gpu_runtime(launch: object, install_root: Path) -> int | None:
        interpreter = getattr(launch, "interpreter", None)
        environment = getattr(launch, "environment", None)
        if not isinstance(interpreter, Path) or not isinstance(environment, dict):
            raise PipelineError("CUDA OCR runtime is unavailable")
        probe = (
            "import json, paddle\n"
            "if not paddle.device.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1: raise SystemExit(2)\n"
            "try:\n"
            " total = int(paddle.device.cuda.get_device_properties(0).total_memory)\n"
            "except Exception:\n"
            " total = None\n"
            "print(json.dumps({'totalVramBytes': total if total is None or total >= 0 else None}, separators=(',', ':')))\n"
        )
        try:
            completed = subprocess.run(
                [str(interpreter), "-B", "-I", "-c", probe],
                cwd=str(install_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode != 0 or len(completed.stdout) > 1024:
                raise PipelineError("CUDA OCR runtime is unavailable")
            result = json.loads(completed.stdout)
            if not isinstance(result, dict) or set(result) != {"totalVramBytes"}:
                raise PipelineError("CUDA OCR runtime is unavailable")
            total = result["totalVramBytes"]
            if total is not None and (type(total) is not int or total < 0):
                raise PipelineError("CUDA OCR runtime is unavailable")
            return total
        except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineError("CUDA OCR runtime is unavailable") from exc

    def _select_ocr_runtime(
        self,
        database: StateDatabase,
        job_id: str,
        config: dict[str, object],
        binding_path: Path,
    ) -> _OcrRuntimeSelection:
        try:
            if binding_path.exists():
                binding = read_runtime_binding(binding_path)
                return _OcrRuntimeSelection(
                    binding.runtimeId,
                    binding.runtimeFingerprint,
                    binding.totalVramBytes,
                    binding.startupReason,
                )
            ocr = config.get("ocr")
            if not isinstance(ocr, dict) or ocr.get("device") not in {"auto", "cuda", "cpu"}:
                raise OcrExecutionError("frozen OCR device is invalid")
            device = ocr["device"]
            if device in {"auto", "cuda"}:
                try:
                    launch, fingerprint = self._resolve_ocr_runtime("ocr-paddle-gpu")
                    return _OcrRuntimeSelection(
                        "ocr-paddle-gpu",
                        fingerprint,
                        self._probe_ocr_gpu_runtime(launch, self.install_root),
                        None,
                    )
                except PipelineError:
                    if device == "cuda":
                        raise
            _, fingerprint = self._resolve_ocr_runtime("ocr-paddle")
            return _OcrRuntimeSelection(
                "ocr-paddle",
                fingerprint,
                None,
                "gpu_runtime_unavailable" if device == "auto" else None,
            )
        except OcrExecutionError as exc:
            raise PipelineError("OCR runtime binding is unavailable or invalid") from exc

    def _selected_resource(
        self,
        config: dict[str, object],
        module_id: str,
    ) -> tuple[str, str]:
        mapping: dict[str, tuple[ResourceKind, str, str]] = {
            "caption": ("tagging-model", "caption", "taggingModel"),
            "classify": ("classification-index", "classify", "classificationIndex"),
            "replace": ("replacement-index", "replace", "replacementIndex"),
            "dropout": ("dropout-model", "dropout", "dropoutModel"),
        }
        if module_id == "ocr":
            raw_section = config.get("ocr")
            if not isinstance(raw_section, dict):
                raise PipelineError("frozen ocr resource selection is invalid")
            try:
                snapshot = self.resource_catalog.scan()
                resource_id = raw_section.get("resourceId")
                if not isinstance(resource_id, str):
                    raise ResourceCatalogError("ocr resourceId is invalid")
                package = snapshot.package("ocr-model", resource_id, verify_hashes=True, profile="shared")
            except ResourceCatalogError as exc:
                raise PipelineError(str(exc)) from exc
            if (
                raw_section.get("resourceManifestRelativePath") != package.manifest_relative_path
                or raw_section.get("resourceFingerprint") != package.fingerprint
            ):
                raise PipelineError("frozen ocr resource no longer matches the resource library")
            return package.manifest_relative_path, package.fingerprint
        kind, section_name, default_key = mapping[module_id]
        raw_section = config.get(section_name)
        if module_id == "dropout" and isinstance(raw_section, dict):
            raw_section = raw_section.get("quality")
        if not isinstance(raw_section, dict):
            raise PipelineError(f"frozen {module_id} resource selection is invalid")
        profile = config.get("profile")
        if not isinstance(profile, str):
            raise PipelineError("frozen profile is invalid")
        try:
            snapshot = self.resource_catalog.scan()
            resource_id = raw_section.get("resourceId", snapshot.defaults_for(profile)[default_key])
            if not isinstance(resource_id, str):
                raise ResourceCatalogError(f"{module_id} resourceId is invalid")
            package = snapshot.package(kind, resource_id, verify_hashes=False, profile=profile)
        except ResourceCatalogError as exc:
            if profile == "danbooru" and kind in {"tagging-model", "classification-index"}:
                selected_id = raw_section.get("resourceId")
                if isinstance(selected_id, str):
                    raise PipelineError(
                        danbooru_resource_install_message(kind, selected_id)
                    ) from exc
            raise PipelineError(str(exc)) from exc
        relative = raw_section.get("resourceManifestRelativePath")
        fingerprint = raw_section.get("resourceFingerprint")
        if relative is None and fingerprint is None:
            return package.manifest_relative_path, package.fingerprint
        if relative != package.manifest_relative_path or fingerprint != package.fingerprint:
            raise PipelineError(f"frozen {module_id} resource no longer matches the resource library")
        return package.manifest_relative_path, package.fingerprint

    def _nl_credentials(self, config: dict[str, object]) -> NlApiCredentials:
        nl = config.get("nl")
        if not isinstance(nl, dict):
            raise PipelineError("frozen NL configuration is invalid")
        profile_id = nl.get("apiProfileId", "default")
        if not isinstance(profile_id, str):
            raise PipelineError("frozen NL profile reference is invalid")
        try:
            profile = next(item for item in self.profile_store.load_all() if item.profileId == profile_id)
            secret = self.credential_store.load(profile.apiCredentialRef)
        except (StopIteration, NlProfileError, CredentialStoreError) as exc:
            raise PipelineError("NL profile or DPAPI credential is unavailable") from exc
        return NlApiCredentials(profile.endpoint, profile.model, secret, profile.backupModel)

    def _run_active_module(self, database: StateDatabase, scheduler: BoundedScheduler, job_id: str, module_id: str, config: dict[str, object]) -> str:
        job = database.get_job(job_id)
        overlay_root = job["overlay_root"]
        if not isinstance(overlay_root, str) or not overlay_root:
            raise PipelineError("prepared job has no annotation overlay")
        layout = OverlayLayout.open_existing(overlay_root, job_id)
        view = WorkingAnnotationView(BaselineView(Path(str(job["dataset_root"]))), layout)
        raw_e621_reader = lambda annotation_key: parse_raw_e621_annotation(
            view.baseline.read(annotation_key, ".json")
        )
        worker_instance_id = f"{module_id}-{uuid.uuid4().hex}"
        if module_id == "caption":
            resource_path, fingerprint = self._selected_resource(config, "caption")
            classify_resource_path, _ = self._selected_resource(config, "classify")
            with self._spawn_transport("caption") as transport:
                return CaptionRunner(database, scheduler, transport, job_id=job_id, worker_instance_id=worker_instance_id,
                    resource_manifest_relative_path=resource_path, resource_fingerprint=fingerprint,
                    classify_resource_manifest_relative_path=classify_resource_path,
                    result_consumer=CaptionOverlayWriter.open_for_job(database, job_id),
                    raw_e621_reader=raw_e621_reader,
                    install_root=self.resource_root).run().status
        if module_id == "classify":
            resource_path, fingerprint = self._selected_resource(config, "classify")
            with self._spawn_transport("classify") as transport:
                return ClassifyRunner(database, scheduler, transport, view, ClassifyOverlayWriter.open_for_job(database, job_id),
                    job_id=job_id, worker_instance_id=worker_instance_id, install_root=self.resource_root,
                    resource_manifest_relative_path=resource_path, resource_fingerprint=fingerprint,
                    raw_e621_reader=raw_e621_reader).run().status
        if module_id == "replace":
            if job["profile"] != "e621":
                raise PipelineError("Danbooru Replace must be completed by the scheduler profile skip")
            replace = config.get("replace")
            if not isinstance(replace, dict):
                raise PipelineError("frozen replace configuration is invalid")
            runner_args: dict[str, object] = {}
            if replace.get("indexMode") == "bundled":
                resource_path, fingerprint = self._selected_resource(config, "replace")
                runner_args = {"resource_manifest_relative_path": resource_path, "resource_fingerprint": fingerprint}
            elif replace.get("indexMode") == "custom":
                digest, rule_count = replace.get("customIndexSha256"), replace.get("customIndexRuleCount")
                if not isinstance(digest, str) or type(rule_count) is not int:
                    raise PipelineError("frozen custom replace index metadata is invalid")
                runner_args = {"custom_index_path": str(layout.resource_path("replace\\custom-index.csv")), "custom_index_overlay_root": str(layout.root), "custom_index_sha256": digest, "custom_index_rule_count": rule_count}
            else:
                raise PipelineError("frozen replace index mode is invalid")
            with self._spawn_transport("replace") as transport:
                return ReplaceRunner(database, scheduler, transport, view, ReplaceOverlayWriter(database, layout, job_id),
                    job_id=job_id, worker_instance_id=worker_instance_id, install_root=str(self.resource_root),
                    **runner_args).run()
        if module_id == "count_review":
            return CountReviewRunner(
                database,
                scheduler,
                CountReviewOverlayWriter(database, layout, view, job_id),
                job_id=job_id,
                worker_instance_id=worker_instance_id,
            ).run()
        if module_id == "dropout":
            resource_path, fingerprint = self._selected_resource(config, "dropout")
            with self._spawn_transport("dropout") as transport:
                return PolicyRunner(database, scheduler, transport, view, job_id=job_id,
                worker_instance_id=worker_instance_id, install_root=self.resource_root,
                resource_manifest_relative_path=resource_path, resource_fingerprint=fingerprint).run()
        if module_id == "token_budget":
            with self._spawn_transport("token_budget") as transport:
                return TokenBudgetRunner(database, scheduler, transport, view, job_id=job_id,
                    worker_instance_id=worker_instance_id).run()
        if module_id == "ocr":
            ocr = config.get("ocr")
            if not isinstance(ocr, dict):
                raise PipelineError("frozen OCR configuration is invalid")
            ocr_view = OcrWorkingSidecarView(Path(str(job["dataset_root"])), layout)
            if ocr.get("enabled") is not True:
                return OcrRunner(database, scheduler, _NoExchangeTransport(), ocr_view, job_id=job_id,
                    worker_instance_id=worker_instance_id,
                    resource_manifest_relative_path="",
                    resource_fingerprint="").run().status
            resource_path, fingerprint = self._selected_resource(config, "ocr")
            if not job_config_supports_ocr_device(config.get("schemaVersion")):
                with self._spawn_transport("ocr") as transport:
                    return OcrRunner(database, scheduler, transport, ocr_view, job_id=job_id,
                        worker_instance_id=worker_instance_id,
                        resource_manifest_relative_path=resource_path,
                        resource_fingerprint=fingerprint).run().status
            binding_path = self._ocr_binding_path(database, job_id, layout)
            selection = self._select_ocr_runtime(database, job_id, config, binding_path)
            with self._spawn_ocr_transport(selection.runtime_id) as transport:
                return OcrRunner(database, scheduler, transport, ocr_view, job_id=job_id,
                    worker_instance_id=worker_instance_id,
                    resource_manifest_relative_path=resource_path,
                    resource_fingerprint=fingerprint,
                    runtime_id=selection.runtime_id,
                    runtime_fingerprint=selection.runtime_fingerprint,
                    binding_path=binding_path,
                    total_vram_bytes=selection.total_vram_bytes,
                    startup_reason=selection.startup_reason).run().status
        if module_id == "export":
            with self._spawn_transport("export") as transport:
                return ExportRunner(database, scheduler, transport, view, job_id=job_id,
                    worker_instance_id=worker_instance_id).run()
        nl = config["nl"]
        if not isinstance(nl, dict):
            raise PipelineError("frozen NL configuration is invalid")
        writer = NlOverlayWriter(database, layout, view, job_id)
        if nl.get("apiEnabled") is not True or _is_input_txt_nl(config):
            return NlRunner(database, scheduler, _NoExchangeTransport(), view, writer, job_id=job_id,
                worker_instance_id=worker_instance_id).run()
        with self._spawn_transport("nl") as transport:
            return NlRunner(database, scheduler, transport, view, writer, job_id=job_id,
                worker_instance_id=worker_instance_id, credentials=self._nl_credentials(config)).run()
