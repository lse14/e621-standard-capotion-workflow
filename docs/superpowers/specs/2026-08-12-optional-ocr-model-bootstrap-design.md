# OCR 手动模型与自动启动设计

状态：用户已确认

日期：2026-08-12

适用仓库：`lse14/anima-idg-standard-annotation-processing`

## 目标与边界

用户只双击 `Install-WebUI.bat`。安装器完成已发布的非模型组件安装后自动启动
WebUI。PaddleOCR CPU runtime 始终属于安装路线；检测到 NVIDIA 时 OCR GPU runtime
也属于安装路线。PaddleOCR 模型归档不自动下载、不加入 Git、不加入项目 Release。

本增补只改变 OCR 模型的获取和就绪语义。它不伪造生产 `install-manifest.json`、
CPython 发布资产、模型许可证结论或干净机验收。现有生产清单缺失时，首阶段
PowerShell 仍必须 fail-closed。

## 已批准行为

1. 新建任务的 OCR 默认关闭。
2. 模型未就绪时，WebUI 仍可启动，其他 E621 步骤可用；不显示 OCR 已就绪。
3. 用户主动启用 OCR 时，预检必须验证完整本地 OCR resource package 的文件大小和
   SHA-256。缺失、损坏或不完整时，预检失败，不创建可运行 OCR 任务，也不静默跳过。
4. 预检错误明确给出 `OCR_MODEL_DOWNLOAD.md`、项目根目录
   `ocr-model-archives\\`、每个官方 HTTPS URL、目标文件名、大小和 SHA-256。
5. 用户把三个原始归档放入项目根目录 `ocr-model-archives\\` 后，再双击同一个
   `Install-WebUI.bat`。安装器在既有 runtime 已完整时只验证/导入 OCR 模型；模型导入
   继续使用现有 staging、离线 probe、事务发布与幂等校验。
6. 成功的安装器在其命令行输出 OCR 下载说明的绝对路径，并自动调用现有 Start 行为。
   自动启动失败必须保留完整安装状态与日志，并以非零退出报告启动失败。

## 数据与状态

OCR 模型归档固定为以下三个文件，只允许从文档列出的 Paddle 官方 HTTPS URL 获得：

| 文件 | 目标目录 | 大小（bytes） | SHA-256 |
| --- | --- | ---: | --- |
| `PP-OCRv5_server_det_infer.tar` | `ocr-model-archives\\` | 88,340,480 | `22a33e0ba6a21425ea4192da03bf4395c9a0c67902bd924b7328fc859073045d` |
| `PP-OCRv5_server_rec_infer.tar` | `ocr-model-archives\\` | 84,869,120 | `d99be2ffd348943ab52876179168be4fb5b14f5f0812f2ae4c76d89ec2ea750a` |
| `PP-LCNet_x1_0_textline_ori_infer.tar` | `ocr-model-archives\\` | 6,871,040 | `6171f69605215a85624d650e9079fa45f7c3eaf944296181bcc5395bf3ddc7f6` |

`ocr-model-archives\\` 是用户提供、被 Git 忽略的输入目录；安装器不会删除该目录。
导入成功后正式资源仍位于 `resource-library\\ocr-models\\ocr-ppocrv5-server-paddle-v1`，
并以现有 `resource.json` 的逐文件哈希为真实性来源。

## 组件选择

安装 manifest 将 OCR runtime 与 OCR model resource 分开：`ocr-cpu` 和可选
`ocr-gpu` 是安装组件；`ocr-models` 是 `required: false` 的延迟资源，不参与基础
`install-state.json` 的成功条件。安装器先安装并验证 runtime；若归档不存在，则输出
说明并跳过模型导入。若归档存在但校验、导入或离线 probe 失败，则安装失败且不发布
不完整 OCR resource。

NVIDIA GPU probe 失败时，GPU OCR runtime 不发布，CPU runtime 保留。OCR 模型一经
成功导入由 CPU/GPU 共用；它不改变 GPU 回退规则。

## 实现边界

- `packaging/installer/assemble.py` 从必装集合移除 `ocr-models`。
- `packaging/installer/probes.py` 仅在 OCR model target 同时存在时运行 OCR probe；基础
  install 把未探测的 OCR runtime 明确记录为未验证模型功能，而不是把 import 或 runtime
  manifest 当作 OCR 功能成功。
- `packaging/installer/install.py` 在基础状态已发布后检测 `ocr-model-archives\\`，只在三个
  归档文件均存在时调用模型专用本地导入逻辑；该逻辑复用 `ocr_resource.py` 的归档校验、
  安全解压、资源 staging、离线 CPU 推理和资源事务发布，不重建 OCR runtime。缺失时记录
  可操作提示。
- `bootstrap_install.ps1` 在 Python 安装器成功后调用 `desktop_control.ps1 -Action Start`，
  并输出 OCR 说明书路径。它不自行下载 OCR 模型。
- `job_preflight.py` 继续将用户主动启用 OCR 的缺失资源视为错误，但错误文本改为指向
  用户可执行的下载说明和目标目录。
- `OCR_MODEL_DOWNLOAD.md`、README、RULES、ROADMAP 和 MEMORY 统一上述目录、入口和
  默认关闭行为。没有 `-OcrMode` 参数。

## 验收与限制

自动化测试覆盖：基础安装计划不要求 `ocr-models`；有模型时 CPU/GPU 离线 probe 仍严格；
缺模型时启用 OCR 的预检显示说明书和目标目录；默认 OCR 关闭不触发模型校验；
`Install-WebUI.bat` 的成功路径调用 Start 且不出现旧 `OcrMode` 参数。

本轮不声称已完成真实模型离线推理、NVIDIA、干净机、公开下载链接或发布资产验证。
这些仍受生产清单、CPython Release 和许可证门禁限制。
