"""Read-only validation-set benchmark contracts for module batching.

The executable harness added below this contract is deliberately kept outside
production code.  It never relaxes ``OverlayLayout`` or worker path checks.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
SHARED_SRC = ROOT / "shared" / "anima_caption_format"
for _path in (CORE_SRC, SHARED_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


BENCHMARK_MODULES = (
    "caption",
    "classify",
    "replace",
    "ocr",
    "countReview",
    "dropout",
    "tokenBudget",
    "export",
)
CANDIDATE_BATCHES = (1, 2, 4, 8, 16, 32, 64, 128, 256, 500)

# These are protocol maxima, not recommendations.  The benchmark never sends
# a deliberately invalid request merely to fill an unsupported candidate row.
MODULE_PROTOCOL_MAX_BATCH = {
    "caption": 64,
    "classify": 500,
    "replace": 500,
    "ocr": 1_024,
    "countReview": 500,
    "dropout": 16,
    "tokenBudget": 500,
    "export": 500,
}

MODULE_RUNTIME = {
    "caption": ("caption-e621", "caption"),
    "classify": ("classify-e621", "classify"),
    "replace": ("replace-e621", "replace"),
    "ocr": ("ocr-paddle", "ocr"),
    "dropout": ("policy", "policy"),
    "tokenBudget": ("token-budget", "token-budget"),
    "export": ("export", "export"),
}

RESOURCE_RELATIVE = {
    "caption": r"tagging-models\caption-e621-eva02-large-full-v1\resource.json",
    "classify": r"classification-indexes\e621-classify-20260724-v1\resource.json",
    "replace": r"replacement-indexes\e621-replace-20260726-v2\resource.json",
    "ocr": r"ocr-models\ocr-ppocrv5-server-paddle-v1\resource.json",
    "dropout": r"dropout-models\lse14-scorer-5k-v1\resource.json",
    "tokenBudget": r"tokenizers\tokenizer-qwen3-0.6b-anima-v1\resource.json",
}
RESOURCE_FINGERPRINT = {
    "caption": "ba31816d7e8283ab13f8127419fdb5ea9f322344fc88bb01f6d3a64afab62ec3",
    "classify": "530323a5d1ca5c3f903c0d57b04d6f1014cdcc0ca01b8de5dc0a41e27e1d2baf",
    "replace": "3cabbeeffd379a893a0b53d427c3dbb26ea6c587f474ae761b21afde4ee4c47b",
    "ocr": "368c31b8af0e96cc61239097688a457a050dfcc1205d054d4e631bd20529c9ca",
    "dropout": "1281c8365e0a2d9bc62b5cd8953665cf8d6f5ce32f41c4ec10a347c673b128ba",
    "tokenBudget": "274dac06b71d9cd4f531a85808874507768ef80ba47d3ebdb28c9a4ac7d1299d",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
FIELDS = ("quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl")
ARRAY_FIELDS = {"quality", "appearance", "tags", "environment"}
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
FAILURE_DETAIL_CATEGORIES = frozenset({
    "deterministic_fixture",
    "overflow",
    "transport",
    "timeout",
    "oom",
    "crash",
})
FAILURE_DETAIL_TEXT_LIMIT = 512
BENCHMARK_BASELINE_VERSION = "module-batching-v1-validated-20260828"


def candidate_batches_for(module: str) -> tuple[int, ...]:
    """Return the approved candidate grid restricted to a module's protocol."""
    try:
        maximum = MODULE_PROTOCOL_MAX_BATCH[module]
    except KeyError as exc:
        raise ValueError("unsupported benchmark module") from exc
    candidates = tuple(batch for batch in CANDIDATE_BATCHES if batch <= maximum)
    return (*candidates, 1_024) if module == "ocr" else candidates


def _formal_runs(result: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    runs = result.get("runs")
    if not isinstance(runs, list):
        return ()
    formal: list[Mapping[str, object]] = []
    for run in runs:
        if isinstance(run, Mapping) and run.get("warmup") is False:
            formal.append(run)
    return tuple(formal)


def _failure_signature(run: Mapping[str, object]) -> tuple[tuple[str, str, str, str], ...] | None:
    details = run.get("failureDetails")
    if not isinstance(details, list):
        return None
    values: list[tuple[str, str, str, str]] = []
    for detail in details:
        if not isinstance(detail, Mapping):
            return None
        values.append((
            str(detail.get("sampleId")),
            str(detail.get("relativePath")),
            str(detail.get("category")),
            str(detail.get("code")),
        ))
    return tuple(sorted(values))


def select_stable_recommendation(
    candidates: Mapping[str, object],
    *,
    baseline_digest: str | None,
) -> tuple[int, list[int], str]:
    """Choose the fastest stable candidate, preferring a smaller batch within 3%."""
    if baseline_digest is None or not _is_sha256(baseline_digest):
        return 1, [], "no batch 1 baseline; fallback to 1"
    baseline_candidate = candidates.get("1")
    baseline_formal = _formal_runs(baseline_candidate) if isinstance(baseline_candidate, Mapping) else ()
    if len(baseline_formal) < 3:
        return 1, [], "batch 1 baseline has fewer than three formal runs; fallback to 1"
    baseline_failures = baseline_formal[0].get("failures")
    if type(baseline_failures) not in (int, float) or float(baseline_failures) < 0:
        return 1, [], "batch 1 baseline failure count is invalid; fallback to 1"
    if any(
        type(run.get(name)) not in (int, float) or float(run.get(name, 0)) != 0.0
        for run in baseline_formal
        for name in ("timeouts", "oom", "crashed")
    ):
        return 1, [], "batch 1 baseline has timeout, OOM, or crash; fallback to 1"
    baseline_failure_signature = _failure_signature(baseline_formal[0])
    stable: list[tuple[int, float]] = []
    for key, raw in candidates.items():
        try:
            batch_size = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, Mapping):
            continue
        formal = _formal_runs(raw)
        if len(formal) < 3:
            continue
        if any(
            type(run.get(name)) not in (int, float)
            or float(run.get(name, 0)) != 0.0
            for run in formal
            for name in ("timeouts", "oom", "crashed")
        ):
            continue
        if any(type(run.get("failures")) not in (int, float) or float(run["failures"]) != float(baseline_failures) for run in formal):
            continue
        if baseline_failure_signature is not None and any(
            _failure_signature(run) != baseline_failure_signature for run in formal
        ):
            continue
        if any(run.get("outputDigest") != baseline_digest for run in formal):
            continue
        throughputs = [run.get("samplesPerSecond") for run in formal]
        if any(type(value) not in (int, float) or float(value) <= 0 for value in throughputs):
            continue
        stable.append((batch_size, sum(float(value) for value in throughputs) / len(throughputs)))
    if not stable:
        return 1, [], "no stable formal candidate; fallback to 1"
    maximum = max(throughput for _, throughput in stable)
    eligible = [(batch_size, throughput) for batch_size, throughput in stable if throughput >= maximum * 0.97]
    chosen_batch, chosen_throughput = min(eligible, key=lambda item: item[0])
    stable_batches = sorted(batch_size for batch_size, _ in stable)
    return (
        chosen_batch,
        stable_batches,
        f"highest average samplesPerSecond={maximum:.3f}; smallest batch within 3% is {chosen_batch} ({chosen_throughput:.3f})",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_dataset(dataset_root: Path | str) -> dict[str, object]:
    """Return a deterministic identity snapshot without writing to ``dataset_root``."""
    root = Path(dataset_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("benchmark dataset root must be a directory")

    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"benchmark dataset must not contain symlinks: {path}")
        if not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        stat = path.stat()
        files.append({
            "relativePath": relative_path,
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
            "sha256": _sha256_file(path),
        })

    tree = hashlib.sha256()
    for item in files:
        tree.update(str(item["relativePath"]).encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(item["size"]).encode("ascii"))
        tree.update(b"\0")
        tree.update(str(item["mtimeNs"]).encode("ascii"))
        tree.update(b"\0")
        tree.update(str(item["sha256"]).encode("ascii"))
        tree.update(b"\n")
    return {
        "fileCount": len(files),
        "totalBytes": sum(int(item["size"]) for item in files),
        "treeSha256": tree.hexdigest(),
        "files": files,
    }


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_nonnegative_number(value: object, name: str) -> None:
    if type(value) not in (int, float) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")


def _validate_failure_detail(value: object, module: str, index: int) -> Mapping[str, Any]:
    detail = _require_mapping(value, f"modules.{module}.runs.failureDetails[{index}]")
    sample_id = detail.get("sampleId")
    if sample_id is not None and (type(sample_id) is not int or sample_id < 1):
        raise ValueError(f"modules.{module}.runs.failureDetails[{index}].sampleId is invalid")
    relative_path = detail.get("relativePath")
    if relative_path is not None and (not isinstance(relative_path, str) or not relative_path):
        raise ValueError(f"modules.{module}.runs.failureDetails[{index}].relativePath is invalid")
    category = detail.get("category")
    if category not in FAILURE_DETAIL_CATEGORIES:
        raise ValueError(f"modules.{module}.runs.failureDetails[{index}].category is invalid")
    if category in {"deterministic_fixture", "overflow"} and (sample_id is None or relative_path is None):
        raise ValueError(f"modules.{module}.runs.failureDetails[{index}] lacks fixture identity")
    for key in ("code", "reason", "status"):
        text = detail.get(key)
        if not isinstance(text, str) or not text:
            raise ValueError(f"modules.{module}.runs.failureDetails[{index}].{key} is invalid")
        if len(text.encode("utf-8")) > FAILURE_DETAIL_TEXT_LIMIT:
            raise ValueError(f"modules.{module}.runs.failureDetails[{index}].{key} is too long")
    if not _IDENTIFIER.fullmatch(str(detail["code"])):
        raise ValueError(f"modules.{module}.runs.failureDetails[{index}].code is invalid")
    return detail


def _validate_snapshot(value: object, name: str) -> Mapping[str, Any]:
    snapshot = _require_mapping(value, name)
    for key in ("fileCount", "totalBytes"):
        if type(snapshot.get(key)) is not int or snapshot[key] < 0:
            raise ValueError(f"{name}.{key} must be a non-negative integer")
    if not _is_sha256(snapshot.get("treeSha256")):
        raise ValueError(f"{name}.treeSha256 must be a SHA-256 digest")
    files = snapshot.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise ValueError(f"{name}.files must be a list")
        previous = ""
        for item in files:
            row = _require_mapping(item, f"{name}.files")
            relative = row.get("relativePath")
            if not isinstance(relative, str) or not relative or relative <= previous:
                raise ValueError(f"{name}.files must be sorted by relativePath")
            previous = relative
            if type(row.get("size")) is not int or row["size"] < 0:
                raise ValueError(f"{name}.files.size must be a non-negative integer")
            if type(row.get("mtimeNs")) is not int or row["mtimeNs"] < 0:
                raise ValueError(f"{name}.files.mtimeNs must be a non-negative integer")
            if not _is_sha256(row.get("sha256")):
                raise ValueError(f"{name}.files.sha256 must be a SHA-256 digest")
    return snapshot


def _validate_run(run: object, module: str) -> Mapping[str, Any]:
    row = _require_mapping(run, f"modules.{module}.runs")
    if type(row.get("batchSize")) is not int or row["batchSize"] < 1:
        raise ValueError(f"modules.{module}.runs.batchSize must be a positive integer")
    if type(row.get("warmup")) is not bool:
        raise ValueError(f"modules.{module}.runs.warmup must be a boolean")
    for key in (
        "totalSeconds",
        "samplesPerSecond",
        "cpuPercent",
        "peakMemoryBytes",
        "gpuUtilizationPercent",
        "peakVramBytes",
        "failures",
        "timeouts",
        "oom",
        "crashed",
    ):
        _require_nonnegative_number(row.get(key), f"modules.{module}.runs.{key}")
    if not _is_sha256(row.get("outputDigest")):
        raise ValueError(f"modules.{module}.runs.outputDigest must be a SHA-256 digest")
    failure_details = row.get("failureDetails")
    if not isinstance(failure_details, list):
        raise ValueError(f"modules.{module}.runs.failureDetails must be a list")
    if len(failure_details) != row["failures"]:
        raise ValueError(f"modules.{module}.runs.failureDetails must match failures")
    for index, detail in enumerate(failure_details):
        _validate_failure_detail(detail, module, index)
    return row


def validate_report(report: object) -> None:
    """Reject incomplete, mutating, or NL-contaminated benchmark reports."""
    document = _require_mapping(report, "report")
    if document.get("schemaVersion") != 1:
        raise ValueError("report.schemaVersion must be 1")
    if document.get("benchmarkVersion") != "module-batching-v1":
        raise ValueError("report.benchmarkVersion is unsupported")
    if document.get("status") != "validated":
        raise ValueError("report.status must be validated")
    dataset = _require_mapping(document.get("dataset"), "report.dataset")
    before = _validate_snapshot(dataset.get("before"), "report.dataset.before")
    after = _validate_snapshot(dataset.get("after"), "report.dataset.after")
    if dict(before) != dict(after):
        raise ValueError("benchmark dataset snapshot changed")
    if type(document.get("nlRequests")) is not int or document["nlRequests"] != 0:
        raise ValueError("benchmark must make zero NL requests")

    modules = _require_mapping(document.get("modules"), "report.modules")
    missing = [module for module in BENCHMARK_MODULES if module not in modules]
    unexpected = sorted(set(modules) - set(BENCHMARK_MODULES))
    if missing or unexpected:
        raise ValueError(f"benchmark modules are invalid; missing={missing}, unexpected={unexpected}")
    for module in BENCHMARK_MODULES:
        result = _require_mapping(modules[module], f"modules.{module}")
        baseline_digest = result.get("batch1OutputDigest")
        if not _is_sha256(baseline_digest):
            raise ValueError(f"modules.{module}.batch1OutputDigest must be a SHA-256 digest")
        if type(result.get("recommendation")) is not int or result["recommendation"] < 1:
            raise ValueError(f"modules.{module}.recommendation must be a positive integer")
        recommendation = int(result["recommendation"])
        runs = result.get("runs")
        if not isinstance(runs, list):
            raise ValueError(f"modules.{module}.runs must be a list")
        validated_runs = [_validate_run(run, module) for run in runs]
        expected_batches = candidate_batches_for(module)
        candidates = _require_mapping(result.get("candidates"), f"modules.{module}.candidates")
        expected_keys = {str(batch) for batch in expected_batches}
        if set(candidates) != expected_keys:
            raise ValueError(
                f"modules.{module}.candidates must match the complete candidate grid "
                f"{sorted(expected_keys, key=int)}"
            )
        candidate_map: dict[str, Mapping[str, Any]] = {}
        for candidate_batch in expected_batches:
            candidate_key = str(candidate_batch)
            candidate_result = _require_mapping(candidates[candidate_key], f"modules.{module}.candidates.{candidate_key}")
            candidate_runs = candidate_result.get("runs")
            if not isinstance(candidate_runs, list):
                raise ValueError(f"modules.{module}.candidates.{candidate_key}.runs must be a list")
            validated_candidate_runs = [_validate_run(run, module) for run in candidate_runs]
            formal_candidate_runs = [run for run in validated_candidate_runs if not run["warmup"]]
            if len(formal_candidate_runs) != 3:
                raise ValueError(
                    f"modules.{module}.candidates.{candidate_key} requires exactly three formal runs"
                )
            if any(run["batchSize"] != candidate_batch for run in validated_candidate_runs):
                raise ValueError(f"modules.{module}.candidates.{candidate_key} batch size is inconsistent")
            candidate_map[candidate_key] = {"runs": validated_candidate_runs}

        formal_by_batch: dict[int, list[Mapping[str, Any]]] = {batch: [] for batch in expected_batches}
        for run in validated_runs:
            batch_size = int(run["batchSize"])
            if batch_size not in formal_by_batch:
                raise ValueError(f"modules.{module}.runs contains a batch outside the candidate grid")
            if not run["warmup"]:
                formal_by_batch[batch_size].append(run)
        if any(len(formal_by_batch[batch]) != 3 for batch in expected_batches):
            raise ValueError(f"modules.{module}.runs requires exactly three formal runs per candidate")
        batch_one_formal = formal_by_batch[1]
        if any(run["outputDigest"] != baseline_digest for run in batch_one_formal):
            raise ValueError(f"modules.{module} batch 1 output digest is inconsistent")
        if any(run["outputDigest"] != baseline_digest for run in candidate_map["1"]["runs"] if not run["warmup"]):
            raise ValueError(f"modules.{module}.candidates.1 output digest is inconsistent")

        stable_value = result.get("stableBatchSizes")
        if not isinstance(stable_value, list) or any(type(batch) is not int or batch < 1 for batch in stable_value):
            raise ValueError(f"modules.{module}.stableBatchSizes must be a list of positive integers")
        if stable_value != sorted(set(stable_value)):
            raise ValueError(f"modules.{module}.stableBatchSizes must be sorted and unique")
        if recommendation not in expected_batches or recommendation not in set(stable_value):
            raise ValueError(f"modules.{module}.recommendation must belong to candidates and stableBatchSizes")
        expected_recommendation, expected_stable, _ = select_stable_recommendation(
            candidate_map,
            baseline_digest=str(baseline_digest),
        )
        if recommendation != expected_recommendation or stable_value != expected_stable:
            raise ValueError(f"modules.{module} has a recomputed recommendation or stable candidate mismatch")


@dataclass(frozen=True)
class DatasetSample:
    sample_id: int
    relative_path: str
    annotation_key: str
    image_path: Path
    json_path: Path | None
    annotation: dict[str, object]
    txt_text: str
    image_format: str
    image_size: int
    image_mtime_ns: int
    image_file_id: str
    image_sha256: str


@dataclass
class _ResourceMetrics:
    cpu_percent: float = 0.0
    peak_memory_bytes: int = 0
    gpu_utilization_percent: float = 0.0
    peak_vram_bytes: int = 0


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _finalize_state(
    state_path: Path,
    state: dict[str, object],
    *,
    dataset_root: Path,
    before: Mapping[str, object],
    after: Mapping[str, object],
    report_path: Path,
) -> None:
    state["status"] = "validated"
    state["reportPath"] = str(report_path)
    state["dataset"] = {"root": str(dataset_root), "before": before, "after": after}
    state["completedModules"] = list(BENCHMARK_MODULES)
    _atomic_json_write(state_path, state)


def _json_digest(value: object) -> str:
    return hashlib.sha256(_canonical(_digest_value(value)).encode("utf-8")).hexdigest()


def _digest_value(value: object) -> object:
    """Canonicalize CUDA score noise without hiding semantic output changes."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, Mapping):
        return {str(key): _digest_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digest_value(item) for item in value]
    return value


def _file_id(path: Path) -> str:
    stat = path.stat()
    return f"{getattr(stat, 'st_dev', 0)}:{getattr(stat, 'st_ino', 0)}"


def _annotation_value(value: object, field: str) -> object:
    if field in ARRAY_FIELDS:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return []
    return value.strip() if isinstance(value, str) else ""


def _normalize_annotation(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {field: _annotation_value(source.get(field), field) for field in FIELDS}


def _annotation_txt(annotation: Mapping[str, object]) -> str:
    values: list[str] = []
    for field in FIELDS[:-1]:
        value = annotation[field]
        if field in ARRAY_FIELDS:
            values.extend(str(item) for item in value if isinstance(item, str) and item)
        elif isinstance(value, str) and value:
            values.extend(part.strip() for part in value.split(",") if part.strip())
    return ", ".join(values) or "solo"


def collect_samples(dataset_root: Path, snapshot: Mapping[str, object]) -> tuple[DatasetSample, ...]:
    """Collect only image inputs and paired JSON; never create files in the dataset."""
    file_rows = snapshot.get("files")
    if not isinstance(file_rows, list):
        raise ValueError("dataset snapshot has no file rows")
    identity = {
        str(row["relativePath"]): row
        for row in file_rows
        if isinstance(row, Mapping) and isinstance(row.get("relativePath"), str)
    }
    samples: list[DatasetSample] = []
    for image_path in sorted(
        (path for path in dataset_root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda path: path.relative_to(dataset_root).as_posix().casefold(),
    ):
        relative = image_path.relative_to(dataset_root).as_posix()
        row = identity.get(relative)
        if not isinstance(row, Mapping):
            raise ValueError(f"image is missing from dataset snapshot: {relative}")
        json_path = image_path.with_suffix(".json")
        annotation: dict[str, object] = {}
        if json_path.is_file():
            try:
                parsed = json.loads(json_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"benchmark annotation is unreadable: {json_path}") from exc
            annotation = _normalize_annotation(parsed)
        suffix = image_path.suffix.lower()
        image_format = "jpeg" if suffix in {".jpg", ".jpeg"} else suffix[1:]
        stat = image_path.stat()
        samples.append(
            DatasetSample(
                sample_id=len(samples) + 1,
                relative_path=relative,
                annotation_key=relative.rsplit(".", 1)[0],
                image_path=image_path,
                json_path=json_path if json_path.is_file() else None,
                annotation=annotation,
                txt_text=_annotation_txt(annotation),
                image_format=image_format,
                image_size=int(row["size"]),
                image_mtime_ns=int(row["mtimeNs"]),
                image_file_id=_file_id(image_path),
                image_sha256=str(row["sha256"]),
            )
        )
    if not samples:
        raise ValueError("benchmark dataset contains no supported images")
    return tuple(samples)


def _lease_id(module: str, sample_id: int) -> str:
    return f"bench-{module}-{sample_id}"


def _chunks(values: Sequence[DatasetSample], size: int) -> list[tuple[DatasetSample, ...]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    return [tuple(values[start : start + size]) for start in range(0, len(values), size)]


def _caption_hello(dataset_root: Path, job_id: str, config_hash: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "caption_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "profile": "e621",
        "datasetRoot": str(dataset_root),
        "resourceManifestRelativePath": RESOURCE_RELATIVE["caption"],
        "resourceFingerprint": RESOURCE_FINGERPRINT["caption"],
        "thresholdPolicy": {"mode": "model_default"},
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": False,
            "triggerTerms": [],
        },
        "imageDecode": {
            "extensions": [".jpg", ".jpeg", ".png", ".webp", ".bmp"],
            "rejectMultiFrame": True,
            "applyExifTranspose": True,
            "alphaBackground": "#FFFFFF",
        },
    }


def _classify_hello(job_id: str, config_hash: str, resource_root: Path) -> dict[str, object]:
    manifest = json.loads(
        (resource_root / Path(RESOURCE_RELATIVE["classify"].replace("\\", os.sep))).read_text(encoding="utf-8")
    )
    return {
        "schemaVersion": 1,
        "payloadType": "classify_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "profile": "e621",
        "resourceManifestRelativePath": RESOURCE_RELATIVE["classify"],
        "resourceFingerprint": RESOURCE_FINGERPRINT["classify"],
        "wikiDataSourceId": manifest["metadata"]["wikiDataSourceId"],
        "overwriteCount": False,
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": False,
            "triggerTerms": [],
        },
    }


def _replace_hello(job_id: str, config_hash: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "replace_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "resourceManifestRelativePath": RESOURCE_RELATIVE["replace"],
        "resourceFingerprint": RESOURCE_FINGERPRINT["replace"],
    }


def _ocr_hello(job_id: str, config_hash: str, runtime_id: str, runtime_fingerprint: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "ocr_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "resourceId": "ocr-ppocrv5-server-paddle-v1",
        "resourceManifestRelativePath": RESOURCE_RELATIVE["ocr"],
        "resourceFingerprint": RESOURCE_FINGERPRINT["ocr"],
        "requestedDevice": "cuda" if runtime_id == "ocr-paddle-gpu" else "cpu",
        "expectedRuntimeId": runtime_id,
        "expectedRuntimeFingerprint": runtime_fingerprint,
        "inference": {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": True,
            "textRecScoreThresh": 0,
            "textDetLimitSideLen": 1920,
            "textDetLimitType": "max",
        },
    }


def _ocr_benchmark_runtime(install_root: Path) -> tuple[str, str]:
    """Select the installed OCR runtime from the observed GPU facts."""
    snapshot = _device_snapshot()
    gpu = snapshot.get("gpu") if isinstance(snapshot, Mapping) else None
    available = isinstance(gpu, Mapping) and gpu.get("available") is True
    preferred = "ocr-paddle-gpu" if available else "ocr-paddle"
    manifest = install_root / "manifests" / "runtimes" / f"{preferred}.json"
    if not manifest.is_file() and preferred == "ocr-paddle-gpu":
        preferred = "ocr-paddle"
        manifest = install_root / "manifests" / "runtimes" / "ocr-paddle.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"OCR runtime manifest is missing: {manifest}")
    return preferred, _sha256_file(manifest)


def _policy_hello(dataset_root: Path, overlay_root: Path, job_id: str, config_hash: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "policy_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "datasetRoot": str(dataset_root),
        "overlayRoot": str(overlay_root),
        "artistRootName": dataset_root.name,
        "resourceManifestRelativePath": RESOURCE_RELATIVE["dropout"],
        "resourceFingerprint": RESOURCE_FINGERPRINT["dropout"],
        "policy": {
            "policyVersion": "dataset-batch-policy-v1",
            "seed": "module-batching-benchmark-v1",
            "artist": {"enabled": False, "dropoutProbability": 0.0},
            "quality": {
                "enabled": True,
                "dropoutProbability": 0.0,
                "device": "auto",
                "batchSize": 4,
                "resourceId": "lse14-scorer-5k-v1",
            },
            "appearanceNl": {
                "enabled": False,
                "solo": {"dropNl": 0.0, "dropAppearance": 0.0},
                "nonSolo": {"dropNl": 0.0, "dropAppearance": 0.0},
                "unknown": {"dropNl": 0.0, "dropAppearance": 0.0},
            },
        },
    }


def _token_hello(job_id: str, config_hash: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "token_budget_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "resourceId": "tokenizer-qwen3-0.6b-anima-v1",
        "resourceManifestRelativePath": RESOURCE_RELATIVE["tokenBudget"],
        "resourceFingerprint": RESOURCE_FINGERPRINT["tokenBudget"],
        "contextLimit": 40_960,
        "maxTokens": 4_096,
    }


def _export_hello(dataset_root: Path, overlay_root: Path, job_id: str, config_hash: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "payloadType": "export_hello_request",
        "jobId": job_id,
        "configHash": config_hash,
        "datasetRoot": str(dataset_root),
        "overlayRoot": str(overlay_root),
        "format": "both",
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": False,
            "triggerTerms": [],
            "flatTxtLayout": "nl_newline",
        },
    }


def _items_for(module: str, samples: Sequence[DatasetSample]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for sample in samples:
        lease_id = _lease_id(module, sample.sample_id)
        if module == "caption":
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "source": "e621", "relativeImagePath": sample.relative_path,
                "annotationKey": sample.annotation_key, "imageFormat": sample.image_format,
                "imageFrameCount": 1, "imageFileId": sample.image_file_id,
                "imageSize": sample.image_size, "imageMtimeNs": sample.image_mtime_ns,
            })
        elif module == "classify":
            count = sample.annotation.get("count")
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "source": "e621", "relativeImagePath": sample.relative_path,
                "annotationKey": sample.annotation_key, "txtText": sample.txt_text,
                "txtProvenance": "original_preserved",
                "originalCount": count if count in {"solo", "duo", "trio", "group"} else None,
            })
        elif module == "replace":
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "source": "e621", "relativeImagePath": sample.relative_path,
                "projection": dict(sample.annotation),
            })
        elif module == "ocr":
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "relativeImagePath": sample.relative_path, "imagePath": str(sample.image_path),
                "imageSize": sample.image_size, "imageSha256": sample.image_sha256,
            })
        elif module == "dropout":
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "relativeImagePath": sample.relative_path, "annotationKey": sample.annotation_key,
                "imageSize": sample.image_size, "imageMtimeNs": sample.image_mtime_ns,
                "imageFileId": sample.image_file_id,
            })
        elif module == "tokenBudget":
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "annotation": dict(sample.annotation),
            })
        elif module == "export":
            items.append({
                "schemaVersion": 1, "sampleId": sample.sample_id, "leaseId": lease_id,
                "relativeImagePath": sample.relative_path, "annotationKey": sample.annotation_key,
            })
        else:
            raise ValueError(f"unsupported benchmark module: {module}")
    return items


def _process_payload(module: str, samples: Sequence[DatasetSample]) -> dict[str, object]:
    payload_type = {
        "caption": "caption_process_request", "classify": "classify_process_request",
        "replace": "replace_process_request", "ocr": "ocr_process_request",
        "dropout": "policy_process_request", "tokenBudget": "token_budget_process_request",
        "export": "export_process_request",
    }[module]
    payload: dict[str, object] = {"schemaVersion": 1, "payloadType": payload_type, "items": _items_for(module, samples)}
    if module == "tokenBudget":
        payload["captionFormat"] = {
            "replaceUnderscoresWithSpaces": True, "preserveEscapes": True,
            "triggersEnabled": False, "triggerTerms": [], "flatTxtLayout": "nl_newline",
        }
    return payload


def _outcomes(module: str, payload: Mapping[str, object]) -> list[dict[str, object]]:
    key = "items" if module == "ocr" else "outcomes"
    values = payload.get(key)
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise ValueError(f"{module} worker result has no outcomes")
    return [dict(value) for value in values]


def _outcome_digest(outcomes: Sequence[Mapping[str, object]]) -> str:
    ordered = sorted((dict(value) for value in outcomes), key=lambda value: (int(value.get("sampleId", 0)), str(value.get("leaseId", ""))))
    return _json_digest(ordered)


def _is_failure_outcome(module: str, outcome: Mapping[str, object]) -> bool:
    payload_type = outcome.get("payloadType")
    if module in {"caption", "classify", "replace"}:
        return isinstance(payload_type, str) and payload_type.endswith("_issue")
    status = outcome.get("status")
    if module == "ocr":
        return status == "failed"
    if module == "dropout":
        return status == "issue"
    if module == "tokenBudget":
        return status in {"failed", "overflow"}
    if module == "export":
        return status == "issue"
    return False


def _bounded_detail_text(value: object, fallback: str) -> str:
    text = value if isinstance(value, str) and value else fallback
    encoded = text.encode("utf-8", errors="replace")[:FAILURE_DETAIL_TEXT_LIMIT]
    return encoded.decode("utf-8", errors="ignore") or fallback[:FAILURE_DETAIL_TEXT_LIMIT]


def _failure_details(
    module: str,
    outcomes: Sequence[Mapping[str, object]],
    samples: Sequence[object],
) -> list[dict[str, object]]:
    sample_by_id = {
        int(getattr(sample, "sample_id")): sample
        for sample in samples
        if type(getattr(sample, "sample_id", None)) is int
    }
    details: list[dict[str, object]] = []
    for outcome in outcomes:
        if not _is_failure_outcome(module, outcome):
            continue
        raw_sample_id = outcome.get("sampleId")
        sample_id = raw_sample_id if type(raw_sample_id) is int and raw_sample_id > 0 else None
        sample = sample_by_id.get(sample_id)
        raw_path = outcome.get("relativeImagePath")
        relative_path = raw_path if isinstance(raw_path, str) and raw_path else getattr(sample, "relative_path", None)
        status = outcome.get("status")
        status_text = status if isinstance(status, str) and status else outcome.get("payloadType")
        status_text = _bounded_detail_text(status_text, "failed")
        error = outcome.get("error")
        error_code = error.get("code") if isinstance(error, Mapping) else None
        error_message = error.get("message") if isinstance(error, Mapping) else None
        code = outcome.get("code")
        if not isinstance(code, str) or not code:
            code = error_code
        if not isinstance(code, str) or not code:
            field_errors = outcome.get("fieldErrors")
            if isinstance(field_errors, list):
                code = next(
                    (item.get("code") for item in field_errors if isinstance(item, Mapping) and isinstance(item.get("code"), str)),
                    None,
                )
        if not isinstance(code, str) or not code:
            code = "token_budget_overflow" if module == "tokenBudget" and status == "overflow" else "worker_failure"
        reason = outcome.get("message")
        if not isinstance(reason, str) or not reason:
            reason = error_message
        if not isinstance(reason, str) or not reason:
            field_errors = outcome.get("fieldErrors")
            if isinstance(field_errors, list):
                codes = [
                    str(item.get("code"))
                    for item in field_errors
                    if isinstance(item, Mapping) and isinstance(item.get("code"), str) and item.get("code")
                ]
                reason = ", ".join(codes) if codes else None
        if not isinstance(reason, str) or not reason:
            reason = f"{status_text} outcome"
        category = "overflow" if module == "tokenBudget" and status == "overflow" else "deterministic_fixture"
        details.append({
            "sampleId": sample_id,
            "relativePath": relative_path if isinstance(relative_path, str) and relative_path else None,
            "category": category,
            "code": _bounded_detail_text(code, "worker_failure"),
            "reason": _bounded_detail_text(reason, "worker failure"),
            "status": status_text,
        })
    return details


def _failure_count(module: str, outcomes: Sequence[Mapping[str, object]]) -> int:
    return sum(1 for outcome in outcomes if _is_failure_outcome(module, outcome))


def _gpu_sample() -> tuple[float, int]:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0, 0
    if completed.returncode != 0:
        return 0.0, 0
    line = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 2:
        return 0.0, 0
    try:
        return max(0.0, float(parts[0])), max(0, int(float(parts[1]) * 1024 * 1024))
    except ValueError:
        return 0.0, 0


def _windows_process_memory_bytes(pid: int) -> int:
    if os.name != "nt":
        return 0
    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, int(pid))
    if not handle:
        return 0
    try:
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


class _WindowsCpuSampler:
    def __init__(self) -> None:
        self._previous_process: int | None = None
        self._previous_system: int | None = None

    @staticmethod
    def _filetime(value: object) -> int:
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    def sample(self, pid: int) -> float:
        if os.name != "nt":
            return 0.0
        class _FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not process_handle:
            return 0.0
        try:
            creation = _FileTime()
            exit_time = _FileTime()
            kernel = _FileTime()
            user = _FileTime()
            if not kernel32.GetProcessTimes(process_handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
                return 0.0
        finally:
            kernel32.CloseHandle(process_handle)
        idle = _FileTime()
        system_kernel = _FileTime()
        system_user = _FileTime()
        if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system_kernel), ctypes.byref(system_user)):
            return 0.0
        process_total = self._filetime(kernel) + self._filetime(user)
        system_total = self._filetime(system_kernel) + self._filetime(system_user)
        result = 0.0
        if self._previous_process is not None and self._previous_system is not None:
            process_delta = process_total - self._previous_process
            system_delta = system_total - self._previous_system
            if process_delta >= 0 and system_delta > 0:
                result = max(0.0, min(100.0, process_delta * 100.0 / system_delta))
        self._previous_process = process_total
        self._previous_system = system_total
        return result
class _ResourceSampler:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.metrics = _ResourceMetrics()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            import psutil  # type: ignore[import-not-found]
        except Exception:
            psutil = None
        self._psutil = psutil
        self._windows_cpu = _WindowsCpuSampler() if psutil is None and os.name == "nt" else None

    def _sample(self) -> None:
        memory = 0
        cpu = 0.0
        if self._psutil is not None:
            try:
                root = self._psutil.Process(self.process.pid)
                processes = [root, *root.children(recursive=True)]
                for process in processes:
                    try:
                        cpu += max(0.0, float(process.cpu_percent(None)))
                        memory += max(0, int(process.memory_info().rss))
                    except (self._psutil.Error, OSError):
                        continue
            except (self._psutil.Error, OSError):
                pass
        elif self._windows_cpu is not None:
            memory = _windows_process_memory_bytes(self.process.pid)
            cpu = self._windows_cpu.sample(self.process.pid)
        gpu, vram = _gpu_sample()
        with self._lock:
            self.metrics.cpu_percent = max(self.metrics.cpu_percent, cpu)
            self.metrics.peak_memory_bytes = max(self.metrics.peak_memory_bytes, memory)
            self.metrics.gpu_utilization_percent = max(self.metrics.gpu_utilization_percent, gpu)
            self.metrics.peak_vram_bytes = max(self.metrics.peak_vram_bytes, vram)

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._psutil is not None:
            try:
                self._psutil.Process(self.process.pid).cpu_percent(None)
            except Exception:
                pass
        self._thread = threading.Thread(target=self._loop, name="module-benchmark-resources", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(0.2)
        self._sample()

    def snapshot(self, *, reset: bool = False) -> _ResourceMetrics:
        with self._lock:
            result = _ResourceMetrics(**vars(self.metrics))
            if reset:
                self.metrics = _ResourceMetrics()
        return result

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._sample()


class _WorkerSessionError(RuntimeError):
    def __init__(self, code: str, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.timeout = timeout


class _WorkerSession:
    def __init__(
        self,
        install_root: Path,
        resource_root: Path,
        runtime_id: str,
        owner: str,
        job_id: str,
        config_hash: str,
        *,
        timeout_seconds: float,
        deterministic_random_seed: int | None = None,
    ) -> None:
        from anima_core.launcher import WorkerLauncher

        self.runtime_id = runtime_id
        self.owner = owner
        self.job_id = job_id
        self.config_hash = config_hash
        self.timeout_seconds = timeout_seconds
        self._counter = 0
        self._closed = False
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        launcher = WorkerLauncher.from_install_root(install_root, resource_root=resource_root)
        try:
            launch = launcher.resolve(runtime_id, expected_owner=owner, verify_interpreter=False)
        except Exception as exc:
            raise _WorkerSessionError("runtime_unavailable", str(exc)) from exc
        if deterministic_random_seed is not None and owner == "replace":
            command = (
                launch.command[0], *launch.command[1:4], "-c",
                f"import random,runpy; random.seed({int(deterministic_random_seed)}); runpy.run_module('anima_replace_worker.entry', run_name='__main__')",
            )
        else:
            command = launch.command
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=launch.environment,
                cwd=str(install_root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise _WorkerSessionError("runtime_unavailable", str(exc)) from exc
        assert self.process.stdout is not None and self.process.stderr is not None
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="module-benchmark-stderr", daemon=True)
        self._stderr_thread.start()
        self.sampler = _ResourceSampler(self.process)
        self.sampler.start()

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            try:
                chunk = self.process.stderr.read(8_192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._stderr_lock:
                self._stderr.extend(chunk)
                if len(self._stderr) > 65_536:
                    del self._stderr[:-65_536]

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    def _next_message_id(self, method: str) -> str:
        self._counter += 1
        return f"bench-{method}-{self._counter}"

    def _readline(self) -> bytes:
        assert self.process.stdout is not None
        result: list[bytes] = []
        error: list[BaseException] = []

        def read() -> None:
            try:
                result.append(self.process.stdout.readline(1_048_578))
            except BaseException as exc:  # pragma: no cover - platform pipe errors are classified below
                error.append(exc)

        thread = threading.Thread(target=read, name="module-benchmark-stdout", daemon=True)
        thread.start()
        thread.join(timeout=self.timeout_seconds)
        if thread.is_alive():
            self._terminate()
            raise _WorkerSessionError("worker_timeout", "worker response timed out", timeout=True)
        if error or not result or not result[0]:
            raise _WorkerSessionError("worker_crashed", "worker closed stdout")
        return result[0]

    def request(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        if self._closed or self.process.poll() is not None:
            raise _WorkerSessionError("worker_crashed", "worker is not running")
        from anima_core.worker_protocol import ProtocolEnvelopeV1, decode_frame, encode_frame

        request = ProtocolEnvelopeV1(
            "1.0", "request", self._next_message_id(method), self.runtime_id, self.owner,
            method, payload, jobId=self.job_id, configHash=self.config_hash,
        )
        try:
            frame = encode_frame(request)
            assert self.process.stdin is not None
            self.process.stdin.write(frame)
            self.process.stdin.flush()
            response = decode_frame(self._readline(), runtime_id=self.runtime_id, owner=self.owner)
        except _WorkerSessionError:
            raise
        except Exception as exc:
            raise _WorkerSessionError("protocol_error", str(exc)) from exc
        if (
            response.kind != "response" or response.replyTo != request.messageId
            or response.jobId != self.job_id or response.configHash != self.config_hash
        ):
            raise _WorkerSessionError("protocol_error", "worker response identity mismatch")
        if response.method == "error":
            code = response.payload.get("code")
            raise _WorkerSessionError(str(code) if isinstance(code, str) else "worker_error", "worker rejected request")
        expected_method = "hello" if method == "hello" else "result"
        if response.method != expected_method:
            raise _WorkerSessionError("protocol_error", "worker response method is invalid")
        return response.payload

    def _terminate(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                except OSError:
                    pass
                self.process.wait(timeout=5)

    def close(self) -> None:
        if self._closed:
            return
        if self.process.poll() is None:
            try:
                self.request("shutdown", {})
            except Exception:
                self._terminate()
        else:
            self._terminate()
        self._closed = True
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate()
        self.sampler.close()
        self._stderr_thread.join(timeout=3)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _error_failure_detail(error: str, *, code: str, category: str) -> dict[str, object]:
    return {
        "sampleId": None,
        "relativePath": None,
        "category": category,
        "code": _bounded_detail_text(code, "benchmark_error"),
        "reason": _bounded_detail_text(error, "benchmark failure"),
        "status": "error",
    }


def _error_run(
    batch_size: int,
    *,
    warmup: bool,
    error: str,
    metrics: _ResourceMetrics,
    failures: int = 1,
    timeouts: int = 0,
    oom: int = 0,
    crashed: int = 0,
    code: str = "benchmark_error",
    category: str = "crash",
) -> dict[str, object]:
    return {
        "batchSize": batch_size,
        "warmup": warmup,
        "totalSeconds": 0.0,
        "samplesPerSecond": 0.0,
        "cpuPercent": metrics.cpu_percent,
        "peakMemoryBytes": metrics.peak_memory_bytes,
        "gpuUtilizationPercent": metrics.gpu_utilization_percent,
        "peakVramBytes": metrics.peak_vram_bytes,
        "failures": failures,
        "timeouts": timeouts,
        "oom": oom,
        "crashed": crashed,
        "outputDigest": _json_digest({"error": error}),
        "error": error,
        "failureDetails": [] if failures == 0 else [_error_failure_detail(error, code=code, category=category)] * failures,
    }


def _run_worker_candidate(
    module: str,
    samples: tuple[DatasetSample, ...],
    batch_size: int,
    *,
    dataset_root: Path,
    resource_root: Path,
    install_root: Path,
    overlay_root: Path | None,
    formal_runs: int,
    timeout_seconds: float,
) -> dict[str, object]:
    runtime_id, owner = MODULE_RUNTIME[module]
    job_id = f"bench-{module}-{batch_size}"
    config_hash = hashlib.sha256(f"module-batching-v1/{module}/{batch_size}".encode("ascii")).hexdigest()
    ocr_runtime_fingerprint: str | None = None
    if module == "ocr":
        runtime_id, ocr_runtime_fingerprint = _ocr_benchmark_runtime(install_root)
    if module == "caption":
        hello_payload = _caption_hello(dataset_root, job_id, config_hash)
    elif module == "classify":
        hello_payload = _classify_hello(job_id, config_hash, resource_root)
    elif module == "replace":
        hello_payload = _replace_hello(job_id, config_hash)
    elif module == "ocr":
        assert ocr_runtime_fingerprint is not None
        hello_payload = _ocr_hello(job_id, config_hash, runtime_id, ocr_runtime_fingerprint)
    elif module == "dropout":
        assert overlay_root is not None
        hello_payload = _policy_hello(dataset_root, overlay_root, job_id, config_hash)
    elif module == "tokenBudget":
        hello_payload = _token_hello(job_id, config_hash)
    elif module == "export":
        assert overlay_root is not None
        hello_payload = _export_hello(dataset_root, overlay_root, job_id, config_hash)
    else:
        raise ValueError(f"unsupported benchmark module: {module}")

    runs: list[dict[str, object]] = []
    hello_evidence: dict[str, object] = {}
    session: _WorkerSession | None = None
    deterministic_random_seed = 0 if module == "replace" else None

    def open_session() -> _WorkerSession:
        nonlocal hello_evidence
        current = _WorkerSession(
            install_root, resource_root, runtime_id, owner, job_id, config_hash,
            timeout_seconds=timeout_seconds,
            deterministic_random_seed=deterministic_random_seed,
        )
        payload = current.request("hello", hello_payload)
        hello_evidence = {
            key: payload.get(key)
            for key in ("provider", "device", "observedDevice", "runtimeId", "modelLoadCount", "modelSessionLoads")
            if key in payload
        }
        return current

    def execute(warmup: bool) -> dict[str, object]:
        nonlocal session
        outcomes: list[dict[str, object]] = []
        started: float | None = None
        try:
            if module == "replace" and session is not None:
                session.close()
                session = None
            if session is None:
                session = open_session()
            started = time.perf_counter()
            for chunk in _chunks(samples, batch_size):
                payload = _process_payload(module, chunk)
                outcomes.extend(_outcomes(module, session.request("process_batch", payload)))
            elapsed = max(time.perf_counter() - (started or time.perf_counter()), 1e-9)
            metrics = session.sampler.snapshot(reset=True)
            return {
                "batchSize": batch_size,
                "warmup": warmup,
                "totalSeconds": elapsed,
                "samplesPerSecond": len(samples) / elapsed,
                "cpuPercent": metrics.cpu_percent,
                "peakMemoryBytes": metrics.peak_memory_bytes,
                "gpuUtilizationPercent": metrics.gpu_utilization_percent,
                "peakVramBytes": metrics.peak_vram_bytes,
                "failures": _failure_count(module, outcomes),
                "timeouts": 0,
                "oom": 0,
                "crashed": 0,
                "outputDigest": _outcome_digest(outcomes),
                "outputCount": len(outcomes),
                "failureDetails": _failure_details(module, outcomes, samples),
            }
        except _WorkerSessionError as exc:
            elapsed = max(time.perf_counter() - started, 0.0) if started is not None else 0.0
            if session is not None:
                metrics = session.sampler.snapshot(reset=True)
                stderr = session.stderr_tail
                session.close()
                session = None
            else:
                metrics = _ResourceMetrics()
                stderr = ""
            text = f"{exc.code}: {exc}"
            if stderr:
                text = f"{text}; stderr={stderr[-1024:]}"
            lowered = text.casefold()
            is_oom = int("out of memory" in lowered or "cuda" in lowered and "memory" in lowered or "oom" in lowered)
            category = "timeout" if exc.timeout else "oom" if is_oom else "crash" if exc.code in {"worker_crashed", "runtime_unavailable"} else "transport"
            return {
                "batchSize": batch_size,
                "warmup": warmup,
                "totalSeconds": elapsed,
                "samplesPerSecond": 0.0,
                "cpuPercent": metrics.cpu_percent,
                "peakMemoryBytes": metrics.peak_memory_bytes,
                "gpuUtilizationPercent": metrics.gpu_utilization_percent,
                "peakVramBytes": metrics.peak_vram_bytes,
                "failures": 1,
                "timeouts": int(exc.timeout),
                "oom": is_oom,
                "crashed": int(exc.code in {"worker_crashed", "runtime_unavailable"}),
                "outputDigest": _json_digest({"error": text}),
                "error": text,
                "failureDetails": [_error_failure_detail(text, code=exc.code, category=category)],
            }

    try:
        runs.append(execute(True))
        for _ in range(formal_runs):
            runs.append(execute(False))
    finally:
        if session is not None:
            session.close()
    formal = [run for run in runs if not run["warmup"]]
    baseline = next((run["outputDigest"] for run in formal if run["failures"] == 0 and run["timeouts"] == 0 and run["crashed"] == 0), formal[0]["outputDigest"])
    stable = [
        run for run in formal
        if run["failures"] == 0 and run["timeouts"] == 0 and run["oom"] == 0 and run["crashed"] == 0 and run["outputDigest"] == baseline
    ]
    output_consistent = bool(formal) and all(run["outputDigest"] == baseline for run in formal if run["failures"] == 0 and run["timeouts"] == 0 and run["crashed"] == 0)
    recommendation_run = max(stable, key=lambda run: (float(run["samplesPerSecond"]), int(run["batchSize"])), default=None)
    recommendation = int(recommendation_run["batchSize"]) if recommendation_run is not None else 1
    return {
        "batch1OutputDigest": next((run["outputDigest"] for run in formal if int(run["batchSize"]) == 1), SHA256_EMPTY),
        "runs": runs,
        "recommendation": recommendation,
        "recommendationReason": (
            f"highest samplesPerSecond among stable outputs ({recommendation_run['samplesPerSecond']:.3f} samples/s)"
            if recommendation_run is not None else "no stable formal candidate; fallback to 1"
        ),
        "outputConsistent": output_consistent,
        "stableBatchSizes": sorted({int(run["batchSize"]) for run in stable}),
        "workerEvidence": hello_evidence,
    }


def _count_review_batch(samples: Sequence[DatasetSample]) -> list[dict[str, object]]:
    from anima_core.classify_protocol import ClassifyCountDecisionV1
    from anima_core.count_review_protocol import CountEvidenceV1, CountObservationV1, initial_count_review_decision

    values: list[dict[str, object]] = []
    for sample in samples:
        count = sample.annotation.get("count") if sample.annotation.get("count") in {"solo", "duo", "trio", "group"} else ""
        decision = ClassifyCountDecisionV1(
            value=count,
            baseValue=count,
            selectedSource="original_json" if count else "none",
            originalRaw=count or None,
            originalNormalized=count or None,
            wikiValue=None,
            matchedTags=(),
            conflict=False,
            issueCodes=(),
            warnings=(),
            appliedLowerBounds=(),
        )
        evidence = CountEvidenceV1.from_decision(decision)
        observation = CountObservationV1.not_requested("benchmark_nl_excluded")
        result = initial_count_review_decision(evidence, observation)
        values.append({"sampleId": sample.sample_id, "leaseId": _lease_id("countReview", sample.sample_id), **result.to_dict()})
    return values


def _run_count_review_candidate(
    samples: tuple[DatasetSample, ...],
    batch_size: int,
    formal_runs: int,
    *,
    dataset_root: Path,
    temp_root: Path,
    overlay_roots: list[object] | None = None,
) -> dict[str, object]:
    """Measure the real persisted Count Review application lifecycle.

    This remains benchmark-only code. Each run gets a fresh SQLite state file
    and sibling overlay so a successful write is exercised from lease claim
    through prepared artifact commit and decision application.
    """
    from types import SimpleNamespace

    from anima_core.classify_protocol import ClassifyCountDecisionV1
    from anima_core.contracts import JobConfig
    from anima_core.count_review_overlay import CountReviewOverlayWriter
    from anima_core.count_review_protocol import CountEvidenceV1, CountObservationV1
    from anima_core.count_review_runner import CountReviewRunner
    from anima_core.db import StateDatabase
    from anima_core.overlay import BaselineView, OverlayLayout, WorkingAnnotationView
    from anima_core.path_safety import windows_key
    from anima_core.scheduler import BoundedScheduler

    def _job_row(job_id: str, config: JobConfig, overlay_root: Path) -> dict[str, object]:
        return {
            "job_id": job_id,
            "config_schema_version": config.schemaVersion,
            "config_json": json.dumps(config.to_dict(), ensure_ascii=False),
            "config_hash": config.config_hash,
            "work_mode": "in_place",
            "overwrite_mode": "incremental",
            "source_root": str(dataset_root),
            "output_root": None,
            "dataset_root": str(dataset_root),
            "dataset_root_key": windows_key(dataset_root),
            "manifest_schema_version": 1,
            "recursive": 0,
            "sample_count": len(samples),
            "manifest_generated_at": "2026-08-28T00:00:00Z",
            "status": "ready",
            "current_module_id": None,
            "last_event_id": 0,
            "pinned": 0,
            "api_budget_extra": 0,
            "api_budget_revision": 0,
            "overlay_root": str(overlay_root),
            "commit_journal_path": None,
            "resume_status": None,
            "created_at": "2026-08-28T00:00:00Z",
            "started_at": None,
            "cancel_requested_at": None,
            "finished_at": None,
        }

    def _insert_count_inputs(database: StateDatabase, job_id: str) -> None:
        now = "2026-08-28T00:00:00Z"
        for sample in samples:
            count = sample.annotation.get("count")
            count = count if count in {"solo", "duo", "trio", "group"} else "solo"
            decision = ClassifyCountDecisionV1(
                value=str(count),
                baseValue=str(count),
                selectedSource="original_json",
                originalRaw=str(count),
                originalNormalized=str(count),
                wikiValue=None,
                matchedTags=(),
                conflict=False,
                issueCodes=(),
                warnings=(),
                appliedLowerBounds=(),
            )
            evidence = CountEvidenceV1.from_decision(decision)
            observation = CountObservationV1.not_requested("benchmark_nl_excluded")
            database.connection.execute(
                """INSERT INTO count_evidence(
                       job_id,sample_id,schema_version,value,decision_json,
                       review_warning_codes_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (job_id, sample.sample_id, 1, evidence.value, evidence.decision_json,
                 evidence.review_warning_codes_json, now, now),
            )
            database.connection.execute(
                """INSERT INTO count_observations(
                       job_id,sample_id,schema_version,status,count_value,layout_value,
                       same_character_repeated,warning_codes_json,not_requested_reason,
                       created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, sample.sample_id, 1, observation.status, observation.countValue,
                 observation.layoutValue, None, observation.warning_codes_json,
                 observation.notRequestedReason, now, now),
            )

    def _run_once(run_index: int) -> dict[str, object]:
        job_id = f"bench-countReview-{batch_size}-{run_index}"
        overlay = OverlayLayout.create(dataset_root, job_id)
        if overlay_roots is not None:
            overlay_roots.append(str(overlay.root))
        state_path = temp_root / f"count-review-{batch_size}-{run_index}.db"
        database = StateDatabase.open(state_path)
        sampler = _ResourceSampler(SimpleNamespace(pid=os.getpid()))
        try:
            config = JobConfig(
                workMode="in_place",
                overwriteMode="incremental",
                sourceRoot=str(dataset_root),
                moduleBatchSize={
                    "caption": 4, "classify": 128, "replace": 128, "ocr": 4, "nl": 3,
                    "countReview": batch_size, "dropout": 4, "tokenBudget": 128, "export": 500,
                },
            )
            database.insert_job(_job_row(job_id, config, overlay.root))
            database.insert_samples(job_id, [
                {
                    "sample_id": sample.sample_id,
                    "relative_image_path": sample.relative_path,
                    "annotation_key": sample.annotation_key,
                    "source": "e621",
                    "in_processing_scope": True,
                    "image_format": sample.image_format,
                    "image_frame_count": 1,
                    "original_txt_state": "missing_or_blank",
                    "original_json_state": "nonblank",
                    "image_file_id": sample.image_file_id,
                    "image_size": sample.image_size,
                    "image_mtime_ns": sample.image_mtime_ns,
                }
                for sample in samples
            ])
            for module_id in ("caption", "classify", "replace", "ocr", "nl"):
                database.initialize_module_summary(job_id, module_id, total=len(samples), status="completed")
            database.connection.execute(
                "UPDATE sample_state SET current_module_id='nl',status='completed' WHERE job_id=?",
                (job_id,),
            )
            _insert_count_inputs(database, job_id)
            lease_ids = iter(f"count-review-{batch_size}-{run_index}-{index}" for index in range(1, len(samples) + 1))
            scheduler = BoundedScheduler(database, lease_id_factory=lease_ids.__next__)
            scheduler.start_module(job_id, "count_review", enabled=True)
            writer = CountReviewOverlayWriter(
                database,
                overlay,
                WorkingAnnotationView(BaselineView(dataset_root), overlay),
                job_id,
            )
            runner = CountReviewRunner(
                database,
                scheduler,
                writer,
                job_id=job_id,
                worker_instance_id=f"count-review-benchmark-{run_index}",
            )
            sampler.start()
            started = time.perf_counter()
            outcome = runner.run()
            elapsed = max(time.perf_counter() - started, 1e-9)
            if outcome != "completed":
                raise RuntimeError(f"count review runner ended in {outcome}")
            metrics = sampler.snapshot(reset=True)
            outputs: list[dict[str, object]] = []
            for sample in samples:
                output = overlay.annotation_path(sample.annotation_key, ".json").read_bytes()
                parsed = json.loads(output.decode("utf-8"))
                outputs.append({
                    "sampleId": sample.sample_id,
                    "sha256": hashlib.sha256(output).hexdigest(),
                    "count": parsed.get("count"),
                })
            return {
                "batchSize": batch_size,
                "warmup": run_index == 0,
                "totalSeconds": elapsed,
                "samplesPerSecond": len(samples) / elapsed,
                "cpuPercent": metrics.cpu_percent,
                "peakMemoryBytes": metrics.peak_memory_bytes,
                "gpuUtilizationPercent": metrics.gpu_utilization_percent,
                "peakVramBytes": metrics.peak_vram_bytes,
                "failures": 0,
                "timeouts": 0,
                "oom": 0,
                "crashed": 0,
                "outputDigest": _outcome_digest(outputs),
                "outputCount": len(outputs),
                "failureDetails": [],
            }
        finally:
            sampler.close()
            metrics = sampler.snapshot()
            database.close()
            if state_path.exists():
                state_path.unlink()
            if overlay.root.exists():
                overlay.discard()

    runs: list[dict[str, object]] = []
    for run_index in range(formal_runs + 1):
        try:
            run = _run_once(run_index)
        except Exception as exc:
            run = _error_run(
                batch_size,
                warmup=run_index == 0,
                error=f"count_review_runner_failed:{exc}",
                metrics=_ResourceMetrics(),
                crashed=1,
            )
        runs.append(run)
    formal = [run for run in runs if not run["warmup"]]
    baseline = next((str(run["outputDigest"]) for run in formal if run["failures"] == 0), SHA256_EMPTY)
    stable = [
        run for run in formal
        if run["failures"] == 0 and run["timeouts"] == 0 and run["oom"] == 0
        and run["crashed"] == 0 and run["outputDigest"] == baseline
    ]
    chosen = max(stable, key=lambda run: (float(run["samplesPerSecond"]), int(run["batchSize"])), default=None)
    return {
        "batch1OutputDigest": baseline if batch_size == 1 else SHA256_EMPTY,
        "runs": runs,
        "recommendation": int(chosen["batchSize"]) if chosen is not None else 1,
        "recommendationReason": "highest samplesPerSecond among deterministic CountReviewRunner applications" if chosen is not None else "no stable formal candidate; fallback to 1",
        "outputConsistent": bool(stable) and len(stable) == len(formal),
        "stableBatchSizes": sorted({int(run["batchSize"]) for run in stable}),
        "workerEvidence": {"implementation": "count-review-runner", "sqliteLifecycle": True},
    }


def _device_snapshot() -> dict[str, object]:
    try:
        from anima_core.device_recommendation import DeviceRecommendationService

        facts = DeviceRecommendationService().probe()
        return {
            "cpuPhysicalCores": facts.cpu_physical_cores,
            "cpuLogicalCores": facts.cpu_logical_cores,
            "gpu": {
                "available": facts.gpu.available, "name": facts.gpu.name,
                "totalVramBytes": facts.gpu.total_vram_bytes, "freeVramBytes": facts.gpu.free_vram_bytes,
                "probeSource": facts.gpu.probe_source,
            },
            "probeErrors": list(facts.probe_errors),
        }
    except Exception as exc:
        logical = max(1, int(os.cpu_count() or 1))
        return {
            "cpuPhysicalCores": 1,
            "cpuLogicalCores": logical,
            "gpu": {"available": False, "name": None, "totalVramBytes": None, "freeVramBytes": None, "probeSource": "unavailable"},
            "probeErrors": [f"device_probe_failed:{exc}"],
        }


def _module_baseline_rows(report: Mapping[str, object]) -> list[dict[str, object]]:
    device = report.get("device")
    device = device if isinstance(device, Mapping) else {}
    gpu = device.get("gpu") if isinstance(device.get("gpu"), Mapping) else {}
    rows: list[dict[str, object]] = []
    for module in BENCHMARK_MODULES:
        result = report["modules"][module]  # type: ignore[index]
        assert isinstance(result, Mapping)
        evidence = result.get("workerEvidence") if isinstance(result.get("workerEvidence"), Mapping) else {}
        gpu_used = _worker_evidence_uses_gpu(evidence)
        rows.append({
            "module": module,
            "minPhysicalCores": int(device.get("cpuPhysicalCores", 1)),
            "minLogicalCores": int(device.get("cpuLogicalCores", 1)),
            "gpuRequired": gpu_used,
            "minTotalVramBytes": int(gpu.get("totalVramBytes") or 0) if gpu_used else 0,
            "minFreeVramBytes": int(gpu.get("freeVramBytes") or 0) if gpu_used else 0,
            "stableBatchSize": int(result["recommendation"]),
            "reason": str(result.get("recommendationReason", "validated benchmark candidate")),
        })
    return rows


def _worker_evidence_uses_gpu(evidence: Mapping[str, object]) -> bool:
    provider = evidence.get("provider")
    if isinstance(provider, str) and "cuda" in provider.casefold():
        return True
    for key in ("device", "observedDevice"):
        value = evidence.get(key)
        if isinstance(value, str) and value.casefold() == "cuda":
            return True
    runtime_id = evidence.get("runtimeId")
    return isinstance(runtime_id, str) and runtime_id.casefold().endswith("-gpu")


def _create_overlay(dataset_root: Path, module: str, batch_size: int) -> Path:
    from anima_core.overlay import OverlayLayout

    job_id = f"bench-{module}-{batch_size}"
    return OverlayLayout.create(dataset_root, job_id).root


def run_benchmark(
    *,
    dataset_root: Path,
    install_root: Path,
    resource_root: Path,
    state_path: Path,
    report_path: Path,
    formal_runs: int = 3,
    timeout_seconds: float = 600.0,
) -> dict[str, object]:
    if formal_runs < 3:
        raise ValueError("formal_runs must be at least 3")
    dataset_root = dataset_root.resolve(strict=True)
    install_root = install_root.resolve(strict=True)
    resource_root = resource_root.resolve(strict=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    before = snapshot_dataset(dataset_root)
    samples = collect_samples(dataset_root, before)
    state: dict[str, object] = {
        "schemaVersion": 1, "benchmarkVersion": "module-batching-v1", "status": "running",
        "dataset": {"root": str(dataset_root), "before": before}, "modules": {}, "nlRequests": 0,
        "overlayRoots": [],
    }
    _atomic_json_write(state_path, state)
    modules: dict[str, object] = {}
    try:
        for module in BENCHMARK_MODULES:
            candidates: dict[str, object] = {}
            for batch_size in candidate_batches_for(module):
                overlay_root: Path | None = None
                try:
                    if module in {"dropout", "export"}:
                        overlay_root = _create_overlay(dataset_root, module, batch_size)
                        state["overlayRoots"] = [*state["overlayRoots"], str(overlay_root)]  # type: ignore[index]
                        _atomic_json_write(state_path, state)
                    if module == "countReview":
                        result = _run_count_review_candidate(
                            samples,
                            batch_size,
                            formal_runs,
                            dataset_root=dataset_root,
                            temp_root=state_path.parent,
                            overlay_roots=state["overlayRoots"],  # type: ignore[arg-type]
                        )
                    else:
                        result = _run_worker_candidate(
                            module, samples, batch_size, dataset_root=dataset_root,
                            resource_root=resource_root, install_root=install_root,
                            overlay_root=overlay_root, formal_runs=formal_runs,
                            timeout_seconds=timeout_seconds,
                        )
                    candidates[str(batch_size)] = result
                except Exception as exc:
                    candidates[str(batch_size)] = {
                        "batch1OutputDigest": _json_digest({"error": str(exc)}),
                        "runs": [_error_run(batch_size, warmup=warmup, error=str(exc), metrics=_ResourceMetrics(), crashed=1) for warmup in (True, *([False] * formal_runs))],
                        "recommendation": 1, "recommendationReason": f"candidate failed: {exc}",
                        "outputConsistent": False, "stableBatchSizes": [], "workerEvidence": {},
                    }
                finally:
                    if overlay_root is not None and overlay_root.exists():
                        shutil.rmtree(overlay_root, ignore_errors=False)
                    state["modules"] = {**modules, module: {"candidates": candidates}}  # type: ignore[index]
                    _atomic_json_write(state_path, state)
            baseline_candidate = candidates.get("1")
            baseline_digest: str | None = None
            if isinstance(baseline_candidate, Mapping):
                baseline_formal = _formal_runs(baseline_candidate)
                baseline_digest = next(
                    (str(run["outputDigest"]) for run in baseline_formal if _is_sha256(run.get("outputDigest"))),
                    None,
                )
            recommendation, stable_batches, recommendation_reason = select_stable_recommendation(
                candidates,
                baseline_digest=baseline_digest,
            )
            selected_candidate = candidates.get(str(recommendation))
            if not isinstance(selected_candidate, Mapping):
                selected_candidate = baseline_candidate if isinstance(baseline_candidate, Mapping) else {}
            modules[module] = {
                "candidates": candidates,
                "batch1OutputDigest": baseline_digest or SHA256_EMPTY,
                "recommendation": recommendation,
                "recommendationReason": recommendation_reason,
                "stableBatchSizes": stable_batches,
                "workerEvidence": selected_candidate.get("workerEvidence", {}),
            }
            state["modules"] = modules
            _atomic_json_write(state_path, state)
        after = snapshot_dataset(dataset_root)
        flattened: dict[str, object] = {}
        for module, value in modules.items():
            assert isinstance(value, Mapping)
            candidates = value.get("candidates")
            assert isinstance(candidates, Mapping)
            recommended = int(value.get("recommendation", 1))
            all_runs: list[Mapping[str, object]] = []
            for candidate in candidates.values():
                if isinstance(candidate, Mapping):
                    candidate_runs = candidate.get("runs")
                    if isinstance(candidate_runs, list):
                        all_runs.extend(run for run in candidate_runs if isinstance(run, Mapping))
            flattened[module] = {
                "batch1OutputDigest": value.get("batch1OutputDigest", SHA256_EMPTY),
                "runs": all_runs,
                "recommendation": recommended,
                "recommendationReason": value.get("recommendationReason", ""),
                "outputConsistent": bool(value.get("stableBatchSizes")),
                "stableBatchSizes": value.get("stableBatchSizes", []),
                "workerEvidence": value.get("workerEvidence", {}),
                "candidates": candidates,
            }
        report: dict[str, object] = {
            "schemaVersion": 1,
            "benchmarkVersion": "module-batching-v1",
            "status": "validated",
            "dataset": {"root": str(dataset_root), "before": before, "after": after},
            "sampleCount": len(samples),
            "device": _device_snapshot(),
            "nlRequests": 0,
            "modules": flattened,
            "benchmarkConstraints": {
                "validationSetReadOnly": True,
                "overlayPolicy": "benchmark-only sibling overlay under E:\\Desktop; production path safety unchanged",
                "formalRunsPerCandidate": formal_runs,
                "candidateBatches": {module: list(candidate_batches_for(module)) for module in BENCHMARK_MODULES},
                "failurePolicy": {
                    "zeroFailuresRequired": False,
                    "batchOneDeterministicFixtureFailuresMayBeBaseline": True,
                    "transportTimeoutOomCrashDisqualifyCandidate": True,
                    "failureDetailsAreBounded": True,
                },
            },
            "baselineVersion": BENCHMARK_BASELINE_VERSION,
        }
        validate_report(report)
        _atomic_json_write(report_path, report)
        _finalize_state(
            state_path,
            state,
            dataset_root=dataset_root,
            before=before,
            after=after,
            report_path=report_path,
        )
        return report
    finally:
        for path_value in state.get("overlayRoots", []):
            path = Path(str(path_value))
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)


def update_baseline_from_report(report: Mapping[str, object], baseline_path: Path) -> None:
    payload = {
        "schemaVersion": 1,
        "baselineVersion": str(report.get("baselineVersion") or BENCHMARK_BASELINE_VERSION),
        "status": "validated",
        "dataset": str(report.get("dataset", {}).get("root", "")) if isinstance(report.get("dataset"), Mapping) else "",
        "rows": _module_baseline_rows(report),
    }
    _atomic_json_write(baseline_path, payload)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark v10 module batching on the read-only validation set.")
    parser.add_argument("--dataset", type=Path, default=Path(r"E:\Desktop\10_uiokv"))
    parser.add_argument("--install-root", type=Path, default=ROOT / ".runtime-build")
    parser.add_argument("--resource-root", type=Path, default=Path(r"E:\Desktop\Anima idg标准标注处理\resource-library"))
    parser.add_argument("--temp-root", type=Path, default=ROOT / ".test-tmp")
    parser.add_argument("--formal-runs", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--keep-artifacts", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    temp_root = arguments.temp_root.resolve()
    state_path = temp_root / "module-batch-benchmark-state.json"
    report_path = temp_root / "module-batch-benchmark-report.json"
    try:
        report = run_benchmark(
            dataset_root=arguments.dataset,
            install_root=arguments.install_root,
            resource_root=arguments.resource_root,
            state_path=state_path,
            report_path=report_path,
            formal_runs=arguments.formal_runs,
            timeout_seconds=arguments.timeout_seconds,
        )
        update_baseline_from_report(report, ROOT / "core" / "src" / "anima_core" / "benchmark_baseline_v1.json")
        summary = {
            "reportPath": str(report_path),
            "sampleCount": report["sampleCount"],
            "datasetUnchanged": report["dataset"]["before"] == report["dataset"]["after"],  # type: ignore[index]
            "recommendations": {module: report["modules"][module]["recommendation"] for module in BENCHMARK_MODULES},  # type: ignore[index]
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not arguments.keep_artifacts:
            for path in (state_path, report_path):
                if path.exists():
                    path.unlink()
            if temp_root.exists() and not any(temp_root.iterdir()):
                temp_root.rmdir()
        return 0
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
