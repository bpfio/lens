# Lens 架构与业务逻辑只读审查报告

- 审查日期：2026-10-12
- 审查对象：`bpfio/lens`
- 审查提交：`1b54b233ed4e7b500dc048964cd5d404a6661ce6`
- 审查方式：静态代码审查、协议转换探针、完整测试套件
- 结论等级：架构清晰且适合轻量部署，但当前仍是有损协议适配器，不具备完整透明代理能力

## 1. 执行摘要

Lens 的 Translator、Handler、Middleware 分层清楚，异步 SSE、共享连接池、主备回退、限流和结构化日志等基础能力齐全，120 个现有测试全部通过。

但项目当前不能证明“支持 Anthropic 或 OpenAI Responses API 的后端能力会完整转发”。默认 `PASSTHROUGH_MODE=false`，两类原生请求都会被转换为 Chat Completions；翻译器只保留函数工具，服务端搜索、文件检索、推理和托管工具等能力会被丢弃。即使开启透传，代码仍会显式过滤 `web_search`，且只转发三个固定端点，不保留原始路径、query 和全部协议头。

因此，Claude CLI 关于“当前会话没有后端 API 服务端搜索通道”的判断可能成立：

- 本地 MCP、WebFetch 等客户端自定义工具通常可作为函数工具转发，由本地 harness 执行。
- Anthropic 内置 `web_search_*` 等服务端工具不会进入上游请求，由 Anthropic 后端完成搜索的链路会被 Lens 截断。
- OpenAI Responses 的 `web_search`、`file_search` 等托管工具同样会在翻译模式中丢失，在透传模式中也会被过滤。

另有一个需要立即处置的 P1 安全问题：跟踪文件中存在明文上游 API Key，且默认监听所有网络接口。

## 2. 关键发现

### P1-1：跟踪文件暴露明文 API Key

`lens.service` 第 13 行包含真实 API Key，而同一 `API_KEY` 还会作为上游认证凭据用于 `Authorization: Bearer`。

- 证据：[`lens.service`](../lens.service#L13)、[`aperture/upstream.py`](../aperture/upstream.py#L25)
- 影响：仓库读取者可冒用客户端身份并消耗上游额度；服务同时监听 `0.0.0.0`，扩大暴露面。
- 处置：立即吊销并轮换密钥，然后从当前代码和 Git 历史中清除。仅删除文件不足以使已泄漏密钥失效。

### P1-2：服务端搜索及托管工具在所有模式中都可能丢失

| 请求模式 | Anthropic 服务端工具 | Responses 托管工具 | 结果 |
|---|---|---|---|
| 默认翻译模式 | 仅接受 `custom/function` | 仅接受函数工具 | 原生工具声明被丢弃 |
| 原生透传模式 | 受同一工具过滤规则影响 | 显式过滤 `web_search` | 搜索声明仍被丢弃 |

证据：

- Anthropic 工具白名单：[`aperture/translators/anthropic.py`](../aperture/translators/anthropic.py#L173)
- Responses 工具白名单：[`aperture/translators/responses.py`](../aperture/translators/responses.py#L232)
- 透传模式过滤 `web_search`：[`aperture/passthrough.py`](../aperture/passthrough.py#L69)

只读探针结果：

```text
Anthropic web_search_20250305 -> translated request has no tools
Responses web_search          -> translated request has no tools
Responses passthrough          -> tools becomes []
```

影响：后端即使原生支持服务端搜索、文件检索或托管执行能力，Lens 也不会把对应工具声明发送给后端，无法产生 `server_tool_use`、`web_search_tool_result` 或 `web_search_call` 等结果。

### P1-3：认证默认允许局域网和回环来源绕过

认证中间件默认 `AUTH_ALLOW_LOCAL=true`，允许 `127.0.0.1`、`::1` 和所有 `192.168.*` 来源免认证。默认监听地址为 `0.0.0.0`，CORS 又允许任意来源。部署在反向代理后时，代理来源还可能被识别为回环地址。

- 证据：[`aperture/index.py`](../aperture/index.py#L183)、[`lens.service`](../lens.service#L15)、[`aperture/helpers.py`](../aperture/helpers.py#L63)
- 影响：局域网或跨站请求可在不提供 API Key 的情况下消耗上游额度。
- 处置：生产环境默认关闭本地旁路；仅通过显式 allowlist 放行可信代理，并配置可信代理层级后使用真实客户端 IP。

### P2-1：默认模式不是完整协议转发

`PASSTHROUGH_MODE` 默认为 `false`，当前部署文件也未开启。Anthropic Messages 和 OpenAI Responses 请求都会经过转换器改写为 Chat Completions，再固定发送到 `/chat/completions`。

- 证据：[`aperture/config.py`](../aperture/config.py#L15)、[`aperture/index.py`](../aperture/index.py#L230)
- 影响：请求路径、协议头、请求字段、输出事件和原生扩展均可能发生语义损失。

翻译模式的主要能力损失包括：

- Anthropic 的 `web_search_*`、文件检索、代码执行、MCP 等非函数工具不会进入上游。
- Responses 的 `store`、`background`、`reasoning`、`include`、`service_tier`、`text` 等字段未映射。
- Responses 输入图片没有被可靠转换为上游图片内容，视觉输入可能降级或丢失。
- Anthropic `max_tokens` 会被强制提高到至少 1024，覆盖客户端请求值。
- 响应转换只覆盖文本、函数调用和基础 usage，不保留引用、推理项、服务端工具结果等原生事件。

### P2-2：透传模式仍只代理固定端点

统一 POST 路由会捕获所有请求，但透传处理只根据协议选择三个固定 URL，不转发原始路径和 query。GET 路由仅覆盖模型、健康检查和 Claude 探测端点。

- 证据：[`aperture/index.py`](../aperture/index.py#L303)、[`aperture/passthrough.py`](../aperture/passthrough.py#L96)
- 影响：`count_tokens`、读取已有 Response、取消任务、文件或批次管理等 API 不能被完整代理。
- 头部损失：Responses 仅转发 `x-openai-*`；Anthropic 仅保留 `anthropic-version`，`anthropic-beta` 等能力协商头可能丢失。

### P2-3：请求体和 SSE 缓冲没有上限

服务显式设置 `client_max_size=0`，Chat SSE 过滤器和通用 SSE 解析器也将缓冲上限设为 0，即不限制。

- 证据：[`aperture/index.py`](../aperture/index.py#L255)、[`aperture/handlers/chat.py`](../aperture/handlers/chat.py#L16)、[`aperture/stream.py`](../aperture/stream.py#L17)
- 影响：与 OpenWRT 轻量边缘部署目标冲突，单个超大请求或 SSE 行可能造成内存耗尽。
- 处置：配置可调的请求体、单条 SSE 消息和总流大小上限；大上下文需求应通过明确的高水位配置满足。

### P2-4：回退和错误路径未释放主上游响应

主上游返回可重试状态后，代码直接切换备用上游，但没有读取、关闭或 release 主响应。Handler 返回错误时也没有释放非流式上游响应。

- 证据：[`aperture/upstream.py`](../aperture/upstream.py#L256)、[`aperture/handlers/chat.py`](../aperture/handlers/chat.py#L100)
- 影响：持续 4xx/5xx 故障可能积累连接和缓冲区。
- 处置：在 fallback 前释放失败响应；所有错误返回路径统一通过上下文管理或显式 `release()` 收尾。

### P2-5：路由兜底不可达，缺少请求 schema 校验

`_detect_route()` 默认把未知请求判为 Responses，导致 `_handle_post()` 中的 `route.unknown` 分支实际上不可达。空对象或缺少 `messages/input` 的请求会被送往上游，而不是本地返回 400。

- 证据：[`aperture/index.py`](../aperture/index.py#L29)、[`aperture/index.py`](../aperture/index.py#L240)
- 影响：协议错误延后到上游才发现，上游错误响应又被通用错误体替换，诊断信息损失。

### P3-1：测试全绿但存在架构维护风险

完整测试结果为 `120 passed, 64 warnings`。其中 aiohttp 报告回路断路器在应用启动后修改 `Application` 状态已被弃用。

- 证据：[`aperture/upstream.py`](../aperture/upstream.py#L250)
- 影响：未来 aiohttp 版本可能拒绝该行为。
- 处置：将断路器状态放入启动时创建的独立可变对象，不直接修改已启动的 `Application` 映射。

仓库当前没有 CI 配置，因此测试、静态检查和打包验证不会在每次提交时自动执行。

## 3. 转发能力矩阵

| 能力 | 翻译模式 | 透传模式 | 结论 |
|---|---|---|---|
| 自定义函数工具 | 转换为 Chat function | 基本保留 | 可用 |
| MCP/WebFetch 等本地工具 | 作为函数调用可工作 | 基本保留 | 由本地执行 |
| Anthropic 服务端搜索 | 丢弃工具声明 | 仍可能被过滤 | 不可依赖 |
| Responses `web_search` | 丢弃工具声明 | 明确过滤 | 不可用 |
| Responses `file_search` 等托管工具 | 丢弃 | 未形成通用能力协商 | 不可依赖 |
| 原始 POST 路径 | 改写为固定端点 | 改写为三个固定端点 | 不完整 |
| query 参数 | 不转发 | 不转发 | 不完整 |
| 全部协议头 | 不转发 | 仅转发选定头 | 不完整 |
| 原生流事件 | 重建为有限事件集 | 基本透传 | 透传模式较完整 |
| 非 POST API | 不支持 | 不支持 | 不完整 |

## 4. 架构评价

### 优点

- Translator、Handler、Middleware 边界清晰，纯函数转换便于单元测试。
- aiohttp 同时承担服务端和上游客户端，依赖少，适合轻量部署。
- SSE 使用异步生成器逐段转发，未采用整响应缓冲。
- 已实现主备回退、断路器、入站限流、上游 RPM/并发限制和结构化日志。
- 现有测试覆盖主要协议转换、流式响应、回退和端到端基础路径。

### 不足

- 当前架构是 Hub-and-Spoke 协议降级，不是能力协商驱动的透明代理。
- 没有按上游能力选择“原生透传或翻译”的路由策略。
- 缺少请求 schema、错误语义、资源生命周期和连接压力测试。
- 单进程内存状态适合边缘单机，但限流、断路器和日志不具备多实例一致性。
- 没有 CI、静态检查、打包验证和协议契约矩阵。

### 总体判断

Lens 的分层属于清晰、务实的轻量实现，但尚不能称为“先进”。真正先进的多协议网关应当以能力协商为中心：原生兼容时完整透传路径、query、header、body 和 SSE；不兼容时才做字段级翻译，并对无法翻译的能力明确拒绝或降级，而不是静默丢弃。

## 5. 整改优先级

1. 立即轮换并清除仓库中的明文 API Key，关闭默认认证旁路。
2. 为支持 Anthropic/Responses 的上游默认启用原生透传，并移除无条件 `web_search` 过滤。
3. 改为基于原始路径的路由器，完整保留 query、协议头和请求体。
4. 建立能力协商配置，明确哪些上游支持搜索、文件检索、推理和托管执行。
5. 为每种原生工具和流事件增加契约测试，禁止静默丢弃未支持字段。
6. 增加请求与 SSE 大小限制、上游响应释放和错误体透传。
7. 引入 CI，至少执行完整测试、静态检查和构建验证。

## 6. 验证记录

```text
Git status before report: clean, main aligned with origin/main
Test command: PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' python3 -m pytest tests/ -q
Test result: 120 passed, 64 warnings in 4.08s
Protocol probes:
  Anthropic web_search -> tools absent
  Responses web_search -> tools absent
  Passthrough web_search -> tools == []
```

本报告不包含实际密钥值。
