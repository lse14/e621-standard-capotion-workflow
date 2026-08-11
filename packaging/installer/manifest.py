"""Strict frozen install-manifest parsing for the source bootstrap installer."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_ARTIFACT_ID = re.compile(r"^[a-z][a-z0-9+.-]{0,127}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_VARIANT = re.compile(r"^(?:cpu|cuda|shared)$")
_CPU_CUDA_PAYLOAD = re.compile(r"(?:cuda|\+cu\d*|cudnn|nvidia[-_])", re.IGNORECASE)


class ManifestError(ValueError):
    """Raised when a frozen installer manifest cannot safely identify content."""


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    url: str
    allowed_hosts: tuple[str, ...]
    size_bytes: int
    sha256: str
    relative_path: str
    repository: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class ComponentVariant:
    name: Literal["cpu", "cuda", "shared"]
    artifacts: tuple[Artifact, ...]
    peak_bytes: int
    probe: str


@dataclass(frozen=True)
class Component:
    component_id: str
    kind: str
    required: bool
    target_relative_path: str
    variants: dict[str, ComponentVariant]


@dataclass(frozen=True)
class SelectedComponent:
    component: Component
    variant: Literal["cpu", "cuda", "shared"]


@dataclass(frozen=True)
class InstallManifest:
    schema_version: int
    release_version: str
    source_commit: str
    allowed_hosts: frozenset[str]
    bootstrap_artifact: Artifact
    bootstrap_entry_relative_path: str
    bootstrap_peak_bytes: int
    components: tuple[Component, ...]
    cleanup_success_relative_paths: tuple[str, ...]
    fingerprint: str

    def select_components(self, accelerator: str) -> dict[str, SelectedComponent]:
        if accelerator not in {"cpu", "nvidia"}:
            raise ManifestError("accelerator is invalid")
        selected: dict[str, SelectedComponent] = {}
        for component in self.components:
            variant = "cuda" if accelerator == "nvidia" and "cuda" in component.variants else "cpu"
            if variant not in component.variants:
                variant = "shared"
            if variant not in component.variants:
                raise ManifestError(f"component has no usable variant: {component.component_id}")
            selected[component.component_id] = SelectedComponent(component, variant)  # type: ignore[arg-type]
        return selected


def canonical_json(value: object) -> bytes:
    """Serialize a manifest identity without ordering or whitespace ambiguity."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError("manifest cannot be canonically encoded") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(value: object, field: str, *, pattern: re.Pattern[str] | None = None, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ManifestError(f"{field} is invalid")
    if len(value.encode("utf-8")) > maximum or pattern is not None and pattern.fullmatch(value) is None:
        raise ManifestError(f"{field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ManifestError(f"{field} is invalid")
    return value


def _safe_relative(value: object, field: str) -> str:
    text = _text(value, field, maximum=1024).replace("/", "\\")
    path = PureWindowsPath(text)
    if path.is_absolute() or path.drive or path.root or text.startswith("\\"):
        raise ManifestError(f"{field} must be a safe relative path")
    for part in text.split("\\"):
        if not part or part in {".", ".."} or ":" in part or part.endswith((".", " ")):
            raise ManifestError(f"{field} must be a safe relative path")
    return text


def _validate_hosts(value: object, top_level_hosts: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError("artifact allowed host is invalid")
    hosts: list[str] = []
    for raw in value:
        host = _text(raw, "artifact allowed host", pattern=_HOST, maximum=253).lower()
        if host not in top_level_hosts:
            raise ManifestError("artifact allowed host is invalid")
        hosts.append(host)
    if len(hosts) != len(set(hosts)):
        raise ManifestError("artifact allowed host is invalid")
    return tuple(hosts)


def _validate_https_url(value: object, allowed_hosts: tuple[str, ...]) -> str:
    url = _text(value, "artifact URL", maximum=2048)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ManifestError("artifact URL must use an allowed HTTPS host")
    return url


def _validate_huggingface_identity(artifact: dict[str, Any], *, url: str) -> tuple[str | None, str | None]:
    repository = artifact.get("repository")
    revision = artifact.get("revision")
    if repository is None and revision is None:
        return None, None
    repository_text = _text(repository, "artifact repository", maximum=256)
    revision_text = _text(revision, "artifact revision", maximum=40)
    if _COMMIT.fullmatch(revision_text) is None:
        raise ManifestError("artifact revision must be a full commit SHA")
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.hostname.lower() != "huggingface.co":
        raise ManifestError("Hugging Face artifact URL is invalid")
    expected_prefix = "/" + repository_text + "/resolve/" + revision_text + "/"
    if not parsed.path.startswith(expected_prefix):
        raise ManifestError("Hugging Face artifact URL does not use the full revision")
    return repository_text, revision_text


def _validate_artifact(value: object, *, top_level_hosts: frozenset[str]) -> Artifact:
    fields = {"id", "url", "allowedHosts", "sizeBytes", "sha256", "relativePath", "repository", "revision"}
    if not isinstance(value, dict) or not {"id", "url", "allowedHosts", "sizeBytes", "sha256", "relativePath"}.issubset(value) or set(value) - fields:
        raise ManifestError("artifact fields are invalid")
    if ("repository" in value) != ("revision" in value):
        raise ManifestError("artifact repository identity is invalid")
    artifact_id = _text(value["id"], "artifact id", pattern=_ARTIFACT_ID, maximum=128)
    allowed_hosts = _validate_hosts(value["allowedHosts"], top_level_hosts)
    url = _validate_https_url(value["url"], allowed_hosts)
    size_bytes = _positive_int(value["sizeBytes"], "artifact size")
    sha256 = _text(value["sha256"], "artifact SHA-256", pattern=_SHA256, maximum=64)
    relative_path = _safe_relative(value["relativePath"], "artifact relative path")
    repository, revision = _validate_huggingface_identity(value, url=url)
    return Artifact(artifact_id, url, allowed_hosts, size_bytes, sha256, relative_path, repository, revision)


def _validate_variant(name: object, value: object, *, hosts: frozenset[str]) -> ComponentVariant:
    variant = _text(name, "component variant", pattern=_VARIANT, maximum=16)
    if not isinstance(value, dict) or set(value) != {"artifacts", "peakBytes", "probe"}:
        raise ManifestError("component variant fields are invalid")
    artifacts_value = value["artifacts"]
    if not isinstance(artifacts_value, list) or not artifacts_value:
        raise ManifestError("component variant artifacts are invalid")
    artifacts = tuple(_validate_artifact(item, top_level_hosts=hosts) for item in artifacts_value)
    if variant == "cpu" and any(_CPU_CUDA_PAYLOAD.search(item.artifact_id + "\n" + item.url) for item in artifacts):
        raise ManifestError("CPU variant contains CUDA payload")
    return ComponentVariant(variant, artifacts, _positive_int(value["peakBytes"], "component peak bytes"), _text(value["probe"], "component probe", pattern=_IDENTIFIER, maximum=128))


def _validate_component(value: object, *, hosts: frozenset[str]) -> Component:
    if not isinstance(value, dict) or set(value) != {"componentId", "kind", "required", "targetRelativePath", "variants"}:
        raise ManifestError("component fields are invalid")
    component_id = _text(value["componentId"], "component id", pattern=_IDENTIFIER, maximum=128)
    kind = _text(value["kind"], "component kind", pattern=_IDENTIFIER, maximum=128)
    if type(value["required"]) is not bool:
        raise ManifestError("component required is invalid")
    variants_value = value["variants"]
    if not isinstance(variants_value, dict) or not variants_value:
        raise ManifestError("component variants are invalid")
    variants = {str(name): _validate_variant(name, record, hosts=hosts) for name, record in variants_value.items()}
    if len(variants) != len(variants_value):
        raise ManifestError("component variants are invalid")
    return Component(component_id, kind, value["required"], _safe_relative(value["targetRelativePath"], "component target relative path"), variants)


def _validate_bootstrap(value: object, *, hosts: frozenset[str]) -> tuple[Artifact, str, int]:
    if not isinstance(value, dict) or set(value) != {"artifact", "entryRelativePath", "peakBytes"}:
        raise ManifestError("bootstrap fields are invalid")
    return (
        _validate_artifact(value["artifact"], top_level_hosts=hosts),
        _safe_relative(value["entryRelativePath"], "bootstrap entry relative path"),
        _positive_int(value["peakBytes"], "bootstrap peak bytes"),
    )


def _validate_cleanup(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict) or set(value) != {"successRelativePaths"}:
        raise ManifestError("cleanup fields are invalid")
    paths = value["successRelativePaths"]
    if not isinstance(paths, list) or not paths:
        raise ManifestError("cleanup paths are invalid")
    normalized = tuple(_safe_relative(path, "cleanup relative path") for path in paths)
    if len({path.casefold() for path in normalized}) != len(normalized) or any(not path.casefold().startswith(".runtime-build\\") for path in normalized):
        raise ManifestError("cleanup paths are invalid")
    return normalized


def load_manifest(value: object) -> InstallManifest:
    expected_fields = {"schemaVersion", "releaseVersion", "sourceCommit", "allowedHosts", "bootstrap", "components", "cleanup"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ManifestError("install manifest fields are invalid")
    if value["schemaVersion"] != 1:
        raise ManifestError("install manifest schema is invalid")
    release_version = _text(value["releaseVersion"], "release version", maximum=128)
    source_commit = _text(value["sourceCommit"], "source commit", pattern=_COMMIT, maximum=40)
    raw_hosts = value["allowedHosts"]
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ManifestError("allowed hosts are invalid")
    hosts = frozenset(_text(host, "allowed host", pattern=_HOST, maximum=253).lower() for host in raw_hosts)
    if len(hosts) != len(raw_hosts):
        raise ManifestError("allowed hosts are invalid")
    bootstrap_artifact, bootstrap_entry, bootstrap_peak = _validate_bootstrap(value["bootstrap"], hosts=hosts)
    raw_components = value["components"]
    if not isinstance(raw_components, list) or not raw_components:
        raise ManifestError("components are invalid")
    components = tuple(_validate_component(component, hosts=hosts) for component in raw_components)
    if len({component.component_id for component in components}) != len(components):
        raise ManifestError("component IDs are duplicate")
    if len({component.target_relative_path.casefold() for component in components}) != len(components):
        raise ManifestError("component targets are duplicate")
    artifact_paths = [bootstrap_artifact.relative_path]
    for component in components:
        for variant in component.variants.values():
            artifact_paths.extend(artifact.relative_path for artifact in variant.artifacts)
    if len({path.casefold() for path in artifact_paths}) != len(artifact_paths):
        raise ManifestError("duplicate artifact target")
    cleanup_paths = _validate_cleanup(value["cleanup"])
    return InstallManifest(
        1,
        release_version,
        source_commit,
        hosts,
        bootstrap_artifact,
        bootstrap_entry,
        bootstrap_peak,
        components,
        cleanup_paths,
        sha256_bytes(canonical_json(value)),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("manifest JSON has duplicate keys")
        result[key] = value
    return result


def load_manifest_path(path: str | Path) -> InstallManifest:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys, parse_constant=lambda _: (_ for _ in ()).throw(ManifestError("manifest JSON constant is invalid")))
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError("install manifest is unreadable") from exc
    return load_manifest(value)


def validate_release_artifacts(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "releaseVersion", "artifacts"} or value["schemaVersion"] != 1:
        raise ManifestError("release artifacts are invalid")
    release_version = _text(value["releaseVersion"], "release artifact release version", maximum=128)
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ManifestError("release artifact records are invalid")
    identifiers: set[str] = set()
    normalized: list[dict[str, object]] = []
    for record in artifacts:
        if not isinstance(record, dict) or set(record) != {"id", "publishedUrl", "sizeBytes", "sha256"}:
            raise ManifestError("release artifact record is invalid")
        artifact_id = _text(record["id"], "release artifact id", pattern=_IDENTIFIER, maximum=128)
        if artifact_id in identifiers:
            raise ManifestError("release artifact IDs are duplicate")
        identifiers.add(artifact_id)
        url = _text(record["publishedUrl"], "release artifact URL", maximum=2048)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ManifestError("release artifact URL is invalid")
        size_bytes = _positive_int(record["sizeBytes"], "release artifact size")
        sha256 = _text(record["sha256"], "release artifact SHA-256", pattern=_SHA256, maximum=64)
        normalized.append({"id": artifact_id, "publishedUrl": url, "sizeBytes": size_bytes, "sha256": sha256})
    return {"schemaVersion": 1, "releaseVersion": release_version, "artifacts": normalized}
