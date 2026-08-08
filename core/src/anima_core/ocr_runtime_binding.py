"""Immutable task-owned OCR execution requests and runtime bindings."""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


GIB = 1024 ** 3
_SHA256_LENGTH = 64
_REQUEST_SCHEMA_VERSION = 1
_BINDING_SCHEMA_VERSION = 1


class OcrExecutionError(ValueError):
    pass


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise OcrExecutionError(f"{field} must be a lowercase SHA-256")
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise OcrExecutionError(f"{field} is invalid")
    return value


def _limit(value: object, field: str = "textDetLimitSideLen") -> int:
    result = _integer(value, field, 1920, 3840)
    if result % 32:
        raise OcrExecutionError(f"{field} must be divisible by 32")
    return result


def _batch(value: object, field: str = "textBatchSize") -> int:
    return _integer(value, field, 1, 8)


@dataclass(frozen=True)
class OcrExecutionTuningV1:
    mode: Literal["auto", "manual"]
    value: int | None

    @classmethod
    def from_dict(cls, value: object, field: str) -> "OcrExecutionTuningV1":
        if not isinstance(value, dict) or set(value) != {"mode", "value"}:
            raise OcrExecutionError(f"{field} is invalid")
        mode = value.get("mode")
        raw = value.get("value")
        if mode == "auto" and raw is None:
            return cls("auto", None)
        if mode != "manual":
            raise OcrExecutionError(f"{field} mode is invalid")
        validator = _limit if field == "textDetLimitSideLen" else _batch
        return cls("manual", validator(raw, field))

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "value": self.value}


@dataclass(frozen=True)
class OcrExecutionRequestV1:
    textDetLimitSideLen: OcrExecutionTuningV1
    textBatchSize: OcrExecutionTuningV1

    @classmethod
    def auto(cls) -> "OcrExecutionRequestV1":
        return cls(OcrExecutionTuningV1("auto", None), OcrExecutionTuningV1("auto", None))

    @classmethod
    def from_dict(cls, value: object) -> "OcrExecutionRequestV1":
        if not isinstance(value, dict) or set(value) != {"textDetLimitSideLen", "textBatchSize"}:
            raise OcrExecutionError("ocrExecution is invalid")
        return cls(
            OcrExecutionTuningV1.from_dict(value["textDetLimitSideLen"], "textDetLimitSideLen"),
            OcrExecutionTuningV1.from_dict(value["textBatchSize"], "textBatchSize"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "textDetLimitSideLen": self.textDetLimitSideLen.to_dict(),
            "textBatchSize": self.textBatchSize.to_dict(),
        }


def normalize_ocr_execution(value: object) -> OcrExecutionRequestV1:
    if value is None:
        return OcrExecutionRequestV1.auto()
    if not isinstance(value, dict):
        raise OcrExecutionError("ocrExecution must be an object")
    unknown = set(value) - {"textDetLimitSideLen", "textBatchSize"}
    if unknown:
        raise OcrExecutionError("ocrExecution contains unknown fields")
    return OcrExecutionRequestV1(
        OcrExecutionTuningV1.from_dict(value.get("textDetLimitSideLen", {"mode": "auto", "value": None}), "textDetLimitSideLen"),
        OcrExecutionTuningV1.from_dict(value.get("textBatchSize", {"mode": "auto", "value": None}), "textBatchSize"),
    )


@dataclass(frozen=True)
class RecommendedTuning:
    textDetLimitSideLen: int
    textBatchSize: int
    source: Literal["cpu", "gpu_vram_table", "unavailable_fallback"]


def recommend_tuning(*, device: str, total_vram_bytes: object) -> RecommendedTuning:
    if device == "cpu":
        return RecommendedTuning(1920, 1, "cpu")
    if device != "cuda":
        raise OcrExecutionError("requested OCR device is invalid")
    if isinstance(total_vram_bytes, bool) or not isinstance(total_vram_bytes, (int, float)):
        return RecommendedTuning(1920, 1, "unavailable_fallback")
    vram = float(total_vram_bytes)
    if not math.isfinite(vram) or vram < 0 or int(vram) != vram:
        return RecommendedTuning(1920, 1, "unavailable_fallback")
    if vram < 12 * GIB:
        return RecommendedTuning(1920, 1, "gpu_vram_table")
    if vram < 24 * GIB:
        return RecommendedTuning(2304, 2, "gpu_vram_table")
    return RecommendedTuning(2560, 4, "gpu_vram_table")


def _requested(value: object) -> tuple[Literal["auto", "cuda", "cpu"], OcrExecutionRequestV1]:
    if not isinstance(value, dict) or set(value) != {"device", "textDetLimitSideLen", "textBatchSize"}:
        raise OcrExecutionError("OCR binding requested values are invalid")
    device = value.get("device")
    if device not in {"auto", "cuda", "cpu"}:
        raise OcrExecutionError("OCR binding requested device is invalid")
    return device, OcrExecutionRequestV1.from_dict({
        "textDetLimitSideLen": value["textDetLimitSideLen"],
        "textBatchSize": value["textBatchSize"],
    })  # type: ignore[return-value]


@dataclass(frozen=True)
class OcrRuntimeBindingV1:
    requestedDevice: Literal["auto", "cuda", "cpu"]
    request: OcrExecutionRequestV1
    recommended: RecommendedTuning
    totalVramBytes: int | None
    effectiveTextDetLimitSideLen: int
    effectiveTextBatchSize: int
    runtimeId: Literal["ocr-paddle", "ocr-paddle-gpu"]
    runtimeFingerprint: str
    observedDevice: Literal["cpu", "cuda"]
    paddleVersion: str
    compiledWithCuda: bool
    cudaVersion: str | None
    gpuName: str | None
    resourceFingerprint: str
    startupReason: Literal["gpu_runtime_unavailable"] | None

    @classmethod
    def from_dict(cls, value: object) -> "OcrRuntimeBindingV1":
        if not isinstance(value, dict) or set(value) - {
            "schemaVersion", "requested", "recommended", "effective", "runtime", "resourceFingerprint", "startupReason",
        } or {"schemaVersion", "requested", "recommended", "effective", "runtime", "resourceFingerprint"} - set(value) or value.get("schemaVersion") != _BINDING_SCHEMA_VERSION:
            raise OcrExecutionError("OCR runtime binding is invalid")
        requested_device, request = _requested(value["requested"])
        recommended = value["recommended"]
        effective = value["effective"]
        runtime = value["runtime"]
        if not isinstance(recommended, dict) or set(recommended) != {
            "source", "totalVramBytes", "textDetLimitSideLen", "textBatchSize",
        }:
            raise OcrExecutionError("OCR binding recommendation is invalid")
        source = recommended.get("source")
        if source not in {"cpu", "gpu_vram_table", "unavailable_fallback"}:
            raise OcrExecutionError("OCR binding recommendation source is invalid")
        total_vram = recommended.get("totalVramBytes")
        if total_vram is not None:
            total_vram = _integer(total_vram, "totalVramBytes", 0, 2 ** 63 - 1)
        recommendation = RecommendedTuning(
            _limit(recommended.get("textDetLimitSideLen"), "recommended textDetLimitSideLen"),
            _batch(recommended.get("textBatchSize"), "recommended textBatchSize"),
            source,
        )
        if not isinstance(effective, dict) or set(effective) != {"textDetLimitSideLen", "textBatchSize"}:
            raise OcrExecutionError("OCR binding effective tuning is invalid")
        effective_limit = _limit(effective.get("textDetLimitSideLen"), "effective textDetLimitSideLen")
        effective_batch = _batch(effective.get("textBatchSize"), "effective textBatchSize")
        if request.textDetLimitSideLen.mode == "manual" and effective_limit != request.textDetLimitSideLen.value:
            raise OcrExecutionError("OCR binding manual detection limit is invalid")
        if request.textBatchSize.mode == "manual" and effective_batch != request.textBatchSize.value:
            raise OcrExecutionError("OCR binding manual text batch is invalid")
        if request.textDetLimitSideLen.mode == "auto" and effective_limit != recommendation.textDetLimitSideLen:
            raise OcrExecutionError("OCR binding automatic detection limit is invalid")
        if request.textBatchSize.mode == "auto" and effective_batch != recommendation.textBatchSize:
            raise OcrExecutionError("OCR binding automatic text batch is invalid")
        if not isinstance(runtime, dict) or set(runtime) != {
            "runtimeId", "runtimeFingerprint", "observedDevice", "paddleVersion", "compiledWithCuda", "cudaVersion", "gpuName",
        }:
            raise OcrExecutionError("OCR binding runtime is invalid")
        runtime_id = runtime.get("runtimeId")
        observed = runtime.get("observedDevice")
        compiled = runtime.get("compiledWithCuda")
        if runtime_id not in {"ocr-paddle", "ocr-paddle-gpu"} or observed not in {"cpu", "cuda"} or type(compiled) is not bool:
            raise OcrExecutionError("OCR binding runtime identity is invalid")
        cuda_version = runtime.get("cudaVersion")
        gpu_name = runtime.get("gpuName")
        if observed == "cpu":
            if runtime_id != "ocr-paddle" or compiled or cuda_version is not None or gpu_name is not None:
                raise OcrExecutionError("OCR binding CPU runtime evidence is invalid")
        elif runtime_id != "ocr-paddle-gpu" or not compiled or not isinstance(cuda_version, str) or not cuda_version or not isinstance(gpu_name, str) or not gpu_name:
            raise OcrExecutionError("OCR binding CUDA runtime evidence is invalid")
        if requested_device == "cpu" and observed != "cpu":
            raise OcrExecutionError("OCR binding requested device is incompatible")
        if requested_device == "cuda" and observed != "cuda":
            raise OcrExecutionError("OCR binding requested device is incompatible")
        paddle_version = runtime.get("paddleVersion")
        if not isinstance(paddle_version, str) or not paddle_version or len(paddle_version.encode("utf-8")) > 128:
            raise OcrExecutionError("OCR binding Paddle version is invalid")
        startup_reason = value.get("startupReason")
        if startup_reason not in {None, "gpu_runtime_unavailable"}:
            raise OcrExecutionError("OCR binding startup reason is invalid")
        return cls(
            requested_device, request, recommendation, total_vram, effective_limit, effective_batch,
            runtime_id, _sha256(runtime.get("runtimeFingerprint"), "runtimeFingerprint"), observed,
            paddle_version, compiled, cuda_version, gpu_name, _sha256(value.get("resourceFingerprint"), "resourceFingerprint"), startup_reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": _BINDING_SCHEMA_VERSION,
            "requested": {
                "device": self.requestedDevice,
                **self.request.to_dict(),
            },
            "recommended": {
                "source": self.recommended.source,
                "totalVramBytes": self.totalVramBytes,
                "textDetLimitSideLen": self.recommended.textDetLimitSideLen,
                "textBatchSize": self.recommended.textBatchSize,
            },
            "effective": {
                "textDetLimitSideLen": self.effectiveTextDetLimitSideLen,
                "textBatchSize": self.effectiveTextBatchSize,
            },
            "runtime": {
                "runtimeId": self.runtimeId,
                "runtimeFingerprint": self.runtimeFingerprint,
                "observedDevice": self.observedDevice,
                "paddleVersion": self.paddleVersion,
                "compiledWithCuda": self.compiledWithCuda,
                "cudaVersion": self.cudaVersion,
                "gpuName": self.gpuName,
            },
            "resourceFingerprint": self.resourceFingerprint,
            "startupReason": self.startupReason,
        }


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OcrExecutionError(f"{label} is missing or invalid") from exc


def _write_immutable(path: Path, value: dict[str, object], label: str) -> None:
    payload = _canonical_bytes(value)
    if path.exists():
        try:
            if _canonical_bytes(_read_json(path, label)) == payload:
                return
        except OcrExecutionError:
            pass
        raise OcrExecutionError(f"existing {label} does not match")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            if _canonical_bytes(_read_json(path, label)) == payload:
                temporary.unlink(missing_ok=True)
                return
            raise OcrExecutionError(f"existing {label} does not match")
        os.replace(temporary, path)
    except OcrExecutionError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise OcrExecutionError(f"{label} could not be written") from exc


def write_execution_request(path: str | Path, request: OcrExecutionRequestV1) -> None:
    target = Path(path)
    _write_immutable(target, {"schemaVersion": _REQUEST_SCHEMA_VERSION, **request.to_dict()}, "OCR execution request")


def read_execution_request(path: str | Path) -> OcrExecutionRequestV1:
    value = _read_json(Path(path), "OCR execution request")
    if not isinstance(value, dict) or value.get("schemaVersion") != _REQUEST_SCHEMA_VERSION:
        raise OcrExecutionError("OCR execution request is invalid")
    return OcrExecutionRequestV1.from_dict({
        "textDetLimitSideLen": value.get("textDetLimitSideLen"),
        "textBatchSize": value.get("textBatchSize"),
    })


def write_runtime_binding(path: str | Path, binding: OcrRuntimeBindingV1) -> None:
    _write_immutable(Path(path), binding.to_dict(), "OCR runtime binding")


def read_runtime_binding(path: str | Path) -> OcrRuntimeBindingV1:
    return OcrRuntimeBindingV1.from_dict(_read_json(Path(path), "OCR runtime binding"))
