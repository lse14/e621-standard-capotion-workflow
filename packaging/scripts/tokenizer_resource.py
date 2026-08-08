"""Preview-first tokenizer resource lifecycle for Roadmap Task 2.4."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


TOKENIZER_SOURCES = {
    "tokenizer-qwen3-0.6b-anima-v1": {
        "model_id": "Qwen/Qwen3-0.6B",
        "context_path": ("max_position_embeddings",),
    },
    "tokenizer-qwen3-vl-4b-krea2-v1": {
        "model_id": "Qwen/Qwen3-VL-4B-Instruct",
        "context_path": ("text_config", "max_position_embeddings"),
    },
}
TOKENIZER_ALLOWLIST = frozenset({
    "added_tokens.json", "config.json", "merges.txt", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json", "vocab.json", "vocab.txt",
})
TOKENIZER_REQUIRED_FILES = frozenset({"config.json", "tokenizer.json"})
DIRECT_REQUIREMENTS = ("tokenizers==0.21.4",)
CACHE_ROOT_RELATIVE = Path(".runtime-build") / "tokenizer-import" / "v1"


class TokenizerResourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfficialTokenizerSource:
    resource_id: str
    model_id: str
    revision: str
    files: tuple[str, ...]


class TokenizerProjectLayout:
    def __init__(
        self,
        *,
        project_root: Path,
        requirements_input: Path,
        requirements_target: Path,
        wheelhouse_target: Path,
        runtime_target: Path,
        runtime_manifest_target: Path,
        resource_root: Path,
        resource_target: Path,
        cache_root: Path,
    ) -> None:
        self.project_root = project_root
        self.requirements_input = requirements_input
        self.requirements_target = requirements_target
        self.wheelhouse_target = wheelhouse_target
        self.runtime_target = runtime_target
        self.runtime_manifest_target = runtime_manifest_target
        self.resource_root = resource_root
        self.resource_target = resource_target
        self.cache_root = cache_root


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TokenizerResourceError("path cannot be inspected") from exc
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate))))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def assert_project_path(project_root: str | Path, candidate: str | Path) -> Path:
    root = _absolute(project_root)
    if not root.is_dir() or _is_reparse(root):
        raise TokenizerResourceError("project root is unavailable or unsafe")
    target = _absolute(candidate)
    if not _is_within(root, target):
        raise TokenizerResourceError("path escapes project root")
    current = root
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise TokenizerResourceError("path escapes project root") from exc
    for component in relative.parts:
        current = current / component
        if not current.exists():
            break
        if _is_reparse(current):
            raise TokenizerResourceError("reparse point is not allowed")
    return target


def project_layout(project_root: str | Path) -> TokenizerProjectLayout:
    root = _absolute(project_root)
    assert_project_path(root, root)
    return TokenizerProjectLayout(
        project_root=root,
        requirements_input=assert_project_path(root, root / "packaging" / "requirements" / "token-budget.in"),
        requirements_target=assert_project_path(root, root / "packaging" / "requirements" / "token-budget.lock"),
        wheelhouse_target=assert_project_path(root, root / "packaging" / "wheelhouse" / "token-budget"),
        runtime_target=assert_project_path(root, root / ".runtime-build" / "runtimes" / "token-budget"),
        runtime_manifest_target=assert_project_path(root, root / ".runtime-build" / "manifests" / "runtimes" / "token-budget.json"),
        resource_root=assert_project_path(root, root / "resource-library"),
        resource_target=assert_project_path(root, root / "resource-library" / "tokenizers"),
        cache_root=assert_project_path(root, root / CACHE_ROOT_RELATIVE),
    )


def _requirements(path: Path) -> list[str]:
    try:
        requirements = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TokenizerResourceError("token-budget requirements input is unreadable") from exc
    if requirements != list(DIRECT_REQUIREMENTS):
        raise TokenizerResourceError("token-budget requirements input must contain exactly tokenizers==0.21.4")
    return requirements


def _context_values(value: object) -> list[int]:
    values: list[int] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "max_position_embeddings":
                if type(nested) is not int or nested < 1:
                    raise TokenizerResourceError("context limit must be a positive integer")
                values.append(nested)
            values.extend(_context_values(nested))
    return values


def context_limit(config: object, context_path: tuple[str, ...]) -> int:
    if not isinstance(config, dict) or not context_path:
        raise TokenizerResourceError("context limit is unavailable")
    current: object = config
    for part in context_path:
        if not isinstance(current, dict) or part not in current:
            raise TokenizerResourceError("context limit is unavailable")
        current = current[part]
    if type(current) is not int or current < 1:
        raise TokenizerResourceError("context limit must be a positive integer")
    values = _context_values(config)
    if len(values) != 1 or values[0] != current:
        raise TokenizerResourceError("conflicting context limit declarations")
    return current


def validate_downloaded_files(files: Mapping[str, tuple[int, str]]) -> None:
    for path, record in files.items():
        if path not in TOKENIZER_ALLOWLIST:
            raise TokenizerResourceError("tokenizer file is not in the allowlist")
        if (
            not isinstance(record, tuple) or len(record) != 2 or type(record[0]) is not int or record[0] < 1
            or not isinstance(record[1], str) or len(record[1]) != 64
            or set(record[1]) - set("0123456789abcdef")
        ):
            raise TokenizerResourceError("tokenizer file record is invalid")
    if not TOKENIZER_REQUIRED_FILES <= set(files):
        raise TokenizerResourceError("tokenizer files must include config.json and tokenizer.json")


def _official_url(source: OfficialTokenizerSource, filename: str) -> str:
    if filename not in source.files or filename not in TOKENIZER_ALLOWLIST:
        raise TokenizerResourceError("tokenizer file is not in the resolved allowlist")
    return "https://huggingface.co/{}/resolve/{}/{}".format(
        source.model_id, source.revision, urllib.parse.quote(filename, safe=""),
    )


def _read_official_head(url: str) -> tuple[int, str, dict[str, str]]:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, redirect_url):
            return None

    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            return response.status, response.geturl(), {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, url, {}
        if exc.code in {301, 302, 303, 307, 308}:
            return exc.code, url, {key.lower(): value for key, value in exc.headers.items()}
        raise TokenizerResourceError("official tokenizer metadata is unavailable") from exc
    except OSError as exc:
        raise TokenizerResourceError("official tokenizer metadata is unavailable") from exc


def _read_official_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            value = response.read()
    except OSError as exc:
        raise TokenizerResourceError("official tokenizer file is unavailable") from exc
    if not value:
        raise TokenizerResourceError("official tokenizer file is empty")
    return value


def _main_resolution_url(model_id: str, filename: str) -> str:
    return "https://huggingface.co/{}/resolve/main/{}".format(
        model_id, urllib.parse.quote(filename, safe=""),
    )


def _revision_from_head(final_url: str, headers: Mapping[str, str]) -> str | None:
    revision = headers.get("x-repo-commit")
    if isinstance(revision, str) and len(revision) == 40 and not (set(revision) - set("0123456789abcdef")):
        return revision
    marker = "/resolve/"
    if marker not in final_url:
        return None
    candidate = final_url.split(marker, 1)[1].split("/", 1)[0]
    if len(candidate) == 40 and not (set(candidate) - set("0123456789abcdef")):
        return candidate
    return None


def resolve_official_tokenizer_sources(
    *, fetch_head: Callable[[str], tuple[int, str, Mapping[str, str]]] = _read_official_head,
) -> dict[str, OfficialTokenizerSource]:
    """Resolve only fixed official model file URLs to a single immutable commit."""
    result: dict[str, OfficialTokenizerSource] = {}
    for resource_id, identity in TOKENIZER_SOURCES.items():
        model_id = identity["model_id"]
        files: list[str] = []
        revisions: set[str] = set()
        for filename in sorted(TOKENIZER_ALLOWLIST):
            status, final_url, headers = fetch_head(_main_resolution_url(model_id, filename))
            if status == 404:
                continue
            if status not in {200, 301, 302, 303, 307, 308} or not isinstance(final_url, str) or not isinstance(headers, Mapping):
                raise TokenizerResourceError("official tokenizer file inventory is unavailable")
            revision = _revision_from_head(final_url, headers)
            if revision is None:
                raise TokenizerResourceError("official tokenizer revision is not immutable")
            files.append(filename)
            revisions.add(revision)
        if len(revisions) != 1:
            raise TokenizerResourceError("official tokenizer revision is unavailable")
        revision = revisions.pop()
        files = tuple(files)
        if not TOKENIZER_REQUIRED_FILES <= set(files):
            raise TokenizerResourceError("official tokenizer is missing a required allowlisted file")
        result[resource_id] = OfficialTokenizerSource(resource_id, model_id, revision, files)
    return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _manifest(
    source: OfficialTokenizerSource,
    *, context_limit: int,
    files: Mapping[str, tuple[int, str]],
) -> dict[str, object]:
    records = [
        {"path": path, "sizeBytes": files[path][0], "sha256": files[path][1]}
        for path in sorted(files)
    ]
    value: dict[str, object] = {
        "schemaVersion": 3,
        "kind": "tokenizer",
        "resourceId": source.resource_id,
        "owner": "token-budget",
        "profile": "shared",
        "resourceVersion": "qwen3-tokenizer-v1",
        "officialModelId": source.model_id,
        "revision": source.revision,
        "tokenizerFamily": "qwen3",
        "contextLimit": context_limit,
        "rootRelativePath": f"tokenizers\\{source.resource_id}",
        "files": records,
        "distribution": {
            "mode": "local-only",
            "sourceUrl": f"https://huggingface.co/{source.model_id}",
            "licenseStatus": "unverified",
        },
    }
    unsigned = {key: value[key] for key in sorted(value)}
    value["fingerprint"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return value


def stage_tokenizer_resources(
    layout: TokenizerProjectLayout,
    stage: str | Path,
    sources: Mapping[str, OfficialTokenizerSource],
    *,
    fetch_bytes: Callable[[str], bytes] = _read_official_bytes,
) -> dict[str, Path]:
    """Download and hash-verify resources into a private staging library only."""
    stage_root = assert_project_path(layout.project_root, stage)
    if stage_root.exists() or set(sources) != set(TOKENIZER_SOURCES):
        raise TokenizerResourceError("tokenizer staging layout is invalid")
    staged: dict[str, Path] = {}
    for resource_id in sorted(sources):
        source = sources[resource_id]
        if source.resource_id != resource_id or source.model_id != TOKENIZER_SOURCES[resource_id]["model_id"]:
            raise TokenizerResourceError("official tokenizer identity is invalid")
        package = stage_root / "resource-library" / "tokenizers" / resource_id
        package.mkdir(parents=True)
        records: dict[str, tuple[int, str]] = {}
        for filename in source.files:
            if filename not in TOKENIZER_ALLOWLIST:
                raise TokenizerResourceError("tokenizer file is not in the allowlist")
            content = fetch_bytes(_official_url(source, filename))
            if not isinstance(content, bytes) or not content:
                raise TokenizerResourceError("official tokenizer file is invalid")
            (package / filename).write_bytes(content)
            records[filename] = (len(content), _sha256_bytes(content))
        validate_downloaded_files(records)
        try:
            config = json.loads((package / "config.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TokenizerResourceError("official tokenizer config is invalid") from exc
        manifest = _manifest(
            source,
            context_limit=context_limit(config, TOKENIZER_SOURCES[resource_id]["context_path"]),
            files=records,
        )
        _write_json(package / "resource.json", manifest)
        staged[resource_id] = package
    return staged


def _resource_preview(layout: TokenizerProjectLayout) -> dict[str, object]:
    preview: dict[str, object] = {}
    for resource_id, source in TOKENIZER_SOURCES.items():
        target = layout.resource_target / resource_id
        fingerprint = _existing_fingerprint(target)
        preview[resource_id] = {
            "officialModelId": source["model_id"],
            "officialPage": f"https://huggingface.co/{source['model_id']}",
            "contextLimitPath": list(source["context_path"]),
            "target": str(target),
            "installation": "installed" if fingerprint is not None else "not_installed",
            "fingerprint": fingerprint,
        }
    return preview


def plan_import(project_root: str | Path) -> dict[str, object]:
    layout = project_layout(project_root)
    return {
        "schemaVersion": 1,
        "action": "ImportTokenizerResources",
        "mode": "preview",
        "requirements": _requirements(layout.requirements_input),
        "resources": _resource_preview(layout),
        "targets": {
            "lock": str(layout.requirements_target),
            "wheelhouse": str(layout.wheelhouse_target),
            "runtime": str(layout.runtime_target),
            "runtimeManifest": str(layout.runtime_manifest_target),
            "runtimeManifestLock": str(layout.runtime_manifest_target.parent.parent / "requirements" / "token-budget.lock"),
            "resourceRoot": str(layout.resource_target),
            "cache": str(layout.cache_root),
        },
        "applyChanges": [
            "resolve tokenizers==0.21.4 into a project-local lock and wheelhouse",
            "download tokenizer files",
            "restrict tokenizer downloads to the allowlist and two frozen official model identities",
            "validate immutable revisions, per-file SHA-256, context limits, and offline tokenizer loading",
            "atomically publish both tokenizer resources, the token-budget runtime, and its manifest in Task 2.4",
        ],
    }


def plan_reset(project_root: str | Path) -> dict[str, object]:
    layout = project_layout(project_root)
    return {
        "schemaVersion": 1,
        "action": "ResetTokenBudgetRuntime",
        "mode": "preview",
        "targets": {
            "runtime": str(layout.runtime_target),
            "manifest": str(layout.runtime_manifest_target),
            "lock": str(layout.requirements_target),
            "wheelhouse": str(layout.wheelhouse_target),
        },
        "applyChanges": [
            "remove only the project-local token-budget runtime, manifest, lock, and wheelhouse after Task 2.4 creates them",
        ],
    }


def _safe_size(path: Path) -> int:
    if _is_reparse(path):
        raise TokenizerResourceError("reparse point is not allowed")
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        raise TokenizerResourceError("cache target type is unsupported")
    total = 0
    for child in path.rglob("*"):
        if _is_reparse(child):
            raise TokenizerResourceError("reparse point is not allowed")
        if child.is_file():
            total += child.stat().st_size
    return total


def plan_clean(project_root: str | Path) -> dict[str, object]:
    layout = project_layout(project_root)
    targets: list[dict[str, object]] = []
    if layout.cache_root.exists():
        targets.append({"path": str(layout.cache_root), "bytes": _safe_size(layout.cache_root)})
    return {
        "schemaVersion": 1,
        "action": "CleanTokenBudgetArtifacts",
        "mode": "preview",
        "targets": targets,
        "applyChanges": ["remove only disposable tokenizer-import cache paths after Task 2.4"],
    }


def _run(command: list[str], *, label: str, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        raise TokenizerResourceError(f"{label} could not start") from exc
    if completed.returncode != 0:
        raise TokenizerResourceError(f"{label} failed with exit code {completed.returncode}")
    return completed.stdout


def _safe_remove(root: Path, target: Path) -> None:
    assert_project_path(root, target)
    if not target.exists():
        return
    if _is_reparse(target):
        raise TokenizerResourceError("reparse point is not allowed")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _publish_paths(root: Path, pairs: list[tuple[Path, Path]]) -> None:
    moved: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for staged, target in pairs:
            assert_project_path(root, staged)
            assert_project_path(root, target)
            if not staged.exists() or _is_reparse(staged):
                raise TokenizerResourceError("staged tokenizer artifact is unavailable")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if _is_reparse(target):
                    raise TokenizerResourceError("formal tokenizer artifact is unsafe")
                backup = target.with_name("." + target.name + ".tokenizer-backup-" + uuid.uuid4().hex)
                os.replace(target, backup)
                backups.append((target, backup))
            os.replace(staged, target)
            moved.append((staged, target))
    except Exception as exc:
        for staged, target in reversed(moved):
            try:
                if target.exists():
                    os.replace(target, staged)
            except OSError:
                pass
        for target, backup in reversed(backups):
            try:
                if backup.exists():
                    os.replace(backup, target)
            except OSError:
                pass
        raise TokenizerResourceError("tokenizer installation transaction was rolled back") from exc
    for _, backup in backups:
        _safe_remove(root, backup)


def _build_environment(layout: TokenizerProjectLayout) -> Path:
    toolchain = assert_project_path(
        layout.project_root, layout.project_root / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64" / "python.exe",
    )
    build_root = assert_project_path(layout.project_root, layout.cache_root / "build-environment")
    python = build_root / "Scripts" / "python.exe"
    if not python.is_file():
        _safe_remove(layout.project_root, build_root)
        build_root.parent.mkdir(parents=True, exist_ok=True)
        _run([str(toolchain), "-B", "-I", "-m", "venv", str(build_root)], label="tokenizer build environment creation")
    _run([str(python), "-B", "-I", "-m", "pip", "--version"], label="tokenizer build environment verification")
    return python


def _stage_runtime(layout: TokenizerProjectLayout, stage: Path, build_python: Path) -> tuple[Path, Path, Path]:
    requirements = stage / "requirements"
    wheelhouse = stage / "wheelhouse"
    requirements.mkdir(parents=True)
    shutil.copy2(layout.requirements_input, requirements / "token-budget.in")
    script_root = layout.project_root / "packaging" / "scripts"
    _run([
        str(build_python), "-B", "-I", str(script_root / "resolve_wheels.py"), "token-budget",
        "--requirements-root", str(requirements), "--wheelhouse-root", str(wheelhouse), "--python", str(build_python),
    ], label="tokenizer dependency resolution")
    _run([
        str(build_python), "-B", "-I", str(script_root / "verify_locks.py"),
        "--requirements-root", str(requirements), "--wheelhouse-root", str(wheelhouse), "token-budget",
    ], label="tokenizer dependency lock verification")
    toolchain_root = layout.project_root / ".toolchains" / "Python-3.11.15"
    install_root = stage / "install"
    _run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_root / "build_cpython311_runtime.ps1"),
        "-PythonSourceRoot", str(toolchain_root), "-OutputRoot", str(install_root), "-ReuseExistingBuild",
    ], label="tokenizer base runtime assembly")
    _run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_root / "assemble_runtime.ps1"),
        "-BaseRuntime", str(install_root / "runtimes" / "_base"),
        "-DestinationRuntime", str(install_root / "runtimes" / "token-budget"),
        "-RequirementsLock", str(requirements / "token-budget.lock"),
        "-Wheelhouse", str(wheelhouse / "token-budget"),
        "-OwnerSource", str(layout.project_root / "workers" / "token_budget" / "src" / "anima_token_budget_worker"),
        "-SharedSource", str(layout.project_root / "shared" / "anima_caption_format" / "anima_caption_format"),
        "-BuildPython", str(build_python),
    ], label="tokenizer runtime assembly")
    _safe_remove(layout.project_root, install_root / "runtimes" / "_base")
    _run([
        str(build_python), "-B", "-I", str(script_root / "generate_runtime_manifests.py"),
        "--install-root", str(install_root), "--requirements-root", str(requirements), "--runtime-id", "token-budget",
    ], label="tokenizer runtime manifest generation")
    return install_root / "runtimes" / "token-budget", requirements / "token-budget.lock", wheelhouse / "token-budget"


_OFFLINE_PROBE = """\
import json
import socket
import sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise RuntimeError('network is blocked during tokenizer probe')

socket.socket.connect = blocked
socket.socket.connect_ex = blocked
from tokenizers import Tokenizer

root = Path(sys.argv[1])
fixtures = ('English caption: blue sky.', '\u65e5\u672c\u8a9e\u306e\u8aac\u660e\u3067\u3059\u3002', '\u4e2d\u6587\u8bf4\u660e\uff0c\u5e26\u6709 Unicode\u3002')
result = {}
for resource_id in sys.argv[2:]:
    tokenizer = Tokenizer.from_file(str(root / 'tokenizers' / resource_id / 'tokenizer.json'))
    counts = [len(tokenizer.encode(text, add_special_tokens=False).ids) for text in fixtures]
    if not all(type(value) is int and value >= 0 for value in counts):
        raise RuntimeError('tokenizer count is invalid')
    result[resource_id] = counts
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
"""


def _offline_probe(runtime: Path, stage_library: Path, resource_ids: tuple[str, ...]) -> dict[str, list[int]]:
    output = _run([
        str(runtime / "python.exe"), "-B", "-I", "-c", _OFFLINE_PROBE, str(stage_library), *resource_ids,
    ], label="offline tokenizer runtime probe", cwd=runtime.parent.parent)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise TokenizerResourceError("offline tokenizer probe returned invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != set(resource_ids) or any(
        not isinstance(counts, list) or len(counts) != 3 or not all(type(count) is int and count >= 0 for count in counts)
        for counts in value.values()
    ):
        raise TokenizerResourceError("offline tokenizer probe result is invalid")
    return {resource_id: list(value[resource_id]) for resource_id in resource_ids}


def _existing_fingerprint(path: Path) -> str | None:
    try:
        value = json.loads((path / "resource.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    fingerprint = value.get("fingerprint") if isinstance(value, dict) else None
    return fingerprint if isinstance(fingerprint, str) and len(fingerprint) == 64 else None


def import_tokenizer_resources(
    project_root: str | Path,
    *,
    apply: bool = False,
    fetch_head: Callable[[str], tuple[int, str, Mapping[str, str]]] = _read_official_head,
    fetch_bytes: Callable[[str], bytes] = _read_official_bytes,
) -> dict[str, object]:
    preview = plan_import(project_root)
    if not apply:
        return preview
    layout = project_layout(project_root)
    layout.cache_root.mkdir(parents=True, exist_ok=True)
    staging_root = assert_project_path(layout.project_root, layout.cache_root / "staging")
    staging_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="import-", dir=staging_root))
    try:
        sources = resolve_official_tokenizer_sources(fetch_head=fetch_head)
        resources = stage_tokenizer_resources(layout, stage / "resources", sources, fetch_bytes=fetch_bytes)
        build_python = _build_environment(layout)
        runtime, lock, wheelhouse = _stage_runtime(layout, stage, build_python)
        resource_ids = tuple(sorted(resources))
        counts = _offline_probe(runtime, stage / "resources" / "resource-library", resource_ids)
        resource_pairs: list[tuple[Path, Path]] = []
        resource_actions: dict[str, str] = {}
        for resource_id in resource_ids:
            formal = layout.resource_target / resource_id
            staged = resources[resource_id]
            existing = _existing_fingerprint(formal) if formal.exists() else None
            staged_fingerprint = _existing_fingerprint(staged)
            if existing is not None and existing == staged_fingerprint:
                resource_actions[resource_id] = "idempotent"
            elif formal.exists():
                raise TokenizerResourceError("existing tokenizer resource fingerprint conflicts with the staged package")
            else:
                resource_pairs.append((staged, formal))
                resource_actions[resource_id] = "installed"
        manifest = stage / "install" / "manifests" / "runtimes" / "token-budget.json"
        manifest_lock = stage / "install" / "manifests" / "requirements" / "token-budget.lock"
        _publish_paths(layout.project_root, [
            (lock, layout.requirements_target),
            (wheelhouse, layout.wheelhouse_target),
            (runtime, layout.runtime_target),
            (manifest_lock, layout.runtime_manifest_target.parent.parent / "requirements" / "token-budget.lock"),
            (manifest, layout.runtime_manifest_target),
            *resource_pairs,
        ])
        return {
            "schemaVersion": 1,
            "action": "ImportTokenizerResources",
            "mode": "apply",
            "resources": {
                resource_id: {
                    "officialModelId": sources[resource_id].model_id,
                    "revision": sources[resource_id].revision,
                    "files": list(sources[resource_id].files),
                    "fingerprint": _existing_fingerprint(layout.resource_target / resource_id),
                    "offlineTokenCounts": counts[resource_id],
                    "publication": resource_actions[resource_id],
                }
                for resource_id in resource_ids
            },
        }
    finally:
        _safe_remove(layout.project_root, layout.cache_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "reset", "clean"):
        subcommand = subcommands.add_parser(command)
        subcommand.add_argument("--project-root", type=Path, required=True)
        subcommand.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "import":
            result = import_tokenizer_resources(arguments.project_root, apply=arguments.apply)
        elif arguments.command == "reset":
            if arguments.apply:
                raise TokenizerResourceError("tokenizer reset Apply is not part of Roadmap Task 2.4")
            result = plan_reset(arguments.project_root)
        else:
            if arguments.apply:
                raise TokenizerResourceError("tokenizer cleanup Apply is not part of Roadmap Task 2.4")
            result = plan_clean(arguments.project_root)
    except TokenizerResourceError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
