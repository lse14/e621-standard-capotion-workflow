# MEMORY

## 用户目标

用户希望把源码目录直接交给其他 Windows 用户。对方下载源码后只需双击
`Install-WebUI.bat`，安装器就在命令行中自动配置完整环境。源码仓库保持轻量，
大型运行时和模型在安装阶段下载。

## 已批准决定

- 设计批准日期：2026-08-11。
- 平台：Windows 10/11 x64。
- 交互：命令行安装窗口。
- 分发：源码自举，不要求额外完整便携包。
- 系统依赖：不要求 Python、Node、CUDA Toolkit、Visual Studio 或 Windows SDK。
- Profile：首发只支持 E621。
- 必装：E621 Tagger、Qwen3 0.6B tokenizer、完整质量评分、OCR。
- GPU：自动检测 NVIDIA；通用推理选择 CUDA 或 CPU。
- OCR：NVIDIA 机器同时安装 CPU/GPU，无 NVIDIA 机器安装 CPU。
- 文件策略：续传、大小和 SHA-256 校验、staging、离线探测、事务发布。
- 清理策略：成功后不保留 wheelhouse、完整 staging 或构建缓存。

## 冻结模型身份

| 用途 | 上游身份 | Revision |
| --- | --- | --- |
| E621 Tagger | `nzs234/eva02_large_E621_FULL_V1` | `04a88fab40711ea5fdad1a8d051d25bdcb77a4e3` |
| Qwen3 tokenizer | `Qwen/Qwen3-0.6B` | `c1899de289a04d12100db370d81485cdf75e47ca` |
| LSE14 fusion head | `lse14/lse14-scorer` | `655377cb813d35291a2010031f724e778b7d80dd` |
| JTP-3 | `RedRocket/Hydra` | `d82e15954de3d99b94217fe015d5005d30e24332` |
| Waifu scorer | `Eugeoter/waifu-scorer-v3` | `c2a747fd61d310a90e9cbbf8fc590c522f234424` |

CLIP 文件使用 OpenAI 官方 CDN：

- URL: `https://openaipublic.azureedge.net/clip/models/b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836/ViT-L-14.pt`
- 大小：`932768134` bytes
- SHA-256: `b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836`

## 当前证据

- `Install-WebUI.bat` 当前只把 `Install` 动作转给 `desktop_control.ps1`。
- `desktop_control.ps1` 当前要求 Core runtime 和 `resource-library` 已存在。
- `.gitignore` 排除了 `.runtime-build`、`packaging/wheelhouse`、`resource-library` 和
  `frontend/dist`，因此 GitHub 源码不含当前安装入口假定的负载。
- `build_cpython311_runtime.ps1` 从源码编译 CPython 3.11.15，需要 Windows SDK。
- `build_distribution.ps1` 需要 Node v24.18.0、npm 11.16.0 和本地 wheelhouse。
- `policy.in` 当前固定 `torch==2.9.1+cu128` 与 `torchvision==0.24.1+cu128`。
- `caption-e621.in` 当前固定 `onnxruntime-gpu==1.26.0`。

## 当前实测体积

| 内容 | 体积 |
| --- | ---: |
| Core runtime | 约 90.7 MiB |
| Caption E621 runtime | 约 478.1 MiB |
| Policy CUDA runtime | 约 4507.7 MiB |
| OCR CPU runtime | 约 721.8 MiB |
| OCR GPU runtime | 约 3620.1 MiB |
| E621 Tagger | 约 1200.0 MiB |
| 完整质量模型 | 约 1881.5 MiB |
| OCR 模型 | 约 171.7 MiB |

按当前正式文件估算，NVIDIA 默认完整安装约 12.7 GiB。源码 ZIP 不包含这些大型负载。

## 清理记录

- 已永久删除约 16.0 GiB `.runtime-build\ocr-gpu` 失败构建残留。
- 已永久删除约 5.27 GiB `packaging\wheelhouse`。
- 已永久删除约 3.38 GiB 可测 OCR 导入暂存内容。
- 正式 Core、Caption、Policy、OCR CPU/GPU runtime 仍存在。
- 正式 E621、质量评分和 OCR 资源仍存在。
- 清理后 Core 探测输出 `anima-core-runtime-ok`。
- `.runtime-build\ocr-import\v1\staging` 下仍有七个不可访问目录；可测内容为零，
  `takeown` 也未能取得访问权。
- 当前旧离线发行构建需要先重新生成 wheelhouse。

## 实施注意

- 需要为 Caption 和 Policy 增加 CPU 安装变体，不能只把 CUDA 导入失败当作 CPU 支持。
- 预编译 CPython 3.11.15 基础资产必须由受控开发机构建并发布到当前 GitHub Release，
  用户电脑不进行编译。
- 前端构建产物很小，应随源码提供，避免目标电脑安装 Node。
- 安装清单必须列出具体 wheel/model 文件，不在目标电脑做浮动依赖解析。
- OCR 与部分第三方模型的许可证仍是公开发布门禁，不得把未核对资源镜像进项目 Release。
