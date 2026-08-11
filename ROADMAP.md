# ROADMAP

## 当前目标

让用户从 GitHub 获取源码后，在 Windows 10/11 x64 电脑上直接双击
`Install-WebUI.bat`，无需预装 Python、Node、CUDA Toolkit、Visual Studio 或 Windows
SDK，即可得到可离线运行的 E621 打标、质量评分、Qwen3 tokenizer 和 OCR 环境。

设计于 2026-08-11 经用户批准。实现尚未开始。

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

## 实施顺序

- [x] R1: 核对当前源码、运行时、资源、忽略规则和安装入口。
- [x] R2: 比较完整便携包、拆分包和源码自举方案。
- [x] R3: 批准源码自举架构、组件边界、错误恢复和验收标准。
- [x] R4: 写入并复核正式设计说明。
- [x] R5: 编写可执行实施计划，并按依赖顺序拆分任务。
- [ ] R6: 建立冻结的安装组件清单和清单验证器。
- [ ] R7: 建立预编译 CPython 3.11.15 基础资产的构建、探测和发布流程。
- [ ] R8: 实现 PowerShell 首阶段自举、日志、空间检查和基础资产获取。
- [ ] R9: 实现 Python 下载器、断点续传、哈希验证和事务发布。
- [ ] R10: 增加 Caption 和 Policy 的 CPU/CUDA 安装变体。
- [ ] R11: 接入 E621、Qwen3、质量评分和 E621 索引资源下载。
- [ ] R12: 接入默认 OCR CPU，以及 NVIDIA 机器的 OCR GPU 安装。
- [ ] R13: 接入幂等修复、缓存清理和失败恢复。
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

## R5 验证记录

- [x] 2026-08-11：已写入 `docs/superpowers/plans/2026-08-11-source-bootstrap-installer.md`，
  覆盖 PowerShell 自举、冻结清单、下载/事务、CPU/CUDA、必装资源、离线探测、
  故障矩阵、前端产物与发布门禁。
- [x] 2026-08-11：已复读计划并运行 `git diff --check`；当前隔离 worktree 不含
  `.runtime-build`，计划显式要求后续测试只使用受控项目内嵌解释器，未把缺少
  开发运行时误报为测试通过。
