# Anima IDG 标准标注处理

面向 Windows 10/11 x64 的本地图片数据集标注工具。它通过本地 WebUI 组织图片、已有 TXT/JSON 标注、Caption、分类、替换、OCR、自然语言生成、质量策略和导出流程，所有中间结果先写入隔离 overlay，确认后再提交到原数据集或完整副本。

项目针对约 10 万张图片的数据集，采用单项有界处理、可恢复任务状态、SQLite 分页和原子提交，避免把整批业务数据一次性载入内存。

## 功能

- E621 标注工作流：Caption、Classify、Replace、OCR、NL、Count Review、Policy、Token Budget 和 Export。
- 原地处理或完整副本两种工作模式，支持增量、重建、暂停、恢复、取消、问题复核和修复任务。
- 支持 `.jpg`、`.jpeg`、`.png`、`.webp`、`.bmp`，以及标准 JSON、扁平 TXT 和原始 E621 分组 JSON；Export 可输出 JSON、扁平 TXT，或两者同时输出。
- 本地 API 与 React WebUI；任务快照、凭据、资源和生成结果分离保存。
- Caption、Policy 和 OCR 资源在发布前进行版本、路径、大小、SHA-256 和离线探测校验。

Danbooru 代码边界已保留，但首发安装范围是 E621；不会在缺少正式 Danbooru 资源时回退到 E621。

## 快速开始

1. 从源码 ZIP 或 GitHub clone 获取项目。
2. 双击根目录的 `Install-WebUI.bat`。
3. 安装完成后打开 `http://127.0.0.1:8765/`。
4. 再次使用时双击 `Start-WebUI.bat`，结束后双击 `Stop-WebUI.bat`。

安装器使用项目内运行时和工具链，不要求用户预装 Python、Node、CUDA Toolkit、Visual Studio 或 Windows SDK，也不会修改系统 `PATH`、注册表或系统 Python/Node。

源码不包含大型运行时、模型权重、浏览器二进制或数据集。安装器只把依赖写入项目目录；已提交的资源仅包括必要的 E621 分类/Count 与替换索引。

## OCR 和可选资源

OCR runtime 属于基础安装，但 OCR 模型权重必须由用户从官方来源手动下载。将三个原始归档放入 `ocr-model-archives`，再双击 `Install-WebUI.bat`；文件名、官方 URL、大小和 SHA-256 见 [OCR_MODEL_DOWNLOAD.md](OCR_MODEL_DOWNLOAD.md)。

缺少或校验失败的 OCR 模型只会阻止启用 OCR 的任务，不会阻止基础 WebUI 启动。

OCR is disabled by default。OCR sidecar 使用
`ocr_annotations/<relative-image-path-with-extension>.ocr.json`；only OCR-enabled jobs are blocked when the model is unavailable。导入流程会执行 offline CPU OCR probe，失败时不会发布 OCR 资源。

需要单独导入可选资源时，可使用：

```text
Import-OcrResource.bat -Apply
Import-TokenizerResources.bat -Apply
```

这些入口会先预览目标和校验范围，只有显式带 `-Apply` 才会写入项目目录。

## 数据与输出

标准 JSON 使用固定字段：

```json
{
  "quality": [],
  "count": "solo",
  "character": "",
  "series": "",
  "artist": "",
  "appearance": [],
  "tags": [],
  "environment": [],
  "nl": ""
}
```

`quality`、`appearance`、`tags`、`environment` 是字符串数组，其余字段是字符串；`count` 可为 `""`、`solo`、`duo`、`trio` 或 `group`。NL 文本必须是严格 UTF-8、不能包含 NUL，最大 16 KiB。

每个任务都使用独立 overlay。只有 Export 和提交阶段通过身份、指纹、JSON/TXT 格式及路径安全校验后，才会修改目标数据集。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| `core/src/anima_core/` | 本地 API、任务状态、调度、恢复、资源目录和提交事务 |
| `workers/` | 各处理模块的隔离 worker |
| `frontend/` | React/Vite WebUI 与 Playwright 测试 |
| `contracts/schemas/` | 版本化 worker 和任务 JSON Schema |
| `profiles/` | E621 与 Danbooru profile 声明 |
| `resource-library/` | 已验证的轻量资源索引和资源清单 |
| `packaging/` | 安装、运行时组装、资源导入和发布校验脚本 |
| `tests/` | unit、contract、integration 和 stress 测试 |

## 维护入口

日常用户只需要 `Install-WebUI.bat`、`Start-WebUI.bat` 和 `Stop-WebUI.bat`。OCR GPU、Token Budget 和清理/重置 BAT 是维护入口，供资源导入失败或需要清理项目内缓存时使用；它们不会操作系统级环境。

## 验证

维护者应使用项目内嵌 Python 和 Node 运行测试，不要改用系统环境。完整验证覆盖 Core unit、integration、contract、前端构建、Playwright 和 10 万样本容量回归。

项目内工具链路径如下：

```text
.runtime-build\runtimes\core\python.exe
.toolchains\Python-3.11.15\PCbuild\amd64\python.exe
.toolchains\node-v24.18.0-win-x64\node.exe
.toolchains\node-v24.18.0-win-x64\npm.cmd
```

维护者可运行以下预览/验证入口；只有带 `-Apply` 的命令才会写入项目目录：

```text
.\packaging\scripts\Verify-Project.ps1 -Level Fast
.\packaging\scripts\Verify-Project.ps1 -Level Full
.\packaging\scripts\Verify-Project.ps1 -Level Release
.\packaging\scripts\Sync-CoreRuntime.ps1
.\packaging\scripts\Sync-CoreRuntime.ps1 -Apply
.\packaging\scripts\Clean-LocalArtifacts.ps1
.\packaging\scripts\Clean-LocalArtifacts.ps1 -Apply
.\packaging\scripts\Install-FrontendBrowser.ps1
.\packaging\scripts\Install-FrontendBrowser.ps1 -Apply
-Reset
```

端到端测试使用 `ANIMA_E2E_PORT` 指定临时回环端口。核心入口包括
`core/src/anima_core/api.py`、`core/src/anima_core/db.py`、`core/src/anima_core/db_schema.py`、
`core/src/anima_core/pipeline.py`、`core/src/anima_core/pipeline_dispatch.py`、
`core/src/anima_core/resource_catalog.py`、`core/src/anima_core/resource_catalog_package.py`、
`core/src/anima_core/count_review_service.py`、`workers/caption/src/anima_caption_worker/` 和
`frontend/src/App.tsx`。

当前限制：formal Danbooru CL/WD resources and real model acceptance remain unavailable。发布前仍需在隔离环境完成对应验收。

## Token Budget

Tokenizer 导入 preview by default。支持 `Qwen/Qwen3-0.6B` 和 `Qwen/Qwen3-VL-4B-Instruct`。Token Budget validation is enabled by default；`maxTokens defaults to `512`，范围为 `1..selected resource.contextLimit`，not linked to `nl.apiPolicy.maxTokens`。Disabling Token Budget validation does not guarantee the training token limit。

NL 预设提供 `general`, `style`, and `character`，以及 stable short/medium/long selection，对应 `2-3`, `4-5`, and `6-8` sentences。超限时进入 overflow review page，由用户执行 edit, recount, `rewrite-short`, and `apply`；这些是 explicit user action，may incur NL API usage。proposal does not change the final JSON until `apply`，并 never automatically loops rewrites。

```text
.\Import-TokenizerResources.bat
.\Import-TokenizerResources.bat -Apply
```

资源、第三方许可证和 OCR 手动下载边界见：

- [OCR 模型下载说明](OCR_MODEL_DOWNLOAD.md)
- [第三方声明](docs/THIRD_PARTY_NOTICES.md)

## 安全

不要提交 GitHub token、API key、`.env`、私钥、数据集或模型权重。运行第三方兼容 API 前确认 endpoint 属于可信服务；发布前检查暂存文件列表并执行敏感信息扫描。

## License

本源码快照未附带项目许可证，未授予复制、修改或再分发本项目源码的许可。第三方组件和资源遵循各自许可证与使用条款，详见 [第三方声明](docs/THIRD_PARTY_NOTICES.md)。
