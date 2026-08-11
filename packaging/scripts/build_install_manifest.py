"""Developer-only validator and writer for frozen source-bootstrap inventories."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


_LOCK_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>\S+)\s+--hash=sha256:(?P<sha256>[0-9a-f]{64})$"
)
_SELECTOR = re.compile(r"^(?P<component>[a-z0-9][a-z0-9_.-]*):(?P<variant>[a-z0-9][a-z0-9_.-]*)$")


class ManifestBuildError(ValueError):
    """The developer-supplied release inventory is incomplete or inconsistent."""


@dataclass(frozen=True)
class LockedWheel:
    distribution: str
    version: str
    sha256: str


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _load_manifest_contract() -> Any:
    path = Path(__file__).resolve().parents[1] / "installer" / "manifest.py"
    name = "_source_bootstrap_manifest_contract"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ManifestBuildError(f"manifest contract cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestBuildError(f"inventory JSON has duplicate key: {key}")
        value[key] = item
    return value


def _read_inventory(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestBuildError) as exc:
        if isinstance(exc, ManifestBuildError):
            raise
        raise ManifestBuildError(f"inventory is unreadable: {path}") from exc


def parse_lock(path: Path) -> dict[str, LockedWheel]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ManifestBuildError(f"wheel lock is unreadable: {path}") from exc
    wheels: dict[str, LockedWheel] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_ENTRY.fullmatch(line)
        if match is None:
            raise ManifestBuildError(f"wheel lock entry is invalid: {path}: {line}")
        wheel = LockedWheel(
            _normalized_distribution(match.group("name")),
            match.group("version"),
            match.group("sha256"),
        )
        if wheel.distribution in wheels:
            raise ManifestBuildError(f"wheel lock has duplicate distribution: {path}: {wheel.distribution}")
        wheels[wheel.distribution] = wheel
    if not wheels:
        raise ManifestBuildError(f"wheel lock has no artifacts: {path}")
    return wheels


def _wheel_from_artifact(artifact: object, locked: dict[str, LockedWheel]) -> LockedWheel | None:
    if not isinstance(artifact, dict):
        raise ManifestBuildError("wheel artifact is invalid")
    relative_path = artifact.get("relativePath")
    if not isinstance(relative_path, str):
        raise ManifestBuildError("wheel artifact relative path is invalid")
    filename = PureWindowsPath(relative_path.replace("/", "\\")).name
    if not filename.lower().endswith(".whl"):
        return None
    lower_filename = filename.lower()
    matches: list[LockedWheel] = []
    for wheel in locked.values():
        prefixes = {
            f"{wheel.distribution}-{wheel.version}-",
            f"{wheel.distribution.replace('-', '_')}-{wheel.version}-",
        }
        if any(lower_filename.startswith(prefix.lower()) for prefix in prefixes):
            matches.append(wheel)
    if len(matches) != 1:
        raise ManifestBuildError(f"wheel artifact is not uniquely represented by its lock: {filename}")
    sha256 = artifact.get("sha256")
    if sha256 != matches[0].sha256:
        raise ManifestBuildError(f"artifact SHA-256 does not match wheel lock: {filename}")
    return matches[0]


def _raw_component_variant(manifest: dict[str, object], selector: str) -> list[object]:
    match = _SELECTOR.fullmatch(selector)
    if match is None:
        raise ManifestBuildError(f"variant lock selector is invalid: {selector}")
    components = manifest.get("components")
    if not isinstance(components, list):
        raise ManifestBuildError("manifest components are invalid")
    for component in components:
        if not isinstance(component, dict) or component.get("componentId") != match.group("component"):
            continue
        variants = component.get("variants")
        if not isinstance(variants, dict) or match.group("variant") not in variants:
            raise ManifestBuildError(f"variant lock selector has no matching component variant: {selector}")
        variant = variants[match.group("variant")]
        if not isinstance(variant, dict) or not isinstance(variant.get("artifacts"), list):
            raise ManifestBuildError(f"variant lock selector has invalid artifacts: {selector}")
        return variant["artifacts"]
    raise ManifestBuildError(f"variant lock selector has no matching component: {selector}")


def _validate_release_identity(manifest: dict[str, object], release_artifacts: object, contract: Any) -> None:
    try:
        release = contract.validate_release_artifacts(release_artifacts)
    except contract.ManifestError as exc:
        raise ManifestBuildError(str(exc)) from exc
    if release["releaseVersion"] != manifest["releaseVersion"]:
        raise ManifestBuildError("release artifact version does not match install manifest")
    records = {record["id"]: record for record in release["artifacts"]}
    bootstrap = manifest.get("bootstrap")
    if not isinstance(bootstrap, dict) or not isinstance(bootstrap.get("artifact"), dict):
        raise ManifestBuildError("bootstrap artifact is invalid")
    artifact = bootstrap["artifact"]
    record = records.get(artifact.get("id"))
    if record is None:
        raise ManifestBuildError("bootstrap artifact has no published release identity")
    for field, release_field in (("url", "publishedUrl"), ("sizeBytes", "sizeBytes"), ("sha256", "sha256")):
        if artifact.get(field) != record[release_field]:
            raise ManifestBuildError("bootstrap artifact does not match its published release identity")


def build_manifest(inventory: object, requirements_root: str | Path) -> dict[str, object]:
    """Validate a complete developer inventory and return the generated manifest value."""
    if not isinstance(inventory, dict) or set(inventory) != {"manifest", "releaseArtifacts", "variantLocks"}:
        raise ManifestBuildError("inventory fields are invalid")
    manifest = inventory["manifest"]
    if not isinstance(manifest, dict):
        raise ManifestBuildError("manifest inventory is invalid")
    contract = _load_manifest_contract()
    try:
        contract.load_manifest(manifest)
    except contract.ManifestError as exc:
        raise ManifestBuildError(str(exc)) from exc
    _validate_release_identity(manifest, inventory["releaseArtifacts"], contract)
    variant_locks = inventory["variantLocks"]
    if not isinstance(variant_locks, dict) or not variant_locks:
        raise ManifestBuildError("variant locks are invalid")
    requirements = Path(requirements_root)
    for selector, lock_name in variant_locks.items():
        if not isinstance(selector, str) or not isinstance(lock_name, str) or not lock_name:
            raise ManifestBuildError("variant locks are invalid")
        locked = parse_lock(requirements / f"{lock_name}.lock")
        matched: set[str] = set()
        for artifact in _raw_component_variant(manifest, selector):
            wheel = _wheel_from_artifact(artifact, locked)
            if wheel is not None:
                matched.add(wheel.distribution)
        if matched != set(locked):
            missing = ", ".join(sorted(set(locked) - matched))
            extra = ", ".join(sorted(matched - set(locked)))
            raise ManifestBuildError(f"wheel artifacts do not exactly match lock {lock_name}: missing={missing}; extra={extra}")
    return manifest


def write_manifest(manifest: dict[str, object], output: Path, contract: Any) -> None:
    payload = contract.canonical_json(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with temporary.open("xb") as target:
            target.write(payload)
        os.replace(temporary, output)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ManifestBuildError(f"cannot write generated manifest: {output}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--requirements-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    arguments = parser.parse_args()
    if not arguments.validate_only and arguments.output is None:
        parser.error("--output is required unless --validate-only is used")
    inventory = _read_inventory(arguments.inventory)
    manifest = build_manifest(inventory, arguments.requirements_root)
    if arguments.output is not None:
        write_manifest(manifest, arguments.output, _load_manifest_contract())
    print(f"validated source bootstrap manifest for {manifest['releaseVersion']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
