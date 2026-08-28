"""Deterministic device facts and versioned module batch recommendations."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .contracts import DEFAULT_MODULE_BATCH_SIZE, MODULE_BATCH_SIZE_BOUNDS


MODULE_CONFIG_KEYS = {
    "caption": "caption",
    "classify": "classify",
    "replace": "replace",
    "ocr": "ocr",
    "nl": "nl",
    "countReview": "countReview",
    "dropout": "dropout",
    "tokenBudget": "tokenBudget",
    "export": "export",
}


@dataclass(frozen=True)
class GpuFacts:
    available: bool
    name: str | None = None
    total_vram_bytes: int | None = None
    free_vram_bytes: int | None = None
    probe_source: str = "unavailable"


@dataclass(frozen=True)
class DeviceFacts:
    cpu_physical_cores: int
    cpu_logical_cores: int
    gpu: GpuFacts
    probe_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _BaselineRow:
    module: str
    min_physical_cores: int
    min_logical_cores: int
    gpu_required: bool
    min_total_vram_bytes: int
    min_free_vram_bytes: int
    stable_batch_size: int
    reason: str


def _default_cpu_probe() -> tuple[int, int]:
    logical = max(1, int(os.cpu_count() or 1))
    try:
        import psutil  # type: ignore[import-not-found]

        physical = int(psutil.cpu_count(logical=False) or logical)
    except Exception:
        physical = logical
    return max(1, physical), logical


def _parse_gpu_payload(value: object, source: str) -> GpuFacts | None:
    if not isinstance(value, Mapping):
        return None
    name = value.get("name")
    total = value.get("totalVramBytes")
    free = value.get("freeVramBytes")
    if not isinstance(name, str) or not name.strip() or type(total) is not int or total <= 0 or type(free) is not int or free < 0:
        return None
    return GpuFacts(True, name.strip(), total, min(free, total), source)


def _default_cuda_probe() -> GpuFacts | None:
    """Read an explicitly verified runtime evidence file when one is supplied."""
    path = os.environ.get("ANIMA_CUDA_RUNTIME_EVIDENCE")
    if not path:
        return None
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return _parse_gpu_payload(json.load(handle), "cuda-runtime")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _default_nvidia_smi_probe() -> GpuFacts | None:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    first = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    parts = [part.strip() for part in first.split(",")]
    if len(parts) != 3:
        return None
    try:
        total = int(float(parts[1]) * 1024 * 1024)
        free = int(float(parts[2]) * 1024 * 1024)
    except ValueError:
        return None
    return _parse_gpu_payload({"name": parts[0], "totalVramBytes": total, "freeVramBytes": free}, "nvidia-smi")


class DeviceRecommendationService:
    def __init__(
        self,
        baseline_path: Path | str | None = None,
        *,
        cpu_probe: Callable[[], tuple[int, int]] | None = None,
        cuda_probe: Callable[[], GpuFacts | None] | None = None,
        nvidia_smi_probe: Callable[[], GpuFacts | None] | None = None,
    ) -> None:
        self.baseline_path = Path(baseline_path) if baseline_path is not None else Path(__file__).with_name("benchmark_baseline_v1.json")
        self.cpu_probe = cpu_probe or _default_cpu_probe
        self.cuda_probe = cuda_probe or _default_cuda_probe
        self.nvidia_smi_probe = nvidia_smi_probe or _default_nvidia_smi_probe

    def _load_baseline(self) -> tuple[str, list[_BaselineRow]]:
        with self.baseline_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict) or document.get("schemaVersion") != 1 or not isinstance(document.get("rows"), list):
            raise ValueError("device recommendation baseline is invalid")
        rows: list[_BaselineRow] = []
        for raw in document["rows"]:
            if not isinstance(raw, dict):
                raise ValueError("device recommendation baseline row is invalid")
            row = _BaselineRow(
                module=str(raw["module"]),
                min_physical_cores=int(raw.get("minPhysicalCores", 0)),
                min_logical_cores=int(raw.get("minLogicalCores", 0)),
                gpu_required=bool(raw.get("gpuRequired", False)),
                min_total_vram_bytes=int(raw.get("minTotalVramBytes", 0)),
                min_free_vram_bytes=int(raw.get("minFreeVramBytes", 0)),
                stable_batch_size=int(raw["stableBatchSize"]),
                reason=str(raw.get("reason", "validated baseline")),
            )
            if row.module not in MODULE_CONFIG_KEYS or row.stable_batch_size < 1:
                raise ValueError("device recommendation baseline row is invalid")
            rows.append(row)
        return str(document.get("baselineVersion", "v1")), rows

    def probe(self) -> DeviceFacts:
        errors: list[str] = []
        try:
            physical, logical = self.cpu_probe()
            if type(physical) is not int or type(logical) is not int or physical < 1 or logical < 1:
                raise ValueError
        except Exception:
            physical, logical = 1, 1
            errors.append("cpu_probe_failed")
        gpu = None
        try:
            gpu = self.cuda_probe()
        except Exception:
            errors.append("cuda_probe_failed")
        if gpu is None:
            try:
                gpu = self.nvidia_smi_probe()
            except Exception:
                errors.append("nvidia_smi_probe_failed")
        if gpu is None:
            gpu = GpuFacts(False)
            errors.append("gpu_unavailable")
        return DeviceFacts(physical, logical, gpu, tuple(dict.fromkeys(errors)))

    @staticmethod
    def _matches(row: _BaselineRow, facts: DeviceFacts) -> bool:
        gpu = facts.gpu
        if facts.cpu_physical_cores < row.min_physical_cores or facts.cpu_logical_cores < row.min_logical_cores:
            return False
        if row.gpu_required and not gpu.available:
            return False
        if row.min_total_vram_bytes and (not gpu.available or gpu.total_vram_bytes is None or gpu.total_vram_bytes < row.min_total_vram_bytes):
            return False
        if row.min_free_vram_bytes and (not gpu.available or gpu.free_vram_bytes is None or gpu.free_vram_bytes < row.min_free_vram_bytes):
            return False
        return True

    def recommend(self, *, rpm: int = 60) -> dict[str, object]:
        facts = self.probe()
        try:
            baseline_version, rows = self._load_baseline()
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            baseline_version, rows = "unavailable", []
            facts = DeviceFacts(facts.cpu_physical_cores, facts.cpu_logical_cores, facts.gpu, (*facts.probe_errors, "baseline_unavailable"))
        selected: dict[str, int] = {}
        reasons: dict[str, str] = {}
        for module, config_key in MODULE_CONFIG_KEYS.items():
            candidates = [row for row in rows if row.module == module and self._matches(row, facts)]
            if candidates:
                chosen = max(candidates, key=lambda row: row.stable_batch_size)
                minimum, maximum = MODULE_BATCH_SIZE_BOUNDS[config_key]
                selected[config_key] = min(maximum, max(minimum, chosen.stable_batch_size))
                reasons[module] = chosen.reason
            else:
                selected[config_key] = 1
                reasons[module] = "no matching validated baseline; fallback to 1"
        try:
            rpm_value = max(1, min(100_000, int(rpm)))
        except (TypeError, ValueError):
            rpm_value = 60
        selected["nl"] = max(1, min(16, min(3, rpm_value)))
        reasons["nl"] = f"API RPM limit: min(3, {rpm_value})"
        return {
            "schemaVersion": 1,
            "baselineVersion": baseline_version,
            "cpuPhysicalCores": facts.cpu_physical_cores,
            "cpuLogicalCores": facts.cpu_logical_cores,
            "gpu": {
                "available": facts.gpu.available,
                "name": facts.gpu.name,
                "totalVramBytes": facts.gpu.total_vram_bytes,
                "freeVramBytes": facts.gpu.free_vram_bytes,
                "probeSource": facts.gpu.probe_source,
            },
            "moduleBatchSize": selected,
            "reasons": reasons,
            "probeErrors": list(facts.probe_errors),
        }
