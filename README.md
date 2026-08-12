# e621-standard-capotion-workflow

e621-standard-capotion-workflow 是一个面向 Windows 的本地图片数据集标注工具。它通过本地 WebUI
把图片、已有 TXT/JSON 标注、OCR 与自然语言生成组织成可复核、可恢复的
标准化处理流程。

> 本仓库是源码发布，不包含数据集、模型权重、浏览器二进制、Python/Node
> 运行时。发布源码包含已构建的 `frontend/dist` 和固定的 E621 分类/Count 索引；
> 其余运行时和模型只由安装器写入项目目录。当前开发快照未附带经过核对的生产安装
> 清单，安装器会明确 fail-closed，不能替代已通过发布门禁的源码版本。

## 主要功能

- 本地 WebUI，支持中文和英文界面。
- 支持原地标注和完整副本两种工作模式。
- 支持增量处理、完全重建、预检、工作区确认、暂停、恢复和问题复核。
- 支持 E621 端到端标注流程；Danbooru 流程已接入，但正式模型和分类资源不随
  本仓库分发，需要从上游来源手动安装。
- 支持图片无标注、TXT 标注、标准 JSON 和原始 E621 分组 JSON 混合导入。
- 支持 OCR、OpenAI-compatible NL API、Count Review、Dropout、Token
  Budget Review 和 JSON/扁平 TXT 导出。
- API 凭据与任务快照分离，模型、数据和生成结果保持在本机。

## 处理流程

```text
Caption -> Classify -> Replace -> OCR -> NL
        -> Count Review -> Dropout/Policy -> Token Budget -> Export
```

每个阶段通过版本化 JSON contract 与 Core 通信。任务先写入隔离 overlay，完成
复核后再提交到源数据集或完整副本。

## 输入数据

默认支持 `.jpg`、`.jpeg`、`.png`、`.webp` 和 `.bmp`。同一个数据集可以按图片
混合以下状态：

| 单张图片旁的标注 | 默认行为 |
| --- | --- |
| 无 TXT、无 JSON | Caption 使用已安装 Tagger 生成标签，然后继续分类 |
| 非空 TXT，TXT 模式为 `Tag` | 把 TXT 解析为标签并跳过该图片的 Tagger |
| 缺失或空 TXT，TXT 模式为 `Tag` | 默认启用 Tagger 补全；关闭 fallback 时记录问题且不导出该样本 |
| 非空 TXT，TXT 模式为 `NL` | 把 TXT 写入标准 JSON 的 `nl`；Tagger 仍生成分类标签，且不会接收 TXT 内容 |
| 标准 JSON | 增量模式保留已有字段；是否覆盖由对应开关决定 |
| 原始 E621 分组 JSON | 严格转换为标准字段并跳过 Caption；格式错误时不会退回 Tagger |

`NL` TXT 必须是 UTF-8，不能包含 NUL，且最大为 16 KiB。详细布局和边界见
[data/README.md](data/README.md)。

## 目录名写入 artist

这个功能位于 WebUI 的 `Dropout/Policy` 步骤：

- 新任务默认关闭整个 Policy；开启 Policy 后，`将目录名追加到 JSON artist`
  子开关默认开启，画师丢弃率默认是 `0`。
- 从图片的一级目录读取 `数字_名称` 中的名称，并以 `@名称` 追加到 JSON
  `artist`。
- 例如 `001_角色名/image.png` 会得到 `"artist": "@角色名"`。
- 它与 NL 的 `Character` 预设无关；无论目录内容代表画师还是角色，目录映射
  始终写入 `artist`，不会写入 `character`。
- 已有 `artist` 会保留并去重。要保证目录值不被丢弃，请保持画师丢弃率为 `0`。

## 标准 JSON

Export 使用固定的九字段结构：

```json
{
  "quality": [],
  "count": "solo",
  "character": "",
  "series": "",
  "artist": "@角色名",
  "appearance": [],
  "tags": [],
  "environment": [],
  "nl": ""
}
```

`quality`、`appearance`、`tags`、`environment` 是字符串数组；其余字段是
字符串。`count` 的规范值为 `""`、`solo`、`duo`、`trio` 或 `group`。

## 安装与运行 WebUI

从通过发布门禁的源码 ZIP 解压或 clone 对应版本后，唯一的用户安装入口是双击项目
根目录的 `Install-WebUI.bat`。不要传入 OCR 模式，也不需要运行 Python、npm、CUDA
Toolkit、Visual Studio 或 Windows SDK。

安装器会检测现有 NVIDIA 驱动：NVIDIA 可用时安装 Caption/Policy CUDA、OCR CPU 和
OCR GPU，并默认使用 GPU、保留 CPU 回退；其他机器只安装 CPU 变体。E621 Tagger、
Qwen3 0.6B tokenizer、LSE14/JTP-3/Waifu/CLIP 质量栈和 OCR 都是必装项，任一离线
探测失败都会使安装失败，不会报告成功。

成功后双击 `Start-WebUI.bat`，使用完毕后双击 `Stop-WebUI.bat`。默认端口为 `8765`，
启动成功后会打开 `http://127.0.0.1:8765/`。日志位于 `.runtime-build\logs`；下载失败时
安装窗口会打印官方直链、目标文件名、大小和 SHA-256，用户放入指定缓存后可再次双击继续。

源码克隆不包含以下本地依赖；安装器只会把它们写入项目目录：

```text
.runtime-build/runtimes/core/python.exe
.toolchains/Python-3.11.15/PCbuild/amd64/python.exe
.toolchains/node-v24.18.0-win-x64/node.exe
resource-library/（已提交的 E621 分类/Count 索引除外）
```

所有依赖安装、同步和验证脚本都只应操作项目目录内的运行时。不要用系统 Python
替代项目内嵌环境。

## 资源与发布门禁

- 模型和 tokenizer：见 [models/README.md](models/README.md)。
- 数据集布局：见 [data/README.md](data/README.md)。
- 第三方代码与上游资源说明：见
  [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md)。

一键安装只从冻结清单中的上游 HTTPS URL 下载并校验模型，不把模型权重提交到 Git。
源码目标电脑不会运行 npm，`frontend/dist` 已随源码提供。发布前必须通过
`Validate-SourceBootstrapRelease.ps1`：生产清单、基础 Python Release 身份、前端产物和
第三方许可证任一缺失或未核对都会阻止发布。Danbooru 不在首发支持范围，也不会回退到
E621 资源。

## 验证

验证入口只使用项目内解释器和工具链：

```powershell
& .\packaging\scripts\Verify-Project.ps1 -Level Fast
& .\packaging\scripts\Verify-Project.ps1 -Level Full
& .\packaging\scripts\Verify-Project.ps1 -Level Release
```

- `Fast`：Core/contract/worker 快速检查和前端 typecheck。
- `Full`：在 Fast 基础上增加前端构建及已安装 OCR 的集成检查。
- `Release`：增加发布树漂移、Playwright E2E 和资源校验。

缺少项目内 Playwright Chromium、正式资源或必装 OCR 组件时，对应检查不能视为
已经验证。

### 项目内工具链

源码验证只使用项目内工具链：

```text
.runtime-build\runtimes\core\python.exe
.toolchains\Python-3.11.15\PCbuild\amd64\python.exe
.toolchains\node-v24.18.0-win-x64\node.exe
.toolchains\node-v24.18.0-win-x64\npm.cmd
```

运行时同步、缓存清理和浏览器准备都默认只预览；只有显式使用 `-Apply` 才会写入，
`-Reset` 也只作用于项目内浏览器缓存。端到端测试使用 `ANIMA_E2E_PORT` 指定
临时回环端口：

```powershell
.\packaging\scripts\Sync-CoreRuntime.ps1
.\packaging\scripts\Sync-CoreRuntime.ps1 -Apply
.\packaging\scripts\Clean-LocalArtifacts.ps1
.\packaging\scripts\Clean-LocalArtifacts.ps1 -Apply
.\packaging\scripts\Install-FrontendBrowser.ps1
.\packaging\scripts\Install-FrontendBrowser.ps1 -Apply
.\packaging\scripts\Install-FrontendBrowser.ps1 -Reset
```

## OCR 资源边界

OCR 是一键安装的必装组件。CPU runtime 始终存在；检测到可用 NVIDIA 驱动时还会安装
GPU runtime，默认执行 GPU 并保留 CPU 回退。OCR 结果位于
`ocr_annotations/<relative-image-path-with-extension>.ocr.json`。partial OCR state fails
closed，安装后的 offline probe 不得访问网络。当前模型许可证仍是公开 Release 门禁；在
许可证未核对前，安装器不会把任何开发快照描述为可公开发布的一键安装版本。

## Token Budget 边界

Tokenizer 导入 preview by default。Anima 使用 `Qwen/Qwen3-0.6B`，Krea 2 使用
`Qwen/Qwen3-VL-4B-Instruct`。Token Budget validation is enabled by default；
maxTokens defaults to `512`，范围为 `1..selected resource.contextLimit`，且
not linked to `nl.apiPolicy.maxTokens`。Disabling Token Budget validation does not guarantee the training token limit。
NL 预设提供 `general`, `style`, and `character` 和 stable short/medium/long
selection，分别对应 `2-3`、`4-5`、`6-8` sentences。超限时进入 overflow review page，
由用户执行 edit, recount, `rewrite-short`, and `apply`；这些都是 explicit user action，
may incur NL API usage。proposal does not change the final JSON until `apply`，也 never
automatically loops rewrites。

The preset contract keeps stable short/medium/long selection and uses `2-3`, `4-5`, and `6-8` sentences.
The proposal does not change the final JSON until `apply` and never automatically loops rewrites.

```powershell
.\Import-TokenizerResources.bat
.\Import-TokenizerResources.bat -Apply
```

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `core/src/anima_core/` | 本地 HTTP API、任务状态、调度、恢复和提交 |
| `workers/` | Caption、Classify、Replace、OCR、NL、Policy、Token Budget 和 Export worker |
| `frontend/` | React/Vite WebUI 与 Playwright 测试 |
| `contracts/schemas/` | 版本化 JSON Schema |
| `profiles/` | E621 与 Danbooru profile 声明 |
| `shared/anima_caption_format/` | 标准 JSON 与扁平 TXT 规范化 |
| `packaging/` | 项目内运行时、资源导入、组装和验证脚本 |
| `tests/` | unit、contract、integration 和 stress 测试 |

核心入口也可按职责直接定位：`core/src/anima_core/api.py`、`core/src/anima_core/db.py`、
`core/src/anima_core/db_schema.py`、`core/src/anima_core/pipeline.py`、
`core/src/anima_core/pipeline_dispatch.py`、`core/src/anima_core/resource_catalog.py`、
`core/src/anima_core/resource_catalog_package.py`、`core/src/anima_core/count_review_service.py`、
`workers/caption/src/anima_caption_worker/` 和 `frontend/src/App.tsx`。当前
formal Danbooru CL/WD resources and real model acceptance remain unavailable。

## 安全与隐私

- 不要提交 API key、PAT、`.env`、私钥、数据集或模型权重。
- NL 诊断使用的临时 API key 不写入任务快照；正式凭据使用本地引用。
- 运行第三方兼容 API 前，请确认 endpoint 属于你信任的服务。
- 发布 fork 前应检查暂存文件列表并重新运行敏感信息扫描。

## License

此公开源码快照不附带项目许可证，未授予复制、修改或再分发本项目源码的许可。
第三方组件和资源适用各自的许可证与使用条款，详见
[第三方声明](docs/THIRD_PARTY_NOTICES.md)。
