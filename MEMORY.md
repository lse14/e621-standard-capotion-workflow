# Anima 项目 Memory

> 更新：2026-08-08。只记录仍有效的决定、边界、实现摘要和可复核 evidence；重复的逐轮过程已压缩。

## 已确认决定

- 主流程内部 ID 固定为 `caption -> classify -> replace -> ocr -> nl -> count_review -> dropout -> export`；UI 显示编号与内部 ID 解耦。
- JobConfig v2-v7 canonical bytes/hash、SQLite schema、worker wire protocol、NL 七段 prompt assembly、OCR 设备绑定均为受保护合同。
- 基础发行包默认不含 OCR runtime/manifest/lock mirror/wheelhouse/model；安装选择 `None`（默认）、`Cpu`、`Gpu`。None 不写 OCR、不删除完整既有安装；GPU 必须含 CPU fallback；partial fail-closed。
- OCR 模型只接受冻结本地 archive，`local-only / license unverified`；不自动下载或再分发；安装后 probe/inference 断网。
- NL API diagnostics 只用于 API 设置：模型发现、固定测试消息、结构化反馈和独立全局 v4 base/custom preset CRUD；不替换正式任务 prompt，不接入 NL worker，不调用真实 provider。
- 工作流 UI 保留现有结构/配色；统一 `FormField`/`ToggleField`/资源选择器说明，说明通过可访问 info button 提供；桌面双列、760px 以下单列，覆盖 1440/390/320px。
- TXT 输入解释只在新的 JobConfig v8 提供：默认 `inputTxtMode="tag"`，`taggerFallbackOnMissingTxt=true`。Tag 模式的非空 TXT 直接解析为 Tag 且跳过 Tagger；关闭缺失/空 TXT 补全时只记录非阻断 warning、该 sample 不导出，修正源 TXT 后新建任务重跑。
- NL 模式将 baseline TXT 作为最终 JSON `nl`，并强制 Tagger 为 Classify 生成 Tag；TXT 不传入 Tagger。缺失/空 TXT 的最终 `nl` 为空；非 UTF-8、NUL 或超过 16,384 UTF-8 bytes 的 TXT 阻断任务，不截断或回退。不会新增人工审核队列，Count Review/Token Budget Review 不承担该问题。
- v8 的逐项 TDD 实施计划位于 `docs/superpowers/plans/2026-08-08-input-txt-mode.md`。截至 2026-08-08，Tasks 1-7 已完成：v8 默认 `caption.inputTxtMode="tag"`、`caption.taggerFallbackOnMissingTxt=true`，继承 v7 OCR `device`，新增集中 capability helpers 和严格 schema；Caption 仅在 Core 本地决定跳过、启动 Tagger 或写非阻断 warning；NL 模式的 baseline TXT 严格 UTF-8/NUL/16,384-byte 验证后覆盖 overlay JSON 的 `nl`，但不会替代 Classify 的 Tagger TXT；NL runner 直接完成该 `nl`，不读取 baseline JSON 或 API。preflight 会把 v8 NL 视为零 API 工作量且不冻结 HTTP 预算，dispatch 不读取凭据，v8 OCR 复用 v7 device/binding。Caption UI（Tag 默认补全、NL 强制 Tagger、双语说明、v8 payload）和完整浏览器 E2E 均已验证。
- 待实施但已确认的 NL 下一版：任务提示词与 API 诊断提示词统一为一个类型化预设库；三内置 `General/Style/Character` 可编辑并重置，自定义预设创建必选类型且之后可编辑/删除；删除用户附加要求。NL 页面把所有 API Profile、连接、模型、密钥、限额、发现、测试和结果放进同一折叠区，API 关闭时仅折叠且保留值。正式任务使用新增 JobConfig v9 和新增 worker 分支冻结所选预设文本，绝不修改 v2-v8；完整规格为 `docs/superpowers/specs/2026-08-08-unified-nl-presets-and-api-ui-design.md`，当前等待用户书面复核。
- 待实施但已确认的全局路径选择器：源数据集、完整副本输出和自定义替换 CSV 使用同一“路径输入框 + 选择按钮”控件；本地后端按受限 purpose 打开 Windows 原生目录或 CSV 文件对话框，取消不改值，手输仍可用。没有浏览器磁盘枚举，既有预检仍是路径合法性唯一裁决。完整规格为 `docs/superpowers/specs/2026-08-08-global-native-path-picker-design.md`，当前等待用户书面复核。

## 当前实现摘要

- Core NL owners：`core/src/anima_core/api_context.py`、`api_models.py`、`api_nl.py`、`api.py`、`nl_diagnostics.py`、`nl_profiles.py`、`nl_prompt_presets.py`。
- Frontend UI owners：`frontend/src/components/NlApiTools.tsx`、`frontend/src/components/steps/NlStep.tsx`、`frontend/src/api.ts`、`frontend/src/mockApi.ts`、`frontend/src/i18n.ts`、`frontend/src/styles.css`、相关 `FormField`/step 组件。
- NL WebUI 修正：`general/style/character` 和短/中/长比例由 `NlStep.tsx` 独占；`TokenBudgetStep.tsx` 只保留 Token Budget 字段。三套冻结 v4 预设仍由 `nl_profiles.py` 的既有只读 default-prompt 路径校验并由 NL 页面并行读取、完整只读显示，不复制提示词到前端，不改变 JobConfig、worker 或 Token Budget 语义。2026-08-08 的资源/API/前端静态合同与 typecheck/build 均通过；用户授权的项目内 Chromium 使本地 mock 完整 Playwright `65/65` 通过，且已复核浏览器交互与截图。
- Guidance popup 修正：`FieldHelp` 只由显式点击或键盘激活打开，第二次点击、Escape、失焦和外部 pointer 均关闭；OCR tooltip 的绝对定位锚点为 `.ocr-tuning legend`。禁止 tooltip 接收 pointer events，Dropout 中的 native input 可禁用而 info button 仍可访问。
- WebUI launcher 修正：`desktop_control.ps1` 的 `Get-Listener` 保留 `Get-NetTCPConnection`，并在受限进程查询失败时用 `netstat` 查 `127.0.0.1:<port>`；所有 `.Count` 分支将函数输出显式包装为数组。若状态文件丢失而本机 `/api/health` 返回 `status=ok`、`protocolVersion=1.0`，Start 只打开既有服务，不会创建第二个后端或停止其他程序；Stop 仍要求带 token 的匹配状态文件，避免不安全关闭。
- Tests-only compatibility fix：`frontend/tests/e2e/task-status.spec.ts` 使用 `getByLabel(..., { exact: true })` 和 `getByRole(..., { exact: true })`，没有生产改动。
- Prompt preset store 使用严格 JSON、UTF-8 byte limits、稳定 custom ID、sibling temp + `os.replace`；diagnostic client 仅标准库、单次 bounded request、拒绝 redirect/不安全 URL、错误脱敏。
- v8 合同 owner 为 `core/src/anima_core/contracts.py` 和 `contracts/schemas/job-config-v8.schema.json`；Tasks 2-5 改动 `core/src/anima_core/caption_runner.py`、`classify_runner.py`、`classify_overlay.py`、`nl_runner.py`、`job_preflight.py`、`pipeline_dispatch.py` 和 `ocr_runner.py`。Caption/Classify/NL worker协议、SQLite 和运行时资源均未改动。Task 6 owner 是 `frontend/src/draft.ts`、`App.tsx`、`components/steps/CaptionStep.tsx`、`i18n.ts`、`appCopy.ts` 与 local mock/tests。

## TDD / Gate evidence

- Core embedded `-B -I` focused：preset `8/8`、diagnostics `9/9`、profiles `9/9`、API `32/32`、Core decomposition `13/13`、frontend decomposition `9/9`、payload `14/14`。
- v8 合同新鲜验证：`& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests/contract -t . -p 'test_payload_schemas.py' -v` 于 2026-08-08 退出 `0`，`15/15` 通过。独立 JSON 结构归一化检查确认 v7/v8 仅有 `$id`、`schemaVersion` 与两个 Caption 字段差异；测试同时断言 v7 `1881` bytes / `a85332ab0c70e3838a386b7489f48e37aee39d5966aab481a9b14de5b2019377`。
- v8 Caption 新鲜验证：先前述 embedded Python 运行 `tests\unit\test_caption_runner.py` 时，新增三例因 v8 未支持而以 `caption_profile_mismatch` 失败；实现后同一命令退出 `0`，`16/16` 通过。测试断言 fallback-off issue 的 code、warning severity、`blocking=0`、`retriable=0`、空 `repair_start_module` 与 failed sample；构造 worker hello 的代码未含 v8 字段。
- v8 Classify 新鲜验证：`tests\unit\test_classify_runner.py` 初次运行显示 baseline/blank `nl` 不会覆盖旧值，且三类非法 baseline TXT 未产生 issue；实现后同一命令退出 `0`，`9/9` 通过。测试覆盖 overlay Tagger TXT、精确 baseline NL、空 NL、非法 UTF-8/NUL/16,385-byte 阻断与 raw E621 旧路径。
- v8 NL 新鲜验证：`tests\unit\test_nl_runner.py` 初次运行在旧 v7-only OCR device 校验处拒绝 v8；实现后同一命令退出 `0`，`32/32` 通过。测试以无凭据、无效 baseline JSON、有效 working `nl` 覆盖本地完成和空字符串，确认零 hello/process、零 HTTP attempts 与 `input_txt_nl` observation。
- v8 preflight/dispatch/OCR 新鲜验证：embedded `-B -I` 运行 `tests\unit\test_job_preflight.py`、`tests\unit\test_pipeline.py`、`tests\unit\test_ocr_runner.py` 分别退出 `0`（`29/29`、`29/29`、`16/16`）。v8 input-NL 在空 API prompt 下得到全零 API summary、无 `maxHttpAttempts`，dispatch 使用 `_NoExchangeTransport` 且不解析凭据；v8 的 `auto/cuda/cpu` OCR device 复用 v7 execution-request 与 device-aware binding。
- v8 Caption UI 新鲜验证：project Node 的 `npm run typecheck`、`npm run build` 均退出 `0`；`tests\contract\test_frontend_module_decomposition.py` 为 `10/10`，经用户授权安装的项目内 Chromium 后，完整 local-mock Playwright 为 `65/65`，包含两条 TXT mode 用例。
- Guidance/Chromium 新鲜验证：`workflow-ui-guidance.spec.ts` `12/12`；Chromium 仅在 `frontend\node_modules\playwright-core\.local-browsers\`，通过 `PLAYWRIGHT_BROWSERS_PATH=0` 使用。桌面与 `390x844` 移动 OCR tooltip 截图人工复核，未见 tooltip 越界。
- Start-WebUI 新鲜验证：新增 launcher 静态合同先因缺少回退/健康恢复分支失败，再通过 `tests\unit\test_desktop_control.py` `6/6`；PowerShell 解析无错误。已有 `127.0.0.1:8765` 单监听和 `status=ok`/协议 `1.0` 条件下，实际 `Start-WebUI.bat` 退出 `0`，未创建第二个监听者。
- Frontend local mock：完整 `65/65`，typecheck/build exit `0`。
- RED 记录保留在权威计划：API profile/store、diagnostics、decomposition、UI guidance/responsive 各有唯一预期失败；所有 GREEN 均 `Ran > 0`。
- Core sync preview `2 add / 5 update / 0 remove / 52815 bytes`，Apply 后 post-preview `0/0/0/0`；assembled drift `7/7`。
- 四张 UI evidence：`output/playwright/workflow-ui-desktop-nl.png`、`workflow-ui-desktop-policy.png`、`workflow-ui-mobile-zh.png`、`workflow-ui-mobile-en-320.png`，人工复核无重叠、裁切、tooltip 越界或横向溢出。

## OCR optional-install evidence

- 隔离验证根：`E:\AnimaOptionalOcrReleaseValidation-20260805-01`；合规 roots 为 `final-none-20260806-02`、`final-cpu-8`、`final-gpu-2`。
- None：formal-target difference `0`，状态 `none`；CPU：`runtimeIds=[ocr-paddle]`、`status=ready`；GPU：`runtimeIds=[ocr-paddle, ocr-paddle-gpu]`、`status=ready`，包含 CPU fallback。
- CPU component：`17230` files；`component.json` `3292597` bytes / `0dce1ae06e106273dcb1b50b057401fd80a1f55b54016c745668979f50855753`。
- GPU component：`17471` files；`component.json` `3409052` bytes / `0b3c358f84e32c395221523063a718393785225ab78107d58003c11e22a4d9e2`。
- 允许的本地模型 archive：orientation `6871040` / `6171f696...ddc7f6`；detection `88340480` / `22a33e0b...73045d`；recognition `84869120` / `d99be2ff...a750a`。无下载、无再分发。
- Release Full selector 已修正为 `tests/unit`、`tests/contract`、`tests/integration`；`tests/stress` 仅显式 `--level stress`，本阶段不运行 stress。

## 受保护与延期

- 不修改 `JobConfig` v2-v7、canonical hash、preflight、NL runner/worker、OCR worker/runtime/resource/model/lock、SQLite；不运行系统 Python/Node、真实 provider、下载、stress、Task 3.5、benchmark、100k。例外：2026-08-08 用户明确授权下载 Playwright Chromium，且仅存于项目 `frontend\node_modules`。
- OCR Task 3.5 性能校准与真实 100k Task 4 均无限期延期，均保持 `[ ]`；顶层 OCR Task 3 保持 `[ ]`。
- Danbooru CL/WD 正式资源和真实模型验收继续 local-only/未就绪，等待独立授权。

## 文档清理记录（2026-08-06）

- 六个候选删除前均存在且无有效引用：`FINAL_REMAINING_FIX_PLAN_2026-07-26.md`（8121 bytes）、`docs/STAGE3_MODULE2_DESIGN.md`（17842）、`docs/STAGE4_MODULE3_DESIGN.md`（3509）、`docs/STAGE5_MODULE4_DESIGN.md`（6490）、`docs/STAGE6_EXPORT_DESIGN.md`（5774）、`docs/UI_LANGUAGE_SWITCH_PLAN.md`（1418）。
- 压缩前后：`ROADMAP.md` `333424 -> 5358` bytes（减少 `328066`），`MEMORY.md` `153221 -> 5914` bytes（减少 `147307`）。
- `README.md`/`RULES.md` 无候选引用，未改；`tests/` 与 `frontend/tests/` 源文件全部保留，包括 `tests/stress/test_control_plane_100k.py`。
- 本轮只允许删除上述六个文档和本轮生成的 `frontend/test-results/.last-run.json`（先核验绝对路径/大小/时间）；不删除 output/playwright、runtime、resource、model、lock、历史 evidence 或用户数据。

## 权威资料

- OCR：`docs/superpowers/plans/2026-08-05-optional-ocr-installation.md`
- NL API：`docs/superpowers/specs/2026-08-06-nl-api-diagnostics-and-prompt-presets-design.md`、`docs/superpowers/plans/2026-08-06-nl-api-diagnostics-and-prompt-presets.md`
- Workflow UI：`docs/superpowers/specs/2026-08-06-workflow-ui-alignment-and-guidance-design.md`、`docs/superpowers/plans/2026-08-06-workflow-ui-alignment-and-guidance.md`
