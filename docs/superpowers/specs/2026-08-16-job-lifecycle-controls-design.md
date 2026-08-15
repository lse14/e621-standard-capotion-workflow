# 任务级暂停、恢复与可恢复终止设计

## 状态

- 设计已由用户确认。
- 本文档只定义行为和验收，不代表代码已经实现。

## 背景与现状

当前项目已经具备以下基础能力：

- `GET /api/jobs` 返回有界任务列表，前端可以选择任意已加载任务。
- `POST /api/jobs/{job_id}/recover` 可恢复 `interrupted` 任务。
- `POST /api/jobs/{job_id}/cancel` 已实现 `cancelling -> cancelled_recoverable` 的可恢复取消流程。
- 状态机已有 `running -> paused -> running`；各主要 Runner 在循环中检查 `paused` 和 `cancelling`。
- NL 和 Policy 已有模块级暂停/恢复接口，但其他模块没有统一任务级入口。

本功能补齐统一的任务级控制，不改变既有样本协议、工作区提交协议或 worker 进程协议。

## 目标与边界

### 目标

1. 对当前选中的任务提供整条 pipeline 的暂停和恢复。
2. 将“终止”定义为可恢复取消，不强制杀掉 worker。
3. 支持从任务列表切换到其他任务，再对该任务执行恢复。
4. 保留安全提交、租约回收、prepared artifact 恢复和源文件指纹校验。

### 边界

- 暂停是协作式的：已开始的当前样本或请求允许在安全点完成，之后停止继续领取工作。
- `committing` 阶段的目录切换不可暂停；此时只能等待提交完成或按既有故障恢复流程处理。
- 暂停只针对当前选中的一个任务，不暂停其他任务。
- 终止不会删除 overlay、备份或样本状态；终止后的任务保持可恢复。
- 不提供立即强杀进程、强制回滚业务文件或跨任务批量控制。
- 不新增任务表；复用 `jobs.status`、`resume_status`、`current_module_id` 和 `module_summary`。

## 状态与行为

### 任务状态

| 操作 | 允许的初始状态 | 目标状态 | 说明 |
| --- | --- | --- | --- |
| 暂停 | `running` 且当前模块为可暂停 worker | `paused` | 同步将当前模块摘要置为 `paused`，保存 `resume_status=running` |
| 恢复暂停任务 | `paused` | `running` | 只恢复当前模块，保留已完成样本和未完成租约的安全处理 |
| 终止 | `preparing_workspace`、`running`、`paused`、`reviewing`、`exporting` | `cancelling` | 写入取消屏障，不再领取新工作 |
| 终止结算 | `cancelling` 且无在途工作 | `cancelled_recoverable` | 保留工作区和状态，允许后续恢复 |
| 恢复中断/终止任务 | `interrupted` 或 `cancelled_recoverable` | 当前模块的可运行状态 | 先执行恢复校验，再恢复当前模块；不跳过不确定 API 结果确认 |

`committing`、`succeeded`、`discarded` 不显示暂停；已完成任务只能使用既有还原原始标注流程。

### 恢复安全条件

恢复必须继续复用现有恢复协议：

- 任务配置 hash、manifest schema、worker protocol 版本一致。
- 源文件指纹仍与不可变 manifest 一致。
- `leased`、`prepared`、`response_staged` 等状态按模块恢复规则处理。
- NL 未知 API 结果仍需要显式确认，不能自动重复请求。
- 取消后重新恢复时清理完成时间和取消屏障，不能创建第二个 pipeline 线程。

## 接口与前端行为

### 后端接口

- 新增 `POST /api/jobs/{job_id}/pause`：校验任务级暂停条件，写入 job/module 状态。
- 新增 `POST /api/jobs/{job_id}/resume`：恢复 `paused` 任务；内部复用现有 `PipelineService.resume`。
- 保留 `POST /api/jobs/{job_id}/cancel`：作为“终止”按钮的实现，不改变现有响应语义。
- 扩展 `POST /api/jobs/{job_id}/recover`：允许 `interrupted` 和 `cancelled_recoverable`，并沿用已有安全校验。
- 非法状态返回现有风格的 `400`；不存在任务返回 `404`；线程仍在收尾时返回明确冲突错误。

现有 NL/Policy 专用接口可以保留兼容，但前端任务操作区使用统一任务级接口，避免用户误解为只暂停一个模块。

### 前端

- 保留现有最近任务下拉框和手动 `jobId` 选择。
- 操作按钮始终绑定当前选中的 `jobId`，切换任务时立即失效旧快照请求。
- `running` 显示“暂停任务”和“终止任务”。
- `paused` 显示“恢复任务”和“终止任务”。
- `interrupted`、`cancelled_recoverable` 显示“恢复任务”。
- `committing`、终态任务禁用暂停/恢复/终止按钮。
- 请求进行中禁用同一按钮；操作成功后刷新任务列表和任务快照。

## 数据流

```text
选择 jobId
  -> 读取当前快照
  -> 任务级操作接口
  -> 持久化 jobs/module_summary 状态
  -> Runner 在安全点观察 paused/cancelling
  -> 释放或保留租约/准备产物
  -> 快照轮询显示最终状态
```

## 错误处理与并发

- 暂停与终止都先持久化状态屏障，再等待 worker 安全退出；不依赖前端按钮状态保证安全。
- 同一任务的重复暂停、恢复、终止必须由后端状态校验拒绝或幂等处理。
- 任务切换时取消旧快照 fetch，旧响应不得覆盖新任务。
- 恢复前检查 `PipelineService` 的线程表，防止同一任务并发启动两个 pipeline。
- 取消过程仍依赖 `count_in_flight`，在途工作未清空前不能结算为 `cancelled_recoverable`。

## 验收与测试

### 后端单元/契约

- 覆盖所有允许和拒绝的任务状态转换。
- 覆盖整条 pipeline 各 worker 模块的暂停安全点。
- 覆盖在途 lease、prepared artifact、未知 NL API 结果的暂停/终止/恢复。
- 覆盖重复请求和 pipeline 线程冲突。

### 前端端到端

- 任务 A 与任务 B 之间切换后，操作只发送到当前选中的 jobId。
- running -> paused -> running。
- running/paused -> cancelling -> cancelled_recoverable -> recover。
- interrupted 任务切换后恢复。
- committing 和终态任务按钮保持禁用。

### 构建与同步

- 使用项目内部 `.runtime-build` Python 运行后端测试。
- 后端修改后同步 Core runtime。
- 运行前端 typecheck、production build 和相关 Playwright 回归。
- `git diff --check` 通过；不保留临时脚本或服务进程。

## 方案取舍

### 采用：统一任务级控制

复用现有状态机、任务列表、恢复协议和取消屏障，新增接口最少，用户语义一致。

### 不采用：继续增加各模块专用按钮

会让用户误以为暂停只影响当前模块，且需要为每个 Runner 维护不同的控制协议。

### 不采用：强制终止 worker 进程

可能在写入 overlay 或提交目录时留下不完整状态，与“可恢复取消”和现有提交日志设计冲突。
