# 源码自举安装器设计

状态：已由用户批准
日期：2026-08-11
适用仓库：`lse14/anima-idg-standard-annotation-processing`

## 1. 背景

项目业务层已经按 Core 和多个 Worker 隔离运行时，但当前安装入口不是从零安装器。
`Install-WebUI.bat` 只调用 `desktop_control.ps1 -Action Install`，后者要求 Core runtime
与资源库已经存在。与此同时，Git 忽略规则排除了本地 runtime、wheelhouse、模型资源
和前端构建产物，因此 GitHub 源码快照不能在新电脑上直接运行现有安装入口。

维护端的发行构建也不能转交给普通用户执行：当前流程会从源码编译 CPython 3.11.15，
要求 Windows SDK，并用固定 Node/npm 构建前端。需要补齐的不是业务模块，而是一个把
现有模块转换为可获取、可验证组件的发布和自举层。

## 2. 目标

用户下载某个版本的源码 ZIP 或克隆对应源码后，只需双击 `Install-WebUI.bat`。安装器
在命令行完成平台检查、运行时获取、硬件选择、依赖组装、模型下载、离线探测和清理。
目标电脑无需预装 Python、Node、CUDA Toolkit、Visual Studio 或 Windows SDK。

安装完成后，E621 打标、质量评分、Qwen3 token 预算和 OCR 在断网状态下均可运行。

## 3. 非目标

- 不增加 Danbooru Profile。
- 不支持 Windows ARM64、Linux 或 macOS。
- 不安装或升级 NVIDIA 驱动。
- 不建立应用自动更新器。
- 不把大型第三方模型权重提交到 Git。
- 不重构与安装无关的 Core、Worker 或前端业务逻辑。

## 4. 方案选择

### 4.1 采用方案：源码自举

源码保留安装入口、安装逻辑、冻结清单、前端静态文件和业务代码。首阶段 PowerShell
下载项目发布的预编译 CPython 3.11.15 Windows x64 基础资产；随后由该 Python 执行
清单驱动的依赖与模型安装。

大型 Python wheels 从批准的 PyPI、PyTorch 或 Paddle 官方源获取，模型从固定上游
Revision 获取。项目 Release 只承载项目必须提供且不能在目标电脑编译的基础资产，
以及项目生成且许可证允许发布的小型索引资产。

### 4.2 未采用：完整便携包

现有 NVIDIA 完整环境和必装资源约 12.7 GiB。完整包下载、更新、托管和失败重试成本
过高，也会让 CPU 用户下载无效 GPU 负载。

### 4.3 未采用：让用户本地构建

本地构建会重新引入 Python、Node、Visual Studio、Windows SDK 和 wheelhouse 前置条件，
与一键安装目标直接冲突。

## 5. 支持矩阵

| 目标电脑 | Caption/Policy | OCR | 不下载内容 |
| --- | --- | --- | --- |
| NVIDIA 驱动和离线 GPU 探测通过 | CUDA 变体 | CPU 与 GPU 均安装，默认 GPU | 无 |
| 无 NVIDIA 或驱动预检不通过 | CPU 变体 | CPU | 通用 CUDA 与 OCR GPU |
| GPU 预检通过但实际探测失败 | 自动重建为 CPU 变体 | CPU 保持可用 | 失败 GPU staging |

OCR 模型由 CPU/GPU runtime 共享，只下载和发布一份。

## 6. 组件边界

### 6.1 源码内组件

- `Install-WebUI.bat`：稳定的双击入口，只启动 Windows PowerShell。
- `bootstrap_install.ps1`：执行平台、路径、网络和磁盘预检，获取基础 Python，并启动
  Python 安装器。
- Python 安装器：解析冻结清单、下载和校验文件、组装 runtime、执行探测和事务发布。
- 安装清单：描述平台组件、依赖文件、模型资源、上游身份和验证方法。
- `frontend/dist`：随源码提供的已构建静态前端，目标电脑不运行 npm。
- Core/Worker 源码与小型配置：在 staging runtime 中复制，不从网络获取另一份源码。

### 6.2 运行时组件

- Core
- Caption E621 CPU 或 CUDA
- Classify E621
- Replace E621
- NL
- Policy CPU 或 CUDA
- Export
- Token Budget
- OCR CPU
- OCR GPU，仅 NVIDIA 路线

每个 runtime 保持当前独立目录边界。Caption 和 Policy 的逻辑 runtime ID 不变，安装
状态记录实际使用的 CPU/CUDA 变体，避免业务调用方感知安装实现。

### 6.3 必装资源

- E621 replacement index 与 classification index
- E621 EVA02 Tagger
- Qwen3 0.6B tokenizer
- LSE14 fusion head、JTP-3、Waifu scorer 与 CLIP `ViT-L-14.pt`
- PaddleOCR PP-OCRv5 Server detection、recognition 与 textline orientation
- 现有 NL prompt 与 Profile 配置

首发不安装 Qwen3-VL 4B tokenizer、Danbooru 模型或 Danbooru 索引。

## 7. 冻结上游身份

| 资源 | 固定来源 |
| --- | --- |
| E621 Tagger | `nzs234/eva02_large_E621_FULL_V1@04a88fab40711ea5fdad1a8d051d25bdcb77a4e3` |
| Qwen3 tokenizer | `Qwen/Qwen3-0.6B@c1899de289a04d12100db370d81485cdf75e47ca` |
| LSE14 head | `lse14/lse14-scorer@655377cb813d35291a2010031f724e778b7d80dd` |
| JTP-3 | `RedRocket/Hydra@d82e15954de3d99b94217fe015d5005d30e24332` |
| Waifu scorer | `Eugeoter/waifu-scorer-v3@c2a747fd61d310a90e9cbbf8fc590c522f234424` |
| CLIP | OpenAI CDN 文件，SHA-256 `b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836` |

CLIP 固定大小为 `932768134` bytes。其完整 URL 为：

`https://openaipublic.azureedge.net/clip/models/b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836/ViT-L-14.pt`

Hugging Face URL 必须使用 `/resolve/<full-commit-sha>/...`，禁止使用 `main`。运行时依赖
清单必须解析为具体 Windows wheel 文件，不允许目标电脑重新求解版本。

## 8. 数据结构

### 8.1 安装清单

顶层字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `schemaVersion` | integer | 清单结构版本 |
| `releaseVersion` | string | 与源码及基础资产对应的发布版本 |
| `platforms` | object | 支持平台和基础 Python 资产 |
| `components` | array | runtime、资源和前端组件定义 |
| `allowedHosts` | array | 下载和重定向允许的 HTTPS 主机 |

每个组件包含：`componentId`、`kind`、`required`、`variants`、`targetRelativePath`、
`artifacts`、`probe` 和 `licenseReference`。每个 artifact 包含 `url`、`sizeBytes`、
`sha256`、`relativePath`，Hugging Face artifact 还包含 `repository` 与 `revision`。

清单本身的 SHA-256 固定在 PowerShell 首阶段脚本中。发布流程拒绝空 URL、浮动 Revision、
重复目标路径、无大小、无 SHA-256 或非允许主机。

### 8.2 安装状态

`.runtime-build/manifests/install-state.json` 只在完整安装成功后写入，字段包括：

- `schemaVersion`
- `sourceCommit`
- `releaseVersion`
- `installManifestSha256`
- `accelerator`
- `components`，记录组件 ID、变体、清单指纹和正式路径
- `completedAtUtc`

状态文件不保存令牌、代理凭证或用户数据。组件目录中的现有 runtime/resource manifest
仍是单组件真实性来源，顶层状态只表达本次完整安装是否闭环。

## 9. 安装数据流

1. BAT 以自身目录为项目根启动 PowerShell，完整引用带空格和中文的路径。
2. PowerShell 确认 Windows x64、普通项目目录、TLS 能力和写权限，并创建安装日志。
3. 读取冻结清单元数据，按所选硬件和现有组件计算下载量、展开量与 staging 空间。
4. 获取预编译 CPython 3.11.15 基础资产，验证后解压到私有 bootstrap staging。
5. 运行 stdlib-only Python 安装器，检测 `nvidia-smi` 与清单声明的驱动门槛。
6. 为每个选中 runtime 下载具体 wheels，验证后离线组装 staging runtime 并复制对应源码。
7. 下载资源文件，生成或核对现有 `resource.json`，在 staging resource library 中加载。
8. 运行 Core、各 Worker、tokenizer、Tagger、质量模型和 OCR 的离线最小探测。
9. 通过事务日志和同卷目录重命名发布组件；失败则恢复旧组件或在下次启动完成恢复。
10. 全部必装组件成功后写入顶层安装状态，删除 bootstrap、wheels、完整缓存和 staging。
11. 输出安装摘要、日志路径和 `Start-WebUI.bat` 启动提示。

## 10. 下载与恢复

- 大文件先写到以内容 SHA-256 命名的 `.partial` 文件。
- HTTP 服务器支持 Range 时从当前长度续传；不支持时删除该文件并从零重下。
- 每个 artifact 最多进行有限次数的退避重试，认证、403、404 和哈希错误直接给出明确原因。
- 哈希错误文件立即删除，不能作为续传基础。
- 自动下载失败时打印可点击官方 URL、目标文件名、大小和 SHA-256；用户把文件放入安装器
  指定下载目录后，重新双击即可被验证和继续使用。
- 已发布组件只有清单指纹与全部文件匹配时才跳过；缺失或漂移组件进入修复流程。
- 成功后不保留 wheelhouse。失败后只保留仍可续传的 `.partial` 与日志。

## 11. 文件与供应链安全

- 下载只允许 HTTPS；重定向后的主机仍须在清单允许列表。
- 所有内容都以大小和 SHA-256 为最终身份，不信任文件名或 HTTP 元数据。
- 解压拒绝绝对路径、`..`、设备路径、重解析点和链接条目。
- staging、缓存、正式目标和清理目标都必须解析后证明位于项目根目录。
- Python 依赖使用具体 wheel artifact，安装时使用离线、无索引、哈希锁定模式。
- 预编译 Python 基础资产记录 CPython 源版本、构建器、构建脚本哈希和离线探测证据。
- 模型直接从上游下载；在许可证未核对前不得镜像到项目 GitHub Release。

## 12. 命令行体验

安装窗口持续显示：阶段编号、组件名、已下载/总大小、百分比、速度、当前重试和所选硬件
路线。质量评分和 OCR 不提供默认跳过选项。用户中断时不发布正在处理的组件。

GPU 不可用、驱动不兼容或实际探测失败时，窗口必须显示具体检测证据和 CPU 回退结果，
不能静默声称 GPU 安装成功。

## 13. 验证方案

### 13.1 静态和单元验证

- 安装清单 schema、路径、host allowlist、Revision、大小和 SHA-256 验证。
- Range 续传、服务器忽略 Range、错误大小、错误哈希和重定向 host 测试。
- ZIP 路径穿越、绝对路径、链接和重解析点拒绝测试。
- 硬件选择、幂等跳过、漂移修复、事务恢复和清理边界测试。

### 13.2 干净机矩阵

- Windows 10 x64 CPU：无 Python/Node/SDK/CUDA，从源码 ZIP 双击安装。
- Windows 11 x64 CPU：同上，并从中断下载继续。
- Windows 11 x64 NVIDIA：安装 Caption/Policy CUDA、OCR CPU/GPU，并完成实际 CUDA 推理。
- 中文和空格路径：安装、停止、启动、修复和重新安装。

### 13.3 离线验收

安装后断开网络并验证：

- `anima_core --check-runtime`
- E621 Tagger 对固定样例产生可解析标签
- LSE14、JTP-3、Waifu 与 CLIP 完整质量评分路径产生有限数值
- Qwen3 0.6B tokenizer 对固定文本产生冻结 token 计数
- OCR CPU 对固定中英文样例产生预期结果
- NVIDIA 机器的 OCR GPU 报告实际 CUDA device，并与 CPU 样例结果满足约定容差
- WebUI 启动并能完成一条端到端任务

仅能导入包不视为通过。

## 14. 发布门禁

一个源码版本只有同时满足以下条件才能提供一键链接：

1. 源码 commit、安装清单和基础 Python 资产属于同一 `releaseVersion`。
2. GitHub Release 中的基础资产已从公开 URL 下载并重新核对 SHA-256。
3. CPU 与 NVIDIA 干净机验收记录均通过。
4. `docs/THIRD_PARTY_NOTICES.md` 已覆盖实际下载组件，模型许可证状态已核对。
5. 标签对应的 GitHub 源码 ZIP 可以直接双击安装，README 链接指向该不可变版本。

## 15. 风险与控制

- NVIDIA 完整安装约 12.7 GiB。安装器按清单动态计算峰值空间，并在任何下载前阻止空间不足。
- 外部源可能在特定网络不可达。安装器保留续传，并提供官方直链和清晰的失败证据。
- 当前 Caption/Policy 只有 CUDA 偏向锁。实施必须先建立 CPU locks 和真实 CPU 探测。
- OCR GPU 曾有兼容失败。GPU runtime 只有完成真实离线推理后才发布，CPU runtime 始终保留。
- 第三方模型许可尚未全部核对。公开发布门禁阻止未经批准的项目镜像，但不伪造许可证结论。
