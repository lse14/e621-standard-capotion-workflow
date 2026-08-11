"""Standard-library orchestration for the source bootstrap installer."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from assemble import (
    AssemblyError,
    InstallationPlan,
    PlannedComponent,
    assemble_resource,
    assemble_runtime,
    build_install_state,
    component_is_current,
    component_record,
    installation_plan,
    read_install_state,
    write_install_state,
)
from download import download_verified
from manifest import Artifact, InstallManifest, ManifestError, load_manifest_path, sha256_bytes
from paths import ProjectLayout, cleanup_failure, publish_directories, recover_transactions


ArtifactFetcher = Callable[[Artifact], Path]
Probe = Callable[[PlannedComponent, Path], bool]
RuntimeManifestWriter = Callable[[PlannedComponent, ProjectLayout], str]

_RUNTIME_SOURCES: dict[str, dict[str, str]] = {
    "core": {"anima_core": "core/src/anima_core", "anima_caption_format": "shared/anima_caption_format/anima_caption_format"},
    "caption-e621": {"anima_caption_worker": "workers/caption/src/anima_caption_worker"},
    "classify-e621": {"anima_classify_worker": "workers/classify/src/anima_classify_worker"},
    "replace-e621": {"anima_replace_worker": "workers/replace/src/anima_replace_worker"},
    "nl": {"anima_nl_worker": "workers/nl/src/anima_nl_worker"},
    "policy": {"anima_policy_worker": "workers/policy/src/anima_policy_worker"},
    "export": {"anima_export_worker": "workers/export/src/anima_export_worker", "anima_caption_format": "shared/anima_caption_format/anima_caption_format"},
    "token-budget": {"anima_token_budget_worker": "workers/token_budget/src/anima_token_budget_worker", "anima_caption_format": "shared/anima_caption_format/anima_caption_format"},
    "ocr-paddle": {"anima_ocr_worker": "workers/ocr/src/anima_ocr_worker"},
    "ocr-paddle-gpu": {"anima_ocr_worker": "workers/ocr/src/anima_ocr_worker"},
}


@dataclass(frozen=True)
class InstallResult:
    installed_component_ids: tuple[str, ...]
    skipped_component_ids: tuple[str, ...]
    state_path: Path


def _owner_sources(source_root: Path, runtime_id: str) -> dict[str, Path]:
    try:
        values = _RUNTIME_SOURCES[runtime_id]
    except KeyError as exc:
        raise AssemblyError(f"runtime source mapping is missing: {runtime_id}") from exc
    result = {name: source_root / relative for name, relative in values.items()}
    missing = [str(path) for path in result.values() if not path.is_dir()]
    if missing:
        raise AssemblyError("runtime source is missing: " + ", ".join(missing))
    return result


def _state_records(manifest: InstallManifest, plan: InstallationPlan, state: object) -> dict[str, object]:
    if not isinstance(state, dict):
        return {}
    if (
        state.get("schemaVersion") != 1
        or state.get("sourceCommit") != manifest.source_commit
        or state.get("releaseVersion") != manifest.release_version
        or state.get("installManifestSha256") != manifest.fingerprint
        or state.get("accelerator") != plan.accelerator
        or not isinstance(state.get("components"), dict)
    ):
        return {}
    return state["components"]


def _load_runtime_manifest_generator(source_root: Path):
    path = source_root / "packaging" / "scripts" / "generate_runtime_manifests.py"
    spec = importlib.util.spec_from_file_location("_source_bootstrap_runtime_manifest_generator", path)
    if spec is None or spec.loader is None:
        raise AssemblyError(f"runtime manifest generator is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _default_runtime_manifest_writer(source_root: Path, item: PlannedComponent, layout: ProjectLayout) -> str:
    if item.runtime_id is None or item.lock_name is None:
        raise AssemblyError("runtime manifest requires a runtime and lock identity")
    generator = _load_runtime_manifest_generator(source_root)
    specs = generator.runtime_specs(
        include_ocr_paddle=item.runtime_id == "ocr-paddle",
        include_ocr_paddle_gpu=item.runtime_id == "ocr-paddle-gpu",
        lock_names={item.runtime_id: item.lock_name},
    )
    try:
        owner, entry_module, _lock_name, dll_paths = specs[item.runtime_id]
    except KeyError as exc:
        raise AssemblyError(f"runtime manifest identity is missing: {item.runtime_id}") from exc
    specs[item.runtime_id] = (owner, entry_module, item.lock_name, dll_paths)
    requirements_root = source_root / "packaging" / "requirements"
    value = generator.manifest(layout.runtime_root, requirements_root, item.runtime_id, specs)
    manifest_path = layout.runtime_root / "manifests" / "runtimes" / f"{item.runtime_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    source_lock = requirements_root / f"{item.lock_name}.lock"
    if not source_lock.is_file():
        raise AssemblyError(f"runtime lock is missing: {source_lock}")
    lock_target = layout.runtime_root / "manifests" / "requirements" / source_lock.name
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    lock_target.write_bytes(source_lock.read_bytes())
    return str(manifest_path.relative_to(layout.project_root)).replace("/", "\\")


def _default_probe(item: PlannedComponent, _target: Path) -> bool:
    raise AssemblyError(
        f"offline probe implementation is required before publishing {item.component.component_id}; "
        "the installer refuses import-only success"
    )


def install_project(
    *,
    project_root: str | Path,
    source_root: str | Path,
    manifest: InstallManifest,
    accelerator: str,
    base_runtime: str | Path,
    fetch_artifact: ArtifactFetcher | None = None,
    probe_component: Probe | None = None,
    write_runtime_manifest: RuntimeManifestWriter | None = None,
    require_mandatory_e621: bool = True,
) -> InstallResult:
    """Assemble, probe, publish and record a manifest-selected installation."""
    layout = ProjectLayout.create(project_root)
    source = Path(source_root)
    if require_mandatory_e621:
        from assemble import validate_mandatory_e621_components

        validate_mandatory_e621_components(manifest)
    plan = installation_plan(manifest, accelerator=accelerator)
    fetch = fetch_artifact or (lambda artifact: download_verified(artifact, layout.cache))
    probe = probe_component or _default_probe
    writer = write_runtime_manifest or (lambda item, current_layout: _default_runtime_manifest_writer(source, item, current_layout))
    try:
        layout.ensure_directories()
        recover_transactions(layout)
        existing = _state_records(manifest, plan, read_install_state(layout))
        skipped: list[PlannedComponent] = []
        pending: list[PlannedComponent] = []
        for item in plan.components:
            if component_is_current(layout, item, existing.get(item.component.component_id)):
                skipped.append(item)
            else:
                pending.append(item)
        staged: dict[str, Path] = {}
        for item in pending:
            artifact_paths = {artifact.artifact_id: fetch(artifact) for artifact in item.variant.artifacts}
            stage = layout.staging / (item.component.component_id + "-" + uuid.uuid4().hex)
            if item.runtime_id is not None:
                wheels = [artifact_paths[artifact.artifact_id] for artifact in item.variant.artifacts]
                assemble_runtime(
                    layout,
                    item,
                    base_runtime=base_runtime,
                    wheel_paths=wheels,
                    destination=stage,
                    owner_sources=_owner_sources(source, item.runtime_id),
                )
            else:
                assemble_resource(layout, item, artifact_paths=artifact_paths, destination=stage)
            if not probe(item, stage):
                raise AssemblyError(f"offline probe failed: {item.component.component_id}")
            staged[item.component.target_relative_path] = stage
        publish_directories(layout, staged)
        for item in pending:
            if item.runtime_id is not None:
                writer(item, layout)
        records: dict[str, object] = {}
        for item in plan.components:
            records[item.component.component_id] = component_record(layout, item)
            if not component_is_current(layout, item, records[item.component.component_id]):
                raise AssemblyError(f"published component cannot be verified: {item.component.component_id}")
        state = build_install_state(manifest, plan, records)
        state_path = write_install_state(layout, state)
        return InstallResult(
            tuple(item.component.component_id for item in pending),
            tuple(item.component.component_id for item in skipped),
            state_path,
        )
    except Exception:
        cleanup_failure(layout)
        raise


def _bootstrap_runtime_from_argument(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_dir() or not (path / "python.exe").is_file():
        raise AssemblyError(f"bootstrap runtime is invalid: {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--accelerator", choices=("cpu", "nvidia"), required=True)
    parser.add_argument("--bootstrap-runtime", type=Path, required=True)
    arguments = parser.parse_args()
    raw_manifest = arguments.manifest.read_bytes()
    actual_sha256 = sha256_bytes(raw_manifest)
    if actual_sha256 != arguments.manifest_sha256:
        parser.error("install manifest SHA-256 does not match the PowerShell bootstrap identity")
    try:
        manifest = load_manifest_path(arguments.manifest)
        result = install_project(
            project_root=arguments.project_root,
            source_root=arguments.project_root,
            manifest=manifest,
            accelerator=arguments.accelerator,
            base_runtime=_bootstrap_runtime_from_argument(arguments.bootstrap_runtime),
        )
    except (AssemblyError, ManifestError, OSError) as exc:
        print(f"source bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print("installed components: " + ", ".join(result.installed_component_ids))
    print("skipped components: " + ", ".join(result.skipped_component_ids))
    print(f"install state: {result.state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
