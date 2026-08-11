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

## 实施计划

- 2026-08-11：实施计划位于
  `docs/superpowers/plans/2026-08-11-source-bootstrap-installer.md`；它按 TDD 拆分为
  清单、下载、路径/事务、PowerShell 自举、基础资产/CPU-CUDA 锁、组装、离线探测、
  故障矩阵与最终复核。
- 当前独立 worktree 不含被忽略的 `.runtime-build`、`.toolchains`、`resource-library`
  或 `frontend/dist`。实施测试只能使用经验证的项目内嵌 CPython，不得改用系统 Python；
  该现状不是任何功能测试通过的证据。
- 2026-08-11：已实现并测试 `packaging/installer/manifest.py` 的冻结清单契约；
  项目内嵌 CPython 3.11.15 运行 `tests/unit/test_source_bootstrap_manifest.py` 得到
  6 项通过。生产资产记录尚未写入，因为其公开 Release 身份未被实际核对。
- 2026-08-11：已实现并测试 `packaging/installer/download.py`；项目内嵌 CPython
  3.11.15 运行 `tests/unit/test_source_bootstrap_download.py` 得到 5 项通过。该实现只
  使用 HTTPS/允许主机，支持 Range 续传，哈希错误立即删除，网络失败保留 `.partial`。
- 2026-08-11：已实现并测试 `packaging/installer/paths.py`；同一项目内嵌 CPython
  以 `unittest discover -s tests\\unit -p test_source_bootstrap_paths.py -v` 得到 8 项通过，
  以 `-p test_source_bootstrap_*.py` 得到组合 19 项通过。它拒绝 Windows 路径逃逸、
  ZIP 穿越/链接/非普通条目/大小写碰撞，staging 仅位于项目内，且事务发布仅允许
  `.runtime-build\\runtimes` 或 `resource-library`，失败清理保留 `.partial` 与日志。
  真实组件幂等修复和端到端故障矩阵尚未实施，不能据此声称安装器已完整可用。
- 2026-08-11：`Install-WebUI.bat` 现在只启动 Windows PowerShell 的
  `packaging/scripts/bootstrap_install.ps1`；它使用项目内 `.runtime-build` 日志、缓存
  和 staging，检查 Windows x64、路径、空间、清单身份和所有 bootstrap 重定向主机，
  并在 CPython 可用后调用标准库 `installer/install.py`。`desktop_control.ps1` 的
  launcher 文件已迁入 `.runtime-build\\launcher`，且只有完整 `install-state.json` 才允许
  Install/Start。
- 2026-08-11：项目内嵌 CPython 运行 23 项 source-bootstrap 单测和 6 项 desktop-control
  单测均通过；PowerShell 临时目录实际执行证明缺清单时会写项目内 UTF-8 日志后非零退出。
  `ExpectedInstallManifestSha256` 仍故意为空，因为没有经实际核对的生产清单和 CPython
  Release 资产；当前 fail-closed 行为不是一次成功安装或公开链接验证的证据。
- 2026-08-11：已添加 CPU/CUDA 显式 requirements。CPU direct wheel 身份由实际只读查询
  得到：ONNX Runtime `1.26.0` Windows CPython 3.11 wheel 为
  `https://files.pythonhosted.org/packages/9c/21/9f041de20787cd85498bd48e0ec4d098bf2a6c486e25b24b8dae1bf492b2/onnxruntime-1.26.0-cp311-cp311-win_amd64.whl`，
  `13023165` bytes，SHA-256
  `e6456718125fd777c673f3b78d4a9ab58d6adea641e9afae85ee6444f0e0e9a9`；来源为
  `https://pypi.org/pypi/onnxruntime/1.26.0/json`。
- 2026-08-11：PyTorch 官方 CPU 索引给出 Torch `2.9.1+cpu` Windows CPython 3.11：
  `https://download-r2.pytorch.org/whl/cpu/torch-2.9.1%2Bcpu-cp311-cp311-win_amd64.whl`，
  `110888878` bytes，SHA-256
  `69b3785d28be5a9c56ab525788ec5000349ec59132a74b7d5e954b905015b992`；TorchVision
  `0.24.1+cpu`：
  `https://download-r2.pytorch.org/whl/cpu/torchvision-0.24.1%2Bcpu-cp311-cp311-win_amd64.whl`，
  `4037705` bytes，SHA-256
  `dc41d9345769a24984f54aad914ce40954c11cfc4fbbe0fa4187b07c896c9940`。索引原址为
  `https://download.pytorch.org/whl/cpu/torch/` 与
  `https://download.pytorch.org/whl/cpu/torchvision/`。
- 2026-08-11：新增开发者专用 `build_bootstrap_runtime.ps1` 与
  `build_install_manifest.py`。前者在只读项目内嵌 Core runtime 上实际打包得到所需条目
  与 `bootstrap-stdlib-ok`；后者用严格清单契约、Release 身份和 lock-to-wheel SHA 映射
  验证 developer inventory。没有真实 CPython Release 资产或完整 production inventory，
  因此未生成 `install-manifest.json`、`release-artifacts.json`，也未进行真实 CPU/GPU
  runtime 组装或推理。
- 2026-08-11：Task 6 增加 `packaging/installer/assemble.py` 和 `install.py`。fixture 证明
  CPU/NVIDIA 选择、OCR CPU/GPU 选择、wheel staging、源码复制、构建辅助包清理、完整文件/
  runtime manifest 的幂等校验、事务发布、状态写入和第二次运行零 fetch。`install.py` 默认拒绝
  import-only probe，生产调用还要求完整 E621 组件集；fixture 仅通过显式测试开关运行。
- 2026-08-11：当前不存在 production `install-manifest.json`，也未提供真实 resource JSON 或
  immutable E621/quality/Qwen3/OCR artifact inventory；因此上述 fixture 机制不能表述为真实
  模型安装、CPU/NVIDIA 离线推理、干净机测试或公开 Release 验证。PowerShell 已传递
  `--bootstrap-runtime`，但实际目标机安装仍受该清单与 Task 7 probes 门禁阻断。
- 2026-08-12：Task 7 新增 `packaging/installer/probes.py`。每个子进程清除代理变量、设置
  Hugging Face/Transformers/Paddle 离线标记并封锁 socket；证据验证拒绝 import-only、错误
  accelerator、非有限质量分数和 OCR CPU/GPU 样例文本不一致。CUDA Caption/Policy 探测组失败时，
  `install.py` 延后共享 Tagger/质量资源的失败，重建 CPU runtime 后重探整组；共享资源仍失败会
  fail closed。OCR GPU 探测失败只丢弃 GPU staging，保留 CPU OCR。
- 2026-08-12：项目内嵌 CPython 3.11.15 的新鲜验证为 source-bootstrap 单测 42 项、离线
  fixture 1 项、desktop-control 单测 6 项及 `install.py`/`probes.py` 编译通过。该证据不包含
  真实生产模型、GPU、干净机、中文路径或公开 URL；不可据此宣称一键安装已可交付。
- 2026-08-12：Task 8 已将安装成功清理接入 Python `install_project`，并在 PowerShell 首阶段
  增加项目内成功/失败清理边界；成功删除完整 bootstrap/cache/staging/transactions/build-cache，
  失败不删除可续传 `.partial` 或日志。fixture 安装单测实际确认成功后四类目录不存在、第二次
  安装不发起 fetch。
- 2026-08-12：新增只读 `packaging/scripts/Validate-SourceBootstrapRelease.ps1`。它在网络之外
  检查 manifest/release identity、必装组件、URL host、大小、SHA、完整 revision、前端 dist
  和第三方声明；中文路径实际运行时以 ASCII fail-closed 摘要报告缺失清单，避免 PowerShell
  错误记录编码污染测试。
- 2026-08-12：使用项目内 `E:\Desktop\Anima idg标准标注处理\.toolchains\node-v24.18.0-win-x64\npm.cmd`
  完成 frontend `npm ci`、`typecheck` 和 `build`；已生成并准备提交 `frontend/dist`，不提交
  `frontend/node_modules`。
- 2026-08-12：`README.md` 已把双击 `Install-WebUI.bat` 定为唯一用户安装路径，删除 OCR
  optional/None 文档并记录 CPU/NVIDIA 选择、必装质量/OCR、项目内日志和发布门禁；第三方声明
  已列出冻结模型身份和原始链接，但许可证仍标记待核对，故 validator 必须失败。生产清单、
  CPython Release、真实模型推理、干净机/GPU 和公开链接仍未验证，不能宣称功能已发布。
