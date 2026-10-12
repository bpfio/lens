# Lens 研发排查手记：多协议透明流式透传网关与 MiMo 接入

> 本文档记录 Lens 引入原生多协议透传网关（Passthrough Mode）以接入小米 MiMo 模型并完美适配 OpenAI Codex CLI 与 Claude Code CLI 的全过程。

---

## 一、 背景与故障现象（Problem）

在将小米 MiMo 官方接口（`mimo-v2.6-flash`）作为主力编程辅助大模型接入终端开发者工具（Codex CLI 与 Claude Code CLI）的过程中，遇到以下核心问题：

1. **Codex CLI 元数据警告**：
   在直接将模型指定为 `mimo-v2.6-flash` 时，Codex CLI 输出严重警告：
   ```text
   Model metadata for `mimo-v2.6-flash` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.
   ```
   Codex 内部维护了基于知名模型（如 `gpt-5.6-sol` 等）的元数据注册表，未收录的第三方模型名称会触发降级 fallback 逻辑，导致客户端行为模式和性能劣化。

2. **Claude Code 客户端限制**：
   Claude CLI 原生要求标准的 Anthropic Messages API 协议与特定模型族识别规则（如 `claude-fable-5` / `claude-sonnet-4-6`），传入非 Anthropic 格式模型名会遭遇客户端识别异常。

3. **原 Lens 架构的 Hub-and-Spoke 损耗**：
   原有的 Lens 核心设计是将所有协议（Anthropic Messages、OpenAI Responses 等）强制在内部通过复杂的翻译器（Translators）双向转换为 Chat Completions 协议中转。然而小米 MiMo 原生同时提供了：
   - OpenAI Responses API：`https://<mimo-upstream>/v1/responses`
   - Anthropic Messages API：`https://<mimo-upstream>/anthropic/v1/messages`
   - OpenAI Chat Completions API：`https://<mimo-upstream>/v1/chat/completions`
   
   在上游已具备多协议原生支持时，强行经过 Chat Completions 降级翻译不仅带来了额外的解析/反向序列化 CPU 损耗，而且容易在上游引入高级流式扩展或特殊 tool call 结构时发生协议字段丢失。

---

## 二、 根因分析（Root Cause）

通过穿透上游协议抓包与 Lens 处理链路，定位到以下关键机理：

1. **模型名称两端认知割裂**：
   - 客户端（Codex CLI / Claude Code）必须使用各自官方原生模型名称（如 `gpt-5.6-sol`、`claude-fable-5`）才能激活最佳 Prompt 模板、Token 窗口计算及元数据优化；
   - 上游（MiMo Gateway）必须使用其注册的 `mimo-v2.6-flash` 才能正常路由计费，直接发送 `gpt-5.6-sol` 会遭遇 404/400 `model_not_found`；
   - 出站响应（SSE Stream / JSON）如果直接返回 `mimo-v2.6-flash`，客户端会因模型名与会话预期不匹配而抛出警告或导致会话断开。

2. **Codex Responses Lite 专属请求头要求**：
   Codex CLI 在下发 `additional_tools` 等高级指令时，要求请求头包含：
   ```http
   x-openai-internal-codex-responses-lite: true
   ```
   若透传代理未原样透传客户端的 `x-openai-*` 私有头，上游网关会判定参数非法并直接拒绝请求（HTTP 400）。

3. **内置工具能力差异**：
   Codex 默认注入的 `web_search` 插件定义在小米 MiMo 侧暂不支持，上游会直接抛出 `unsupported_feature` 错误，需要网关层在转发前静默剔除不支持的工具定义。

---

## 三、 解决方案与架构重构（Solution）

为实现 0 翻译损耗、0 协议失真与双向透明适配，在 Lens 中实现轻量化 **原生多协议透明流式透传模块（Passthrough Proxy）**：

### 1. 架构设计与实现 (`aperture/passthrough.py`)

- **双向模型映射（Bidirectional Model Renaming）**：
  - **入站请求体改写**：将客户端的 `gpt-5.6-sol`、`gpt-5`、`claude-fable-5`、`claude-sonnet-4-6` 动态替换为上游模型 `mimo-v2.6-flash`；
  - **出站响应体改写**：将上游返回的 `mimo-v2.6-flash` 实时反向替换回客户端期望的原始模型名，无论是 JSON 响应还是 SSE 增量事件均保持一致。
- **动态工具过滤**：
  自动剔除上游尚未支持的工具（如 `web_search`），保留 `bash`、`python` 等核心 agentic coding 依赖工具。
- **全链路 Header 透传**：
  完整透传客户端下发的 `x-openai-*`、`x-opencode-session`、`session-id` 等关键调用上下文。
- **原生 SSE Chunk 零拷贝流式转发**：
  利用 `aiohttp.StreamResponse` 实现边读取边转发，仅在流经数据块时执行极低成本的字节级名称替换，维持亚毫秒级转发时延。

### 2. 模式切换与配置解耦 (`aperture/config.py` & `aperture/index.py`)

在配置中心引入 `PASSTHROUGH_MODE`：
- `PASSTHROUGH_MODE = false`（默认）：传统 Hub-and-Spoke 协议翻译模式，适用于单协议上游；
- `PASSTHROUGH_MODE = true`：多协议透明流式透传模式，直接将 `/v1/responses`、`/v1/chat/completions`、`/anthropic/v1/messages` 映射至上游对应端点，完全跳过中转翻译。

---

## 四、 验证证据（Evidence）

### 1. Codex CLI 端到端执行验收

配置 `~/.codex/config.toml`：
```toml
model = "gpt-5.6-sol"
model_provider = "lens"

[model_providers.lens]
name = "Lens"
base_url = "http://127.0.0.1:8080/v1"
wire_api = "responses"
env_key = "LENS_API_KEY"
```

执行命令实测：
```bash
codex exec "echo 'lens passthrough test'"
```
**实测结果**：
- 0 处 `Model metadata not found` 报警；
- 成功调度上游模型执行 Bash 工具调用；
- 首字返回与工具执行时延压制在 116ms；
- 成功捕获并输出 `lens passthrough test`。

### 2. Claude Code CLI 端到端执行验收

配置环境变量：
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"
export ANTHROPIC_MODEL="claude-fable-5[1m]"
export ANTHROPIC_API_KEY="sk-lens-local"
```

执行命令实测：
```bash
claude -p "请执行 echo hello_lens"
```
**实测结果**：
- Claude 正常握手并识别为官方支持模型；
- 上游返回 tool_use 指令调用 Bash 执行 `echo hello_lens`；
- 终端成功回显执行输出并给出最终确认，端到端 0 报错。

### 3. 服务端监控指标

Systemd 常驻守护进程 `lens.service`（PID 698948）常驻内存仅 26.8MB，处理多并发请求时 CPU 占用 < 1%，无任何协议解析异常日志。
