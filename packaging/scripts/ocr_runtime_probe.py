"""Run OCR Paddle/ONNX parity and ONNX Runtime provider probes."""
from __future__ import annotations

import argparse
import gc
import json
import socket
from pathlib import Path
from typing import Any


MODEL_CASES = (
    ("detection", "PP-OCRv5_server_det", "text_detection", (1, 3, 64, 64)),
    ("recognition", "PP-OCRv5_server_rec", "text_recognition", (1, 3, 48, 320)),
    (
        "orientation",
        "PP-LCNet_x1_0_textline_ori",
        "textline_orientation",
        (1, 3, 48, 192),
    ),
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    parity = subparsers.add_parser("parity")
    parity.add_argument("--source-root", type=Path, required=True)
    parity.add_argument("--onnx-root", type=Path, required=True)
    parity.add_argument("--samples-root", type=Path, required=True)

    provider = subparsers.add_parser("provider")
    provider.add_argument("--onnx-root", type=Path, required=True)
    provider.add_argument(
        "--provider",
        choices=("CPUExecutionProvider", "CUDAExecutionProvider"),
        required=True,
    )
    provider.add_argument("--profile-root", type=Path, required=True)
    return parser.parse_args()


def deny_network() -> None:
    def blocked_connect(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("network access is forbidden during OCR compatibility inference")

    socket.socket.connect = blocked_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = blocked_connect  # type: ignore[method-assign]


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        return jsonable(value.tolist())
    if hasattr(value, "item"):
        return jsonable(value.item())
    return value


def result_payload(result: Any) -> dict[str, Any]:
    value = result.json
    if callable(value):
        value = value()
    if isinstance(value, str):
        value = json.loads(value)
    value = jsonable(value)
    if isinstance(value, dict) and isinstance(value.get("res"), dict):
        value = value["res"]
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected PaddleOCR result payload: {type(value).__name__}")
    return value


def axis_aligned_bbox(polygon: list[list[float]]) -> list[float]:
    if not polygon:
        raise RuntimeError("empty detection polygon")
    xs = [float(point[0]) for point in polygon]
    ys = [float(point[1]) for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def normalize_result(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "detection":
        polygons = payload.get("dt_polys", payload.get("polys", []))
        scores = payload.get("dt_scores", payload.get("scores", []))
        return {
            "boxes": [axis_aligned_bbox(polygon) for polygon in polygons],
            "scores": [float(score) for score in scores],
        }
    if kind == "recognition":
        texts = payload.get("rec_texts")
        if texts is None:
            text = payload.get("rec_text", payload.get("text", ""))
            texts = [text]
        scores = payload.get("rec_scores")
        if scores is None:
            score = payload.get("rec_score", payload.get("score"))
            scores = [] if score is None else [score]
        return {
            "texts": [str(text) for text in texts],
            "scores": [float(score) for score in scores],
        }
    if kind == "orientation":
        labels = payload.get("label_names", payload.get("labels", []))
        scores = payload.get("scores", [])
        return {
            "labels": [str(label) for label in labels],
            "scores": [float(score) for score in scores],
        }
    raise RuntimeError(f"unknown OCR model kind: {kind}")


def model_class(kind: str) -> type[Any]:
    from paddleocr import (
        TextDetection,
        TextLineOrientationClassification,
        TextRecognition,
    )

    return {
        "detection": TextDetection,
        "recognition": TextRecognition,
        "orientation": TextLineOrientationClassification,
    }[kind]


def create_paddle_model(model_type: type[Any], model_dir: Path) -> Any:
    return model_type(
        model_dir=str(model_dir),
        device="cpu",
        engine="paddle_static",
        enable_hpi=False,
    )


def create_onnx_model(model_type: type[Any], model_dir: Path) -> Any:
    return model_type(
        model_dir=str(model_dir),
        engine="onnxruntime",
        engine_config={"providers": ["CPUExecutionProvider"]},
    )


def predict_once(model: Any, sample: Path) -> dict[str, Any]:
    results = list(model.predict(str(sample), batch_size=1))
    if len(results) != 1:
        raise RuntimeError(f"expected one OCR result, got {len(results)}")
    return result_payload(results[0])


def run_parity(source_root: Path, onnx_root: Path, samples_root: Path) -> dict[str, Any]:
    outputs: dict[str, dict[str, Any]] = {"paddle": {}, "onnx": {}}
    for kind, name, sample_stem, _shape in MODEL_CASES:
        source_dir = (source_root / name).resolve(strict=True)
        converted_dir = (onnx_root / name).resolve(strict=True)
        samples = sorted(samples_root.glob(f"{sample_stem}.*"))
        if len(samples) != 1:
            raise RuntimeError(f"expected one fixed sample for {sample_stem}, got {len(samples)}")
        model_type = model_class(kind)

        paddle_model = create_paddle_model(model_type, source_dir)
        try:
            outputs["paddle"][kind] = normalize_result(
                kind,
                predict_once(paddle_model, samples[0]),
            )
        finally:
            del paddle_model
            gc.collect()

        onnx_model = create_onnx_model(model_type, converted_dir)
        try:
            outputs["onnx"][kind] = normalize_result(
                kind,
                predict_once(onnx_model, samples[0]),
            )
        finally:
            del onnx_model
            gc.collect()
    return outputs


def run_provider_probe(
    onnx_root: Path,
    provider: str,
    profile_root: Path,
) -> dict[str, Any]:
    import numpy as np
    import onnxruntime as ort

    profile_root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for _kind, name, _sample_stem, shape in MODEL_CASES:
        model_file = (onnx_root / name / "inference.onnx").resolve(strict=True)
        options = ort.SessionOptions()
        options.enable_profiling = True
        options.profile_file_prefix = str(profile_root / name)
        requested: list[Any]
        if provider == "CUDAExecutionProvider":
            requested = [("CUDAExecutionProvider", {"device_id": "0"})]
        else:
            requested = ["CPUExecutionProvider"]
        session = ort.InferenceSession(
            str(model_file),
            sess_options=options,
            providers=requested,
        )
        session.disable_fallback()
        inputs = session.get_inputs()
        if len(inputs) != 1 or inputs[0].type != "tensor(float)":
            raise RuntimeError(f"unexpected ONNX input contract for {name}")
        outputs = session.run(None, {inputs[0].name: np.zeros(shape, dtype=np.float32)})
        profile_path = Path(session.end_profiling())
        records = json.loads(profile_path.read_text(encoding="utf-8"))
        node_providers = [
            str(record.get("args", {}).get("provider"))
            for record in records
            if record.get("cat") == "Node" and record.get("args", {}).get("provider")
        ]
        expected_nodes = sum(item == provider for item in node_providers)
        if not expected_nodes:
            raise RuntimeError(f"no {name} node executed with expected provider {provider}")
        results[name] = {
            "requestedProvider": provider,
            "sessionProviders": session.get_providers(),
            "nodeEvents": expected_nodes,
            "observedProviders": sorted(set(node_providers)),
            "outputShapes": [list(output.shape) for output in outputs],
        }
        del session
    return results


def main() -> int:
    arguments = parse_arguments()
    deny_network()
    if arguments.command == "parity":
        result = run_parity(
            arguments.source_root.resolve(strict=True),
            arguments.onnx_root.resolve(strict=True),
            arguments.samples_root.resolve(strict=True),
        )
    else:
        result = run_provider_probe(
            arguments.onnx_root.resolve(strict=True),
            arguments.provider,
            arguments.profile_root.resolve(strict=False),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
