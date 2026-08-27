# 模块批处理与 TXT 布局设计

日期：2026-08-27
状态：已确认，待用户复核书面规格
开发分支：`codex/dev`

## 1. 目标

1. 九个模块都提供可手动调整的批处理数量。
2. 提供一个“根据设备推荐”按钮，一次填写九个模块的推荐值。
3. 除 NL 外，使用 `E:\Desktop\10_uiokv` 分别测试各模块的批量吞吐并校准推荐表。
4. GPU 模块以实际吞吐和稳定性为准充分利用 GPU，不设置人为低推荐值。
5. TXT 支持全单行和仅 NL 换行两种布局；换行只使用 `LF`。

## 2. 范围与非目标

范围内模块：Caption、Classify、Replace、OCR、NL、Count Review、Dropout、Token Budget、Export。

本项只优化模块内部的批量吞吐。模块之间仍按现有顺序串行执行：

```text
Caption -> Classify -> Replace -> OCR -> NL -> Count Review -> Dropout -> Token Budget -> Export
```

本项不实现样本级跨模块流水线，不修改模型权重、标注质量规则、NL prompt、Count Review 人工交互规则或 Export 提交原子性。验证集不用于评价标注质量。

### 2.1 当前实现证据

- `shared/anima_caption_format/anima_caption_format/flat_txt.py:61` 当前用 `", \n\n"` 连接各段，确实会产生双换行。
- `core/src/anima_core/caption_protocol.py:424` 和 `classify_protocol.py:360` 当前都强制一次请求只有一个 item；这只是现有协议限制，不是模块吞吐的合理推荐值。
- `core/src/anima_core/ocr_runner.py:715` 当前固定 `limit=1`，但 `workers/ocr/src/anima_ocr_worker/protocol.py:15` 允许最多 1024 个 item，安装的 PaddleX OCR pipeline 也接受图片数组列表。
- `workers/nl/src/anima_nl_worker/worker.py:272` 已使用 `asyncio.Semaphore` 控制 API 并发。
- Policy、Token Budget、Export worker 的现有批量上限分别为 16、500、500，定义在各自 protocol 的 `MAX_BATCH_SIZE` 或 `MAX_PROCESS_ITEMS`。

## 3. JobConfig v10

新任务升级为 JobConfig v10。顶层新增完整且必填的 `moduleBatchSize`：

```json
{
  "schemaVersion": 10,
  "moduleBatchSize": {
    "caption": 4,
    "classify": 128,
    "replace": 128,
    "ocr": 4,
    "nl": 3,
    "countReview": 100,
    "dropout": 4,
    "tokenBudget": 128,
    "export": 500
  }
}
```

`moduleBatchSize` 表示模块每轮最多接收的样本数，不承诺所有模块使用相同的内部执行方式。该字段参与配置哈希，任务预检后冻结，运行中不得自动改写。

NL 的批处理值统一迁移到 `moduleBatchSize.nl`。v10 不再接受 `nl.apiPolicy.concurrency`，避免两个字段控制同一个请求并发窗口。`maxRequestsPerMinute`、HTTP attempt budget 和既有熔断规则继续保留。

手动输入范围使用模块协议边界：

| 模块 | 最小值 | 最大值 |
|---|---:|---:|
| Caption | 1 | 64 |
| Classify | 1 | 500 |
| Replace | 1 | 500 |
| OCR | 1 | 1024 |
| NL | 1 | 16 |
| Count Review | 1 | 500 |
| Dropout | 1 | 16 |
| Token Budget | 1 | 500 |
| Export | 1 | 500 |

OCR 的 `textBatchSize` 继续表示单次 OCR 内部的文字行识别批量，不等于图片批处理数量。Dropout 的 `quality.batchSize` 也继续保留为质量模型内部 micro-batch；`moduleBatchSize.dropout` 只控制模块每轮领取的图片总数，worker 按 `quality.batchSize` 将该轮拆为一个或多个评分 micro-batch。

旧 JobConfig v2-v9 只保留为历史契约和明确拒绝旧任务所需文件。dev 分支的新建、预检、冻结、运行、repair 和前端草稿入口只接受 v10，不从旧 schema、旧草稿或旧构建产物读取新功能配置。

## 4. 各模块批处理方式

| 模块 | 批处理策略 |
|---|---|
| Caption | 一个 ONNX session 只加载一次模型；一轮接收多张图片，批量完成读取和预处理。E621 模型仍按单图 `session.run`，是否使用有限在途任务以验证集吞吐为准。 |
| Classify | worker 协议接收多 item，一次初始化和一次进程往返处理整批，复用字典和 SQLite 资源。 |
| Replace | worker 协议接收多 item，一次初始化和一次进程往返处理整批，复用替换索引。 |
| OCR | 批量读取和校验图片，调用 PaddleX 已有的 `List[np.ndarray]` 多图片入口；保留文字行 batch 调优。 |
| NL | 一轮提交多 item，继续使用现有 `asyncio.Semaphore`；只受用户值、RPM、HTTP budget 和熔断限制，不参加验证集测速。 |
| Count Review | 一次领取配置数量的已解决 decision，按现有顺序串行写入；不并行人工复核。 |
| Dropout | 批量读取图片和 JSON，质量模型一次评分多张图片，然后应用确定性策略。 |
| Token Budget | 扩大现有多 item 请求，复用已加载 tokenizer 批量计数。 |
| Export | 一次领取配置数量并执行现有批量校验、序列化和暂存；最终提交仍保持原子且不异步。 |

批次返回结果必须包含原有 `sampleId` 和 `leaseId`。单个样本错误只结算该样本，同批其他合法结果继续提交。模型初始化失败、资源指纹变化、批次身份重复或协议结构损坏仍是模块级错误。

SQLite 和 overlay 的最终状态写入由 Core runner 串行协调，不把同一个 `StateDatabase` connection 交给工作线程。暂停或可恢复终止时停止领取新批次，释放尚未开始的 lease，等待已开始工作安全返回；模块只有在所有在途工作归零后才能结束。

手动值导致 OOM 时不得静默修改冻结配置或伪装成功。worker 返回明确的批量/显存错误，释放未开始 lease，并把任务置为可诊断的模块失败状态。

## 5. 设备推荐

新增只读设备探测接口，返回：

```json
{
  "cpuPhysicalCores": 6,
  "cpuLogicalCores": 12,
  "gpu": {
    "available": true,
    "name": "NVIDIA GeForce RTX 4090",
    "totalVramBytes": 25757220864,
    "freeVramBytes": 11534336000,
    "probeSource": "nvidia-smi"
  },
  "moduleBatchSize": {},
  "reasons": {}
}
```

GPU 探测优先复用已验证的 CUDA 运行时证据；不可用时查询 `nvidia-smi`；两者都不可用时返回 CPU-only 结果。探测失败不得覆盖前端现有手动值。

除 NL 外，推荐值来自新的版本化基准表，不引用旧任务或旧构建产物。每一行记录模块、最少物理/逻辑核心、GPU 是否必需、最少总显存、最少空闲显存及已验证批量。运行时选择所有满足当前设备条件的行中批量最大的稳定记录；没有匹配记录时回退为 1。推荐值不得超过本模块 schema 上限。

GPU 模块不设置固定的 1/2 低档。基准表必须包含验证集测得的实际吞吐拐点，并允许高显存设备选择 8、16 或更高的已验证值。空闲显存参与匹配，避免推荐按钮无视其他应用的实时占用。

NL 不读取验证集结果。默认推荐沿用现有值 3；有限 RPM 小于 3 时推荐值降为 RPM 数值，之后统一限制到 1-16。用户仍可手动调整，正式运行继续由 RPM、HTTP budget 和熔断共同约束。

## 6. 前端交互

每个模块自己的配置步骤显示一个“批处理数量”数字输入框、允许范围和当前设备推荐值。模块被关闭时保留草稿值，但输入框随该模块配置一起禁用；任务配置锁定后所有批量输入不可修改。

配置流程提供一个全局“根据设备推荐”按钮。点击后一次获取设备信息并填写九个值；填写后用户可以逐项覆盖。按钮不启动任务、不运行验证集、不修改已冻结任务。

手动值高于推荐值时显示资源风险提示，但只要仍在 schema 范围内就允许预检。推荐结果同时显示 CPU、GPU、总显存、空闲显存以及每个模块采用该数值的原因。

后端、schema 或前端源码修改完成后必须同步项目内嵌运行时并重建 `frontend/dist`。发布和运行入口不得引用旧 v9 草稿、旧哈希前端资源或未同步的 `.runtime-build` 文件。

## 7. TXT 布局

`captionFormat` 新增必填字段：

```json
{
  "flatTxtLayout": "nl_newline"
}
```

Export 步骤提供两种选择，默认 `nl_newline`：

`single_line`：标签和 NL 全部用 `, ` 拼接为一行。

```text
anima style, solo, long hair, Hatsune Miku smiles toward the viewer.
```

`nl_newline`：所有非空标签在第一行，NL 在第二行，两者之间只写一个 `LF` 字节 `0A`。

```text
anima style, solo, long hair
Hatsune Miku smiles toward the viewer.
```

两种模式都保持现有字段顺序、trigger、转义、下划线转换和句号规则。空字段不产生额外逗号、空行或前导分隔符；NL 为空时只输出标签；只有 NL 时直接输出 NL。文件使用 UTF-8，禁止 `CRLF`，末尾不添加换行。

Caption、Token Budget 和 Export 必须使用同一个冻结布局，保证预览、token 计数、摘要哈希和最终 TXT 字节完全一致。JSON 导出不受布局选择影响。

## 8. 验证集与测速方法

验证目录固定为 `E:\Desktop\10_uiokv`。测速不得原地修改该目录，所有 overlay、状态库、临时输出和报告写入项目内测试临时目录，结束后清理临时脚本和测试产物。

NL 完全排除。其余八个模块分别独立测速，不能用整条流水线总耗时替代模块数据。Count Review 预置合法复核结果，只测批量应用；Export 只测批量校验、序列化和暂存。

候选值从 1 开始按模块范围取 `1, 2, 4, 8, 16, 32, 64, 128, 256, 500` 中的合法值；OCR 额外允许在需要时测试 1024。每个候选先预热一次，再至少运行三次完整样本集。

每次记录：总耗时、样本/秒、CPU 使用率、峰值内存、GPU 使用率、峰值显存、失败数、超时数和 OOM。批量 1 生成基线输出摘要；其他候选必须与基线逐样本一致。

稳定候选必须满足：三次正式运行均无进程崩溃、无 OOM、无超时、无新增失败且输出摘要一致。推荐值取稳定候选中平均样本/秒最高者；吞吐差距不超过 3% 时选择较小值，减少显存和内存波动。测速结果及设备信息进入版本化基准表和验收记录。

## 9. 测试与验收

1. JobConfig v10 contract 覆盖完整字段、缺失键、类型、上下限和 v9 拒绝。
2. Scheduler/runner 测试证明每轮领取量来自冻结的 `moduleBatchSize`。
3. 各 worker 覆盖多 item、乱序结果、单项失败、重复身份、暂停、终止和异常退出。
4. TXT 单元测试逐字节断言 `single_line`、`nl_newline`、空 NL、仅 NL、trigger、转义、无 `CR`、无末尾换行。
5. Token Budget 与 Export 对同一 annotation 生成相同 TXT SHA-256。
6. 推荐接口使用注入式硬件数据覆盖 CPU-only、不同显存、空闲显存不足和探测失败。
7. 前端测试覆盖九个手动输入、全局推荐覆盖、手动二次修改、锁定态、禁用模块、风险提示及两种 TXT 布局。
8. 后端定向测试、contract、worker、integration、前端 typecheck/build 和相关 Playwright 全部通过。
9. 使用验证集完成八模块基准；NL 无任何外部 API 请求。
10. Core/worker runtime 同步检查和 assembled drift 通过，`frontend/dist` 与源码构建一致。

## 10. 主要风险

- Caption E621 ONNX 固定单图输出，扩大处理窗口不保证线性提速，必须以验证集结果定标。
- GPU 批量可能因图片尺寸和其他应用占用发生 OOM，推荐必须同时考虑总显存和空闲显存。
- CPU 模块过大批次可能增加暂停延迟；runner 必须在批次边界保持现有协作式暂停语义。
- 多 item 协议扩大了部分失败和结果映射风险，必须严格校验 `sampleId + leaseId` 一一对应。
- JobConfig v10 会触及 Core、schema、前端、repair、测试和内嵌运行时，必须逐模块实施和验证，不能一次性无门禁替换。
