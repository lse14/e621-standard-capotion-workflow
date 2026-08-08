"""Run the isolated OCR dependency and model compatibility experiment."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CANDIDATES = (
    "paddleocr==3.7.0",
    "paddlex[ocr-core]==3.7.2",
    "onnxruntime-gpu==1.26.0",
)
CONVERTER_REQUIREMENT = "paddle2onnx==2.1.0"
PADDLE_CANDIDATE = "paddlepaddle==3.2.2"
CONVERTER_WHEEL_SHA256 = "478993e17ed0212b79a4d6e2d8d0582ebb19c7230b7f365d51222833e98581b3"
CONVERTER_SOURCE_COMMIT = "c8b5048c3a0903986bd3ec1cce2af9915b391c49"
PADDLE_BUILD_REQUIREMENT = "paddlepaddle==3.0.0.dev20250426"
CONVERTER_RUNTIME_REQUIREMENTS = ("packaging==26.2",)
PADDLE_BUILD_WHEEL = {
    "filename": "paddlepaddle-3.0.0.dev20250426-cp311-cp311-win_amd64.whl",
    "url": (
        "https://paddle-whl.bj.bcebos.com/nightly/cpu/paddlepaddle/"
        "paddlepaddle-3.0.0.dev20250426-cp311-cp311-win_amd64.whl"
    ),
    "size": 98376372,
    "sha256": "f62aaab2bd8d3ad4f4f7781bdeed43403546057b7afcdc10a4b33847b2617f1f",
}
PIP_NO_PROXY_HOSTS = ("pypi.org", "files.pythonhosted.org")
LOCAL_VERIFIED_WHEELS = (
    (
        Path("packaging/wheelhouse/caption-e621/onnxruntime_gpu-1.26.0-cp311-cp311-win_amd64.whl"),
        "cc5329aad02d9745cc3ae9cdb185bfa1aad242a7bf89b8c471280002ec40f98a",
    ),
)
PADDLE_WHEEL = {
    "filename": "paddlepaddle-3.2.2-cp311-cp311-win_amd64.whl",
    "url": (
        "https://paddle-whl.bj.bcebos.com/stable/cpu/paddlepaddle/"
        "paddlepaddle-3.2.2-cp311-cp311-win_amd64.whl"
    ),
    "size": 101715828,
    "sha256": "7ee7a0783de00a50f89a959aabfa83dd969ccce57e2c53f92f4d592d1df1aceb",
}
PADDLE_33_WHEEL = {
    "filename": "paddlepaddle-3.3.0-cp311-cp311-win_amd64.whl",
    "url": "https://files.pythonhosted.org/packages/53/62/79dd3233fd28bc7950bcabf57ee32eeb88d6c758a35111dd25af6a6fe216/paddlepaddle-3.3.0-cp311-cp311-win_amd64.whl",
    "size": 104266905,
    "sha256": "ad1b946dd4ea035c268dd6c5468bac9cad15baf81f2f6bd49e803858c041f4da",
}
MODEL_ARTIFACTS = (
    {
        "name": "PP-OCRv5_server_det",
        "url": (
            "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
            "paddle3.0.0/PP-OCRv5_server_det_infer.tar"
        ),
        "size": 88340480,
        "sha256": "22a33e0ba6a21425ea4192da03bf4395c9a0c67902bd924b7328fc859073045d",
    },
    {
        "name": "PP-OCRv5_server_rec",
        "url": (
            "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
            "paddle3.0.0/PP-OCRv5_server_rec_infer.tar"
        ),
        "size": 84869120,
        "sha256": "d99be2ffd348943ab52876179168be4fb5b14f5f0812f2ae4c76d89ec2ea750a",
    },
    {
        "name": "PP-LCNet_x1_0_textline_ori",
        "url": (
            "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
            "paddle3.0.0/PP-LCNet_x1_0_textline_ori_infer.tar"
        ),
        "size": 6871040,
        "sha256": "6171f69605215a85624d650e9079fa45f7c3eaf944296181bcc5395bf3ddc7f6",
    },
)
SAMPLE_ARTIFACTS = (
    {
        "purpose": "text_detection",
        "filename": "text_detection.png",
        "url": "https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/general_ocr_001.png",
        "size": 398527,
        "sha256": "3ac37804e4e292f68c8960d553485147516cdc2e4154afeec6ca742a70e71dca",
    },
    {
        "purpose": "text_recognition",
        "filename": "text_recognition.png",
        "url": "https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/general_ocr_rec_001.png",
        "size": 73730,
        "sha256": "5362ba97741413494c507237b5096ef09ed575a501c4d9e68bfeffe17528a6ad",
    },
    {
        "purpose": "textline_orientation",
        "filename": "textline_orientation.jpg",
        "url": "https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/textline_rot180_demo.jpg",
        "size": 3996,
        "sha256": "872200f57a1408e7aab2856d5f2c687b3a937805e0c4ff74bd7de21df1f742b9",
    },
)
FIXED_CANDIDATE_WHEELS = {
    "paddleocr-3.7.0-py3-none-any.whl": {
        "size": 146750,
        "sha256": "c0f0a81ad4112727f30c6fcf986ac0ef6a120d31ee0991a01fae0357ee32d338",
    },
    "paddlex-3.7.2-py3-none-any.whl": {
        "size": 2239708,
        "sha256": "f1678bf650bbaccfd8f0d4e49d0ae631b4685c829fdae6e802ccd90d4fcb9a7f",
    },
    "onnxruntime_gpu-1.26.0-cp311-cp311-win_amd64.whl": {
        "size": 226539455,
        "sha256": "cc5329aad02d9745cc3ae9cdb185bfa1aad242a7bf89b8c471280002ec40f98a",
    },
    "paddle2onnx-2.1.0-cp311-cp311-win_amd64.whl": {
        "size": 2027711,
        "sha256": CONVERTER_WHEEL_SHA256,
    },
    str(PADDLE_WHEEL["filename"]): {
        "size": int(PADDLE_WHEEL["size"]),
        "sha256": str(PADDLE_WHEEL["sha256"]),
    },
}
CONVERTER_PROBE_CODE = """\
import ctypes
import importlib.util
import json
from pathlib import Path
import sys

import paddle

spec = importlib.util.find_spec("paddle2onnx")
if spec is None or not spec.submodule_search_locations:
    print(json.dumps({"moduleLoaded": False, "error": "paddle2onnx package not found"}))
    raise SystemExit(21)
package_root = Path(next(iter(spec.submodule_search_locations)))
extensions = sorted(package_root.glob("paddle2onnx_cpp2py_export*.pyd"))
if len(extensions) != 1:
    print(json.dumps({"moduleLoaded": False, "error": "unexpected converter extension set"}))
    raise SystemExit(22)
try:
    ctypes.WinDLL(str(extensions[0]))
except OSError as error:
    print(json.dumps({
        "moduleLoaded": False,
        "extension": str(extensions[0]),
        "winerror": error.winerror,
        "error": str(error),
    }))
    raise SystemExit(23)
import paddle2onnx
print(json.dumps({
    "moduleLoaded": True,
    "paddle": paddle.__version__,
    "paddle2onnx": paddle2onnx.__version__,
}))
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def converter_probe_arguments() -> list[str]:
    return ["-c", CONVERTER_PROBE_CODE]


def last_json_object(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def inference_install_arguments(wheelhouse: Path) -> list[str]:
    return [
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        *CANDIDATES,
        PADDLE_CANDIDATE,
    ]


def converter_install_arguments(wheelhouse: Path) -> list[str]:
    return [
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        CONVERTER_REQUIREMENT,
        PADDLE_BUILD_REQUIREMENT,
        *CONVERTER_RUNTIME_REQUIREMENTS,
    ]


def candidate_install_arguments(wheelhouse: Path) -> list[str]:
    """Backward-compatible name for the isolated inference environment install."""
    return inference_install_arguments(wheelhouse)


def conversion_cache_relative_path(path: Path, conversion_cache: Path) -> str:
    resolved_cache = conversion_cache.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        relative = resolved_path.relative_to(resolved_cache)
    except ValueError as error:
        raise RuntimeError(f"converter path escapes conversion cache: {path}") from error
    if relative == Path(".") or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"converter path is not contained by conversion cache: {path}")
    value = relative.as_posix()
    if not value.isascii():
        raise RuntimeError(f"converter path must be ASCII relative to conversion cache: {path}")
    return value


def model_conversion_arguments(
    model_dir: Path,
    output_file: Path,
    *,
    conversion_cache: Path,
) -> list[str]:
    return [
        "-m",
        "paddle2onnx.command",
        "--model_dir",
        conversion_cache_relative_path(model_dir, conversion_cache),
        "--model_filename",
        "inference.json",
        "--params_filename",
        "inference.pdiparams",
        "--save_file",
        conversion_cache_relative_path(output_file, conversion_cache),
        "--opset_version",
        "7",
        "--enable_onnx_checker",
        "True",
    ]


def parity_probe_arguments(
    probe: Path,
    source_root: Path,
    onnx_root: Path,
    samples_root: Path,
) -> list[str]:
    return [
        str(probe),
        "parity",
        "--source-root",
        str(source_root),
        "--onnx-root",
        str(onnx_root),
        "--samples-root",
        str(samples_root),
    ]


def provider_probe_arguments(
    probe: Path,
    onnx_root: Path,
    provider: str,
    profile_root: Path,
) -> list[str]:
    if provider not in {"CPUExecutionProvider", "CUDAExecutionProvider"}:
        raise ValueError(f"unsupported ONNX Runtime provider: {provider}")
    return [
        str(probe),
        "provider",
        "--onnx-root",
        str(onnx_root),
        "--provider",
        provider,
        "--profile-root",
        str(profile_root),
    ]


def find_inference_model_root(root: Path) -> Path:
    matches = [
        candidate.parent
        for candidate in root.rglob("inference.json")
        if (candidate.parent / "inference.pdiparams").is_file()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one complete inference model below {root}, got {len(matches)}"
        )
    return matches[0]


def bbox_iou(left: list[float], right: list[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        raise ValueError("bbox must contain exactly four coordinates")
    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = intersection_width * intersection_height
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def validate_parity(
    paddle_result: dict[str, Any],
    onnx_result: dict[str, Any],
) -> dict[str, Any]:
    confidence_deltas: list[float] = []
    detection_ious: list[float] = []
    for section in ("detection", "recognition", "orientation"):
        if section not in paddle_result or section not in onnx_result:
            raise RuntimeError(f"parity result is missing {section}")

    paddle_detection = paddle_result["detection"]
    onnx_detection = onnx_result["detection"]
    if len(paddle_detection["boxes"]) != len(onnx_detection["boxes"]):
        raise RuntimeError("detection box count differs")
    if len(paddle_detection["scores"]) != len(onnx_detection["scores"]):
        raise RuntimeError("detection score count differs")
    for left, right in zip(paddle_detection["boxes"], onnx_detection["boxes"]):
        detection_ious.append(bbox_iou(left, right))
    for left, right in zip(paddle_detection["scores"], onnx_detection["scores"]):
        confidence_deltas.append(abs(float(left) - float(right)))

    paddle_recognition = paddle_result["recognition"]
    onnx_recognition = onnx_result["recognition"]
    if paddle_recognition["texts"] != onnx_recognition["texts"]:
        raise RuntimeError("recognition text differs")
    if len(paddle_recognition["scores"]) != len(onnx_recognition["scores"]):
        raise RuntimeError("recognition score count differs")
    for left, right in zip(paddle_recognition["scores"], onnx_recognition["scores"]):
        confidence_deltas.append(abs(float(left) - float(right)))

    paddle_orientation = paddle_result["orientation"]
    onnx_orientation = onnx_result["orientation"]
    if paddle_orientation["labels"] != onnx_orientation["labels"]:
        raise RuntimeError("orientation label differs")
    if len(paddle_orientation["scores"]) != len(onnx_orientation["scores"]):
        raise RuntimeError("orientation score count differs")
    for left, right in zip(paddle_orientation["scores"], onnx_orientation["scores"]):
        confidence_deltas.append(abs(float(left) - float(right)))

    minimum_iou = min(detection_ious) if detection_ious else 1.0
    maximum_delta = max(confidence_deltas) if confidence_deltas else 0.0
    if minimum_iou < 0.95:
        raise RuntimeError(f"minimum detection bbox IoU {minimum_iou:.6f} is below 0.95")
    if maximum_delta > 0.02:
        raise RuntimeError(f"maximum confidence delta {maximum_delta:.6f} exceeds 0.02")
    return {
        "minimumBboxIou": minimum_iou,
        "maximumConfidenceDelta": maximum_delta,
        "recognitionTextsMatch": True,
        "orientationLabelsMatch": True,
    }


def validate_provider_profile(
    records: list[dict[str, Any]],
    expected_provider: str,
) -> dict[str, Any]:
    providers = [
        str(record.get("args", {}).get("provider"))
        for record in records
        if record.get("cat") == "Node" and record.get("args", {}).get("provider")
    ]
    matching = sum(provider == expected_provider for provider in providers)
    if not matching:
        raise RuntimeError(f"no node executed with expected provider {expected_provider}")
    return {
        "provider": expected_provider,
        "nodeEvents": matching,
        "observedProviders": sorted(set(providers)),
    }


def extract_verified_tar(archive_path: Path, destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve(strict=True)
    extracted: list[str] = []
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            target = (destination_root / relative).resolve(strict=False)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not target.is_relative_to(destination_root)
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise RuntimeError(f"unsafe tar member: {member.name}")
            archive.extract(member, destination_root)
            extracted.append(member.name)
    return extracted


def collect_wheel_inventory(wheelhouse: Path) -> list[dict[str, Any]]:
    return [
        {
            "filename": wheel.name,
            "size": wheel.stat().st_size,
            "sha256": sha256_file(wheel),
        }
        for wheel in sorted(wheelhouse.glob("*.whl"), key=lambda path: path.name.casefold())
        if wheel.is_file()
    ]


def verify_fixed_candidate_wheels(wheelhouse: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for filename, contract in FIXED_CANDIDATE_WHEELS.items():
        wheel = wheelhouse / filename
        if not wheel.is_file():
            raise RuntimeError(f"fixed candidate wheel is missing: {wheel}")
        actual_size = wheel.stat().st_size
        actual_hash = sha256_file(wheel)
        if actual_size != contract["size"] or actual_hash != contract["sha256"]:
            raise RuntimeError(
                f"fixed candidate wheel mismatch for {filename}: "
                f"expected size={contract['size']} sha256={contract['sha256']}, "
                f"got size={actual_size} sha256={actual_hash}"
            )
        records.append(
            {
                "filename": filename,
                "size": actual_size,
                "sha256": actual_hash,
            }
        )
    return records


def record_converter_blocker(
    evidence: dict[str, Any],
    error: str,
    diagnostic: dict[str, Any] | None = None,
) -> None:
    evidence["gates"].update(
        {
            "converterImport": "blocked",
            "modelDownloads": "skipped",
            "modelConversion": "skipped",
            "paddleOnnxParity": "skipped",
            "cpuProviderInference": "skipped",
            "cudaProviderInference": "skipped",
        }
    )
    evidence["compatibilityBlocker"] = {
        "component": CONVERTER_REQUIREMENT,
        "error": error,
        "policy": "Stop before model download or conversion; do not switch converter or models.",
    }
    if diagnostic is not None:
        evidence["compatibilityBlocker"]["diagnostic"] = diagnostic


def converter_download_arguments(wheelhouse: Path) -> list[str]:
    return [
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--only-binary=:all:",
        "--dest",
        str(wheelhouse),
        CONVERTER_REQUIREMENT,
    ]


def converter_build_dependency_download_arguments(wheelhouse: Path) -> list[str]:
    return [
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--find-links",
        str(wheelhouse),
        "--dest",
        str(wheelhouse),
        PADDLE_BUILD_REQUIREMENT,
    ]


def dependency_download_requirements() -> list[str]:
    return [
        *CANDIDATES,
        CONVERTER_REQUIREMENT,
        PADDLE_CANDIDATE,
        *CONVERTER_RUNTIME_REQUIREMENTS,
    ]


def dependency_download_marker(wheelhouse: Path) -> Path:
    return wheelhouse / ".dependency-download-v1.json"


def dependency_download_contract(wheelhouse: Path) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "requirements": [
            *dependency_download_requirements(),
            PADDLE_BUILD_REQUIREMENT,
        ],
        "wheelInventory": collect_wheel_inventory(wheelhouse),
    }


def dependency_download_required(wheelhouse: Path) -> bool:
    marker = dependency_download_marker(wheelhouse)
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return recorded != dependency_download_contract(wheelhouse)


def mark_dependency_download_complete(wheelhouse: Path) -> None:
    contract = dependency_download_contract(wheelhouse)
    if not contract["wheelInventory"]:
        raise RuntimeError("dependency download produced an empty wheelhouse")
    write_evidence(dependency_download_marker(wheelhouse), contract)


def paddle_probe_plan(probe_root: Path, resolved_wheels: Path) -> dict[str, Any]:
    probe_wheels = probe_root / "wheels"
    return {
        "environment": probe_root / "environment",
        "wheels": probe_wheels,
        "installArguments": [
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(probe_wheels),
            "--find-links",
            str(resolved_wheels),
            "paddlepaddle==3.3.0",
            CONVERTER_REQUIREMENT,
        ],
        "priorIncompatibility": {
            "version": "3.2.2",
            "compatible": False,
            "failure": "WinError 127: missing libpaddle procedures",
        },
    }


def seed_verified_local_wheels(
    project_root: Path,
    wheelhouse: Path,
    sources: tuple[tuple[Path, str], ...] = LOCAL_VERIFIED_WHEELS,
) -> list[dict[str, Any]]:
    project_root = project_root.resolve(strict=True)
    wheelhouse = wheelhouse.resolve(strict=True)
    records: list[dict[str, Any]] = []
    for relative_source, expected_hash in sources:
        source = (project_root / relative_source).resolve(strict=True)
        if not source.is_relative_to(project_root) or not source.is_file():
            raise RuntimeError(f"local wheel source escapes project or is not a file: {source}")
        actual_hash = sha256_file(source)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"local wheel SHA-256 mismatch for {source}: expected {expected_hash}, got {actual_hash}"
            )
        staged = wheelhouse / source.name
        if staged.exists():
            if not staged.is_file() or sha256_file(staged) != expected_hash:
                raise RuntimeError(f"staged wheel conflicts with verified local wheel: {staged}")
        else:
            temporary = staged.with_suffix(staged.suffix + ".tmp")
            shutil.copy2(source, temporary)
            if sha256_file(temporary) != expected_hash:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"staged local wheel failed post-copy hash verification: {source}")
            os.replace(temporary, staged)
        records.append(
            {
                "source": str(source),
                "staged": str(staged),
                "size": staged.stat().st_size,
                "sha256": expected_hash,
            }
        )
    return records


def direct_https_target(url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"verified network artifact must use HTTPS: {url}")
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return parsed.hostname, parsed.port or 443, path


def open_direct_https(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse, str]:
    current_url = url
    for _redirect in range(6):
        host, port, path = direct_https_target(current_url)
        connection = http.client.HTTPSConnection(host, port=port, timeout=timeout)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location")
            response.close()
            connection.close()
            if not location:
                raise RuntimeError(f"verified artifact redirect has no Location: {current_url}")
            current_url = urllib.parse.urljoin(current_url, location)
            continue
        if response.status not in {200, 206}:
            status = response.status
            reason = response.reason
            response.close()
            connection.close()
            raise RuntimeError(
                f"verified artifact HTTP request failed: {status} {reason} for {current_url}"
            )
        return connection, response, current_url
    raise RuntimeError(f"verified artifact redirect limit exceeded: {url}")


def download_request_headers(offset: int) -> dict[str, str]:
    return {
        "User-Agent": "Anima-OCR-Compatibility/1",
        "Range": f"bytes={offset}-",
    }


def download_verified_artifact(
    url: str,
    destination: Path,
    expected_size: int,
    expected_hash: str,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size != expected_size or sha256_file(destination) != expected_hash:
            raise RuntimeError(f"existing artifact does not match expected size/hash: {destination}")
        return {
            "url": url,
            "filename": destination.name,
            "size": expected_size,
            "sha256": expected_hash,
            "reused": True,
        }

    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.scheme == "file":
        source = Path(urllib.request.url2pathname(parsed_url.path))
        copy_verified_artifact(source, destination, expected_size, expected_hash)
        return {
            "url": url,
            "filename": destination.name,
            "size": expected_size,
            "sha256": expected_hash,
            "reused": False,
        }
    if parsed_url.scheme != "https":
        raise RuntimeError(f"unsupported verified artifact URL scheme: {url}")

    partial = destination.with_suffix(destination.suffix + ".part")
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    attempts = 0
    while (partial.stat().st_size if partial.exists() else 0) < expected_size:
        attempts += 1
        if attempts > 6:
            raise RuntimeError(f"download retry limit exceeded: {url}")
        offset = partial.stat().st_size if partial.exists() else 0
        headers = download_request_headers(offset)
        connection: http.client.HTTPSConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            connection, response, _final_url = open_direct_https(url, headers, timeout=120)
            append = bool(offset and response.status == 206)
            mode = "ab" if append else "wb"
            if not append:
                offset = 0
            next_report = offset + 8 * 1024 * 1024
            with partial.open(mode) as stream:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    offset += len(block)
                    if offset >= next_report:
                        print(
                            f"download progress: {destination.name} {offset}/{expected_size} bytes",
                            flush=True,
                        )
                        next_report = offset + 8 * 1024 * 1024
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            print(
                f"download attempt {attempts} interrupted at {offset}/{expected_size} bytes: {error}",
                flush=True,
            )
            if attempts >= 6:
                raise
            time.sleep(min(attempts, 3))
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    actual_size = partial.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(
            f"artifact size mismatch for {destination.name}: expected {expected_size}, got {actual_size}"
        )
    actual_hash = sha256_file(partial)
    if actual_hash != expected_hash:
        partial.unlink()
        raise RuntimeError(
            f"artifact SHA-256 mismatch for {destination.name}: expected {expected_hash}, got {actual_hash}"
        )
    os.replace(partial, destination)
    return {
        "url": url,
        "filename": destination.name,
        "size": actual_size,
        "sha256": actual_hash,
        "reused": False,
    }


def copy_verified_artifact(
    source: Path,
    destination: Path,
    expected_size: int,
    expected_hash: str,
) -> dict[str, Any]:
    if not source.is_file():
        raise RuntimeError(f"verified artifact source is missing: {source}")
    source_size = source.stat().st_size
    source_hash = sha256_file(source)
    if source_size != expected_size or source_hash != expected_hash:
        raise RuntimeError(
            f"verified artifact source mismatch: expected size={expected_size} "
            f"sha256={expected_hash}, got size={source_size} sha256={source_hash}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size != expected_size or sha256_file(destination) != expected_hash:
            raise RuntimeError(f"verified artifact destination conflicts: {destination}")
    else:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != expected_size or sha256_file(temporary) != expected_hash:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"verified artifact failed post-copy validation: {source}")
        os.replace(temporary, destination)
    return {
        "source": str(source),
        "destination": str(destination),
        "size": expected_size,
        "sha256": expected_hash,
    }


def extend_no_proxy(existing: str, hosts: tuple[str, ...]) -> str:
    values = [value.strip() for value in existing.split(",") if value.strip()]
    seen = {value.casefold() for value in values}
    for host in hosts:
        if host.casefold() not in seen:
            values.append(host)
            seen.add(host.casefold())
    return ",".join(values)


def run_python(
    executable: Path,
    arguments: list[str],
    *,
    environment: dict[str, str],
    commands: list[dict[str, Any]],
    heartbeat_seconds: float = 30.0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(executable), "-B", "-I", *arguments]
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    output_chunks: list[str] = []

    def drain_output() -> None:
        while True:
            block = process.stdout.read(8192)
            if not block:
                return
            output_chunks.append(block)

    reader = threading.Thread(target=drain_output, daemon=True)
    reader.start()
    try:
        while True:
            try:
                return_code = process.wait(timeout=heartbeat_seconds)
                break
            except subprocess.TimeoutExpired:
                print(
                    f"python subprocess still running (pid {process.pid}): {' '.join(command[:7])}",
                    flush=True,
                )
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=10)
        process.stdout.close()
    completed = subprocess.CompletedProcess(command, return_code, "".join(output_chunks), None)
    commands.append(
        {
            "command": command,
            "exitCode": completed.returncode,
            "output": completed.stdout[-20000:],
        }
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {' '.join(command)}\n"
            f"{completed.stdout[-4000:]}"
        )
    return completed


def new_command_ledger(_prior_evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    return []


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--toolchain-python", type=Path, required=True)
    parser.add_argument("--working-root", type=Path, required=True)
    parser.add_argument("--probe-paddle-3-3", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def run_paddle_33_probe(
    project_root: Path,
    toolchain_python: Path,
    working_root: Path,
    *,
    resume: bool = False,
) -> int:
    conversion_cache = working_root / "conversion-cache"
    evidence_path = working_root / "evidence" / "paddle-3.3.0-probe.json"
    resolved_wheels = working_root / "downloads" / "wheels"
    probe_root = conversion_cache / "paddle-3.3.0-probe"
    plan = paddle_probe_plan(probe_root, resolved_wheels)
    probe_environment = plan["environment"]
    probe_wheels = plan["wheels"]
    if probe_root.exists() and not resume:
        raise RuntimeError(f"probe root already exists; clean OCR import staging first: {probe_root}")
    if not resolved_wheels.is_dir():
        raise RuntimeError(f"resolved wheelhouse is missing: {resolved_wheels}")
    probe_wheels.mkdir(parents=True, exist_ok=resume)

    process_environment = os.environ.copy()
    no_proxy = extend_no_proxy(
        ",".join(
            value
            for value in (
                process_environment.get("NO_PROXY", ""),
                process_environment.get("no_proxy", ""),
            )
            if value
        ),
        PIP_NO_PROXY_HOSTS,
    )
    process_environment.update(
        {
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
            "PIP_CACHE_DIR": str(probe_root / "pip-cache"),
            "TEMP": str(probe_root / "temp"),
            "TMP": str(probe_root / "temp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    (probe_root / "temp").mkdir(exist_ok=resume)
    commands: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "running",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "probeVersion": "3.3.0",
        "fixedConverterVersion": "2.1.0",
        "priorIncompatibility": plan["priorIncompatibility"],
        "probeRoot": str(probe_root),
        "commands": commands,
    }
    write_evidence(evidence_path, evidence)
    try:
        resolved_paddle_wheel = resolved_wheels / str(PADDLE_33_WHEEL["filename"])
        if resolved_paddle_wheel.is_file():
            evidence["wheel"] = copy_verified_artifact(
                resolved_paddle_wheel,
                probe_wheels / str(PADDLE_33_WHEEL["filename"]),
                int(PADDLE_33_WHEEL["size"]),
                str(PADDLE_33_WHEEL["sha256"]),
            )
            evidence["wheel"]["officialUrl"] = str(PADDLE_33_WHEEL["url"])
        else:
            evidence["wheel"] = download_verified_artifact(
                str(PADDLE_33_WHEEL["url"]),
                probe_wheels / str(PADDLE_33_WHEEL["filename"]),
                int(PADDLE_33_WHEEL["size"]),
                str(PADDLE_33_WHEEL["sha256"]),
            )
        if not probe_environment.exists():
            run_python(
                toolchain_python,
                ["-m", "venv", str(probe_environment)],
                environment=process_environment,
                commands=commands,
            )
        probe_python = probe_environment / "Scripts" / "python.exe"
        run_python(
            probe_python,
            list(plan["installArguments"]),
            environment=process_environment,
            commands=commands,
        )
        site_packages = probe_environment / "Lib" / "site-packages"
        process_environment["PATH"] = os.pathsep.join(
            (
                str(site_packages / "paddle" / "base"),
                str(site_packages / "paddle" / "libs"),
                process_environment.get("PATH", ""),
            )
        )
        completed = run_python(
            probe_python,
            converter_probe_arguments(),
            environment=process_environment,
            commands=commands,
        )
        evidence["importResult"] = last_json_object(completed.stdout)
        evidence["status"] = "passed"
        evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
        write_evidence(evidence_path, evidence)
        print(json.dumps(evidence["importResult"], indent=2))
        return 0
    except Exception as error:
        if commands:
            diagnostic = last_json_object(str(commands[-1].get("output", "")))
            if diagnostic is not None:
                evidence["importResult"] = diagnostic
        evidence["status"] = "blocked"
        evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
        evidence["error"] = str(error)
        evidence["traceback"] = traceback.format_exc()
        write_evidence(evidence_path, evidence)
        print(f"PaddlePaddle 3.3.0 probe BLOCKED: {error}", file=sys.stderr)
        return 1


def environment_with_paddle_libraries(
    environment: dict[str, str],
    environment_root: Path,
) -> dict[str, str]:
    updated = environment.copy()
    site_packages = environment_root / "Lib" / "site-packages"
    updated["PATH"] = os.pathsep.join(
        (
            str(site_packages / "paddle" / "base"),
            str(site_packages / "paddle" / "libs"),
            updated.get("PATH", ""),
        )
    )
    return updated


def file_inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file()
    ]


def materialize_model_archive(
    archive_path: Path,
    source_root: Path,
    model_name: str,
) -> Path:
    target = source_root / model_name
    if target.exists():
        if not target.is_dir():
            raise RuntimeError(f"model target is not a directory: {target}")
        if find_inference_model_root(target) != target:
            raise RuntimeError(f"model target has an unexpected nested layout: {target}")
        return target

    source_root.mkdir(parents=True, exist_ok=True)
    staging = source_root / f".{model_name}.extracting"
    if staging.exists():
        raise RuntimeError(f"incomplete model extraction exists; clean or resume explicitly: {staging}")
    staging.mkdir()
    extract_verified_tar(archive_path, staging)
    discovered = find_inference_model_root(staging)
    shutil.move(str(discovered), str(target))
    if staging.exists():
        shutil.rmtree(staging)
    return target


def prepare_onnx_model_directory(source: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    excluded = {"inference.json", "inference.pdiparams", "inference.onnx"}
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.name in excluded:
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != sha256_file(path):
                raise RuntimeError(f"ONNX model support file conflicts: {destination}")
        else:
            shutil.copy2(path, destination)
    return target / "inference.onnx"


def mark_unfinished_gates(evidence: dict[str, Any]) -> None:
    for gate in (
        "fixedWheelHashes",
        "dependencyResolution",
        "baseImports",
        "converterImport",
        "modelDownloads",
        "modelConversion",
        "paddleOnnxParity",
        "cpuProviderInference",
        "cudaProviderInference",
    ):
        evidence["gates"].setdefault(gate, "skipped")


def parse_command_json(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    value = last_json_object(completed.stdout)
    if value is None:
        raise RuntimeError(f"{label} did not emit a JSON object")
    return value


def legacy_main() -> int:
    arguments = parse_arguments()
    project_root = arguments.project_root.resolve(strict=True)
    toolchain_python = arguments.toolchain_python.resolve(strict=True)
    working_root = arguments.working_root.resolve(strict=False)
    expected_root = project_root / ".runtime-build" / "ocr-import"
    if working_root != expected_root:
        raise RuntimeError(f"working root must be exactly {expected_root}, got {working_root}")
    if toolchain_python != (
        project_root / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64" / "python.exe"
    ):
        raise RuntimeError("unexpected project Python executable")

    if arguments.probe_paddle_3_3:
        return run_paddle_33_probe(
            project_root,
            toolchain_python,
            working_root,
            resume=arguments.resume,
        )

    environment_root = working_root / "environment"
    downloads_root = working_root / "downloads"
    conversion_cache = working_root / "conversion-cache"
    evidence_root = working_root / "evidence"
    wheelhouse = downloads_root / "wheels"
    evidence_path = evidence_root / "latest.json"
    for directory in (downloads_root, conversion_cache, evidence_root, wheelhouse):
        directory.mkdir(parents=True, exist_ok=True)

    process_environment = os.environ.copy()
    local_cache = conversion_cache / "package-caches"
    local_temp = conversion_cache / "temp"
    for directory in (local_cache, local_temp):
        directory.mkdir(parents=True, exist_ok=True)
    package_hosts = PIP_NO_PROXY_HOSTS
    existing_no_proxy = ",".join(
        value
        for value in (
            process_environment.get("NO_PROXY", ""),
            process_environment.get("no_proxy", ""),
        )
        if value
    )
    no_proxy = extend_no_proxy(existing_no_proxy, package_hosts)
    process_environment.update(
        {
            "PIP_CACHE_DIR": str(local_cache / "pip"),
            "XDG_CACHE_HOME": str(local_cache / "xdg"),
            "HF_HOME": str(local_cache / "huggingface"),
            "PADDLE_HOME": str(local_cache / "paddle"),
            "PADDLEX_HOME": str(local_cache / "paddlex"),
            "PADDLEOCR_HOME": str(local_cache / "paddleocr"),
            "TEMP": str(local_temp),
            "TMP": str(local_temp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }
    )
    commands: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "running",
        "mode": "resume" if arguments.resume else "fresh",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "projectPython": str(toolchain_python),
        "workingRoot": str(working_root),
        "candidateRequirements": [*CANDIDATES, CONVERTER_REQUIREMENT, PADDLE_CANDIDATE],
        "commands": commands,
        "gates": {},
        "directNetworkHosts": list(package_hosts),
    }
    write_evidence(evidence_path, evidence)

    try:
        if arguments.resume:
            if not environment_root.is_dir() or not wheelhouse.is_dir():
                raise RuntimeError(
                    f"resume requires an existing environment and wheelhouse under {working_root}"
                )
        else:
            if environment_root.exists():
                raise RuntimeError(
                    f"temporary environment already exists; use -Resume or run Clean-OcrImport.ps1 -Apply first: {environment_root}"
                )
            run_python(
                toolchain_python,
                ["-m", "venv", str(environment_root)],
                environment=process_environment,
                commands=commands,
            )
        environment_python = environment_root / "Scripts" / "python.exe"
        if not environment_python.is_file():
            raise RuntimeError(f"venv Python was not created: {environment_python}")
        evidence["environmentPython"] = str(environment_python)
        if not arguments.resume:
            run_python(
                environment_python,
                converter_download_arguments(wheelhouse),
                environment=process_environment,
                commands=commands,
            )
            evidence["seededLocalWheels"] = seed_verified_local_wheels(
                project_root,
                wheelhouse,
            )
            evidence["paddleCandidateWheel"] = download_verified_artifact(
                str(PADDLE_WHEEL["url"]),
                wheelhouse / str(PADDLE_WHEEL["filename"]),
                int(PADDLE_WHEEL["size"]),
                str(PADDLE_WHEEL["sha256"]),
            )
            write_evidence(evidence_path, evidence)
            run_python(
                environment_python,
                [
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheelhouse),
                    *CANDIDATES,
                    CONVERTER_REQUIREMENT,
                    PADDLE_CANDIDATE,
                ],
                environment=process_environment,
                commands=commands,
            )

        evidence["fixedCandidateWheels"] = verify_fixed_candidate_wheels(wheelhouse)
        evidence["wheelInventory"] = collect_wheel_inventory(wheelhouse)
        evidence["gates"]["fixedWheelHashes"] = "passed"
        run_python(
            environment_python,
            candidate_install_arguments(wheelhouse),
            environment=process_environment,
            commands=commands,
        )
        run_python(
            environment_python,
            ["-m", "pip", "check"],
            environment=process_environment,
            commands=commands,
        )
        evidence["gates"]["dependencyResolution"] = "passed"
        installed = run_python(
            environment_python,
            [
                "-c",
                (
                    "import importlib.metadata as m,json; "
                    "items=sorted((d.metadata.get('Name',d.name),d.version) for d in m.distributions()); "
                    "print(json.dumps(dict(items),sort_keys=True))"
                ),
            ],
            environment=process_environment,
            commands=commands,
        )
        evidence["installedPackages"] = json.loads(installed.stdout.strip().splitlines()[-1])
        base_import = run_python(
            environment_python,
            [
                "-c",
                (
                    "import json,paddle,paddleocr,paddlex,onnxruntime as ort; "
                    "print(json.dumps({'paddle':paddle.__version__,"
                    "'paddleocr':paddleocr.__version__,'paddlex':paddlex.__version__,"
                    "'onnxruntime':ort.__version__,'providers':ort.get_available_providers()}))"
                ),
            ],
            environment=process_environment,
            commands=commands,
        )
        evidence["baseImport"] = json.loads(base_import.stdout.strip().splitlines()[-1])
        evidence["gates"]["baseImports"] = "passed"
        write_evidence(evidence_path, evidence)
        try:
            converter_import = run_python(
                environment_python,
                converter_probe_arguments(),
                environment=process_environment,
                commands=commands,
            )
        except RuntimeError as error:
            diagnostic = last_json_object(str(commands[-1].get("output", "")))
            record_converter_blocker(evidence, str(error), diagnostic)
            raise
        evidence["converterImport"] = last_json_object(converter_import.stdout)
        evidence["gates"]["converterImport"] = "passed"
        raise RuntimeError(
            "converter import passed unexpectedly; model conversion/parity gates remain unimplemented"
        )
    except Exception as error:
        evidence["status"] = "blocked"
        evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
        evidence["error"] = str(error)
        evidence["traceback"] = traceback.format_exc()
        write_evidence(evidence_path, evidence)
        print(f"OCR compatibility experiment BLOCKED: {error}", file=sys.stderr)
        return 1


def main() -> int:
    arguments = parse_arguments()
    project_root = arguments.project_root.resolve(strict=True)
    toolchain_python = arguments.toolchain_python.resolve(strict=True)
    working_root = arguments.working_root.resolve(strict=False)
    expected_root = project_root / ".runtime-build" / "ocr-import"
    expected_python = (
        project_root / ".toolchains" / "Python-3.11.15" / "PCbuild" / "amd64" / "python.exe"
    )
    if working_root != expected_root:
        raise RuntimeError(f"working root must be exactly {expected_root}, got {working_root}")
    if toolchain_python != expected_python:
        raise RuntimeError("unexpected project Python executable")
    if arguments.probe_paddle_3_3:
        return run_paddle_33_probe(
            project_root,
            toolchain_python,
            working_root,
            resume=arguments.resume,
        )

    environment_root = working_root / "environment"
    downloads_root = working_root / "downloads"
    conversion_cache = working_root / "conversion-cache"
    evidence_root = working_root / "evidence"
    wheelhouse = downloads_root / "wheels"
    model_downloads = downloads_root / "models"
    sample_downloads = downloads_root / "samples"
    converter_environment = conversion_cache / "converter-environment"
    source_models = conversion_cache / "source-models"
    onnx_models = conversion_cache / "onnx-models"
    samples_root = conversion_cache / "samples"
    profiles_root = conversion_cache / "profiles"
    evidence_path = evidence_root / "compatibility-v2.json"
    runtime_probe = (project_root / "packaging" / "scripts" / "ocr_runtime_probe.py").resolve(
        strict=True
    )
    for directory in (
        downloads_root,
        conversion_cache,
        evidence_root,
        wheelhouse,
        model_downloads,
        sample_downloads,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    process_environment = os.environ.copy()
    local_cache = conversion_cache / "package-caches"
    local_temp = conversion_cache / "temp"
    for directory in (local_cache, local_temp):
        directory.mkdir(parents=True, exist_ok=True)
    existing_no_proxy = ",".join(
        value
        for value in (
            process_environment.get("NO_PROXY", ""),
            process_environment.get("no_proxy", ""),
        )
        if value
    )
    no_proxy = extend_no_proxy(existing_no_proxy, PIP_NO_PROXY_HOSTS)
    process_environment.update(
        {
            "PIP_CACHE_DIR": str(local_cache / "pip"),
            "XDG_CACHE_HOME": str(local_cache / "xdg"),
            "HF_HOME": str(local_cache / "huggingface"),
            "PADDLE_HOME": str(local_cache / "paddle"),
            "PADDLEX_HOME": str(local_cache / "paddlex"),
            "PADDLEOCR_HOME": str(local_cache / "paddleocr"),
            "TEMP": str(local_temp),
            "TMP": str(local_temp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }
    )

    prior_evidence: dict[str, Any] | None = None
    if arguments.resume and evidence_path.is_file():
        prior_value = json.loads(evidence_path.read_text(encoding="utf-8"))
        if isinstance(prior_value, dict):
            prior_evidence = prior_value
    elif not arguments.resume and evidence_path.is_file():
        archive_name = (
            "compatibility-v2-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".json"
        )
        shutil.copy2(evidence_path, evidence_root / archive_name)

    commands = new_command_ledger(prior_evidence)
    evidence: dict[str, Any] = {
        "schemaVersion": 2,
        "status": "running",
        "mode": "resume" if arguments.resume else "fresh",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "projectPython": str(toolchain_python),
        "workingRoot": str(working_root),
        "inferenceEnvironment": str(environment_root),
        "converterEnvironment": str(converter_environment),
        "compatibilityPaths": [
            {
                "name": "official-build-nightly",
                "converter": CONVERTER_REQUIREMENT,
                "converterWheelSha256": CONVERTER_WHEEL_SHA256,
                "paddle": PADDLE_BUILD_REQUIREMENT,
                "paddleWheel": PADDLE_BUILD_WHEEL,
            },
            {
                "name": "local-v2.1.0-source-build",
                "repository": "https://github.com/PaddlePaddle/Paddle2ONNX.git",
                "tag": "v2.1.0",
                "commit": CONVERTER_SOURCE_COMMIT,
                "status": "fallback_not_needed_yet",
            },
        ],
        "candidateRequirements": [*CANDIDATES, PADDLE_CANDIDATE],
        "commands": commands,
        "gates": {},
        "directNetworkHosts": list(PIP_NO_PROXY_HOSTS),
        "formalResourceCreated": False,
    }
    write_evidence(evidence_path, evidence)
    active_gate = "environmentSetup"

    try:
        if arguments.resume:
            if not environment_root.is_dir() or not converter_environment.is_dir():
                raise RuntimeError(
                    "resume requires both existing inference and converter environments"
                )
        else:
            for environment_path in (environment_root, converter_environment):
                if environment_path.exists():
                    raise RuntimeError(
                        "temporary environment already exists; use -Resume or run "
                        f"Clean-OcrImport.ps1 -Apply first: {environment_path}"
                    )
            run_python(
                toolchain_python,
                ["-m", "venv", str(environment_root)],
                environment=process_environment,
                commands=commands,
            )
            run_python(
                toolchain_python,
                ["-m", "venv", str(converter_environment)],
                environment=process_environment,
                commands=commands,
            )
        inference_python = environment_root / "Scripts" / "python.exe"
        converter_python = converter_environment / "Scripts" / "python.exe"
        if not inference_python.is_file() or not converter_python.is_file():
            raise RuntimeError("one or more isolated Python environments were not created")

        active_gate = "fixedWheelHashes"
        if not arguments.resume:
            run_python(
                inference_python,
                converter_download_arguments(wheelhouse),
                environment=process_environment,
                commands=commands,
            )
            evidence["seededLocalWheels"] = seed_verified_local_wheels(
                project_root,
                wheelhouse,
            )
            evidence["paddleInferenceWheel"] = download_verified_artifact(
                str(PADDLE_WHEEL["url"]),
                wheelhouse / str(PADDLE_WHEEL["filename"]),
                int(PADDLE_WHEEL["size"]),
                str(PADDLE_WHEEL["sha256"]),
            )
            evidence["paddleConverterWheel"] = download_verified_artifact(
                str(PADDLE_BUILD_WHEEL["url"]),
                wheelhouse / str(PADDLE_BUILD_WHEEL["filename"]),
                int(PADDLE_BUILD_WHEEL["size"]),
                str(PADDLE_BUILD_WHEEL["sha256"]),
            )
            write_evidence(evidence_path, evidence)

        if dependency_download_required(wheelhouse):
            run_python(
                inference_python,
                [
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheelhouse),
                    *CANDIDATES,
                    CONVERTER_REQUIREMENT,
                    PADDLE_CANDIDATE,
                ],
                environment=process_environment,
                commands=commands,
            )
            run_python(
                inference_python,
                converter_build_dependency_download_arguments(wheelhouse),
                environment=process_environment,
                commands=commands,
            )
            mark_dependency_download_complete(wheelhouse)

        fixed_wheels = verify_fixed_candidate_wheels(wheelhouse)
        build_wheel_path = wheelhouse / str(PADDLE_BUILD_WHEEL["filename"])
        if (
            not build_wheel_path.is_file()
            or build_wheel_path.stat().st_size != int(PADDLE_BUILD_WHEEL["size"])
            or sha256_file(build_wheel_path) != str(PADDLE_BUILD_WHEEL["sha256"])
        ):
            raise RuntimeError(f"fixed converter Paddle wheel mismatch: {build_wheel_path}")
        fixed_wheels.append(
            {
                "filename": build_wheel_path.name,
                "size": build_wheel_path.stat().st_size,
                "sha256": sha256_file(build_wheel_path),
            }
        )
        evidence["fixedCandidateWheels"] = fixed_wheels
        evidence["wheelInventory"] = collect_wheel_inventory(wheelhouse)
        evidence["gates"][active_gate] = "passed"
        write_evidence(evidence_path, evidence)

        active_gate = "dependencyResolution"
        run_python(
            inference_python,
            inference_install_arguments(wheelhouse),
            environment=process_environment,
            commands=commands,
        )
        run_python(
            inference_python,
            ["-m", "pip", "check"],
            environment=process_environment,
            commands=commands,
        )
        run_python(
            converter_python,
            converter_install_arguments(wheelhouse),
            environment=process_environment,
            commands=commands,
        )
        run_python(
            converter_python,
            ["-m", "pip", "check"],
            environment=process_environment,
            commands=commands,
        )
        evidence["gates"][active_gate] = "passed"

        active_gate = "baseImports"
        inference_import = run_python(
            inference_python,
            [
                "-c",
                (
                    "import json,paddle,paddleocr,paddlex,onnxruntime as ort; "
                    "print(json.dumps({'paddle':paddle.__version__,"
                    "'paddleocr':paddleocr.__version__,'paddlex':paddlex.__version__,"
                    "'onnxruntime':ort.__version__,'providers':ort.get_available_providers()}))"
                ),
            ],
            environment=environment_with_paddle_libraries(process_environment, environment_root),
            commands=commands,
        )
        evidence["baseImport"] = parse_command_json(inference_import, "inference import")
        evidence["gates"][active_gate] = "passed"
        write_evidence(evidence_path, evidence)

        active_gate = "converterImport"
        converter_process_environment = environment_with_paddle_libraries(
            process_environment,
            converter_environment,
        )
        converter_import = run_python(
            converter_python,
            converter_probe_arguments(),
            environment=converter_process_environment,
            commands=commands,
        )
        evidence["converterImport"] = parse_command_json(
            converter_import,
            "converter import",
        )
        evidence["selectedCompatibilityPath"] = "official-build-nightly"
        evidence["compatibilityPaths"][0]["status"] = "passed"
        evidence["compatibilityPaths"][1]["status"] = "not_needed"
        evidence["gates"][active_gate] = "passed"
        write_evidence(evidence_path, evidence)

        active_gate = "modelDownloads"
        archive_records: list[dict[str, Any]] = []
        source_records: list[dict[str, Any]] = []
        for artifact in MODEL_ARTIFACTS:
            archive_path = model_downloads / f"{artifact['name']}_infer.tar"
            archive_records.append(
                download_verified_artifact(
                    str(artifact["url"]),
                    archive_path,
                    int(artifact["size"]),
                    str(artifact["sha256"]),
                )
            )
            model_root = materialize_model_archive(
                archive_path,
                source_models,
                str(artifact["name"]),
            )
            source_records.append(
                {
                    "name": artifact["name"],
                    "files": file_inventory(model_root),
                }
            )
        sample_records: list[dict[str, Any]] = []
        samples_root.mkdir(parents=True, exist_ok=True)
        for artifact in SAMPLE_ARTIFACTS:
            downloaded = sample_downloads / str(artifact["filename"])
            record = download_verified_artifact(
                str(artifact["url"]),
                downloaded,
                int(artifact["size"]),
                str(artifact["sha256"]),
            )
            staged = samples_root / str(artifact["filename"])
            copy_verified_artifact(
                downloaded,
                staged,
                int(artifact["size"]),
                str(artifact["sha256"]),
            )
            record["purpose"] = artifact["purpose"]
            sample_records.append(record)
        evidence["modelArchives"] = archive_records
        evidence["sourceModels"] = source_records
        evidence["samples"] = sample_records
        evidence["gates"][active_gate] = "passed"
        write_evidence(evidence_path, evidence)

        active_gate = "modelConversion"
        converted_records: list[dict[str, Any]] = []
        for artifact in MODEL_ARTIFACTS:
            name = str(artifact["name"])
            source_root = source_models / name
            target_root = onnx_models / name
            output_file = prepare_onnx_model_directory(source_root, target_root)
            if not output_file.is_file():
                run_python(
                    converter_python,
                    model_conversion_arguments(
                        source_root,
                        output_file,
                        conversion_cache=conversion_cache,
                    ),
                    environment=converter_process_environment,
                    commands=commands,
                    heartbeat_seconds=30.0,
                    cwd=conversion_cache,
                )
            check = run_python(
                converter_python,
                [
                    "-c",
                    (
                        "import json,onnx,sys; model=onnx.load(sys.argv[1]); "
                        "onnx.checker.check_model(model); "
                        "print(json.dumps({'opset':[item.version for item in model.opset_import],"
                        "'nodes':len(model.graph.node)}))"
                    ),
                    str(output_file),
                ],
                environment=converter_process_environment,
                commands=commands,
            )
            converted_records.append(
                {
                    "name": name,
                    "onnxSize": output_file.stat().st_size,
                    "onnxSha256": sha256_file(output_file),
                    "checker": parse_command_json(check, f"{name} ONNX checker"),
                    "files": file_inventory(target_root),
                }
            )
        evidence["convertedModels"] = converted_records
        evidence["gates"][active_gate] = "passed"
        write_evidence(evidence_path, evidence)

        inference_process_environment = environment_with_paddle_libraries(
            process_environment,
            environment_root,
        )
        active_gate = "paddleOnnxParity"
        parity_command = run_python(
            inference_python,
            parity_probe_arguments(runtime_probe, source_models, onnx_models, samples_root),
            environment=inference_process_environment,
            commands=commands,
            heartbeat_seconds=30.0,
        )
        parity_outputs = parse_command_json(parity_command, "Paddle/ONNX parity probe")
        evidence["parityOutputs"] = parity_outputs
        evidence["parity"] = validate_parity(
            parity_outputs["paddle"],
            parity_outputs["onnx"],
        )
        evidence["gates"][active_gate] = "passed"
        write_evidence(evidence_path, evidence)

        for gate, provider, profile_name in (
            ("cpuProviderInference", "CPUExecutionProvider", "cpu"),
            ("cudaProviderInference", "CUDAExecutionProvider", "cuda"),
        ):
            active_gate = gate
            provider_command = run_python(
                inference_python,
                provider_probe_arguments(
                    runtime_probe,
                    onnx_models,
                    provider,
                    profiles_root / profile_name,
                ),
                environment=inference_process_environment,
                commands=commands,
                heartbeat_seconds=30.0,
            )
            provider_result = parse_command_json(provider_command, f"{provider} probe")
            expected_names = {str(item["name"]) for item in MODEL_ARTIFACTS}
            if set(provider_result) != expected_names:
                raise RuntimeError(f"{provider} probe did not cover all fixed models")
            if any(int(item.get("nodeEvents", 0)) <= 0 for item in provider_result.values()):
                raise RuntimeError(f"{provider} probe has no actual provider node events")
            evidence.setdefault("providerInference", {})[provider] = provider_result
            evidence["gates"][active_gate] = "passed"
            write_evidence(evidence_path, evidence)

        evidence["status"] = "passed"
        evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
        write_evidence(evidence_path, evidence)
        print(
            json.dumps(
                {
                    "status": evidence["status"],
                    "selectedCompatibilityPath": evidence["selectedCompatibilityPath"],
                    "gates": evidence["gates"],
                    "evidence": str(evidence_path),
                },
                indent=2,
            )
        )
        return 0
    except Exception as error:
        evidence["gates"][active_gate] = "blocked"
        mark_unfinished_gates(evidence)
        evidence["status"] = "blocked"
        evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
        evidence["error"] = str(error)
        evidence["traceback"] = traceback.format_exc()
        write_evidence(evidence_path, evidence)
        print(f"OCR compatibility experiment BLOCKED at {active_gate}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
