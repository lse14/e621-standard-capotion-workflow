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

## 安装清单模型与 tokenizer

这些资源只从清单固定的上游身份下载，当前源码不重新分发权重或 tokenizer 文件。
下表记录实现使用的不可变身份和核对入口；“待核对”会使本地发布门禁失败，不能把
该状态当作许可证批准。

| 资源 | 固定上游身份 | 来源/许可证入口 | 状态 |
| --- | --- | --- | --- |
| E621 EVA02 Tagger | `nzs234/eva02_large_E621_FULL_V1@04a88fab40711ea5fdad1a8d051d25bdcb77a4e3` | <https://huggingface.co/nzs234/eva02_large_E621_FULL_V1> | 待核对 |
| Qwen3 0.6B tokenizer | `Qwen/Qwen3-0.6B@c1899de289a04d12100db370d81485cdf75e47ca` | <https://huggingface.co/Qwen/Qwen3-0.6B> | 待核对 |
| LSE14 fusion head | `lse14/lse14-scorer@655377cb813d35291a2010031f724e778b7d80dd` | <https://huggingface.co/lse14/lse14-scorer> | 待核对 |
| JTP-3 | `RedRocket/Hydra@d82e15954de3d99b94217fe015d5005d30e24332` | <https://huggingface.co/RedRocket/Hydra> | 待核对 |
| Waifu scorer | `Eugeoter/waifu-scorer-v3@c2a747fd61d310a90e9cbbf8fc590c522f234424` | <https://huggingface.co/Eugeoter/waifu-scorer-v3> | 待核对 |
| CLIP `ViT-L-14.pt` | OpenAI CDN SHA-256 `b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836` | <https://openaipublic.azureedge.net/clip/models/b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836/ViT-L-14.pt> | 待核对 |
| PaddleOCR PP-OCRv5 Server | 固定模型归档由发布清单记录 | <https://www.paddleocr.ai/latest/en/version3.x/model_list.html> | 待核对 |

在逐项核对许可证、版本、来源和适用限制前，不得把上述模型镜像到项目 Release；
`Validate-SourceBootstrapRelease.ps1` 会拒绝包含 `license unverified` 状态的公开门禁。
