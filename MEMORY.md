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
- 2026-08-12：用户确认 OCR 模型不由安装器下载或镜像。安装器仍安装 OCR runtime；
  新任务 OCR 默认关闭。用户手动启用 OCR 时，只有完整、哈希验证并离线探测通过的本地
  模型资源可用；缺失时预检阻止任务并给出 `OCR_MODEL_DOWNLOAD.md` 与
  `ocr-model-archives\\` 指引。基础安装成功后应自动启动 WebUI。

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

- 2026-08-13：生产发布元数据已在 `codex/source-bootstrap-installer` 工作树生成；CPython
  3.11.15 ZIP 的本地已核对身份为 `33,887,443` bytes、SHA-256
  `a7bef1285f1a0f4007de9ede5752f105dcf2b137d54670074d16503554fa0169`，provenance
  绑定实现提交 `790502c806f813cca2609281fdf6a687886f90d8`。安装器绑定的
  `install-manifest.json` SHA-256 为
  `2a3d55f74557cb90c06f42790f0b054d21e04ba062612e897d321f84841774f6`；公开 Release
  URL 使用 GitHub 迁移后的 `lse14/e621-standard-capotion-workflow` 规范仓库。
- 发布前本地证据：source-bootstrap 单元测试 89/89、inventory 审计、候选资产独立
  verifier、Windows PowerShell 5.1 与 PowerShell 7 发布门禁均通过。PowerShell 7 会把
  ISO UTC JSON 值反序列化为 `DateTime`，validator 现使用 `ConvertFrom-Json -DateKind String`
  保留严格 UTC 原文；回归测试覆盖该差异。
- 上述不是公开 Release 或干净机成功证据。公开上传后必须用
  `Validate-SourceBootstrapRelease.ps1 -VerifyPublishedBootstrap` 重新下载并核对；CPU 和
  NVIDIA 仍必须在未安装开发工具的独立主机上执行验收脚本。
- 2026-08-13：已在 `lse14/e621-standard-capotion-workflow` 创建公开 Release
  `source-bootstrap-e621-v1`，目标提交为
  `587065d942983bc330330d0ac4983e2fbf5ce5df`。上传资产 URL 为
  `https://github.com/lse14/e621-standard-capotion-workflow/releases/download/source-bootstrap-e621-v1/cpython-3.11.15-win-amd64.zip`；
  GitHub 返回 `33,887,443` bytes、`sha256:a7bef1285f1a0f4007de9ede5752f105dcf2b137d54670074d16503554fa0169`，
  项目 `-VerifyPublishedBootstrap` 已重新下载并字节验证。R14 CPU/NVIDIA 干净机验收仍未执行。
- 2026-08-13：默认 `main` 新 clone 在 RTX 4090 开发机真实执行安装，下载基础资产前失败：
  `Test-Path -LiteralPath $partial -and ...` 被 Windows PowerShell 解析为不存在的 `-and`
  参数，且 `Get-Item` 对缺失 `.partial` 抛错。`bootstrap_install.ps1` 中三处 partial 检查和
  一处解压失败清理均改为 `(Test-Path ...) -and ...`；回归测试扫描该非法形态。修复后
  source-bootstrap 91/91、公开 Release 重新下载门禁及 manifest SHA 绑定通过。该开发机
  预检仍为 `not-clean`，本次发现/修复不能记作 R14 干净机通过。
- 2026-08-13：公开 `main@bcbdc90c2fa1d0e9d4f05c6ce4dffe886f383426` 的全新
  NVIDIA clone 已通过路线、空间、CPython 公开下载和安全解压，随后在标准库安装器导入阶段
  确定性失败。直接捕获完整 stderr 证明 `python -I packaging\installer\install.py` 不会把
  脚本目录放入 `sys.path`，因此 `from assemble import ...` 报
  `ModuleNotFoundError`。回归测试先在同一 `-I` 调用下复现失败；`install.py` 仅将其自身解析
  后的受信任目录插入模块路径，测试及实际 `--help` 调用随后退出 0。提交前新鲜验证为
  source-bootstrap 92/92、Python 编译、PowerShell AST、inventory 审计、公开 Release 字节
  重下和发布门禁均成功。真实组件下载、离线 probe、WebUI 启动和 R14 干净机验收仍未完成。
- 2026-08-13：公开 `main@3ff876ef0c513b3a1bcda61d095f89af25aeb589` 的新 clone
  已进入真实 runtime 下载。首个 `annotated_doc-0.0.4` wheel 的 manifest 身份为 5,303 bytes、
  SHA-256 `571ac1dc6991c450b25a9c2d84a3705e2ae7a53467b5d111c24fa8baabbed320`；下载器按
  SHA 命名完整缓存，而 runtime 装配错误检查缓存文件名后缀，导致所有生产 wheel 必然被拒绝。
  装配接口现同时接收缓存路径与受清单约束的相对路径，以后者验证 `.whl`、以前者安全解压；
  回归覆盖无扩展名缓存成功、非 wheel 清单名拒绝、重复路径拒绝。另因 PowerShell catch 在保存
  原始错误前清理刚退出的 bootstrap runtime，`libcrypto-3.dll` 的短暂占用会用 Access Denied
  覆盖真正 wheel 错误；catch 现保留并最终报告原错，清理失败只作为附加日志。提交前新鲜执行
  source-bootstrap 95/95、Python 编译、PowerShell AST、inventory 与公开发布门禁均成功。

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
- 2026-08-12：同一计划已追加 Release readiness continuation（Task 10-14）：本地可验证
  CPython 候选资产、候选/已发布 Release 身份分离、机器可读许可证证据账本和真实干净机
  验收运行器。该计划明确禁止以本机/fixture 伪装 CPU/NVIDIA 干净机成功，也禁止在没有用户
  明确授权时创建 GitHub Release 或上传任何资产。
- 2026-08-12：维护端新增 `Test-BootstrapRuntimeAsset.ps1`；它独立核对候选 CPython ZIP
  的 provenance、大小/SHA-256、builder 脚本 SHA-256、安全解压和离线标准库探测。实际候选
  位于被忽略的 `.release-candidate\bootstrap`，当前一次验证得到 33,264,397 bytes 与 SHA-256
  `f7a36991fc6ac035f7e3bd30fd8badd06d4309590323bedda2ec958aa0d17096`。这不是公开资产，
  每次源码提交后须以新 HEAD 重建；未授权前不可写入生产 Release 身份或声明发布通过。
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
- 2026-08-12：Task 9 新鲜验证：source-bootstrap 测试 45 项、desktop-control 6 项、fixture
  2 项全部通过，`install.py`/`probes.py` 编译通过；frontend Node v24.18.0 typecheck/build
  通过后已删除 `frontend/node_modules`，没有启动开发服务器，`vite` 进程数为 0。
- 2026-08-12：`Verify-Project.ps1 -Level Fast -OcrMode Auto` 因隔离 worktree 缺少被忽略的
  `.runtime-build` 退出 1；`Validate-SourceBootstrapRelease.ps1` 因缺少真实
  `install-manifest.json` 退出 1。R14/R15 的干净机、真实模型推理、许可证、公开 URL 和 Release
  仍是阻断项，不能把本地 fixture 证据描述为可交付安装。
- 2026-08-12：用户明确要求把 E621 分类索引提交到 GitHub。只允许跟踪
  `resource-library/classification-indexes/e621-classify-20260724-v1` 的
  `resource.json`、`e621_tag_dictionary.json` 和 `e621_count_wiki.sqlite3`；不得将
  `E:\Desktop\e621_normjson_tagger`、其他 `resource-library` 内容、模型、runtime、缓存或数据集
  一并提交。两个载荷的 SHA-256 分别为
  `87c42e0021ea637bc93195c6d37ac4f8b967dd989a8bd5de4b7ebb7546264e59` 和
  `6aa0f944f07de490413aa49bf59d6ead555b6eeaad2e00022dedd6109d0abff9`。
- 2026-08-12：该索引不是模型权重；字典含 120,978 个 E621 标签/别名条目，Count SQLite
  含 21 条 Wiki 投影。E621 官方 Terms 第 4 节限制其网站内容复制和再分发，已将来源、
  文件身份和该限制写入 `docs/THIRD_PARTY_NOTICES.md`；本次 Git 提交不替代模型许可或
  一键安装 Release 门禁。
- 2026-08-12：全局 Git `core.autocrlf=true` 会把词典的 CRLF 改为 LF，导致暂存字节
  与 `resource.json` SHA-256 不符。新增 `.gitattributes` 仅将该词典设为 `-text -diff`，
  保持 37,569,404 bytes 和固定 SHA-256。提交前已验证 Git 暂存对象及从暂存树构造的
  临时 ZIP，并运行分类资源单测 27 项与实际分类/Count 加载探针。
- 2026-08-12：用户明确要求将 E621 替换索引也提交到 GitHub。仅允许跟踪
  `resource-library/replacement-indexes/e621-replace-20260726-v2` 的 `resource.json`、
  `e621_tag_replacement_index.csv` 与该清单引用的中文说明书；不得一并提交 `_recovery`、
  模型、runtime、缓存、数据集或 `E:\Desktop\e621_normjson_tagger`。
- 2026-08-12：替换 CSV 为 3,902,020 bytes，SHA-256
  `24ad8388580a6c3628dec44bd813897c278e4f1b04fccd810f22acaf97c1cbe7`，资源 fingerprint 为
  `3cabbeeffd379a893a0b53d427c3dbb26ea6c587f474ae761b21afde4ee4c47b`。运行时加载除哈希外
  还审计 86,922 条规则、keep/replace/drop 计数与 pipe 规则；资源目录校验要求三项受清单
  约束文件全部存在。`.gitattributes` 对该 CSV 使用 `-text -diff` 保留固定 CRLF 字节。
- 2026-08-12：说明书记录输入为 E621 tags、aliases、implications 和 Wiki 数据导出，官方
  模板为 `https://static1.e621.net/data/db_export/{tags,tag_aliases,tag_implications,wiki_pages}.csv.gz`。
  E621 Terms <https://e621.net/terms_of_use> 第 4 节限制复制和再分发，已更新第三方声明；
  该索引提交不代表模型许可证或一键安装公开 Release 门禁已通过。
- 2026-08-12：延迟 OCR 模型实现将归档固定为项目根 `ocr-model-archives\` 的三个 Paddle
  文件。基础安装始终跳过任何清单中的 `ocr-models`，并在顶层 install state 落盘后才检查归档；
  缺失时仅提示 `OCR_MODEL_DOWNLOAD.md`，齐全时调用模型专用导入。该导入以已发布的
  `.runtime-build\runtimes\ocr-paddle` 作离线 CPU probe，不调用 `_build_environment`、
  `_resolve_and_stage_runtime` 或 runtime/wheelhouse 发布。无效/不完整归档使模型导入失败，
  但不删除已完成的基础状态，也不发布部分 OCR resource。
- 2026-08-12：`bootstrap_install.ps1` 成功安装后记录绝对 `OCR_MODEL_DOWNLOAD.md` 路径并调用
  `desktop_control.ps1 -Action Start`；Start 返回非零时 PowerShell 保留项目内安装状态/日志并退出
  非零。PowerShell 组件选择和空间预算与 Python 对齐：CPU 可选的 CUDA-only 组件跳过，`shared`
  变体仍可选择，`ocr-models` 始终跳过。
- 2026-08-12：上述定向证据为项目内嵌 CPython 运行 source-bootstrap 22 项、PowerShell 7 项、
  desktop-control 6 项、模型专用导入 1 项均通过，以及三个修改模块 `py_compile` 通过；这不等同
  于真实 Paddle 模型、NVIDIA、干净机或公开 source ZIP 安装验证。
- 2026-08-12：OCR-enabled 任务的模型资源缺失或 `SHA-256 mismatch` 现在都产生
  `ocr_resource_install_required`，包含 `OCR_MODEL_DOWNLOAD.md`、`ocr-model-archives` 和
  再次双击 `Install-WebUI.bat` 的可执行指引。OCR 默认关闭，因此未启用 OCR 的任务不会因
  模型缺失而失败。
- 2026-08-12：`Validate-SourceBootstrapRelease.ps1` 保留 `ocr-cpu` 作为基础安装组件，
  不再把用户手动提供的 `ocr-models` 当作自动下载必装项；README、RULES、models README、
  第三方声明和下载指南均明确三份官方归档、本地哈希/离线 probe、无 `-OcrMode` 参数及
  不下载、不镜像、不发布 OCR 权重的界限。
- 2026-08-12：使用项目内嵌 Core Python 定向执行 7 项 OCR 预检、8 项 source-bootstrap
  PowerShell、4 项文档契约测试全部通过，`git diff --check` 无错误。完整
  `test_ocr_resource_scripts.py` 仍不能在当前链接 worktree 运行其引用本 worktree 缺失的
  `.runtime-build` 的旧 CLI/wrapper 用例；未把这一限制或未运行的真实模型/GPU/干净机测试
  表述为通过。
- 2026-08-12：`git merge-base --is-ancestor main HEAD` 返回成功；当前分支只跟踪两套
  E621 索引包的六个清单约束文件，不含模型权重、runtime、OCR archive 或其他资源库内容。
- 2026-08-12：仅使用项目内嵌
  `E:\Desktop\Anima idg标准标注处理\.runtime-build\runtimes\core\python.exe` 重新运行
  source-bootstrap 54 项、desktop-control 6 项、source-bootstrap fixture 2 项，全部通过。
  `Validate-SourceBootstrapRelease.ps1 -ProjectRoot .` 以 `install-manifest.json is missing`
  退出 1，符合预期 fail-closed 行为；生产 manifest、公开基础资产/Release 身份、许可证闭环、
  真实 OCR 模型、CPU/NVIDIA 干净机和公开 source ZIP 验收仍未完成，不能声称可公开一键安装。
- 2026-08-12：基础资产验证器的安全解压新增反斜杠路径归一化；项目内嵌 Python 实际运行的
  release-build 单测确认 `Lib\\..\\outside.txt` ZIP 条目在解压前失败。该修正不改变候选
  资产的本地-only/未发布状态。
- 2026-08-12：基础资产 builder/verifier 的离线标准库 probe 已收紧为精确 CPython
  `3.11.15`（而非仅 `3.11`）；release-build 单测 5 项通过。后续源码提交仍需重新生成
  候选 ZIP/provenance，不能复用旧 commit 身份。
- 2026-08-13：Task 12 新增 `packaging/installer/license-ledger.json` 并把它接入
  `Validate-SourceBootstrapRelease.ps1`。每个 production manifest `licenseReference` 必须有
  严格字段的账本条目；direct-upstream-only、local-only 与 project-source 必须为
  `not-mirrored`，source-redistributed 只有 `approved` 且具有精确 source-tree 文件身份绑定的
  决定才能通过。项目内嵌 CPython 运行定向 PowerShell 测试 14 项通过。
- 2026-08-13：项目负责人通过 `user-confirmed-project-owner` 确认当前两套 E621 派生索引可
  随源码/GitHub 分发。账本将决定限定为分类索引三文件和替换索引三文件的当前大小/SHA-256，
  并记录 E621 Terms URL/响应 SHA-256。它是项目分发决定，不是 E621 上游许可或完整法律审核；
  `docs/THIRD_PARTY_NOTICES.md` 保留 Terms 风险及下游核对边界。
- 2026-08-13：Task 13 新增真实干净机验收运行器和公开说明，`.gitignore` 只放行
  `docs/SOURCE_BOOTSTRAP_ACCEPTANCE.md`。运行器只在项目 `.runtime-build\acceptance` 写证据，
  检查 Python/py/Node/npm/nvcc/cl/Windows SDK；完整模式才运行 `Install-WebUI.bat`，且 finally
  调用 `Stop-WebUI.bat`。源码 ZIP 无 `.git` 时记录 `sourceCommit: null`，不使预检失败。
- 2026-08-13：本开发机 `-PreflightOnly` 的真实 JSON 状态为 `not-clean`，退出 1；检测到系统
  Python、py、Node、npm、nvcc、cl、Windows SDK，未调用安装器或 WebUI。它不能替代四个
  物理/隔离 VM 场景中的 CPU/NVIDIA 验收。
- 2026-08-13：Task 14 仅执行变更相关本地门禁：source-bootstrap PowerShell 测试 16 项、
  inventory `--validate-only` 和 `git diff --check` 均成功。候选 CPython ZIP verifier 针对
  HEAD `1cba8eb0617a2bf87b832461c12b58843ad8ffaf` 以 provenance commit 不匹配退出 1；默认
  release validator 以 `install-manifest.json is missing` 退出 1。两者都是预期 fail-closed
  结果，不得伪装为可公开发布或干净机成功。
- 2026-08-13：后续缺陷复核补齐四项 source-bootstrap OCR 行为：指南测试只禁止实际
  `Install-WebUI.bat -OcrMode` 命令形态；`ocr-gpu` 仅在离线 probe 显式 `False` 时丢弃，
  `None` 保留 CUDA runtime 并记录模型未验证；1 或 2 个手动归档仅提示指南，三份齐全但
  导入失败仍 fail closed；完整 OCR runtime 重建使用既有
  `packaging\wheelhouse\ocr-paddle` 缓存。
- 2026-08-13：干净机验收运行器不再在 Stop 前写入 `passed`。只有安装器、安装状态和
  `Stop-WebUI.bat` 全部成功才通过；Stop 缺失或退出非零写入 `failed`，避免假阳性验收记录。
- 2026-08-13：使用 `E:\Desktop\Anima idg标准标注处理\.runtime-build\runtimes\core\python.exe`
  对 `D:` 工作树新鲜执行 source-bootstrap 单测 88 项、desktop-control 单测 6 项，均通过；
  inventory audit 成功。完整 OCR script 套件为 14 项通过、2 项显式跳过，因为该隔离源码树
  没有自己的 embedded Core runtime；该环境限制不构成 OCR 功能通过证据。
- 2026-08-13：修复 Start 或后续步骤失败时的 bootstrap 缓存回归：失败清理保留完整和
  `.partial` CPython 下载，仍删除 staging、展开 bootstrap 和 transactions。下次使用完整缓存前
  继续做大小/SHA-256 校验，失效文件仍会删除。PowerShell 动态回归构造两类缓存并确认失败清理
  后均保留。
- 2026-08-13：真实运行 `Install-WebUI.bat` 发现 `-ProjectRoot "%~dp0"` 会因 `%~dp0` 的
  尾部反斜杠把非法引号传给 Windows PowerShell。入口改用 `-ProjectRoot "%~dp0."`；动态回归
  与实际 BAT 均确认路径错误消失，当前按设计停在缺少生产 `install-manifest.json` 的发布门禁。
  `test_source_bootstrap_powershell.py` 22 项通过。
