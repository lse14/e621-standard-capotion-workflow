# Third-Party Notices

Anima 源码依赖第三方开源软件，并可连接或加载第三方模型资源。各组件仍受其
各自许可证、版权声明和使用条款约束。本文件用于定位上游声明，不替代上游完整
许可证文本，也不改变 [项目许可证](../LICENSE)。

本源码仓库不包含 Python/Node 运行时、浏览器二进制、模型权重、tokenizer
文件、OCR 模型或数据集。

## 前端与开发工具

精确版本记录在 `frontend/package-lock.json`，其中包含 npm 包的 license 元数据。
主要直接依赖包括：

| 组件 | 上游许可证/项目 |
| --- | --- |
| React / React DOM | <https://github.com/facebook/react/blob/main/LICENSE> |
| Vite | <https://github.com/vitejs/vite/blob/main/LICENSE> |
| TypeScript | <https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt> |
| Playwright | <https://github.com/microsoft/playwright/blob/main/LICENSE> |

## Python 直接依赖

精确版本记录在 `packaging/requirements/*.in` 和对应 lock 文件。主要组件的上游
许可证入口如下：

| 组件 | 上游许可证/项目 |
| --- | --- |
| FastAPI | <https://github.com/fastapi/fastapi/blob/master/LICENSE> |
| Uvicorn | <https://github.com/encode/uvicorn/blob/master/LICENSE.md> |
| pywebview | <https://github.com/r0x0r/pywebview/blob/master/LICENSE.md> |
| Pillow | <https://github.com/python-pillow/Pillow/blob/main/LICENSE> |
| NumPy | <https://github.com/numpy/numpy/blob/main/LICENSE.txt> |
| ONNX Runtime | <https://github.com/microsoft/onnxruntime/blob/main/LICENSE> |
| HTTPX | <https://github.com/encode/httpx/blob/master/LICENSE.md> |
| PyTorch | <https://github.com/pytorch/pytorch/blob/main/LICENSE> |
| torchvision | <https://github.com/pytorch/vision/blob/main/LICENSE> |
| OpenCLIP | <https://github.com/mlfoundations/open_clip/blob/main/LICENSE> |
| safetensors | <https://github.com/huggingface/safetensors/blob/main/LICENSE> |
| tokenizers | <https://github.com/huggingface/tokenizers/blob/main/LICENSE> |
| PaddlePaddle | <https://github.com/PaddlePaddle/Paddle/blob/develop/LICENSE> |
| PaddleOCR | <https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE> |
| PaddleX | <https://github.com/PaddlePaddle/PaddleX> |

传递依赖也适用各自许可证。生成或分发组装包时，应同时审查所选 lock 文件、wheel
元数据及随包许可证文件。

## 可选模型与 tokenizer

以下资源只以本地安装占位或上游链接出现，不在本仓库重新分发：

| 资源 | 上游来源/许可证入口 |
| --- | --- |
| CL Tagger v2 | <https://huggingface.co/cella110n/cl_tagger_v2/blob/main/LICENSE.md> |
| WD EVA02-Large v3 | <https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3> |
| Qwen3 0.6B | <https://huggingface.co/Qwen/Qwen3-0.6B> |
| Qwen3-VL 4B Instruct | <https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct> |
| PaddleOCR 模型 | <https://www.paddleocr.ai/latest/en/version3.x/model_list.html> |

OCR 资源 manifest 在当前实现中标记为 license unverified。安装者必须在使用或
分发前独立核对来源、版本、许可证和适用限制。
