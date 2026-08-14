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

## 2026-08-13 发布准备验证

- [x] 以实现提交 `790502c806f813cca2609281fdf6a687886f90d8` 重建 CPython 3.11.15
  基础 ZIP；本地字节为 `33,887,443`，SHA-256 为
  `a7bef1285f1a0f4007de9ede5752f105dcf2b137d54670074d16503554fa0169`，独立离线
  verifier 通过。
- [x] 生成生产 `install-manifest.json`、`release-artifacts.json`，并把 manifest 的
  SHA-256 `2a3d55f74557cb90c06f42790f0b054d21e04ba062612e897d321f84841774f6` 绑定到
  `bootstrap_install.ps1`；`.gitattributes` 强制两个被绑定 JSON 为 LF，避免 clone
  的换行转换破坏身份校验。
- [x] `test_source_bootstrap_*.py` 89 项、inventory `--validate-only`、资产 verifier，
  以及 Windows PowerShell 5.1/PowerShell 7 的本地发布门禁均通过。PowerShell 7 的
  JSON UTC 时间戳解析差异已有回归测试和兼容修复。Release URL 使用 GitHub 迁移后的
  `lse14/e621-standard-capotion-workflow` 规范仓库。
- [x] R15: GitHub Release `source-bootstrap-e621-v1` 已在规范仓库
  `lse14/e621-standard-capotion-workflow` 发布，目标提交
  `587065d942983bc330330d0ac4983e2fbf5ce5df`；公开 ZIP URL 为
  `https://github.com/lse14/e621-standard-capotion-workflow/releases/download/source-bootstrap-e621-v1/cpython-3.11.15-win-amd64.zip`。
  GitHub asset 元数据与公开重新下载均验证 `33,887,443` bytes 和 SHA-256
  `a7bef1285f1a0f4007de9ede5752f105dcf2b137d54670074d16503554fa0169`。
- [x] 2026-08-13 最终修复基线 `01516f4795156dece6728957a2397d57df6db683`
  重新生成并离线验证不可变资产 `cpython-3.11.15-win-amd64-01516f4.zip`；GitHub
  Release 上传响应和公开 URL 重新下载均为 `34,719,985` bytes、SHA-256
  `b343e49a85d2240c23ae0e75b27e06529c6c859bce350bb5fa77d25cd01540e9`。生产 manifest
  已重建并绑定 SHA-256 `cf19935c75bdc63c6fa3c843222d7f8baad27b551a280dbe38f7a74d9dc0c426`。
- [!] R14 仍未完成：当前开发主机预检为 `not-clean`，不能替代 CPU/NVIDIA 干净机验收。
- [x] 2026-08-13：修复 `Start-WebUI.bat` 双击失败时窗口立即关闭的问题。现在非零退出码
  会显示错误码、`.runtime-build\\launcher` 日志目录并等待确认；成功路径仍自动返回。
  新增回归测试覆盖该可见错误契约。
- [x] 2026-08-14：外部 NVIDIA 安装日志确定生产安装在 runtime 组装阶段失败：已发布 CPython
  基础 ZIP 错误包含开发 `core` runtime 的 982 个 `Lib/site-packages` 条目，随后复制当前
  `anima_caption_format` 源码时触发 `duplicate wheel path`。先加入基础资产 site-packages
  必须为空的失败关闭门禁。以 `9230c7703c465d0c6dcffe9420764cccf294bc16` 重建并发布
  干净资产 `cpython-3.11.15-win-amd64-9230c77.zip`，独立 verifier 与公开重下均核对
  `20,565,968` bytes、SHA-256 `3ab496658760f8bbf90b6593231ba1f4de90d4bb732e7ce19f25683382e1424a`，
  ZIP 的 site-packages 条目为 0；production manifest 绑定 SHA-256
  `47fc7c8ac3a8bae1351a57c26ae046152b96e87f3679703848c66ec00354bb07`。
- [x] 2026-08-14：在用户提供的 `E:\Desktop\tagger测试` 从公开 `main@d550fb3` 实际继续
  NVIDIA 安装，确认 Python 默认 User-Agent 会被 PyTorch/R2 以 HTTP 403 拒绝，且长下载在
  已取得数据后仍会消耗重试预算。下载器现固定 `Anima-Source-Bootstrap/1.0`，有进展时重置
  无进展预算，并在 EOF 读取错误后先发布已完整校验的文件；7 项下载器回归通过。
- [x] 2026-08-14：同一真实安装核对 `torchvision-0.24.1+cu128` 公开字节为
  `9,365,769` bytes、SHA-256
  `6d836745bd3130ef8f3569c9f0d9d70103b5e2e9fa058310bcac5f63bcf2d043`；修正 inventory
  与 `policy-cuda.lock` 的单字符错误，重建 production manifest 并绑定 SHA-256
  `5d428429aa9a39f5ea58890fc18cebb286d1052f89120f527d071ae22ed0c72a`。过期的 fixture
  断言也已改为要求现有公开生产资产通过发布门禁。
- [x] 2026-08-14：`E:\Desktop\tagger测试` 继续安装至全部 94 个 wheel 缓存和 runtime
  组装后，发布门禁拒绝 `e621-classification-source-resource-json`。核对发现两份受保护
  `resource.json` 的 production identity 来自维护工作树 CRLF 字节，而 Git HEAD / 新 clone
  是 LF 字节。现将两条 `.gitattributes` 规则固定为 `text eol=lf`，inventory 改为 Git blob
  的实际大小/SHA-256，并增加逐项使用 `git show HEAD:<path>` 校验全部 source-tree artifact
  的回归测试；重建 manifest 后绑定 SHA-256
  `f489d8782fbe71ff2bc44ede8bc5be3503aaf5151a297b8c0d4e7636c8bdf6bf`。
- [x] 2026-08-13：在 `F:\AnimaSourceBootstrapNvidiaValidation-20260813-01` 的默认
  `main` 新 clone 上实际执行 `Install-WebUI.bat`，复现首次下载尚无 `.partial` 时
  PowerShell 将 `-and` 错绑为 `Test-Path` 参数的生产失败；四处布尔表达式均补齐子表达式
  括号，并增加回归扫描。修复后 source-bootstrap 91 项、公开资产字节门禁和 manifest
  绑定复核通过；需要发布修复后继续真实 NVIDIA 安装。
- [x] 2026-08-13：在 `F:\AnimaSourceBootstrapNvidiaValidation-20260813-02` 从公开
  `main@bcbdc90c2fa1d0e9d4f05c6ce4dffe886f383426` 新 clone 后真实执行入口，确认
  NVIDIA 路线、37,198,942,841 bytes 空间门禁、公开 CPython 下载和解压均通过；随后
  `python -I install.py` 因隔离模式不包含脚本目录而以 `ModuleNotFoundError: assemble`
  退出 1。新增真实隔离入口回归先复现红灯，再由 `install.py` 显式加入自身受信任目录后
  通过；source-bootstrap 92 项、Python 编译、PowerShell AST、inventory 审计、公开资产
  字节重下和发布门禁均退出 0。尚需发布修复并从全新 clone 继续完整 NVIDIA 安装，R14
  不因此完成。
- [x] 2026-08-13：公开 `main@3ff876ef0c513b3a1bcda61d095f89af25aeb589` 的
  `F:\AnimaSourceBootstrapNvidiaValidation-20260813-03` 继续到首个 runtime wheel，确定性
  复现 SHA-256 命名的无扩展名缓存被 `assemble_runtime()` 误判为非 `.whl`；PowerShell
  失败清理同时因刚退出 Python 仍占用 `libcrypto-3.dll` 而用 Access Denied 覆盖原错。
  runtime 现用受清单约束的 `artifact.relativePath` 判断 wheel 类型，仍从已校验缓存读取并
  走安全 ZIP 解压；外层 catch 先保存原错，清理错误仅追加日志。两项均按红绿回归修复，
  source-bootstrap 95 项及全部发布门禁通过；尚需发布后从新 clone 继续端到端安装。

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

- [x] 2026-08-13：补齐 source-bootstrap OCR/验收回归。下载指南测试仅禁止实际命令
  `Install-WebUI.bat -OcrMode`，保留“参数不存在”的文字说明；OCR GPU probe 仅在显式
  `False` 时丢弃，模型缺失导致 `None` 时仍发布 CUDA runtime 并记录模型功能未验证；1 或
  2 个手动归档不会加载导入器，基础安装保留成功提示，三份齐全但导入失败仍 fail closed；完整
  OCR runtime 重建从 `packaging\wheelhouse\ocr-paddle` 读取缓存。
- [x] 2026-08-13：验收运行器仅在 `Install-WebUI.bat`、安装状态校验和
  `Stop-WebUI.bat` 均成功后写入 `passed`。Stop 缺失或非零将结果设为 `failed` 并写入
  JSON，避免停止失败被伪装成干净机验收通过。
- [x] 2026-08-13：使用 `E:` 项目内嵌 Core Python 对 `D:` 源码运行 source-bootstrap
  单测 88 项、desktop-control 单测 6 项，均通过；OCR wrapper 的路径/文档契约通过，
  inventory `--validate-only` 输出 `validated source bootstrap manifest for
  source-bootstrap-e621-v1`。完整 OCR script 套件为 14 项通过、2 项因 `D:` 源码树未安装
  embedded Core runtime 而显式跳过；这不构成 OCR runtime、真实模型或干净机验证。
- [x] 2026-08-13：`bootstrap_install.ps1` 的失败清理只删除 staging、展开的 bootstrap
  和 transactions；不再删除已验证的 CPython 缓存或可续传 partial。重试时完整缓存仍先执行
  大小/SHA-256 验证，验证失败的文件仍由下载器删除。新增动态回归以 Start 失败后的失败清理
  验证完整/partial 缓存保留、临时目录删除。
- [x] 2026-08-13：真实执行 `Install-WebUI.bat` 发现 `%~dp0` 的尾部反斜杠使 Windows
  PowerShell 收到含非法引号的 `ProjectRoot`；入口改为同仓库已有的 `%~dp0.` 形式。动态回归
  确认 BAT 现可到达预期的 manifest 门禁且不再输出 `Illegal characters in path`；
  `test_source_bootstrap_powershell.py` 22 项通过。
- [x] 2026-08-13：Task 14 最小本地复核完成。项目内嵌 CPython 定向运行
  `test_source_bootstrap_powershell.py` 16 项通过；
  `build_install_manifest.py --inventory source-bootstrap.inventory.json --validate-only` 成功；
  `git diff --check` 退出 0（仅有既有 CRLF 归一化警告）。未运行全量回归。
- [!] 2026-08-13：`Test-BootstrapRuntimeAsset.ps1` 针对当前 HEAD
  `1cba8eb0617a2bf87b832461c12b58843ad8ffaf` 退出 1：candidate provenance source commit
  不匹配。候选 ZIP 仍为本地、未发布资产，不能用于当前 commit 的生产身份。
- [!] 2026-08-13：默认 `Validate-SourceBootstrapRelease.ps1 -ProjectRoot .` 退出 1：
  `install-manifest.json is missing`。这是无公开 CPython Release identity / production manifest
  的 fail-closed 门禁；不表示安装成功或公开发布可用。真实 CPU/NVIDIA 干净机仍未运行。

- [x] 2026-08-13：新增 `Invoke-SourceBootstrapAcceptance.ps1`、
  `docs/SOURCE_BOOTSTRAP_ACCEPTANCE.md` 和 README 入口。运行器只在项目内
  `.runtime-build\acceptance` 写入 JSON；它区分 `passed`、`failed`、`not-clean`，检查
  Python/py/Node/npm/nvcc/cl/Windows SDK，并在完整模式的 finally 调用 `Stop-WebUI.bat`。
  无 `.git` 的源码 ZIP 保持 `sourceCommit: null`，不影响干净机预检结果。
- [x] 2026-08-13：项目内嵌 CPython 定向运行 `test_source_bootstrap_powershell.py` 16 项通过。
  当前开发机实际 `-PreflightOnly` 退出 1，生成
  `.runtime-build\acceptance\source-bootstrap-cpu-20260813T045815Z.json`，状态为
  `not-clean`，检测到系统 Python、py、Node、npm、nvcc、cl 和 Windows SDK；installer 与
  Stop-WebUI 均未调用。这不是 CPU/NVIDIA 干净机验收。

- [x] 2026-08-13：新增机器可读 `license-ledger.json`，发布校验器现在要求每个生产
  manifest `licenseReference` 有严格字段、HTTPS 证据 URL、UTC 时间、SHA-256 和明确
  delivery/redistribution 状态的账本条目。direct-upstream-only、local-only 和项目源码条目
  必须保持 `not-mirrored`；source-redistributed 只有 `approved` 且具有精确绑定清单
  source-tree 文件的负责人决定时才可放行。
- [x] 2026-08-13：项目负责人确认允许随源码/GitHub 分发当前两套 E621 派生索引；账本记录
  `user-confirmed-project-owner` 决定、E621 Terms URL/响应 SHA-256 和六个精确文件的
  大小/SHA-256。该记录是项目分发决定，不主张 E621 上游授予法律许可，也不替代 Terms 或
  适用法律核对。
- [x] 2026-08-13：使用项目内嵌 CPython 定向运行
  `test_source_bootstrap_powershell.py` 14 项通过；涵盖缺失账本、错误 local-only 镜像、
  pending E621 状态及缺失/不精确负责人决定的拒绝。未运行全量回归。

- [x] 2026-08-13：新鲜 NVIDIA clone 的生产安装证明 Python 异常路径不能在自身仍运行时删除
  bootstrap runtime；Windows 锁定 `libcrypto-3.dll` 并产生 `WinError 5`。`install_project()`
  失败时现保留 bootstrap/cache，只清理 staging/transactions；子进程退出后由 PowerShell
  外层完成 bootstrap 清理。新增回归后 source-bootstrap 单测 `96/96`、PowerShell AST、
  production inventory 校验和 `git diff --check` 通过。

- [x] 2026-08-14：`E:\Desktop\tagger测试` 的真实 NVIDIA 安装在固定 revision 的 E621
  Tagger `model.onnx` 下载处失败；故障现场中原 Hugging Face resolve URL 返回 `404`，追加
  `?download=true` 后返回完整 `2,527,938` bytes。13 个 Hugging Face 资产统一使用该下载
  响应参数，production manifest 重建并绑定 SHA-256
  `6cf63ac7421122ef6273fa18f56029b8a1fcc1c3642d17de0b11369de73415a1`；回归逐项约束 URL。
  2026-08-14 新鲜复核时两种 URL 均返回 `200` 和相同字节，故原 `404` 记录为代理/CDN
  时态故障，不表述为 Hugging Face 的永久行为。
- [x] 2026-08-14：下载器的 `ManualDownloadRequired` 原先未被 `install.main()` 捕获，导致
  BAT 失败时输出 Python traceback。入口现在返回退出码 1，并完整输出官方 URL、预期大小、
  SHA-256 和缓存提示；新增回归确认无 traceback。定向 installer/release-build 测试分别
  `33/33`、`17/17` 通过；完整 source-bootstrap `101/101`、fixture `2/2`、inventory、
  manifest 可重复生成与 SHA 绑定、Python 编译、PowerShell AST、公开 CPython 资产重下门禁
  和 `git diff --check` 均通过。该结果只关闭当前下载错误可见性阻断，尚不构成 NVIDIA
  端到端验收。
- [x] 2026-08-14：真实 BAT 链路进一步确认 Windows PowerShell 只记录多行 Python 异常的首行，
  会隐藏失败资产 URL。`ManualDownloadRequired` 现改为单行分号分隔消息，保留官方 URL、目标
  路径、大小和 SHA-256；新增回归先红后绿，下载器 `8/8`、installer `33/33` 通过。上次真实
  NVIDIA 安装仍因下载重试耗尽退出 1，尚未生成 install state 或启动 WebUI。
- [x] 2026-08-14：第二次真实 NVIDIA 安装日志显示完整失败资产后，进一步核对 Hugging Face
  固定 URL 的真实 302：E621 `model.onnx` 重定向到官方 `us.aws.cdn.hf.co`，而非原先逐项
  `allowedHosts` 中的 `huggingface.co`。13 个 Hugging Face 资产现显式允许这两个精确主机
  （不使用通配符），顶层 manifest 同步加入 CDN 主机；重建 manifest SHA-256 为
  `480188f5bc865565df62599a60df96a26a422bdd377037c9e4286051884747c2`。生产下载器实测
  2,527,938 bytes 与 SHA-256 `51f6873c7d8618cfceb6b335dbe41815d46992b5df41c153dbe08669b77ec49b`
  通过，未知主机仍由安全重定向校验拒绝。此前安装失败未生成 install state，尚未启动 WebUI。
- [x] 2026-08-14：第三次真实安装越过 E621/Qwen 下载后，RedRocket Hydra URL 返回 404。
  固定 revision API 证明文件实际位于 `models/jtp-3-hydra.safetensors`，且大小/SHA 与清单
  一致；修正遗漏的 `models/` 路径，重建 manifest SHA-256 为
  `3827bf769eb01b71eb64c534a0dbdf4d72b39923f5e728e1e4609486e2f11be4`。
- [x] 2026-08-14：第四次真实 NVIDIA 安装完成模型下载后，OCR GPU 组装发现多个 NVIDIA
  wheels 都含相同的空 `nvidia/__init__.py`。组装器现在只跳过字节完全相同的重复文件，
  仍拒绝不同内容的重复路径；新增相同内容测试通过，原有冲突拒绝测试保持通过。
- [x] 2026-08-14：第五次真实安装到离线 probe 时确认 `classify-e621`、`replace-e621`、
  `nl`、`export` 被初始化为失败但从未运行任何 probe，故生产安装必然失败。现为四个源码
  worker 增加网络阻断下的纯功能 probe，并把证据绑定组件 ID；组合回归通过，且四个已有
  嵌入式 runtime 实跑均返回 `kind=worker, check=ok`。
- [x] 2026-08-14：第六次真实安装进入 Caption/Policy CUDA fallback 后，CPU planner 被整个
  manifest 的 required CUDA-only `ocr-gpu` 阻断。CPU fallback 现只为明确允许 fallback 的
  `caption-e621`、`policy` 构建子计划；生产 manifest 回归确认不再扫描 OCR GPU。
- [x] 2026-08-14：第七次真实安装的 CPU fallback 在 Caption probe 以 `KeyError: 'tags.json'`
  失败。根因是 probe 把业务键 `resource.entrypoints` 直接传给要求文件名键的 `CaptionModel`，
  而生产 worker 已通过 `create_tagger_adapter(resource)` 做格式适配。新增回归先红后绿后，
  probe 改用同一适配器；已有 Caption CPU runtime 和完整 E621 资源真实离线推理返回
  `CPUExecutionProvider` 及 15 个 tags。source-bootstrap 单测 `106/106`、Python 编译、
  production inventory、release gate、manifest SHA 绑定和 `git diff --check` 均退出 0。
  完整安装、自动启动和健康检查仍待继续验证。
- [x] 2026-08-14：第八次真实 BAT 已越过 Caption probe，但首次发布后在 core 自校验失败。
  失败现场的 core `3,217` 个文件含 `60` 个合法空文件，Caption `4,170` 个文件含 `66` 个；
  `component_record()` 会记录空文件，而 `component_is_current()` 错把 `sizeBytes == 0` 当无效。
  回归用同一未漂移树先红后绿，校验现接受非负大小，仍拒绝负数并继续核对完整文件集合、
  精确大小和 SHA-256。source-bootstrap 单测 `107/107`、Python 编译、production inventory、
  release gate 和 `git diff --check` 均退出 0。完整 BAT 需发布此修复后继续验证。
- [x] 2026-08-14：第九次真实 BAT 完成发布并写入 `11,738,079` bytes 的 install state，
  随后仍在运行的 bootstrap Python 调用成功清理，删除自身 `libcrypto-3.dll` 时触发
  `WinError 5`，使入口退出 1 且未启动 WebUI。CLI 入口现要求内部成功清理保留正在运行的
  bootstrap，只清除 cache/staging/transactions；子进程退出后仍由外层 PowerShell 完成
  bootstrap 最终清理。source-bootstrap 单测 `109/109`、Python 编译、PowerShell AST、
  production inventory、release gate 和 `git diff --check` 均退出 0。
- [x] 2026-08-14：修复后真实 BAT 退出 0，install state 记录 NVIDIA 与 15 个组件，WebUI
  `GET /health` 返回 `status=ok`、首页返回 HTTP 200，`Stop-WebUI.bat` 退出 0；但浏览器加载的
  `GET /api/resources` 返回 400：`defaults.json mapping is invalid`。生产 defaults 和发布测试
  明确为 E621-only，catalog schema v2 却强制同时声明 Danbooru。解析器现允许非空的已知
  profile 子集，仍严格校验每个已声明 profile 的完整字段；真实 5 个已安装资源包加载后返回
  `e621.available=true`、0 个 invalid resource。以真实安装根运行 API `37/37`、预检 `29/29`，
  source-bootstrap `109/109` 均通过；D: worktree 缺模型导致项目默认资源用例无法完成，不将其
  记为通过。需发布到新 clone 后复验资源 API。
- [x] 2026-08-14：测试 clone fast-forward 到 `35a3700` 后，先对旧 core 制造可检测漂移，
  再由官方 BAT 自行重建 core/Caption 并更新状态；BAT 退出 0，runtime 中 `resource_catalog.py`
  与源码 SHA-256 同为 `5b251625...ae35`。实际 `/health` 为 OK、首页 HTTP 200、
  `/api/resources` 返回 schema 2、E621 available、0 missing、8 resources、0 invalid；
  install state 为 NVIDIA/15 组件，Stop 退出 0 且端口关闭，bootstrap 临时目录已清理。
  这是现有 NVIDIA 安装的修复/幂等路径证据，不替代最新 HEAD 的全新 CPU/NVIDIA 干净机矩阵。
- [x] 2026-08-14：`origin/main` 已从 `1ccdc42` 快进到 `6d37906`，旧仓库地址和迁移后的
  `lse14/e621-standard-capotion-workflow` 均返回同一 main SHA。合并前 source-bootstrap
  `109/109`、API `37/37`、预检 `29/29`、production inventory、公开 CPython 资产逐字节重下、
  release gate、manifest SHA 绑定、Python/PowerShell 语法均退出 0。
- [!] 2026-08-14：CPU/NVIDIA 正式 acceptance preflight 均以 `not-clean` 退出 1，明确检测到
  Python、py、Node/npm、nvcc、cl 和 Windows Kits；本机也没有 Windows Sandbox、Hyper-V
  控制台、VirtualBox、VMware 或 Docker。不能把本机新目录测试记为 CPU/NVIDIA 干净机通过。
- [x] 2026-08-14：从最新 main 克隆到全新中文/空格路径，无旧 `.runtime-build` 或 OCR 归档。
  NVIDIA 基础安装写入 15 组件状态并保留 `ocr-gpu`，日志明确记录 OCR 模型功能未验证；首次
  自动 Start 的 Windows 事件证据为 `Distributed core runtime verification failed`，稍后同一
  runtime 自检、手动 Start 及第二次 BAT 均成功。bootstrap 现在记录子 PowerShell 输出，并对
  该瞬态 Start 失败做一次 2 秒后的有界重试；TDD 回归先红后绿，source-bootstrap `110/110`、
  desktop-control `7/7`、PowerShell AST 和 `git diff --check` 通过。OCR 三份官方归档仍只由
  用户手动放置，不自动下载、镜像或发布。

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
- [x] 2026-08-14：修复 bootstrap 自动 Start 的输出句柄阻塞。`desktop_control.ps1` 的
  stdout/stderr 现在重定向到独立项目日志，bootstrap 只等待直接 PowerShell 子进程；浏览器等
  后代继承输出句柄时不再阻止 BAT 返回和成功清理。回归实测直接子进程在 4 秒门限内返回，
  8 秒后代仍独立存活；瞬态失败输出仍会写入主日志并重试一次。
- [x] 2026-08-14：最终提交前新鲜门禁通过：source-bootstrap `111/111`、desktop-control
  `7/7`、真实安装根 API `37/37`、预检 `29/29`、fixture `2/2`；production inventory、公开
  bootstrap 资产逐字节验证、release gate、manifest SHA 绑定、Python 编译、PowerShell AST
  与 `git diff --check` 均退出 0。OCR 三份官方归档的手动放置边界未改变。
- [x] 2026-08-14：从公开新仓库 main `9ebeb7a` 克隆到全新中文/空格路径，初始无
  `.runtime-build`、OCR 归档或模型资源。真实 BAT 发布 NVIDIA/15 组件，保留 `ocr-gpu`
  CUDA 和 Policy CUDA；Caption CUDA 离线 probe 失败后按设计回退 CPU。日志记录 OCR 模型
  未验证并指向手动三归档指南，WebUI 第一次 Start 成功且 bootstrap 进程正常返回。
- [x] 2026-08-14：同一 clone 第二次 `Install-WebUI.bat` 明确退出 0；14 个组件幂等跳过，
  Caption CPU fallback 重建后 Start 第一次成功。`/health` OK、首页 200、E621 available、
  8 resources、missing/invalid 均为 null；cache/staging/bootstrap/transactions 均清理。
  `Stop-WebUI.bat` 退出 0，8765 关闭且无项目进程残留。
- [!] 2026-08-14：最新 HEAD 的 CPU/NVIDIA 正式 acceptance preflight 均为 `not-clean`，
  检测到系统 Python/py、Node/npm、nvcc、cl 和 Windows Kits；本机没有可用隔离 Windows VM。
  且本机 Caption CUDA probe 回退 CPU，不满足 NVIDIA 正式矩阵要求的 Caption/Policy CUDA
  证据。因此上述全新 clone 实机成功不能记作 CPU/NVIDIA 干净机验收通过。
- [x] 2026-08-14：原始工作树中的 9 个未提交文件已保存在本地归档分支
  `archive/original-local-changes-20260814`，归档提交为 `fec09ec`（`Archive original OCR
  bootstrap fixes`）；该分支未推送，不属于公开 `main`。
- [x] 2026-08-14：本地测试生成物已清理：`test-results`、`.playwright-cli` 不存在，
  `.test-tmp` 当前为空；三个全新安装验证 clone `tagger测试`、`tagger全新验证 NVIDIA
  20260814`、`tagger最终验证 NVIDIA 20260814` 已永久删除。此项仅记录本机归档与清理，
  不改变 CPU/NVIDIA 正式干净机验收仍未完成的状态。

## R16 可靠性与 10 万图稳定性

- [x] 2026-08-14：用户确认稳定性优先，允许合理模块耦合，不进行 `StateDatabase`、Pipeline
  或前端组合根的无证据拆分。实施范围限定为线程启动/恢复状态、正式丢弃后的 live dataset
  lock、worker stderr 管道，以及 10 万图容量回归。
- [x] 设计与实施计划分别位于
  `docs/superpowers/specs/2026-08-14-reliability-and-100k-stability-design.md` 和
  `docs/superpowers/plans/2026-08-14-reliability-and-100k-stability.md`。
- [x] 修改前 10 万样本基线 `2/2` 通过，用时 `79.163s`；最大峰值内存 `2,195,229` bytes，
  最大 SQLite 文件 `146,776,064` bytes，WAL truncate 后为 `0` bytes。
- [x] 五个已复现故障均按 TDD 修复：首次线程启动失败会注销线程；暂停恢复在任何 SQLite
  改写前拒绝尚未收尾的旧线程；恢复线程启动失败会还原 `interrupted` 和原 `resume_status`；
  discard 成功后释放 `JobPreparationService` 持有的 live Windows lock；worker stderr 由单个
  daemon drainer 持续排空并只保留末尾 `65,536` bytes。新增回归均先在旧实现上复现失败。
- [x] 受影响完整套件通过：Pipeline `32/32`、API `39/39`、NL runner `32/32`、Repair `9/9`、
  lifecycle `5/5`、stdio transport `4/4`、transport restart boundary `1/1`，共 `122/122`。
  Core 模块分解 `13/13`、worker boundary `6/6`、Windows path-lock matrix `5/5` 通过。
- [x] 提交前独立代码审查补出两个失败路径并完成回归：discard 已持久化后 live-lock release
  失败可由同一 confirmed API 请求重试；stderr drainer 线程自身启动失败会回收 worker/管道并
  统一为 transport 错误。两项新增测试均先红后绿，完整 API/transport 复跑通过。
- [x] 修改后 10 万样本压力门禁 `2/2` 通过，用时 `76.819s`；峰值内存分别为 `2,194,301` /
  `893,662` bytes，数据库为 `146,776,064` / `34,029,568` bytes，WAL truncate 后均为 `0`；
  OCR 结果保持 `90,000` success、`10,000` no_text。
- [x] 项目内 Node `24.18.0` / npm `11.16.0` 的前端生产构建通过，Playwright Chromium
  `72/72` 通过；Vite 测试服务已关闭，生成的 report/result 已删除，4173/8765 无监听，
  `.test-tmp` 为空，`git diff --check` 通过。
- [!] 全量 Core discovery 实际运行 `870` 项：`865` 通过、`2` 跳过、`2` 失败、`1` 错误。
  未通过项均位于本轮未修改文件：两项要求 Danbooru 默认资源，但当前生产 defaults 按冻结范围
  为 E621-only；一项要求已按清理决定删除的 `packaging/wheelhouse/ocr-paddle` 存在。完整
  contract `52` 项另有 `3` 项因 GPU formal artifacts 为 `4/5` partial 而失败；integration `29`
  项有 `2` 项因同一 partial 状态和已安装 Core runtime 仍是旧 defaults parser 而失败。本轮不以
  改测试、补 Danbooru 默认资源或伪造 wheelhouse 的方式掩盖这些既有装配/范围冲突。

## R17 验证契约与本机运行时修复

- [-] 2026-08-14：用户确认按最小方案修复上述四类冲突并提交到 `main`。生产范围保持
  E621-only，不恢复安装后应清理的 wheelhouse，不修改 10 万图处理链路。
- [x] 设计与实施计划分别位于
  `docs/superpowers/specs/2026-08-14-verification-contract-and-runtime-repair-design.md` 和
  `docs/superpowers/plans/2026-08-14-verification-contract-and-runtime-repair.md`。
- [x] 修正生产 profile、Danbooru 临时 fixture、OCR lock 和 GPU formal artifacts 测试契约；
  四个定向用例先红后绿，修改后 `4/4` 通过。
- [x] Core unit discovery `874/874`（`2` 项按设计跳过）、contract `52/52`、integration
  `29/29` 全量通过。
- [x] 通过 `Install-WebUI.bat` 刷新本机全部 15 个组件；Policy 的
  `Lib/site-packages/torch/lib`、四项 OCR GPU formal artifacts 和规范锁均已发布，runtime
  根目录无直接 `*.dist-info`。`/health`、首页、`/api/resources` 验证通过，随后
  `Stop-WebUI.bat` 退出 `0` 且 8765 无监听。
- [!] 2026-08-14：首次正式重装退出 `0`，但 contract 发现真实 wheel 被解压到 runtime 根目录，
  Caption 依赖锁校验失败且 Policy 缺少 `Lib/site-packages/torch/lib`。根因是 assembly 目标错误，
  同时既有 fixture 使用非标准 `Lib/site-packages/` 前缀掩盖了缺陷；修复中。
- [x] 2026-08-14：真实 wheel 现在合并到 runtime `Lib/site-packages`，所选 CPU/CUDA 变体锁
  统一发布为 `{runtime_id}.lock`。两项定向回归均先红后绿，完整 source-bootstrap install
  套件 `40/40` 通过；待重新装配本机 runtime 后执行跨层验证。
- [x] 2026-08-14：旧错误 wheel 布局仍可能凭已有规范锁被幂等跳过；新增回归先以
  `True is not false` 复现。`component_is_current()` 现在拒绝 runtime 根目录直接存在
  `*.dist-info` 目录，定向用例随后 `1/1` 通过，完整 source-bootstrap install `41/41` 通过。
- [x] 2026-08-15：integration 仍有一处旧 GPU formal-artifact 契约要求安装后保留 wheelhouse，
  与已确认清理规则冲突；真实安装四项长期工件全部存在，唯一缺项正是应清理的 wheelhouse。
  删除该过期断言后定向 `1/1`、完整 integration `29/29` 通过，生产代码未修改。
- [x] 2026-08-15：最终复核发现上一项只修测试仍会让独立 GPU 安装事务发布或遗留 wheelhouse。
  回归先以多出的第五项 `writes`、未触发清理及旧 wheelhouse 仍存在稳定复现；生产事务现仅发布
  runtime、runtime manifest、安装锁和源码锁，并在发布前安全删除 staging 与旧版 wheelhouse。
  清理失败时不发布任何正式产物。OCR GPU `19/19`、Core unit `875/875`（另 `2` 项按设计跳过）、
  integration `29/29`、contract `52/52`、前端构建及 Playwright `72/72` 均通过；inventory、
  release gate 和修改文件 Python 编译均退出 `0`。
- [!] 当前 `assemble.py` 只满足已冻结 wheel 集合，不是通用 PEP 427 安装器：不会展开 wheel
  `.data` 目录或生成 entry-point 脚本。现有锁仅发现未使用的 SymPy man-page payload，未复现业务
  回归；以后变更依赖集合时必须重新审计该边界。
- [ ] 最终复核通过并提交到 `main`。
