# 数据集占用冲突处理设计

## 目标

修复新任务确认工作区时，旧可恢复任务的数据集占用冲突被返回为无详情 HTTP 500 的问题。保留旧任务的恢复安全边界，并在旧任务成功完成后允许新任务继续使用同一数据集。

## 行为约定

1. `interrupted`、`failed`、`cancelled_recoverable` 任务继续保留其 overlay 和数据集占用，不提供“只解除占用”操作。
2. 用户不需要旧进度时，选择旧任务并确认“丢弃任务”；既有流程安全检查 journal、删除 overlay、将任务设为 `discarded` 并立即释放占用。
3. 用户需要旧进度时，选择旧任务并恢复；恢复期间继续占用数据集，防止新旧任务并发写入。
4. 旧任务到达不可恢复的 `succeeded` 后，下一次确认同一数据集工作区时自动清理其数据库 claim；若当前进程仍持有该任务的 Windows 锁句柄，同时安全关闭句柄。
5. 非 `succeeded` 任务不得被自动抢占、迁移、丢弃或释放。

## 接口与错误

- 数据库 claim 冲突使用带占用任务 ID 的专用 `DatasetLockError` 子类，不解析异常字符串。
- `POST /api/jobs/{job_id}/confirm-workspace` 将数据集锁冲突映射为 HTTP 409。
- 数据库 claim 冲突返回可操作的字符串 `detail`，包含占用任务 ID，并提示选择该任务进行恢复，或丢弃以释放数据集。
- 初次获取数据集锁时发生冲突属于可重试条件，新任务保持 `ready`，不得改为 `failed`，不得创建 overlay。
- 其他已开始工作区物化后的失败继续沿用现有失败和清理语义。
- 不修改数据库 schema、任务状态集合、overlay 格式或前端 API 数据结构。

## 前端

前端现有 `ApiError` 会显示服务端字符串 `detail`，因此不新增按钮、弹窗或页面。后端同步后仍重新构建 `frontend/dist`，并用浏览器回归验证 409 的可操作提示取代 `request failed: 500`。

## 测试与验收

1. RED：真实数据库 claim 冲突当前返回未捕获异常并把新任务置为 `failed`。
2. GREEN：冲突返回 409，提示包含旧任务 ID，新任务保持 `ready`，旧 claim 和 overlay 不变。
3. 覆盖数据库遗留 claim 和当前进程活锁两种 `succeeded` 任务；两者均允许下一次确认同一数据集成功。
4. 覆盖 `interrupted` 任务不得自动释放，丢弃后立即允许同一数据集重新确认。
5. 运行 Job Preflight、API、生命周期和启动恢复定向回归；同步 Core runtime，运行漂移检查、前端 typecheck/build 和相关 Playwright。

## 风险控制

- 自动清理只接受持久化状态 `succeeded`，该状态没有恢复转换，避免旧任务再次写入。
- 清理和重新获取仍经过既有 Windows 排他锁与 SQLite claim 双重门禁；并发确认最多一个成功。
- 不手工删除用户数据库记录或 overlay，不放宽 journal 和目录安全检查。
