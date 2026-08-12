"""Standard-library orchestration for the source bootstrap installer."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

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
from paths import ProjectLayout, cleanup_failure, cleanup_success, publish_directories, recover_transactions, safe_relative


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

_CUDA_PROBE_COMPANIONS = {
    "caption-e621": "e621-tagger",
    "policy": "quality-stack",
}


@dataclass(frozen=True)
class InstallResult:
    installed_component_ids: tuple[str, ...]
    skipped_component_ids: tuple[str, ...]
    state_path: Path
    messages: tuple[str, ...]


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


def _write_runtime_manifest_at(source_root: Path, item: PlannedComponent, runtime_root: Path) -> Path:
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
    value = generator.manifest(runtime_root, requirements_root, item.runtime_id, specs)
    manifest_path = runtime_root / "manifests" / "runtimes" / f"{item.runtime_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    source_lock = requirements_root / f"{item.lock_name}.lock"
    if not source_lock.is_file():
        raise AssemblyError(f"runtime lock is missing: {source_lock}")
    lock_target = runtime_root / "manifests" / "requirements" / source_lock.name
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    lock_target.write_bytes(source_lock.read_bytes())
    return manifest_path


def _default_runtime_manifest_writer(source_root: Path, item: PlannedComponent, layout: ProjectLayout) -> str:
    manifest_path = _write_runtime_manifest_at(source_root, item, layout.runtime_root)
    return str(manifest_path.relative_to(layout.project_root)).replace("/", "\\")


def _published_target(layout: ProjectLayout, item: PlannedComponent) -> Path:
    relative = safe_relative(item.component.target_relative_path)
    return layout.project_root / Path(relative.replace("\\", os.sep))


def _stage_target(stage_root: Path, item: PlannedComponent) -> Path:
    relative = safe_relative(item.component.target_relative_path).split("\\")
    if item.runtime_id is not None:
        if relative[:2] != [".runtime-build", "runtimes"] or len(relative) != 3:
            raise AssemblyError(f"runtime staging target is invalid: {item.component.component_id}")
        return stage_root / "runtimes" / item.runtime_id
    if not relative or relative[0].casefold() != "resource-library":
        raise AssemblyError(f"resource staging target is invalid: {item.component.component_id}")
    return stage_root.joinpath(*relative)


def _remove_staged_component(stage: Path) -> None:
    if not stage.exists():
        return
    if stage.is_symlink() or not stage.is_dir():
        raise AssemblyError(f"staged component is unsafe: {stage}")
    shutil.rmtree(stage)


def _assemble_component(
    *,
    layout: ProjectLayout,
    source_root: Path,
    item: PlannedComponent,
    stage: Path,
    base_runtime: str | Path,
    fetch: ArtifactFetcher,
) -> None:
    artifact_paths = {artifact.artifact_id: fetch(artifact) for artifact in item.variant.artifacts}
    if item.runtime_id is not None:
        wheels = [artifact_paths[artifact.artifact_id] for artifact in item.variant.artifacts]
        assemble_runtime(
            layout,
            item,
            base_runtime=base_runtime,
            wheel_paths=wheels,
            destination=stage,
            owner_sources=_owner_sources(source_root, item.runtime_id),
        )
    else:
        assemble_resource(layout, item, artifact_paths=artifact_paths, destination=stage)


def _custom_probe_results(
    pending: list[PlannedComponent],
    targets: Mapping[str, Path],
    probe: Probe,
) -> dict[str, bool]:
    return {
        item.component.component_id: bool(probe(item, targets[item.component.component_id]))
        for item in pending
    }


def _default_probe_results(
    *,
    source_root: Path,
    plan: InstallationPlan,
    pending: list[PlannedComponent],
    targets: Mapping[str, Path],
) -> dict[str, bool]:
    for item in pending:
        if item.runtime_id is not None:
            _write_runtime_manifest_at(source_root, item, _stage_target_root(targets[item.component.component_id], item))
    from probes import run_offline_probes

    return run_offline_probes(plan.components, component_targets=targets)


def _stage_target_root(target: Path, item: PlannedComponent) -> Path:
    if item.runtime_id is None or target.parent.name.casefold() != "runtimes":
        raise AssemblyError(f"runtime staging target is invalid: {item.component.component_id}")
    return target.parent.parent


def _cpu_fallback_items(manifest: InstallManifest) -> dict[str, PlannedComponent]:
    return {
        item.component.component_id: item
        for item in installation_plan(manifest, accelerator="cpu").components
    }


def _fallback_group_component_ids(fallback_ids: set[str]) -> set[str]:
    return fallback_ids | {
        companion
        for component_id, companion in _CUDA_PROBE_COMPANIONS.items()
        if component_id in fallback_ids
    }


def _classify_probe_failures(
    pending: list[PlannedComponent],
    results: Mapping[str, bool | None],
) -> tuple[set[str], set[str], list[str]]:
    """Separate CUDA fallbacks from fatal probe failures."""
    items = {item.component.component_id: item for item in pending}
    fallback_ids = {
        component_id
        for component_id, item in items.items()
        if component_id in _CUDA_PROBE_COMPANIONS
        and item.variant.name == "cuda"
        and results.get(component_id) is not True
    }
    deferred = _fallback_group_component_ids(fallback_ids) & set(items)
    discarded_gpu = {
        component_id
        for component_id, item in items.items()
        if component_id == "ocr-gpu"
        and item.variant.name == "cuda"
        and results.get(component_id) is not True
    }
    failures = sorted(
        component_id
        for component_id in items
        if results.get(component_id) is not True
        and not (
            component_id in {"ocr-cpu", "ocr-gpu"}
            and results.get(component_id) is None
        )
        and component_id not in deferred | discarded_gpu
    )
    return fallback_ids, discarded_gpu, failures


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
        stage_root = layout.staging / ("install-" + uuid.uuid4().hex)
        staged: dict[str, Path] = {}
        messages: list[str] = []
        targets: dict[str, Path] = {
            item.component.component_id: _published_target(layout, item)
            for item in skipped
        }
        for item in pending:
            stage = _stage_target(stage_root, item)
            _assemble_component(
                layout=layout,
                source_root=source,
                item=item,
                stage=stage,
                base_runtime=base_runtime,
                fetch=fetch,
            )
            staged[item.component.target_relative_path] = stage
            targets[item.component.component_id] = stage

        if probe_component is not None:
            results = _custom_probe_results(pending, targets, probe_component)
        elif pending:
            results = _default_probe_results(
                source_root=source,
                plan=plan,
                pending=pending,
                targets=targets,
            )
        else:
            results = {}

        fallback_ids, discard_gpu, failures = _classify_probe_failures(pending, results)
        if failures:
            raise AssemblyError("offline probe failed: " + ", ".join(sorted(failures)))

        final_components = list(plan.components)
        final_pending = list(pending)
        if fallback_ids:
            cpu_items = _cpu_fallback_items(manifest)
            for index, item in enumerate(tuple(final_components)):
                component_id = item.component.component_id
                if component_id not in fallback_ids:
                    continue
                try:
                    fallback = cpu_items[component_id]
                except KeyError as exc:
                    raise AssemblyError(f"CPU fallback is unavailable: {component_id}") from exc
                stage = targets[component_id]
                _remove_staged_component(stage)
                staged.pop(item.component.target_relative_path, None)
                fallback_stage = _stage_target(stage_root, fallback)
                _assemble_component(
                    layout=layout,
                    source_root=source,
                    item=fallback,
                    stage=fallback_stage,
                    base_runtime=base_runtime,
                    fetch=fetch,
                )
                staged[fallback.component.target_relative_path] = fallback_stage
                targets[component_id] = fallback_stage
                final_components[index] = fallback
                final_pending[final_pending.index(item)] = fallback
                messages.append(f"{component_id} CUDA offline probe failed; rebuilding the CPU variant")
        if discard_gpu:
            for item in tuple(final_components):
                if item.component.component_id not in discard_gpu:
                    continue
                stage = targets.pop(item.component.component_id)
                _remove_staged_component(stage)
                staged.pop(item.component.target_relative_path, None)
                final_components.remove(item)
                final_pending.remove(item)
                messages.append("OCR GPU offline probe failed; GPU runtime was not published and OCR CPU remains selected")

        final_plan = InstallationPlan(plan.accelerator, tuple(final_components))
        if fallback_ids:
            retry_component_ids = _fallback_group_component_ids(fallback_ids)
            retry_items = [
                item
                for item in final_plan.components
                if item.component.component_id in retry_component_ids
            ]
            if probe_component is not None:
                retry_results = _custom_probe_results(
                    retry_items,
                    targets,
                    probe_component,
                )
            else:
                retry_results = _default_probe_results(
                    source_root=source,
                    plan=final_plan,
                    pending=final_pending,
                    targets=targets,
                )
            _retry_fallbacks, _retry_discarded_gpu, retry_failures = _classify_probe_failures(
                retry_items,
                retry_results,
            )
            if retry_failures:
                raise AssemblyError("offline probe failed after CPU fallback: " + ", ".join(sorted(retry_failures)))

        publish_directories(layout, staged)
        for item in final_pending:
            if item.runtime_id is not None:
                writer(item, layout)
        records: dict[str, object] = {}
        for item in final_plan.components:
            records[item.component.component_id] = component_record(layout, item)
            if not component_is_current(layout, item, records[item.component.component_id]):
                raise AssemblyError(f"published component cannot be verified: {item.component.component_id}")
        state = build_install_state(manifest, final_plan, records)
        state_path = write_install_state(layout, state)
        cleanup_success(layout)
        return InstallResult(
            tuple(item.component.component_id for item in final_pending),
            tuple(item.component.component_id for item in skipped),
            state_path,
            tuple(messages),
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
    for message in result.messages:
        print(message)
    print("installed components: " + ", ".join(result.installed_component_ids))
    print("skipped components: " + ", ".join(result.skipped_component_ids))
    print(f"install state: {result.state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
