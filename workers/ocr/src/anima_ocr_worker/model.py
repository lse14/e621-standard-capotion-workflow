from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Protocol

from .resource import OcrResource


class OcrModelError(RuntimeError):
    pass


class OcrEngine(Protocol):
    def predict(self, image: object) -> object: ...


class PaddleOcrModel:
    def __init__(self, engine: OcrEngine, *, convert_input_to_array: bool = False) -> None:
        self._engine = engine
        self._convert_input_to_array = convert_input_to_array

    def predict(self, image: object) -> list[object]:
        try:
            model_input = image
            if self._convert_input_to_array:
                import numpy as np
                model_input = np.asarray(image)
            return list(self._engine.predict(model_input))
        except ImportError as exc:
            raise OcrModelError("NumPy is unavailable in the OCR runtime") from exc
        except Exception as exc:
            raise OcrModelError("PaddleOCR inference failed") from exc


class ModelFactory(Protocol):
    def __call__(
        self,
        resource: OcrResource,
        *,
        device: Literal["cpu", "cuda"],
        text_det_limit_side_len: int = 1920,
        text_batch_size: int = 1,
    ) -> PaddleOcrModel: ...


_MODEL_NAMES = {
    "detection": "PP-OCRv5_server_det",
    "recognition": "PP-OCRv5_server_rec",
    "textlineOrientation": "PP-LCNet_x1_0_textline_ori",
}


def set_offline_environment() -> None:
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _relative_ascii_model_directory(resource: OcrResource, model_root: Path) -> str:
    try:
        relative = model_root.relative_to(resource.root).as_posix()
    except ValueError as exc:
        raise OcrModelError("OCR model directory is outside its resource") from exc
    if not relative or relative == "." or not relative.isascii():
        raise OcrModelError("OCR model directory is not a relative ASCII path")
    return relative


def create_paddle_engine(
    resource: OcrResource,
    *,
    device: Literal["cpu", "cuda"],
    text_det_limit_side_len: int = 1920,
    text_batch_size: int = 1,
) -> PaddleOcrModel:
    if (
        type(text_det_limit_side_len) is not int
        or not 1920 <= text_det_limit_side_len <= 3840
        or text_det_limit_side_len % 32
        or type(text_batch_size) is not int
        or not 1 <= text_batch_size <= 8
    ):
        raise OcrModelError("OCR execution tuning is invalid")
    set_offline_environment()
    try:
        import paddle
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise OcrModelError("PaddleOCR is unavailable in the OCR runtime") from exc
    if device == "cuda":
        try:
            if not paddle.device.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
                raise OcrModelError("CUDA is unavailable in the OCR GPU runtime")
            paddle.device.set_device("gpu:0")
        except OcrModelError:
            raise
        except Exception as exc:
            raise OcrModelError("CUDA is unavailable in the OCR GPU runtime") from exc
        paddle_device = "gpu"
    else:
        paddle_device = "cpu"
    detection_directory = _relative_ascii_model_directory(resource, resource.detection_root)
    recognition_directory = _relative_ascii_model_directory(resource, resource.recognition_root)
    orientation_directory = _relative_ascii_model_directory(resource, resource.textline_orientation_root)
    original_cwd = os.getcwd()
    try:
        # Paddle's native loaders receive only ASCII relative model directories.
        os.chdir(resource.root)
        try:
            engine = PaddleOCR(
                text_detection_model_name=_MODEL_NAMES["detection"],
                text_detection_model_dir=detection_directory,
                textline_orientation_model_name=_MODEL_NAMES["textlineOrientation"],
                textline_orientation_model_dir=orientation_directory,
                text_recognition_model_name=_MODEL_NAMES["recognition"],
                text_recognition_model_dir=recognition_directory,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
                text_rec_score_thresh=0,
                text_det_limit_side_len=text_det_limit_side_len,
                text_det_limit_type="max",
                textline_orientation_batch_size=text_batch_size,
                text_recognition_batch_size=text_batch_size,
                device=paddle_device,
            )
        finally:
            os.chdir(original_cwd)
    except OcrModelError:
        raise
    except Exception as exc:
        raise OcrModelError("PaddleOCR could not load the local OCR models") from exc
    return PaddleOcrModel(engine, convert_input_to_array=True)
