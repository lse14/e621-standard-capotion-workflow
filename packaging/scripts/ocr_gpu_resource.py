"""Preview-only lifecycle contract for the future isolated Paddle GPU runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable


OFFICIAL_WHEEL_URL = (
    "https://paddle-whl.bj.bcebos.com/stable/cu129/paddlepaddle-gpu/"
    "paddlepaddle_gpu-3.3.0-cp311-cp311-win_amd64.whl"
)
GPU_PADDLE_VERSION = "3.3.0"
GPU_CACHE_TARGETS = (
    ".runtime-build/ocr-gpu/v1/build-environment",
    ".runtime-build/ocr-gpu/v1/downloads",
    ".runtime-build/ocr-gpu/v1/staging",
)
GPU_FORMAL_TARGETS = (
    ".runtime-build/runtimes/ocr-paddle-gpu",
    ".runtime-build/manifests/runtimes/ocr-paddle-gpu.json",
    ".runtime-build/manifests/requirements/ocr-paddle-gpu.lock",
    "packaging/requirements/ocr-paddle-gpu.lock",
)
APPLY_GATES = (
    "fixed cu129 wheel URL and observed size/SHA-256 inventory lock",
    "offline install from the unique build-environment/downloads/staging tree",
    "reject CPU paddlepaddle and multiple Paddle distributions",
    "read existing OCR models without copy or download",
    "three-model CUDA probe",
    "atomic publication of the four formal GPU artifacts",
)
OCR_RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1"
OCR_RESOURCE_FINGERPRINT = "368c31b8af0e96cc61239097688a457a050dfcc1205d054d4e631bd20529c9ca"
DIRECT_REQUIREMENTS = (OFFICIAL_WHEEL_URL, "paddleocr==3.7.0", "paddlex[ocr-core]==3.7.2")
_PROBE_SAMPLE_NAMES = ("zh", "ja", "en")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_reparse(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        attributes = 0
    return path.is_symlink() or bool(attributes & 0x400)


def _require_safe_project_root(project_root: Path) -> Path:
    root = _absolute(project_root)
    if not root.is_dir():
        raise ValueError("project root must be an existing directory")
    if _is_reparse(root):
        raise ValueError("project root must not be a reparse point")
    return root


def _assert_no_reparse_between(root: Path, candidate: Path) -> None:
    current = candidate
    while True:
        if current.exists() and _is_reparse(current):
            raise ValueError(f"reparse point is not allowed: {current}")
        if current == root:
            return
        parent = current.parent
        if parent == current:
            raise ValueError("candidate escapes project root")
        current = parent


def safe_gpu_cache_target(project_root: Path, candidate: Path) -> Path:
    """Accept only descendants of the three regenerable GPU cache locations."""
    root = _require_safe_project_root(project_root)
    target = _absolute(candidate if candidate.is_absolute() else root / candidate)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("GPU lifecycle target escapes project root") from exc
    _assert_no_reparse_between(root, target)
    allowed = tuple(_absolute(root / relative) for relative in GPU_CACHE_TARGETS)
    if not any(target == item or item in target.parents for item in allowed):
        raise ValueError("GPU lifecycle target is not a regenerable GPU cache")
    return target


@dataclass(frozen=True)
class GpuInstallPaths:
    project: Path
    build_environment: Path
    downloads: Path
    staging: Path
    staging_runtime: Path
    staging_manifest: Path
    staging_manifest_lock: Path
    staging_lock: Path
    staging_wheelhouse: Path
    runtime: Path
    manifest: Path
    manifest_lock: Path
    lock: Path
    wheelhouse: Path


def _install_paths(project_root: Path) -> GpuInstallPaths:
    project = _require_safe_project_root(project_root)
    cache = project / ".runtime-build" / "ocr-gpu" / "v1"
    staging = cache / "staging"
    values = GpuInstallPaths(
        project=project,
        build_environment=cache / "build-environment",
        downloads=cache / "downloads",
        staging=staging,
        staging_runtime=staging / "runtimes" / "ocr-paddle-gpu",
        staging_manifest=staging / "manifests" / "runtimes" / "ocr-paddle-gpu.json",
        staging_manifest_lock=staging / "manifests" / "requirements" / "ocr-paddle-gpu.lock",
        staging_lock=staging / "packaging" / "requirements" / "ocr-paddle-gpu.lock",
        staging_wheelhouse=staging / "packaging" / "wheelhouse" / "ocr-paddle-gpu",
        runtime=project / ".runtime-build" / "runtimes" / "ocr-paddle-gpu",
        manifest=project / ".runtime-build" / "manifests" / "runtimes" / "ocr-paddle-gpu.json",
        manifest_lock=project / ".runtime-build" / "manifests" / "requirements" / "ocr-paddle-gpu.lock",
        lock=project / "packaging" / "requirements" / "ocr-paddle-gpu.lock",
        wheelhouse=project / "packaging" / "wheelhouse" / "ocr-paddle-gpu",
    )
    for candidate in (
        values.build_environment, values.downloads, values.staging,
        values.runtime, values.manifest, values.manifest_lock, values.lock, values.wheelhouse,
    ):
        _assert_no_reparse_between(project, candidate)
    return values


def _formal_targets(paths: GpuInstallPaths) -> tuple[Path, Path, Path, Path]:
    return (paths.runtime, paths.manifest, paths.manifest_lock, paths.lock)


def _archive_existing_staging(
    paths: GpuInstallPaths,
    *,
    attempt_name: str | None = None,
) -> Path | None:
    """Move a failed staging tree aside before opening a fresh transaction."""
    if not paths.staging.exists():
        return None
    _assert_no_reparse_between(paths.project, paths.staging)
    if not paths.staging.is_dir():
        raise ValueError("GPU staging is not a safe directory")
    archive_root = paths.staging.parent / "failed-attempts"
    _assert_no_reparse_between(paths.project, archive_root)
    if archive_root.exists() and not archive_root.is_dir():
        raise ValueError("GPU failed-attempt archive root is not a safe directory")
    archive_name = attempt_name or datetime.now(timezone.utc).strftime("attempt-%Y%m%dT%H%M%SZ")
    archive = archive_root / archive_name
    _assert_no_reparse_between(paths.project, archive)
    if archive.exists():
        raise ValueError("GPU failed-attempt archive target already exists")
    archive_root.mkdir(parents=True, exist_ok=True)
    paths.staging.replace(archive)
    try:
        paths.staging.mkdir(parents=False, exist_ok=False)
    except Exception:
        if archive.exists() and not paths.staging.exists():
            archive.replace(paths.staging)
        raise
    return archive


def _require_complete_staging(paths: GpuInstallPaths) -> None:
    if (
        not paths.staging_runtime.is_dir()
        or not paths.staging_manifest.is_file()
        or not paths.staging_lock.is_file()
        or not paths.staging_manifest_lock.is_file()
        or not paths.staging_wheelhouse.is_dir()
        or not any(paths.staging_wheelhouse.iterdir())
    ):
        raise ValueError("GPU install staging is incomplete")
    if paths.staging_lock.read_bytes() != paths.staging_manifest_lock.read_bytes():
        raise ValueError("GPU manifest lock mirror differs from the packaging lock")


def _publish_staged_artifacts(paths: GpuInstallPaths) -> None:
    pairs = (
        (paths.staging_runtime, paths.runtime),
        (paths.staging_manifest, paths.manifest),
        (paths.staging_manifest_lock, paths.manifest_lock),
        (paths.staging_lock, paths.lock),
    )
    moved: list[tuple[Path, Path]] = []
    try:
        for source, target in pairs:
            _assert_no_reparse_between(paths.project, source)
            _assert_no_reparse_between(paths.project, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved.append((source, target))
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                target.replace(source)
        raise


def install_transaction(
    project_root: Path,
    *,
    prepare: Callable[[GpuInstallPaths], None],
    probe: Callable[[GpuInstallPaths], None],
) -> dict[str, object]:
    """Publish a complete GPU runtime only after an isolated preparation and probe succeed."""
    paths = _install_paths(project_root)
    existing = [target for target in _formal_targets(paths) if target.exists()]
    if existing:
        raise ValueError("GPU formal artifacts are already present or partial")
    archived_staging = _archive_existing_staging(paths)
    if archived_staging is None:
        paths.staging.mkdir(parents=True, exist_ok=False)
    prepare(paths)
    _require_complete_staging(paths)
    probe(paths)
    transient_wheelhouses = (paths.staging_wheelhouse, paths.wheelhouse)
    for wheelhouse in transient_wheelhouses:
        _assert_no_reparse_between(paths.project, wheelhouse)
        if wheelhouse.exists() and not wheelhouse.is_dir():
            raise ValueError("GPU wheelhouse is not a safe directory")
    for wheelhouse in transient_wheelhouses:
        if wheelhouse.exists():
            shutil.rmtree(wheelhouse)
    _publish_staged_artifacts(paths)
    result: dict[str, object] = {
        "action": "install",
        "mode": "apply",
        "writes": [str(target.relative_to(paths.project)).replace("\\", "/") for target in _formal_targets(paths)],
    }
    if archived_staging is not None:
        result["archivedStaging"] = str(archived_staging.relative_to(paths.project)).replace("\\", "/")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_probe_samples(sample_root: Path, report: object) -> list[tuple[str, Path, str]]:
    """Rebuild probe image paths from a fixed, ASCII-safe child report."""
    if _is_reparse(sample_root) or not sample_root.is_dir():
        raise ValueError("GPU probe sample directory is invalid")
    if not isinstance(report, dict) or not isinstance(report.get("samples"), list):
        raise ValueError("GPU probe sample report is invalid")
    samples = report["samples"]
    if len(samples) != len(_PROBE_SAMPLE_NAMES):
        raise ValueError("GPU probe sample report is incomplete")
    validated: list[tuple[str, Path, str]] = []
    for expected_name, sample in zip(_PROBE_SAMPLE_NAMES, samples):
        if not isinstance(sample, dict):
            raise ValueError("GPU probe sample report is invalid")
        name = sample.get("name")
        digest = sample.get("sha256")
        if name != expected_name:
            raise ValueError("GPU probe sample names are invalid")
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("GPU probe sample hash is invalid")
        image = sample_root / f"{name}.png"
        if _is_reparse(image) or not image.is_file() or _sha256(image) != digest:
            raise ValueError("GPU probe sample content is invalid")
        validated.append((name, image, digest))
    return validated


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        raise RuntimeError("GPU runtime staging command failed")
    return completed


def _wheel_metadata(path: Path) -> tuple[str, str, tuple[str, ...]]:
    with zipfile.ZipFile(path) as archive:
        metadata_paths = [name for name in archive.namelist() if len(PurePosixPath(name).parts) == 2 and name.endswith(".dist-info/METADATA")]
        wheel_paths = [name for name in archive.namelist() if len(PurePosixPath(name).parts) == 2 and name.endswith(".dist-info/WHEEL")]
        if len(metadata_paths) != 1 or len(wheel_paths) != 1:
            raise ValueError("GPU wheel metadata is invalid")
        metadata_text = archive.read(metadata_paths[0]).decode("utf-8")
        wheel_text = archive.read(wheel_paths[0]).decode("utf-8")
    name = next((line[6:] for line in metadata_text.splitlines() if line.startswith("Name: ")), None)
    version = next((line[9:] for line in metadata_text.splitlines() if line.startswith("Version: ")), None)
    tags = tuple(line[5:] for line in wheel_text.splitlines() if line.startswith("Tag: "))
    if not name or not version or not tags:
        raise ValueError("GPU wheel metadata is invalid")
    return name.casefold().replace("_", "-"), version, tags


def _validate_wheel_inventory(wheelhouse: Path) -> list[dict[str, object]]:
    wheels = sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name.casefold())
    if not wheels:
        raise ValueError("GPU wheel inventory is incomplete")
    packages: dict[str, str] = {}
    records: list[dict[str, object]] = []
    for wheel in wheels:
        name, version, tags = _wheel_metadata(wheel)
        compatible = any(
            tag.endswith("-none-any") and tag.split("-", 1)[0] in {"py3", "py311"}
            or tag in {
                "py3-none-win_amd64",
                "cp37-abi3-win_amd64",
                "cp38-abi3-win_amd64",
                "cp310-abi3-win_amd64",
                "cp311-cp311-win_amd64",
                "cp311-abi3-win_amd64",
            }
            for tag in tags
        )
        if not compatible or name in packages:
            raise ValueError("GPU wheel inventory is incompatible or duplicated")
        packages[name] = version
        records.append({"filename": wheel.name, "name": name, "version": version, "sizeBytes": wheel.stat().st_size, "sha256": _sha256(wheel)})
    if "paddlepaddle" in packages or packages.get("paddlepaddle-gpu") != GPU_PADDLE_VERSION or packages.get("paddleocr") != "3.7.0" or packages.get("paddlex") != "3.7.2":
        raise ValueError("GPU wheel inventory has invalid Paddle/OCR dependencies")
    return records


def _download_or_reuse_inventory(
    paths: GpuInstallPaths,
    *,
    builder: Path,
    source: Path,
) -> list[dict[str, object]]:
    if paths.downloads.exists():
        if not paths.downloads.is_dir() or _is_reparse(paths.downloads):
            raise ValueError("GPU download cache is not a safe directory")
        _assert_no_reparse_between(paths.project, paths.downloads)
        return _validate_wheel_inventory(paths.downloads)
    paths.downloads.mkdir(parents=True, exist_ok=False)
    _run(
        [
            str(builder), "-B", "-I", "-m", "pip", "download", "--dest", str(paths.downloads),
            "--only-binary", ":all:", "--platform", "win_amd64", "--implementation", "cp",
            "--python-version", "311", "--abi", "cp311", "-r", str(source),
        ],
        cwd=paths.project,
    )
    return _validate_wheel_inventory(paths.downloads)


def _prepare_real_install(paths: GpuInstallPaths) -> list[dict[str, object]]:
    source = paths.project / "packaging" / "requirements" / "ocr-paddle-gpu.in"
    if tuple(source.read_text(encoding="utf-8").splitlines()) != DIRECT_REQUIREMENTS:
        raise ValueError("GPU direct requirements are not fixed")
    toolchain = paths.project / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64" / "python.exe"
    if not toolchain.is_file():
        raise RuntimeError("project CPython 3.11.15 toolchain is unavailable")
    _run([str(toolchain), "-B", "-I", "-m", "venv", str(paths.build_environment)], cwd=paths.project)
    builder = paths.build_environment / "Scripts" / "python.exe"
    if not builder.is_file():
        raise RuntimeError("GPU build environment is unavailable")
    _download_or_reuse_inventory(paths, builder=builder, source=source)
    requirements = paths.staging / "packaging" / "requirements"
    requirements.mkdir(parents=True)
    (requirements / "ocr-paddle-gpu.in").write_bytes(source.read_bytes())
    resolver = paths.project / "packaging" / "scripts" / "resolve_wheels.py"
    _run([str(builder), "-B", "-I", str(resolver), "ocr-paddle-gpu", "--requirements-root", str(requirements), "--wheelhouse-root", str(paths.staging / "packaging" / "wheelhouse"), "--source-wheelhouse", str(paths.downloads), "--python", str(builder)], cwd=paths.project)
    inventory = _validate_wheel_inventory(paths.staging_wheelhouse)
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    build = paths.project / "packaging" / "scripts" / "build_cpython311_runtime.ps1"
    assemble = paths.project / "packaging" / "scripts" / "assemble_runtime.ps1"
    _run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(build), "-PythonSourceRoot", str(paths.project / ".toolchains" / "Python-3.11.15"), "-OutputRoot", str(paths.staging), "-ReuseExistingBuild"], cwd=paths.project)
    _run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(assemble), "-BaseRuntime", str(paths.staging / "runtimes" / "_base"), "-DestinationRuntime", str(paths.staging_runtime), "-RequirementsLock", str(paths.staging_lock), "-Wheelhouse", str(paths.staging_wheelhouse), "-OwnerSource", str(paths.project / "workers" / "ocr" / "src" / "anima_ocr_worker"), "-BuildPython", str(builder), "-KeepSetuptools"], cwd=paths.project)
    generator = paths.project / "packaging" / "scripts" / "generate_runtime_manifests.py"
    _run([str(toolchain), "-B", "-I", str(generator), "--install-root", str(paths.staging), "--requirements-root", str(requirements), "--include-ocr-paddle-gpu", "--runtime-id", "ocr-paddle-gpu"], cwd=paths.project)
    if not paths.staging_manifest_lock.is_file():
        raise RuntimeError("GPU manifest lock was not generated")
    return inventory


def _probe_real_gpu_runtime(paths: GpuInstallPaths) -> dict[str, object]:
    runtime_python = paths.staging_runtime / "python.exe"
    resource_root = paths.project / "resource-library"
    resource_check = "from pathlib import Path; from anima_ocr_worker.resource import load_ocr_resource; " + f"load_ocr_resource(Path({str(resource_root)!r}), r'ocr-models\\{OCR_RESOURCE_ID}\\resource.json', '{OCR_RESOURCE_FINGERPRINT}'); print('ok')"
    if _run([str(runtime_python), "-B", "-I", "-c", resource_check], cwd=paths.staging).stdout.strip() != "ok":
        raise RuntimeError("GPU OCR resource verification failed")
    font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if not font.is_file():
        raise RuntimeError("a local CJK system font is required for the GPU probe")
    sample_root = paths.staging / "probe-images"
    generator = (
        "import hashlib,json; from pathlib import Path; from PIL import Image,ImageDraw,ImageFont; "
        f"root=Path({str(sample_root)!r}); root.mkdir(); font=Path({str(font)!r}); face=ImageFont.truetype(str(font),72); records=[]\n"
        "for name,text in [('zh','你好世界'),('ja','こんにちは'),('en','offline English')]:\n"
        " image=Image.new('RGB',(900,180),'white'); ImageDraw.Draw(image).text((30,45),text,font=face,fill='black'); path=root/(name+'.png'); image.save(path); records.append({'name':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})\n"
        "print(json.dumps({'fontSha256':hashlib.sha256(font.read_bytes()).hexdigest(),'samples':records},ensure_ascii=True))"
    )
    sample_report = json.loads(_run([str(runtime_python), "-B", "-I", "-c", generator], cwd=paths.staging).stdout)
    probe_samples = _validate_probe_samples(sample_root, sample_report)
    samples = {
        "fontSha256": sample_report.get("fontSha256") if isinstance(sample_report, dict) else None,
        "samples": [{"name": name, "sha256": digest} for name, _, digest in probe_samples],
    }
    manifest_fingerprint = _sha256(paths.staging_manifest)
    hello = {"schemaVersion": 1, "payloadType": "ocr_hello_request", "jobId": "gpu-install-probe", "configHash": "a" * 64, "resourceId": OCR_RESOURCE_ID, "resourceManifestRelativePath": f"ocr-models\\{OCR_RESOURCE_ID}\\resource.json", "resourceFingerprint": OCR_RESOURCE_FINGERPRINT, "inference": {"useDocOrientationClassify": False, "useDocUnwarping": False, "useTextlineOrientation": True, "textRecScoreThresh": 0, "textDetLimitSideLen": 1920, "textDetLimitType": "max"}, "requestedDevice": "cuda", "expectedRuntimeId": "ocr-paddle-gpu", "expectedRuntimeFingerprint": manifest_fingerprint}
    items: list[dict[str, object]] = []
    for index, (name, image, digest) in enumerate(probe_samples, start=1):
        items.append({"schemaVersion": 1, "sampleId": index, "leaseId": f"gpu-probe-{name}", "relativeImagePath": f"probe/{name}.png", "imagePath": str(image), "imageSize": image.stat().st_size, "imageSha256": digest})
    def frame(message_id: str, method: str, payload: dict[str, object]) -> bytes:
        value = {"protocolVersion": "1.0", "kind": "request", "messageId": message_id, "runtimeId": "ocr-paddle-gpu", "owner": "ocr", "method": method, "payload": payload, "jobId": "gpu-install-probe", "configHash": "a" * 64}
        return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    wrapper = "import socket,sys; _deny=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('network is disabled')); socket.socket.connect=_deny; socket.socket.connect_ex=_deny; from anima_ocr_worker.entry import run; raise SystemExit(run(sys.stdin.buffer,sys.stdout.buffer,sys.stderr))"
    environment = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(name, None)
    environment["ANIMA_RESOURCE_ROOT"] = str(resource_root)
    completed = subprocess.run([str(runtime_python), "-B", "-I", "-u", "-c", wrapper], input=b"".join((frame("hello", "hello", hello), frame("process", "process_batch", {"schemaVersion": 1, "payloadType": "ocr_process_request", "items": items}), frame("shutdown", "shutdown", {}))), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, cwd=paths.staging, check=False, timeout=900)
    if completed.returncode != 0:
        raise RuntimeError("offline GPU OCR probe failed")
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    if len(responses) != 3:
        raise RuntimeError("offline GPU OCR probe protocol failed")
    evidence = responses[0].get("payload")
    required = {"requestedDevice": "cuda", "observedDevice": "cuda", "runtimeId": "ocr-paddle-gpu", "runtimeFingerprint": manifest_fingerprint, "paddleVersion": GPU_PADDLE_VERSION, "compiledWithCuda": True}
    if not isinstance(evidence, dict) or any(evidence.get(name) != value for name, value in required.items()) or not isinstance(evidence.get("cudaVersion"), str) or not evidence["cudaVersion"].strip() or not isinstance(evidence.get("gpuName"), str) or not evidence["gpuName"].strip():
        raise RuntimeError("offline GPU OCR device evidence is invalid")
    outcomes = responses[1].get("payload", {}).get("items")
    if not isinstance(outcomes, list) or len(outcomes) != 3 or any(not isinstance(item, dict) or item.get("status") != "success" or not item.get("items") for item in outcomes):
        raise RuntimeError("offline GPU OCR probe did not produce three non-empty outcomes")
    return {"device": {name: evidence[name] for name in (*required, "cudaVersion", "gpuName")}, "samples": samples}


def _preview(action: str) -> dict[str, object]:
    targets = GPU_CACHE_TARGETS + GPU_FORMAL_TARGETS if action == "install" else GPU_CACHE_TARGETS
    return {
        "action": action,
        "applyGates": list(APPLY_GATES),
        "mode": "preview",
        "targets": list(targets),
        "wheelUrl": OFFICIAL_WHEEL_URL,
        "writes": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--action", choices=("install", "reset", "clean"), required=True)
    parser.add_argument("--apply", "-Apply", action="store_true")
    arguments = parser.parse_args()
    _require_safe_project_root(arguments.project_root)
    if not arguments.apply:
        print(json.dumps(_preview(arguments.action), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    if arguments.action != "install":
        parser.error("Reset and Clean Apply are not available for GPU artifacts")
    report: dict[str, object] = {}
    def prepare(paths: GpuInstallPaths) -> None:
        report["inventory"] = _prepare_real_install(paths)
    def probe(paths: GpuInstallPaths) -> None:
        report["probe"] = _probe_real_gpu_runtime(paths)
    result = install_transaction(arguments.project_root, prepare=prepare, probe=probe)
    print(json.dumps({**result, **report}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
