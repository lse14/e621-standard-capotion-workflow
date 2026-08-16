# NL API 提供商兼容修复设计

## 目标

修复 NL API 端点误拼接、Windows 凭据引用文件名非法，以及 Cloudflare 拦截 Python 默认 User-Agent 的问题。保持现有 OpenAI-compatible 请求协议，不增加提供商专用配置或第三方依赖。

## 已确认行为

1. Endpoint 接受基础 URL、以 `/models` 结尾的模型列表 URL，或以 `/chat/completions` 结尾的聊天 URL。
2. 端点归一化仅识别末尾的 `/models` 和 `/chat/completions`；移除已知末尾后，分别生成同一基础路径下的模型列表和聊天地址。
3. IkunCode 配置仍使用提供商确认的基础 URL `https://api.ikuncode.cc/v1`，不在代码中硬编码 IkunCode 域名或版本映射。
4. 新的默认凭据引用为 `nl-profile-<profileId>`，不再包含 Windows 文件名禁止的冒号。
5. 后端凭据引用只接受 `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`；非法引用返回现有 400 错误，不进入文件写入。
6. 已保存的带冒号配置在用户下次保存 Profile 时转换为安全默认引用；此前未成功写入的 API key 需要重新输入一次。
7. NL 诊断请求和正式 NL worker 都发送 `User-Agent: Anima-Dataset-Tool/1.0`。

## 数据流与错误处理

- 前端保存 Profile 前计算安全凭据引用，然后先保存 Profile，再通过现有凭据 API 保存密钥。
- 后端凭据存储在执行 DPAPI 或文件操作前验证引用，避免非法 Windows 路径产生未捕获的 500。
- 诊断模型列表和测试消息共用同一端点归一化规则。
- 正式任务继续使用冻结到 worker hello 中的聊天端点；只补充请求头，不改变请求体、图片传输或响应协议。
- 401/403/404/429/5xx 继续使用现有诊断错误分类，不增加自动重试或绕过 TLS/WAF 的逻辑。

## 修改范围

- Core：NL 诊断端点归一化、诊断 User-Agent、凭据引用校验。
- NL worker：正式请求 User-Agent。
- Frontend：安全的默认凭据引用与旧引用迁移。
- Tests：端点三种输入、非法冒号引用、API 400、诊断和 worker 请求头、前端保存迁移。
- Build：同步 Core/NL runtime 并重新生成 `frontend/dist`。

## 验收标准

1. `https://example.test/v1/models` 不会生成 `/models/chat/completions`。
2. 基础 URL、模型 URL 和聊天 URL 均生成唯一且正确的 models/chat URL。
3. `nl-profile:default` 不能进入 Windows 文件写入；API 返回 400，而不是 500。
4. 前端保存旧引用时提交 `nl-profile-default`，并能保存新输入的 API key。
5. 诊断和正式 worker 请求均包含固定 User-Agent。
6. Core、NL worker、API、前端类型检查、生产构建和相关 Playwright 回归通过。

## 边界与风险

- 不推断不同版本路径之间的提供商私有映射，例如不把 `/v1beta/models` 自动改为 `/v1/chat/completions`。
- 不迁移或读取从未成功创建的非法冒号文件。
- 不修改 API key 加密格式、Profile schema、任务 schema 或 NL 响应协议。
- 自定义 User-Agent 已通过不带密钥的只读请求验证能够到达 IkunCode 鉴权层；真实密钥和模型调用仍需用户环境验证。
