from __future__ import annotations

import hashlib, json, os, re
from pathlib import Path
from collections.abc import Sequence

from anima_caption_format import normalize_annotation, serialize_flat_txt
from anima_caption_format.flat_txt import FlatTextSerializationError
from .protocol import ExportHelloV1, ExportProtocolError, ExportWorkItemV1, parse_hello

_SAFE_LEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

class ExportWorkerError(RuntimeError):
    def __init__(self, code: str, message: str): super().__init__(message); self.code = code

def _within(root: Path, relative: str) -> Path:
    candidate = (root / Path(relative.replace("\\", os.sep))).resolve()
    if os.path.commonpath((str(root), str(candidate))) != str(root): raise ExportWorkerError("export_path_invalid", "path escaped its root")
    return candidate

def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as destination: destination.write(data); destination.flush(); os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()
    return hashlib.sha256(path.read_bytes()).hexdigest()

class ExportWorker:
    def __init__(self) -> None: self.hello: ExportHelloV1 | None = None; self.dataset_root: Path | None = None; self.overlay_root: Path | None = None
    def initialize(self, payload: object) -> dict[str, object]:
        try:
            hello = parse_hello(payload); dataset = Path(hello.dataset_root).resolve(strict=True); overlay = Path(hello.overlay_root).resolve(strict=True)
            marker = json.loads((overlay / "overlay-manifest.json").read_text(encoding="utf-8"))
            if marker.get("schemaVersion") != 1 or marker.get("jobId") != hello.job_id or Path(str(marker.get("datasetRoot", ""))).resolve(strict=True) != dataset: raise ValueError("overlay identity mismatch")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ExportProtocolError) as exc: raise ExportWorkerError("export_initialization_failed", str(exc)) from exc
        self.hello, self.dataset_root, self.overlay_root = hello, dataset, overlay
        return {"schemaVersion": 1, "payloadType": "export_hello_result", "ready": True}
    def _source(self, item: ExportWorkItemV1) -> Path:
        assert self.dataset_root and self.overlay_root
        overlay = _within(self.overlay_root / "annotations", item.annotation_key + ".json")
        return overlay if overlay.is_file() else _within(self.dataset_root, item.annotation_key + ".json")
    def process(self, items: Sequence[ExportWorkItemV1]) -> dict[str, object]:
        if self.hello is None or not items: raise ExportWorkerError("export_protocol_violation", "worker is not initialized")
        outcomes=[]
        for item in items:
            try: raw = self._source(item).read_bytes()
            except FileNotFoundError: raw = b""
            except OSError as exc: outcomes.append({"schemaVersion":1,"status":"issue","sampleId":item.sample_id,"leaseId":item.lease_id,"relativeImagePath":item.relative_image_path,"code":"final_json_invalid","fieldErrors":[{"code":"json_read_failed"}]}); continue
            result = normalize_annotation(raw, self.hello.caption_policy, export_format=self.hello.format)
            if not result.valid:
                outcomes.append({"schemaVersion":1,"status":"issue","sampleId":item.sample_id,"leaseId":item.lease_id,"relativeImagePath":item.relative_image_path,"code":"final_json_invalid","fieldErrors":[{"field":error.field,"code":error.code} for error in result.field_errors]}); continue
            if not _SAFE_LEASE.fullmatch(item.lease_id): raise ExportWorkerError("export_protocol_violation", "unsafe lease")
            try: text = serialize_flat_txt(result.payload, self.hello.caption_policy) if self.hello.format != "json" else None
            except FlatTextSerializationError:
                outcomes.append({"schemaVersion":1,"status":"issue","sampleId":item.sample_id,"leaseId":item.lease_id,"relativeImagePath":item.relative_image_path,"code":"final_json_invalid","fieldErrors":[{"code":"tag_not_flat_txt_representable"}]}); continue
            artifacts=[]
            for kind, data in (("json", result.json_bytes), ("txt", text)):
                if data is None or (kind == "json" and self.hello.format == "flat_txt"): continue
                relative=f"prepared\\export\\{item.lease_id}.{kind}"; artifacts.append({"kind":kind,"relativePath":relative,"sha256":_write(_within(self.overlay_root, relative), data)})
            outcomes.append({"schemaVersion":1,"status":"prepared","sampleId":item.sample_id,"leaseId":item.lease_id,"relativeImagePath":item.relative_image_path,"artifacts":artifacts,"conversions":result.conversions})
        return {"schemaVersion":1,"payloadType":"export_batch_result","outcomes":outcomes}
