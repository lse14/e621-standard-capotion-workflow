# RULES

## 安装边界

1. 支持范围限定为 Windows 10/11 x64 源码自举安装。
2. 用户入口只有 `Install-WebUI.bat`；不得要求用户执行 Python、npm 或编译命令。
3. 不安装或修改系统 Python、Node、CUDA Toolkit、Visual Studio、Windows SDK、`PATH`
   或注册表。
4. 运行时、资源、状态、日志和临时下载只能位于项目目录。
5. 首发只安装 E621 工作流，不增加 Danbooru 支持。
6. 质量评分和 OCR runtime 均为默认安装项。OCR 模型是用户提供的延迟本地归档，OCR
   默认关闭；缺失或 SHA-256 不符只能阻止 OCR-enabled job，必须指向
   `OCR_MODEL_DOWNLOAD.md`、`ocr-model-archives` 和再次双击 `Install-WebUI.bat`，不得
   静默声称 OCR 可用。

## 组件选择

1. E621 EVA02 Tagger、Qwen3 0.6B tokenizer、完整 LSE14 质量评分栈必装。
2. NVIDIA 可用时，Caption 和 Policy 选择 CUDA 变体；否则选择 CPU 变体。
3. NVIDIA 可用时同时安装 OCR CPU 和 OCR GPU，默认执行 GPU 并保留 CPU 回退。
4. 无 NVIDIA 或驱动不兼容时只安装 OCR CPU，不下载无效 GPU 负载。
5. 安装器不得安装 NVIDIA 驱动；只检测并报告现有驱动能力。
6. `Install-WebUI.bat` 没有 `-OcrMode` 参数；三份 OCR 模型归档只能由用户按
   `OCR_MODEL_DOWNLOAD.md` 的官方 URL、文件名、大小和 SHA-256 放入
   `ocr-model-archives`。

## 下载与供应链

1. 每个下载文件必须有 HTTPS URL、不可变上游身份、精确大小和 SHA-256。
2. Hugging Face 模型必须固定完整 commit SHA，禁止使用 `main` 或浮动标签。
3. Python wheels 必须是安装清单列出的具体文件，禁止在用户电脑上动态解析依赖。
4. CUDA wheels 只使用 PyTorch/Paddle 等约定官方源，不下载完整 CUDA Toolkit。
5. CLIP `ViT-L-14.pt` 只使用已验证的 OpenAI 官方 CDN 文件。
6. 下载重定向后的主机必须属于清单允许列表，最终内容仍须通过大小和哈希验证。
7. 第三方来源、许可证入口和版本必须同步维护在 `docs/THIRD_PARTY_NOTICES.md`。
8. OCR 模型权重不自动下载、镜像或纳入 Release；其手动本地导入仍须通过哈希、离线
   探测和事务发布。

## 文件安全

1. 下载写入 `.partial`，验证通过后才能更名为完整缓存文件。
2. 解压和组装必须发生在项目内唯一 staging 目录，不得跟随重解析点逃逸项目根目录。
3. 压缩包条目必须拒绝绝对路径、父目录穿越、设备路径和链接条目。
4. 未通过离线探测的组件不得进入正式运行时或资源目录。
5. 发布采用同卷重命名和事务日志；失败时恢复上一次有效组件。
6. 清理只允许操作已解析且位于项目根目录内的 installer staging/cache。
7. 不删除源码、配置、数据集、输出或用户创建的数据。

## 运行与恢复

1. 所有大文件下载支持 HTTP Range 续传；服务器不支持 Range 时安全重下该文件。
2. 安装可重复执行；已存在组件必须重新核对清单指纹后跳过。
3. GPU 预检通过但实际离线推理失败时，Caption/Policy 自动改走 CPU；OCR 使用已安装 CPU 回退。
4. 自动下载失败时输出官方直链、目标文件名、大小和 SHA-256。
5. 安装成功后删除 wheel、完整缓存和 staging；失败时保留可续传 `.partial`、已验证的
   CPython 完整缓存与日志，重试前仍须重新核对完整缓存的大小和 SHA-256。
6. 基础安装状态只能在全部基础必装组件探测通过后写入；OCR-enabled job 还必须有
   通过 `OCR_MODEL_DOWNLOAD.md` 归档导入的 OCR 资源。

## 验证纪律

1. 模块可导入不等于安装通过；必须执行代表性离线加载或最小推理。
2. CPU 与 NVIDIA 路线必须在没有系统开发环境的干净机分别验证。
3. 网络中断、哈希错误、磁盘不足、重复安装和中文路径必须有自动化测试。
4. 未运行的测试不得记录为通过；所有完成项需在 `ROADMAP.md` 写明证据。
5. 修改保持最小，不重构与源码自举无关的业务模块。
