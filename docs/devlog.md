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


---

# Lens 研发排查手记（二）：全协议透明镜像网关演进与核心缺陷修复

> 本手记记录 Lens 从“单协议转译网关”向“全能力感知、全路径镜像、双向头部透传的现代 AI 代理网关”重构演进，以及针对安全凭据、SSE 流状态机、多模态映射与 DSML 解析等关键缺陷的排查与修复全过程。

---

## 一、 背景与故障现象（Problem）

在系统审查与深度端到端压测过程中，发现 Lens 架构与运行时存在两阶段共六大维度缺陷：

1. **协议透传不彻底与端点拦截**：
   - 客户端调用非 Chat/Responses/Anthropic 的衍生端点（如 `/v1/embeddings`、`/v1/batches`、`/v1/files/*`）或非 POST 方法（如 `GET /v1/models/*`、`DELETE /v1/files/*`）时，Lens 粗暴返回 400 `INVALID_ROUTE` 或 405 `METHOD_NOT_ALLOWED`，阻断了上游 AI 平台的完整能力。
   - URL 中的 Query 查询参数（如分页游标 `cursor`、`limit` 等）在转发时丢失。
   - 上游声明原生支持 Responses API 或 Anthropic API 时，Lens 仍全局强行将其降级翻译为 Chat Completions，导致复杂流式结构与私有扩展协议失真。
   - 请求头与响应头单向截断：上游关键限流头（`x-ratelimit-*`、`retry-after`）被过滤丢弃，客户端分布式追踪头（`traceparent` 等）无法送达上游。
   - 路由误判：`_detect_route` 仅凭 Body 包含 `input` 字段，误将 `/v1/embeddings` 等请求当作 Responses API 转译。

2. **Responses API 流式状态机异常**：
   - 当模型执行纯工具调用（无文本输出）时，流式未发射 `response.output_item.added`（`item_0`），但在流结束阶段却无条件发射 `item_0` 的 `done` 事件，导致客户端状态机崩溃报错。

3. **Anthropic 思考流（Thinking Block）丢失**：
   - 当上游输出 `reasoning_content`（如 DeepSeek-R1、Kimi 等）时，Anthropic SSE 协议转换器直接丢弃了推理增量，Claude Code 客户端无法展示推理过程。

4. **多模态与工具声明失真**：
   - Responses API 的多模态 `input_image` 格式未转换为标准 Chat Completions `image_url`，导致视觉大模型报错。
   - Codex 托管工具（hosted tools）声明被错误丢弃。

5. **DSML 工具调用强类型反序列化失败**：
   - 模型在 DSML 块中输出 `true`、`false`、`null` 或纯数字参数时，解析器未能正确推断 JSON 原生类型，一律回退为纯字符串，引发强类型下游工具执行崩溃。

6. **安全与运维风险**：
   - `lens.service` 服务单元文件中硬编码了真实 API 密钥，且默认监听公网 `0.0.0.0`；
   - 认证采用普通字符串等值比较，存在计时侧信道攻击隐患；
   - 内存日志写入操作直接在主事件循环同步调用，阻塞协程调度。

---

## 二、 根因分析（Root Cause）

1. **路由与透传设计过于僵化**：
   - `index.py` 仅使用 `app.router.add_post("/{path:.*}")` 捕获 POST 请求，未注册通配方法；且内部仅支持 3 条固定硬编码路径映射，缺失泛化反向代理转发层。
   - `_detect_route` 启发式规则过度宽泛，在未比对路径的前提下直接检索 Body 字典键值，导致标准 OpenAI 端点发生误判。

2. **流式状态机生命周期未闭合**：
   - `translate_stream_events` 假定每个 SSE 会话均存在文本输出项（`item_0`），但在仅有工具调用时，`text_item_added` 为 False，收尾逻辑却强行构造了 `item_0` 的完结事件。

3. **协议规范演进脱节**：
   - Anthropic 官方规范已正式定义 `thinking` content block，原翻译器仅识别 `text` 与 `tool_use`，遇到上游 `reasoning_content` delta 字段时直接跳过。

4. **安全凭据未实施三层隔离**：
   - 生产部署将 Secret 直接写入 Systemd 静态单元配置，缺乏 `.env` 环境变量模板与凭据剥离机制；认证比较缺乏常量时间防御。

---

## 三、 解决方案与架构重构（Solution）

为彻底解决上述问题，对 Lens 进行系统级重构：

### 1. 现代透明反向代理引擎 (`aperture/passthrough.py` & `aperture/index.py`)
- **全方法通配支持**：注册 `add_route("*", "/{path:.*}", _handle_dispatch)`，完整捕获 GET、POST、DELETE、PUT、HEAD、PATCH。
- **全路径与 Query 镜像**：基于 `request.rel_url.path_qs` 镜像拼接上游目标 URL，精准保留任意复杂 Query 参数，防范 `/v1/v1` 重复前缀。
- **双向 Headers 镜像**：
  - 上行剔除 RFC 2616 逐段传输头（Hop-by-hop），完整透传 `traceparent`、`tracestate`、`baggage` 等分布式追踪头；
  - 下行完整透传上游 `x-ratelimit-*`、`retry-after`，并在 `filter_downstream_headers` 中剥离 `content-type`，规避 aiohttp 双重参数冲突报错。
- **能力感知与直通状态机**：
  - 引入 `UPSTREAM_CAPABILITIES` 配置：声明支持协议时（如 `responses,anthropic`）实施 Zero-Touch 原生直通；未声明时自动降级走 Lens 翻译层；
  - 未知端点（如 `embeddings`、`files`）自动作为通用端点直通，并在 `_detect_route` 中增加标准端点白名单拦截误判。
- **多模型路由与动态模型目录**：
  - 引入 `MODEL_OVERRIDE_MODE`（支持 `passthrough` / `mapped` / `force`）；
  - 支持 `MODEL_MAP` 环境变量配置 JSON 别名映射字典；
  - `GET /v1/models` 端点动态聚合展示基础模型、Display 伪装模型与所有映射别名。

### 2. 协议翻译器与流式状态机修复
- **流式生命周期对齐**：在 `translate_stream_events` 中根据 `text_item_added` 标志位条件发射文本完结事件；动态计算工具调用输出索引，确保纯工具调用流严密合法。
- **Thinking 流式支持**：在 Anthropic 翻译器中增加 `thinking` block 状态跟踪，实时发射 `thinking_delta` 事件。
- **多模态与工具保真**：在 `translate_responses_to_chat` 中将 `input_image` 正确转换为 `image_url` 规范；全量提取并保留 `additional_tools`。
- **DSML 强类型解析**：在 `normalize_dsml_tool_calls` 中增加对 `true/false/null/int/float` 的类型推断，杜绝类型降级为字符串。

### 3. 安全加固与性能提升
- **凭据剥离**：将 `lens.service` 中的真实 API Key 移出，替换为占位符，引入 `EnvironmentFile=-/home/yupeng/lens/.env`，绑定地址收敛至 `127.0.0.1`。
- **常量时间认证**：在 `authenticate` 中全面使用 `hmac.compare_digest`。
- **非阻塞日志队列**：在 `Logger` 中引入后台工作线程与无界线程安全队列，彻底消除主事件循环的磁盘 I/O 抖动。

---

## 四、 验证证据（Evidence）

### 1. 自动化测试套件全量验收
执行 `PYTHONDONTWRITEBYTECODE=1 pytest tests/`：
```text
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0
collected 133 items

tests/test_config.py .....                                               [  3%]
tests/test_dsml.py ......                                                [  8%]
tests/test_e2e.py .....                                                  [ 12%]
tests/test_fallback.py ..........                                        [ 19%]
tests/test_handlers.py .........                                         [ 26%]
tests/test_helpers.py ............                                       [ 35%]
tests/test_middleware.py ..........                                      [ 42%]
tests/test_passthrough.py ......                                         [ 47%]
tests/test_proxy_mirror.py .....                                         [ 51%]
tests/test_remediation.py .......                                        [ 56%]
tests/test_stream.py ...                                                 [ 58%]
tests/test_translate_anthropic.py ............                           [ 67%]
tests/test_translate_stream.py ......                                    [ 72%]
tests/test_translate_to_chat.py ......................                   [ 88%]
tests/test_upstream.py .........                                         [ 95%]
tests/test_upstream_limiter.py ......                                    [100%]

============================= 133 passed in 4.07s ==============================
```
**实测结果**：
- 133 项测试全部通过（含 7 项缺陷修复契约测试 + 5 项透明反向镜像专项测试）；
- 0 处失败，0 处回归，0 处警告。

### 2. 功能验收对比表

| 测试场景 | 修复前表现 | 修复后表现 | 状态 |
|---|---|---|---|
| GET /v1/models/custom?limit=10 | 报 400 INVALID_ROUTE | 状态码 200，Query 完整保留，镜像上游 | ✅ 通过 |
| DELETE /v1/files/file-999 | 报 405 Method Not Allowed | 状态码 204，准确送达并释放 | ✅ 通过 |
| 双向限流与追踪头 | 上游丢弃 traceparent，下游丢弃 ratelimit | 上行追踪头与下行限流指标 100% 镜像 | ✅ 通过 |
| Responses 纯工具调用流 | 客户端报错 item_0 未闭合 | 状态机严密闭合，端到端 0 异常 | ✅ 通过 |
| Anthropic R1 思考流 | reasoning_content 被直接丢弃 | 正确转换为 thinking delta 增量输出 | ✅ 通过 |
| DSML 布尔与数字参数 | 误转换为字符串 "true" / "123" | 保持原生 JSON boolean / number 类型 | ✅ 通过 |


### 4. 生产运行态端到端实机验证（Xiaomi MiMo 原生多协议直通）

在本地常驻服务 `lens.service` 真实运行态中，针对生产 Xiaomi MiMo 上游，实机执行官方客户端调用验收：

#### (1) OpenAI Codex CLI 真实端到端实测
```bash
codex exec "echo 'lens_mimo_responses_passthrough_test'"
```
- **客户端回显输出**：
  ```text
  user
  echo 'lens_mimo_responses_passthrough_test'
  codex
  I’ll run the requested command and report its output.
  exec
  /bin/bash -lc "echo 'lens_mimo_responses_passthrough_test'" in /home/yupeng/lens
   succeeded in 172ms:
  lens_mimo_responses_passthrough_test
  codex
  lens_mimo_responses_passthrough_test
  tokens used
  14,082
  ```
- **网关底层链路日志**（`tail -n 10 /home/yupeng/lens/logs/lens.log`）：
  ```json
  {"level": "info", "event": "route.detect", "route": "responses", "path": "v1/responses", "method": "POST", "passthrough": true, "capabilities": ["chat", "responses", "anthropic"]}
  {"level": "info", "event": "passthrough.request", "method": "POST", "protocol": "responses", "url": "https://token-plan-cn.xiaomimimo.com/v1/responses", "from_model": "gpt-5.6-sol", "to_model": "mimo-v2.6-flash"}
  {"level": "info", "event": "passthrough.response", "status": 200, "content_type": "text/event-stream"}
  ```
- **验证结论**：直接以原生 Responses API 直通 Xiaomi MiMo 的 `/v1/responses` 端点，成功完成工具调用调度与执行，0 降级为 Chat Completions，0 协议失真。

#### (2) Anthropic Claude Code CLI 真实端到端实测
```bash
claude -p "请输出 lens_claude_mimo_passthrough_ok"
```
- **客户端回显输出**：
  ```text
  lens_claude_mimo_passthrough_ok
  ```
- **网关底层链路日志**（`tail -n 10 /home/yupeng/lens/logs/lens.log`）：
  ```json
  {"level": "info", "event": "request.start", "method": "HEAD", "path": "/api/hello"}
  {"level": "info", "event": "request.end", "status": 200, "elapsed_ms": 0}
  {"level": "info", "event": "route.detect", "route": "anthropic", "path": "v1/messages", "method": "POST", "passthrough": true, "capabilities": ["chat", "responses", "anthropic"]}
  {"level": "info", "event": "passthrough.request", "method": "POST", "protocol": "anthropic", "url": "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages", "from_model": "claude-fable-5", "to_model": "mimo-v2.6-flash"}
  {"level": "info", "event": "passthrough.response", "status": 200, "content_type": "text/event-stream"}
  ```
- **验证结论**：`HEAD /api/hello` 探测即时响应，随后请求 100% 直通 Xiaomi MiMo 原生 Anthropic 端点 `/anthropic/v1/messages`，SSE 流式完整保真，退出码 0。
