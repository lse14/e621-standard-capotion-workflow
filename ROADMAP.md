# ROADMAP

## 当前目标

让用户从 GitHub 获取源码后，在 Windows 10/11 x64 电脑上直接双击
`Install-WebUI.bat`，无需预装 Python、Node、CUDA Toolkit、Visual Studio 或 Windows
SDK，即可得到可离线运行的 E621 打标、质量评分、Qwen3 tokenizer 和 OCR 环境。

设计于 2026-08-11 经用户批准。实现正在按冻结清单、下载、路径事务、自举和离线探测的顺序推进。

## 已确认范围

- [x] 安装界面使用命令行窗口。
- [x] 源码目录原地自举，运行时和资源只写入项目目录。
- [x] 首发只支持 E621，不安装 Danbooru 资源。
- [x] E621 EVA02 Tagger、Qwen3 0.6B tokenizer 和完整质量评分栈必装。
- [x] NVIDIA 电脑使用 CUDA 路线；不可用时自动使用 CPU 路线。
- [x] NVIDIA 电脑默认同时安装 OCR CPU 和 OCR GPU；无 NVIDIA 电脑安装 OCR CPU。
- [x] 不要求用户安装 CUDA Toolkit，CUDA Python 依赖从官方源获取。
- [x] 所有下载固定来源身份、Revision、大小和 SHA-256。
- [x] 下载支持续传、私有 staging、验证后发布和离线探测。
- [x] 成功后清理临时 wheel、完整 staging 和构建缓存。
- [x] 2026-08-12：OCR 模型改为用户手动下载的延迟资源；基础安装仍部署 OCR CPU
  runtime（NVIDIA 时再部署 GPU runtime），OCR 默认关闭。用户启用 OCR 而模型缺失或
  哈希不符时，预检必须停止并指向官方链接、`ocr-model-archives\\`、大小和 SHA-256。

## 实施顺序

- [x] R1: 核对当前源码、运行时、资源、忽略规则和安装入口。
- [x] R2: 比较完整便携包、拆分包和源码自举方案。
- [x] R3: 批准源码自举架构、组件边界、错误恢复和验收标准。
- [x] R4: 写入并复核正式设计说明。
- [x] R5: 编写可执行实施计划，并按依赖顺序拆分任务。
- [-] R6: 建立冻结的安装组件清单和清单验证器。
- [-] R7: 建立预编译 CPython 3.11.15 基础资产的构建、探测和发布流程。
- [-] R8: 实现 PowerShell 首阶段自举、日志、空间检查和基础资产获取。
- [-] R9: 实现 Python 下载器、断点续传、哈希验证和事务发布。
- [-] R10: 增加 Caption 和 Policy 的 CPU/CUDA 安装变体。
- [-] R11: 接入 E621、Qwen3、质量评分和 E621 索引资源下载。
- [-] R12: 接入默认 OCR CPU，以及 NVIDIA 机器的 OCR GPU 安装。
- [-] R13: 接入幂等修复、缓存清理和失败恢复。
- [-] R13.1: 将 OCR 模型从基础安装成功条件分离，统一手动归档路径并在基础安装后自动启动 WebUI。
- [-] R13.2: 建立可审计的生产 inventory、CPython 基础资产候选/Release 身份、许可证证据账本和干净机验收运行器。
- [ ] R14: 在无开发环境的 CPU 和 NVIDIA 干净机上完成端到端验收。
- [ ] R15: 发布版本化基础资产和源码下载入口，并复验公开链接。

## 验收标准

- [ ] Windows 10/11 x64 干净机没有 Python、Node、CUDA Toolkit、Visual Studio 和
  Windows SDK，双击一次即可安装。
- [ ] 中文、空格和长路径下可以安装、启动和修复。
- [ ] CPU 机器不会下载不可用的 CUDA 或 OCR GPU 组件。
- [ ] NVIDIA 机器实际完成 Caption、质量评分和 OCR GPU 推理探测。
- [ ] E621 Tagger、Qwen3 tokenizer、完整质量评分栈和 OCR 模型均可断网加载。
- [ ] 中断大文件下载后重新运行可以续传。
- [ ] 大小或 SHA-256 不符的文件不能进入正式目录。
- [ ] 磁盘不足、下载失败或探测失败不破坏上一次可用环境。
- [ ] 第二次安装会校验并跳过完整组件，不进行无意义重复下载。
- [ ] 成功后没有 wheelhouse、完整 staging 或开发工具残留。
- [ ] 安装器不修改系统 Python、`PATH`、注册表或用户数据。

## 已知风险

- [!] 当前 `policy` 和 `caption-e621` 锁偏向 CUDA，需要建立并验证 CPU 变体。
- [!] NVIDIA 完整安装按当前文件实测约 12.7 GiB，安装前必须动态计算工作空间。
- [!] Hugging Face、PyTorch、OpenAI、PyPI、Paddle 和 GitHub 的网络可达性不由项目控制。
- [!] OCR 和部分第三方模型的许可证状态仍需在公开发布前完成核对。
- [!] 七个旧 OCR staging 目录目前无法访问；可测文件和可测大小均为零，但尚未删除。
- [!] `Clean-OcrGpuArtifacts.ps1 -Apply` 的 Python 驱动当前拒绝 Clean Apply，需在相关实施项中修复。

## 已完成清理

- [x] 删除约 16.0 GiB 的 `.runtime-build\ocr-gpu` 失败构建残留。
- [x] 删除约 5.27 GiB 的 `packaging\wheelhouse` 构建缓存。
- [x] 删除约 3.38 GiB 的可测 OCR 导入暂存内容。
- [x] 清理后 Core 探测输出 `anima-core-runtime-ok`。

## E621 索引发布记录

- [x] 2026-08-12：按用户明确要求，只将
  `resource-library/classification-indexes/e621-classify-20260724-v1` 纳入源码 Git。
  `.gitignore` 保持其他 `resource-library`、运行时、模型、数据集和缓存为忽略状态。
- [x] 2026-08-12：项目内嵌 Python 重新计算两个载荷的大小和 SHA-256，均与
  `resource.json` 一致；`validate_resource_library.py` 识别默认 E621 profile，分类资源
  fingerprint 为 `530323a5d1ca5c3f903c0d57b04d6f1014cdcc0ca01b8de5dc0a41e27e1d2baf`。
- [!] 2026-08-12：该索引含 E621 标签/别名数据和 21 条 Wiki 投影；已在
  `docs/THIRD_PARTY_NOTICES.md` 记录 E621 API/Terms URL 及第 4 节的再分发限制。
  本次索引提交不构成生产安装清单、模型许可闭环、GitHub Release 或 R11/R15 验收完成。
- [x] 2026-08-12：项目内嵌 Python 对 Git 暂存对象和由暂存树构造的临时源码 ZIP
  分别重新核对两个载荷，均保持声明的大小/SHA-256；`test_resource_catalog.py` 23 项、
  `test_classify_resource.py` 4 项通过，实际加载确认 E621 分类投影与 21 条 Wiki 记录。
  `.gitattributes` 只对该受哈希保护的词典禁用 Git 文本换行转换和文本 diff，避免
  `core.autocrlf=true` 改变发布字节。
- [x] 2026-08-12：按用户明确要求，将
  `resource-library/replacement-indexes/e621-replace-20260726-v2` 的完整受清单约束资源包
  纳入源码 Git：`resource.json`、`e621_tag_replacement_index.csv` 和随附说明书。CSV 为
  3,902,020 bytes，SHA-256 为
  `24ad8388580a6c3628dec44bd813897c278e4f1b04fccd810f22acaf97c1cbe7`；其资源
  fingerprint 为 `3cabbeeffd379a893a0b53d427c3dbb26ea6c587f474ae761b21afde4ee4c47b`。
- [x] 2026-08-12：项目内嵌 Python 运行 `test_replace_resource.py` 5 项、
  `test_replace_processing.py` 3 项和 `test_resource_catalog.py` 23 项均通过；实际
  `ReplaceWorker` 加载 86,922 条规则并验证 keep/replace/drop 输出。Git 暂存 CSV 与
  暂存树 ZIP 均保持 3,902,020 bytes 及声明 SHA-256；ZIP 只包含 `resource.json`、CSV
  和清单引用的说明书。
- [!] 2026-08-12：替换索引使用 E621 tags、aliases、implications 和 Wiki 导出构建；E621
  Terms <https://e621.net/terms_of_use> 第 4 节限制网站内容的复制和再分发。第三方声明
  已记录来源、哈希和限制；本次提交不构成独立再分发许可、模型许可闭环、GitHub Release
  或 R11/R15 验收完成。

## R5 验证记录

- [x] 2026-08-11：已写入 `docs/superpowers/plans/2026-08-11-source-bootstrap-installer.md`，
  覆盖 PowerShell 自举、冻结清单、下载/事务、CPU/CUDA、必装资源、离线探测、
  故障矩阵、前端产物与发布门禁。
- [x] 2026-08-11：已复读计划并运行 `git diff --check`；当前隔离 worktree 不含
  `.runtime-build`，计划显式要求后续测试只使用受控项目内嵌解释器，未把缺少
  开发运行时误报为测试通过。

## R6 进行记录

- [-] 2026-08-11：已实现标准库安装清单验证器，覆盖 HTTPS/允许主机、完整
  Hugging Face revision、大小/SHA-256、Windows 相对路径、CPU/CUDA 变体和
  Release 资产身份；项目内嵌 CPython 测试 `test_source_bootstrap_manifest.py`
  已执行 6 项通过。
- [!] 生产 `install-manifest.json` 仍不能冻结：CPython 基础资产和项目生成 E621
  索引必须使用实际公开 Release URL、大小和 SHA-256；尚未得到可验证的资产清单，
  不会填入猜测值。

## R8 进行记录

- [-] 2026-08-11：`Install-WebUI.bat` 已改为只调用 Windows 自带
  `bootstrap_install.ps1`；新脚本检查 Windows x64、项目根/重解析点、项目内可写日志、
  固定清单、NVIDIA 路线、`Get-Volume` 空间和 Range 续传的 CPython 基础资产下载，
  只在项目内 `.runtime-build` 写入状态、缓存、staging 和日志。
- [-] 2026-08-11：桌面控制脚本的 launcher 状态和 stdout/stderr 日志已迁入
  `.runtime-build\\launcher`；`Install` 仅接受带 `install-state.json` 的完整安装，
  不再提示或跳过 OCR。
- [-] 2026-08-11：项目内嵌 CPython 3.11.15 运行
  `test_source_bootstrap_*.py` 23 项和 `test_desktop_control.py` 6 项通过；其中真实
  Windows PowerShell 临时目录测试验证缺少清单时非零退出并只创建项目内 UTF-8 日志。
- [!] 生产清单、其固定 SHA-256 和 CPython Release 基础资产仍不存在，故 bootstrap
  当前在任何下载前明确停止；未运行真实下载、解压、CPU/GPU 安装或干净机验证，不能
  视为一键安装已经可用。

## R7 进行记录

- [-] 2026-08-11：已新增仅供维护端使用的 `build_bootstrap_runtime.ps1`；它要求已有
  CPython 3.11.15 base，核对 `python.exe`、`python311.dll`、`python311._pth` 和标准库，
  用 `-B -I` 执行离线标准库探测，再以固定条目时间写入 ZIP 和 provenance。目标安装器
  不调用此脚本。
- [-] 2026-08-11：以只读项目内嵌 Core Python 实际运行打包器，fixture ZIP 包含
  `python.exe`、`python311.dll`、`python311._pth`、`Lib/os.py`，provenance 为
  CPython `3.11.15` 与 `bootstrap-stdlib-ok`；临时目录已清除。
- [!] 尚未从受控 CPython base 生成、核对并公开发布真实基础资产，因此没有可写入生产
  `release-artifacts.json` 的 URL、大小或 SHA-256。

## R10 进行记录

- [-] 2026-08-11：已增加 Caption/Policy 的独立 CPU 与 CUDA `.in`/`.lock`；CPU 输入
  使用 `onnxruntime` 和 `torch/torchvision +cpu`，不含 CUDA/GPU payload 标识，CUDA
  输入保留已批准的 `onnxruntime-gpu` 和 `+cu128` 组合。
- [-] 2026-08-11：读取 PyPI 与 PyTorch 官方索引后写入 CPU direct-wheel 哈希；单测验证
  CPU 变体无 CUDA 标识，生成器拒绝缺 URL/大小/SHA、Release 身份不完整和 wheel lock
  哈希不匹配。
- [ ] 尚未下载完整 CPU/CUDA wheelhouse、组装 runtime 或进行实际 CPU/CUDA 离线推理，
  因此两条变体尚未标记完成。

## R9 进行记录

- [-] 2026-08-11：已实现标准库下载器；项目内嵌 CPython 运行
  `test_source_bootstrap_download.py` 共 5 项通过，覆盖 HTTP Range 续传、服务器
  忽略 Range 时安全重下、允许主机校验、SHA-256 错误删除与失败时保留 `.partial`。
- [-] 2026-08-11：事务发布和清理边界已有路径安全原语与单元证据；下载器接入实际组装、
  幂等状态和端到端失败矩阵尚未实施，因此 R9 不标记完成。

## R13 进行记录

- [-] 2026-08-11：已实现 `packaging/installer/paths.py` 的 Windows 相对路径校验、
  ZIP 穿越/设备/大小写碰撞/链接和特殊条目拒绝、项目内 staging、日志式目录发布、
  中断恢复及成功/失败清理边界；发布目标仅允许 `.runtime-build\\runtimes` 或
  `resource-library`，不会替换 `data`、`output` 或源码目录。
- [-] 2026-08-11：项目内嵌 CPython 3.11.15 以
  `-m unittest discover -s tests\\unit -p test_source_bootstrap_paths.py -v` 执行 8 项通过；
  再以 `-p test_source_bootstrap_*.py` 执行清单、下载和路径组合测试共 19 项通过。
- [ ] 实际安装器的组件指纹跳过、漂移修复、失败后完整缓存处理和端到端矩阵仍待后续任务验证。

## R11/R12 进行记录

- [-] 2026-08-11：新增标准库 `assemble.py` 与 `install.py`，以冻结清单选择 CPU/NVIDIA
  组件；NVIDIA 选择 Caption/Policy CUDA 并同时保留 OCR CPU/GPU，CPU 不选择 CUDA 或 OCR GPU。
  runtime 仅在项目内 staging 从 base、已核验 wheel 和源码复制组装，不调用 pip；wheel 路径冲突、
  目录链接和 staging 越界均会拒绝。
- [-] 2026-08-11：组件跳过要求变体指纹、完整文件大小/SHA-256 和现有 runtime manifest 同时匹配；
  发布后才写 `.runtime-build\manifests\install-state.json`，第二次 fixture 安装验证零 fetch。
  Caption/Policy runtime manifest 使用所选 `*-cpu`/`*-cuda` lock，而不是旧的 CUDA 偏向 lock。
- [-] 2026-08-11：`bootstrap_install.ps1` 现在将已安全展开的项目内 CPython base 目录传入
  `install.py`。默认 probe 当前明确 fail-closed；尚未生产 `install-manifest.json`，也没有真实
  E621、质量、Qwen3 或 OCR artifact / resource JSON / 离线推理证据，不能标记 R11 或 R12 完成。
- [x] 2026-08-12：Task 7 的 `probes.py` 以独立 `-B -I` 子进程清除代理、设置离线环境并封锁
  socket；fixture 验证拒绝 import-only、错误 CPU/GPU 证据和 OCR CPU/GPU 文本不一致。CUDA
  Caption/Policy 组失败会延后其共享 Tagger/质量资源判定，重建 CPU 后重新探测整组；资源仍失败时
  不发布状态。OCR GPU 失败会丢弃 GPU staging，CPU OCR 保留。
- [x] 2026-08-12：项目内嵌 CPython 3.11.15 执行 `test_source_bootstrap_*.py` 42 项、
  `test_source_bootstrap_fixture.py` 1 项、`test_desktop_control.py` 6 项全部通过；
  `py_compile packaging/installer/install.py packaging/installer/probes.py` 退出码为 0。
- [!] 上述是 stub/fixture 与网络封锁行为的本地证据，不是实际 E621、Qwen3、完整质量栈或
  PaddleOCR 模型加载/推理，也不是 NVIDIA、干净机、中文路径或公开下载链接验收；生产清单与
  不可变 artifact inventory 仍缺失。

## Task 8 验证记录

- [x] 2026-08-12：安装成功在写入 `install-state.json` 后调用既有
  `cleanup_success`；PowerShell 首阶段增加成功清理 `bootstrap`、完整 `cache`、`staging`、
  `transactions` 和 `build-cache` 的项目内路径，失败路径只清理完整缓存并保留可续传
  `.partial` 与日志。fixture 安装单测覆盖成功清理和第二次零 fetch。
- [x] 2026-08-12：新增只读 `packaging/scripts/Validate-SourceBootstrapRelease.ps1`，检查
  顶层清单、必装 E621 组件、HTTPS/允许主机、完整 Hugging Face revision、大小/SHA-256、
  CPU/CUDA 变体、发布资产身份、`frontend/dist/index.html` 和第三方声明；脚本不联网、不
  发布、不写项目状态。中文项目路径下实际运行并按预期 fail-closed。
- [x] 2026-08-12：只使用项目内 Node v24.18.0 执行 `npm ci`、`npm run typecheck` 和
  `npm run build`；`frontend/dist/index.html` 与两个静态资源已生成并纳入源码发布输出，
  未纳入 `node_modules`。
- [!] 2026-08-12：当前 worktree 没有真实 `install-manifest.json`、`release-artifacts.json`
  或已核对模型许可证；发布门禁明确报告 `install-manifest.json is missing`。这不是一键安装
  成功证据，也不能替代 CPU/NVIDIA 干净机、公开 URL 或真实模型离线推理验收。

## Task 9 最终验证记录

- [x] 2026-08-12：对比 `2e85063...HEAD` 的最终 diff，确认修改集中在源码自举、清单/下载/
  路径事务、离线探测、PowerShell 入口、前端静态产物、发布门禁和对应文档测试；未触碰七个
  不可访问的旧 OCR staging 目录。
- [x] 2026-08-12：项目内嵌 CPython 新鲜运行 `test_source_bootstrap_*.py` 45 项、
  `test_desktop_control.py` 6 项、`test_source_bootstrap_fixture.py` 2 项，均退出码 0；
  `install.py`/`probes.py` `py_compile` 退出码 0。
- [x] 2026-08-12：项目内 Node v24.18.0 新鲜运行 frontend `typecheck` 和 `build` 均退出码 0；
  构建后删除 `frontend/node_modules`，确认 `vite` 进程数为 0，未启动或遗留开发服务器。
- [!] 2026-08-12：`Verify-Project.ps1 -Level Fast -OcrMode Auto` 因当前隔离 worktree
  没有 `.runtime-build` 而非零退出；发布 validator 因没有真实生产 `install-manifest.json`
  而非零退出。这两个结果是已知门禁，不可记录为通过。
- [!] R14/R15 仍未完成：没有无开发环境 CPU/NVIDIA 干净机、真实模型离线推理、已核对许可证、
  公开基础资产 URL 或 GitHub Release 复验，因此不 push、不创建 Release、不声称公开可用。

## R13.1 进行记录

- [x] 2026-08-12：基础 `installation_plan()` 即使面对未来清单中的可选
  `ocr-models` 组件也会排除它；PowerShell 空间预算同步跳过该延迟资源和 CPU 路线不可用的
  仅 CUDA 可选组件。基础状态写入后，只有三个归档文件都存在于项目根
  `ocr-model-archives\` 时才导入 OCR 模型；缺失时基础安装保持完整并提示
  `OCR_MODEL_DOWNLOAD.md`。
- [x] 2026-08-12：模型专用导入复用归档哈希校验、安全 staging、离线 CPU probe 与资源事务
  发布，但使用已经发布的 `.runtime-build\runtimes\ocr-paddle`，不重建 runtime、wheelhouse
  或工具链。`bootstrap_install.ps1` 在安装器成功后记录绝对指南路径并调用
  `desktop_control.ps1 -Action Start`；Start 非零时保留安装状态和日志并非零退出。
- [x] 2026-08-12：使用项目内嵌 CPython
  `E:\Desktop\Anima idg标准标注处理\.runtime-build\runtimes\core\python.exe` 实际执行
  `test_source_bootstrap_install.py` 22 项、`test_source_bootstrap_powershell.py` 7 项、
  `test_desktop_control.py` 6 项及模型专用导入回归 1 项，均通过；
  `assemble.py`、`install.py`、`ocr_resource.py` 的 `py_compile` 退出码为 0。
- [!] 当前 worktree 未包含被 Git 忽略的 `.runtime-build`，因此整个
  `test_ocr_resource_scripts.py` 仍无法在此 worktree 运行其调用实际 embedded Core runtime 的
  旧 CLI/wrapper 用例；本项只记录上述不依赖该缺失路径的定向回归，不把它表述为真实 OCR
  模型推理或干净机验收。
- [x] 2026-08-12：OCR-enabled 任务在模型资源缺失或 `SHA-256 mismatch` 时统一抛出
  `ocr_resource_install_required`，并明确指向 `OCR_MODEL_DOWNLOAD.md`、项目根
  `ocr-model-archives` 和再次双击 `Install-WebUI.bat`。OCR 默认关闭，未启用 OCR 的
  任务不因此阻断。
- [x] 2026-08-12：发布校验继续要求 `ocr-cpu`，但不再要求手动归档的 `ocr-models`；
  README、RULES、models README、第三方声明和下载指南同步说明三份官方归档、本地哈希
  校验、离线 CPU probe、无 `-OcrMode` 参数和不镜像权重的边界。
- [x] 2026-08-12：项目内嵌 CPython 定向运行 7 项 OCR 预检、8 项 source-bootstrap
  PowerShell 和 4 项文档契约测试均通过；`git diff --check` 无空白错误。未运行真实 OCR
  模型、NVIDIA、干净机或公开 source ZIP 安装，R14/R15 仍为发布门禁。
- [x] 2026-08-12：`main` 是当前 `codex/source-bootstrap-installer` 分支的祖先；Git
  只跟踪 E621 分类索引的 `resource.json`、词典、Count SQLite，以及替换索引的
  `resource.json`、CSV 和说明书，没有跟踪模型权重、runtime 或 OCR 归档。
- [x] 2026-08-12：只使用项目内嵌
  `E:\Desktop\Anima idg标准标注处理\.runtime-build\runtimes\core\python.exe` 新鲜运行
  source-bootstrap 单元测试 54 项、desktop-control 单元测试 6 项和 source-bootstrap
  fixture 集成测试 2 项，均通过。
- [!] 2026-08-12：`Validate-SourceBootstrapRelease.ps1 -ProjectRoot .` 如预期以
  `Source-bootstrap release gate failed: install-manifest.json is missing` 退出 1。它是
  生产清单/Release 元数据缺失的 fail-closed 发布门禁，不是安装成功证据；R14/R15 仍未完成。

## R13.2 进行记录

- [x] 2026-08-12：新增维护端 `Test-BootstrapRuntimeAsset.ps1`，独立核对基础 ZIP 的
  provenance 字段、文件名、大小、SHA-256、builder 脚本 SHA-256、安全 ZIP 条目和
  解压后的 `python.exe -B -I` 标准库探测。单测实际生成 ZIP、验证成功并以等长篡改触发
  `SHA-256` 拒绝，且拒绝反斜杠 `Lib\\..\\outside.txt` 路径穿越；builder 改为 .NET
  SHA-256，以兼容项目内嵌 Python 启动的 Windows PowerShell 5.1（不依赖 `Get-FileHash`
  的模块自动加载）。
- [x] 2026-08-12：在项目内 `.release-candidate\bootstrap` 实际生成未跟踪候选
  `cpython-3.11.15-win-amd64.zip`，大小 `33,264,397` bytes，SHA-256
  `f7a36991fc6ac035f7e3bd30fd8badd06d4309590323bedda2ec958aa0d17096`，并由独立
  PowerShell 进程通过 provenance/ZIP/离线标准库验证。该候选仅为本地构建证据，尚未
  上传、未生成 Release URL，也未能作为生产 `release-artifacts.json` 身份。
- [!] 候选 provenance 当前对应构建时 commit；每个后续源码 commit 必须重新生成候选并
  核验，只有用户授权上传后，从实际公开 Release 重新下载并核对的 ZIP 才可写入生产清单。
- [x] 2026-08-12：基础资产 builder 与 verifier 的离线探测现在要求精确
  `sys.version_info[:3] == (3, 11, 15)`，不再把同一 minor 版本的其他 CPython 补丁版
  当作可发布基础资产；项目内嵌 CPython 的 release-build 单测 5 项通过。
