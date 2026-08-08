# Anima 训练集标注处理工具 Roadmap

> 更新：2026-08-08。本文只保留当前状态、有效约束、活动计划、延期项和关键验收摘要；逐轮历史记录已移至工作区历史 evidence，不作为当前合同。

## 当前状态

- [x] v2-v7 JobConfig 兼容链保持冻结。v2-v6 默认配置 bytes/SHA-256：`1476/950b3217...b9d4c2`、`1543/41eab9dc...c62157f`、`1543/8dbf56d3...7e25e85`、`1657/76f0d5a3...bcd9608`、`1865/fd1f9b0...4bc28eb8cc9`；v7 只增加严格 `ocr.device`。
- [x] 可选 OCR 安装已完成：基础包 OCR-free；Install-WebUI 支持 `None/Cpu/Gpu`，None 零 OCR 写入，CPU/GPU 原子发布，GPU 含 CPU fallback，partial/缺模型 fail-closed。合规隔离 evidence 根：`E:\AnimaOptionalOcrReleaseValidation-20260805-01`。
- [x] NL API diagnostics 与全局 v4 base preset CRUD 已完成，限 API 设置/诊断，不接入正式任务；只使用本地 injected fake，不调用真实 provider。
- [x] 工作流 UI alignment/guidance 已完成：统一字段/说明、响应式 1440/390/320px 合同、NL API tools、四张截图 evidence。
- [x] 本轮授权的 tests-only locator 修复已完成：`frontend/tests/e2e/task-status.spec.ts` 的 OCR 控件和 repair button 使用 exact accessible locator；完整前端套件 `62/62`，typecheck/build 均退出 `0`。
- [x] Core API Tasks 1-3 已按精确 preview Apply：post-preview `0 add / 0 update / 0 remove / 0 bytes`；assembled drift `7/7`。
- [x] 本轮 NL WebUI 修正：已将 `general/style/character` 与短/中/长比例从 Token Budget 配置归回 NL 配置，并只读显示三套冻结 v4 预设提示词；`test_nl_profiles.py` `10/10`、`test_api.py` `33/33`、前端静态合同 `10/10`、typecheck/build 均通过。经用户授权的项目内 Chromium 位于 `frontend/node_modules/playwright-core/.local-browsers/`；本地 mock Playwright 完整套件 `65/65` 通过，浏览器交互与桌面/移动截图均已复核。未改 JobConfig、NL worker、提示词资源或 Token Budget 语义。
- [x] v8 输入 TXT 模式：Tasks 1-7（JobConfig 合同、Caption Tag 决策、Classify NL 注入、NL 本地完成、preflight/dispatch/OCR 继承、Caption UI、完整回归记录）均已验证；v8 Caption UI、payload/local-mock assertions 和双语说明通过 typecheck/build/static contract 与完整 Playwright `65/65`。默认 `Tag`、缺失/空 TXT 的 Tagger 补全默认开启，v2-v7 合同保持冻结。
- [x] Start-WebUI 启动器修正：Windows PowerShell 5.1 在单个监听对象上返回空 `.Count`，使启动器误判 `8765` 空闲并产生 `10048` 重复绑定。监听查询现在有 `netstat` 回退且调用方显式数组化；状态文件遗失但 `/api/health` 返回 Anima 协议 `1.0` 时，Start 只重新打开现有本机服务，不停止未知进程。RED/GREEN `test_desktop_control.py` `6/6`，实际 `Start-WebUI.bat` 回归退出 `0`。
- [?] NL 统一预设与 API UI：用户已确认 `General/Style/Character` 三内置可编辑可重置、自定义预设创建必选类型且可改/删、任务与诊断共用一个预设源、删除用户附加要求、所有 API 控件同区且 API 关闭时折叠保留配置。规格待用户书面复核：`docs/superpowers/specs/2026-08-08-unified-nl-presets-and-api-ui-design.md`；实施必须使用新增 v9 合同，不改 v2-v8。
- [?] 全局原生路径选择器：用户已确认所有本机路径字段统一为“路径输入框 + 选择按钮”；源/输出用 Windows 原生目录选择，自定义替换索引用原生 CSV 文件选择，手输仍可用。规格待用户书面复核：`docs/superpowers/specs/2026-08-08-global-native-path-picker-design.md`；只新增受限本地 UI route，不提供浏览器文件系统枚举或改动预检/任务合同。

## 有效计划

- `docs/superpowers/plans/2026-08-05-optional-ocr-installation.md`：可选 OCR 安装已验证；Task 3.5 保持无限期延期。
- `docs/superpowers/specs/2026-08-06-nl-api-diagnostics-and-prompt-presets-design.md` 与 `docs/superpowers/plans/2026-08-06-nl-api-diagnostics-and-prompt-presets.md`：API Tasks 1-3、原 Task 4/5 的诊断收尾已完成。
- `docs/superpowers/specs/2026-08-06-workflow-ui-alignment-and-guidance-design.md` 与 `docs/superpowers/plans/2026-08-06-workflow-ui-alignment-and-guidance.md`：Task 0-6 已完成；前端布局补充计划替代原 API 计划旧 frontend Task 4。
- `docs/superpowers/plans/2026-08-05-jobconfig-v7-inheritance-repair.md`：第一阶段已完成，v7 consumer/hash 保持兼容。
- `docs/superpowers/specs/2026-08-08-input-txt-mode-design.md`：v8 TXT 输入解释的已确认设计。
- `docs/superpowers/plans/2026-08-08-input-txt-mode.md`：v8 TXT 输入解释的逐项 TDD 实施计划；Tasks 1-7 已完成并记录了项目内 Chromium 的浏览器验证。

## 关键约束

- 处理顺序使用稳定内部 ID：`caption -> classify -> replace -> ocr -> nl -> count_review -> dropout -> export`；界面编号只按实际 `moduleOrder` 动态显示。
- 生产运行只使用项目内 embedded Python/Node/Chromium；测试 Python 使用显式 `-B -I`。不下载依赖、模型或浏览器，除 2026-08-08 用户明确授权安装的项目内 Playwright Chromium；不使用系统 Python/Node，不调用真实 provider。
- 不改 JobConfig canonical bytes/hash、preflight、NL runner/worker source/protocol、worker wire protocol、SQLite schema、OCR runtime/resource/model/lock 或 OCR 设备绑定，除计划授权的 Core API owners 外不做 sync Apply。
- OCR 模型保持 `local-only / license unverified`；安装后 inference 必须断网。正式安装失败保留旧状态，只清理本轮 staging/temp。
- 不运行 `tests/stress`、Task 3.5 calibration、benchmark、模型 parity、OOM retry 或真实 100k。

## 验收摘要

- Core focused（embedded `-B -I`）：preset `8/8`、diagnostics `9/9`、profiles `9/9`、API `32/32`、Core decomposition `13/13`、frontend decomposition `9/9`、payload `14/14`。
- v8 JobConfig contract（embedded `-B -I`）：`tests/contract/test_payload_schemas.py` `15/15`；v2-v6 默认 hash、v7 `1881` bytes / `a85332ab...2019377` 均由测试断言，v7/v8 schema 仅有预期的版本与两项 Caption 字段差异。
- v8 Caption Tag decision（embedded `-B -I`）：`tests/unit/test_caption_runner.py` `16/16`；Tag/nonblank 零 worker 调用，缺失/空且补全开启调用一次，补全关闭写非阻断不可重试 warning 并保留 failed sample 给 Export skip。
- v8 Classify NL injection（embedded `-B -I`）：`tests/unit/test_classify_runner.py` `9/9`；Tagger overlay TXT 仍给 Classify，baseline TXT 覆盖 JSON `nl`，空值保留为空，非法 UTF-8/NUL/超长 TXT 在 worker 前阻断。
- v8 NL local completion（embedded `-B -I`）：`tests/unit/test_nl_runner.py` `32/32`；无凭据、损坏 baseline JSON 时仍以 working JSON 的 `nl` 完成，零 worker/API 请求和零 HTTP 计数。
- v8 preflight/dispatch/OCR（embedded `-B -I`）：`test_job_preflight.py` `29/29`、`test_pipeline.py` `29/29`、`test_ocr_runner.py` `16/16`；v8 input-NL 的 API summary/预算均为零且 dispatch 不取凭据，v8 OCR 复用 v7 device/binding。
- v8 Caption UI：`tests/contract/test_frontend_module_decomposition.py` `10/10`、项目内 Node `typecheck/build` 均退出 `0`；经项目内 Chromium 的完整 local-mock E2E `65/65`，含两条 v8 TXT 用例。
- Frontend local-mock：完整 Playwright suite `65/65`；本轮 guidance focused suite `12/12`。既有截图：`output/playwright/workflow-ui-desktop-nl.png`、`workflow-ui-desktop-policy.png`、`workflow-ui-mobile-zh.png`、`workflow-ui-mobile-en-320.png`；本轮 OCR tooltip 的桌面和 `390x844` 移动截图另存于 Codex 视觉证据目录。
- Core sync preview（Apply 前）：`2 add / 5 update / 0 remove / 52815 bytes`，仅 API Tasks 1-3 owners；Apply 后 preview `0/0/0/0`。
- 可选 OCR Release evidence：None Core `700/700`、contract `47/47`、integration `27/27`、workers `99/99`；CPU 增加 OCR worker `18/18`、integration `2/2`；GPU CPU/GPU workers 各 `18/18`、integration `2/2`；Full 未发现 `tests/stress`。CPU component `17230` files，`component.json` `3292597` bytes，SHA-256 `0dce1ae0...f50855753`；GPU component `17471` files，`component.json` `3409052` bytes，SHA-256 `0b3c358f...e22a4d9e2`。
- 本轮中止的 Fast 子进程已按精确 PID 清理；未启动 Release。新的文档清理任务不要求再次运行门禁。

## 延期项

- [ ] OCR Task 3.5 性能校准：无限期延期，不运行 fixture/benchmark/parity/OOM。
- [ ] 真实 100k 性能 Task 4：无限期延期，不运行 fixture/benchmark/100k 流程。
- [ ] 顶层 OCR Task 3：保持 `[ ]`，不得因可选安装或 UI/API 完成而标记完成。
- [ ] Danbooru 正式 CL/WD 模型、真实资源和准确率验收：继续 local-only，等待单独授权。

## 文档清理（2026-08-06）

- [x] 六个批准的历史文档候选已逐一核验存在、完成引用审计后删除；未发现有效引用。
- [x] 本文和 `MEMORY.md` 已压缩为当前状态、有效约束、活动计划、延期项和关键验收摘要。
- [x] `README.md`、`RULES.md` 无候选文档重复引用，无需修改。
- [x] 仅清理本轮确认的 `frontend/test-results` 临时 metadata；测试源、stress fixture、历史截图、runtime/resource/model/lock 和用户数据均保留。
