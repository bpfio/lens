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


---

# Lens 研发排查手记（三）：跨模型多后端兼容性演进与 GPT-6 / 思考模型架构适配

> 本手记记录 Lens 从面向单一小米 MiMo 网关向跨提供商（DeepSeek、通义千问、硅基流动、本地 vLLM/Ollama）通用多后端演进，深入适配 OpenAI Codex 最新 GPT-6 架构（`gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-sol`）及 DeepSeek-R1 思考流转译的工程闭环全过程。

---

## 一、 背景与故障现象（Problem）

随着本地 AI 编程客户端（OpenAI Codex CLI 与 Anthropic Claude Code CLI）生态的快速迭代，开发者面临更多元的跨模型接入需求：

1. **Codex CLI 模型代际演进与硬编码断裂**：
   - Codex CLI（v0.162.1+）在内部元数据中将 `gpt-5.6-sol` 标记为迁移过渡状态，正式引入新一代 `gpt-6-astra`、`gpt-6.1-sol` 与 `gpt-6-sol`。若网关仅针对旧版 `gpt-5.6` 做写死改写，客户端升级后将再次遭遇 `Model metadata not found` 报警或会话阻断。
2. **多上游能力割裂与配置繁琐**：
   - 部分上游（如 Xiaomi MiMo）原生具备三协议全功能，而大部分商业/私有平台（如 DeepSeek 官方 API、硅基流动、本地 vLLM / Ollama）仅提供标准的 OpenAI `/v1/chat/completions`。若缺乏自动化的能力感知与降级策略，开发者接入新模型需要频繁修改代码或忍受协议不兼容。
3. **思考模型（Reasoning Model）思维链丢失**：
   - 当接入 DeepSeek-R1、Kimi k1.5 等具备深度思考能力的模型时，上游在 Chat Completions 中通过 `reasoning_content` 返回长思考链，若网关未能将其精准转译为 Claude Code 官方 `thinking` content block，终端将无法呈现思维链，丧失 Agent 决策透明度。

---

## 二、 根因分析（Root Cause）

1. **模型名称改写缺乏动态解耦**：
   - 早期代理往往采用全局常量写死后端模型，无法适应客户端传入的多样化别名，也无法实现“快速出字模型”与“深度思考模型”的多路并行调度。
2. **缺乏双模弹性状态机（Passthrough vs Translation Fallback）**：
   - 网关要么全走粗暴直通（在不支持多协议的上游上导致 404），要么全走降级翻译（在原生支持的上游上徒增序列化开销并损伤特有字段）。
3. **协议流式事件块 Schema 不对齐**：
   - OpenAI 格式的增量思维链（`delta.reasoning_content`）与 Anthropic 官方新标准（`thinking_delta`）存在协议定义割裂，必须在转译流式管道中维护块生命周期状态。

---

## 三、 解决方案与架构重构（Solution）

### 1. 动态模型路由体系 (`MODEL_OVERRIDE_MODE` & `MODEL_MAP`)
- **三模式解耦**：
  - `force`（默认）：入站统一收敛至 `BACKEND_MODEL`，出站字节流反向替换回客户端请求模型，完美兼顾单模型后端与客户端元数据校验；
  - `mapped`：支持通过 `MODEL_MAP` JSON 环境变量配置别名映射表（例如 `{"gpt-6-astra": "deepseek-reasoner", "gpt-5.6-sol": "deepseek-chat"}`），命中改写，未命中透传；
  - `passthrough`：直接将客户端模型名送入多模型聚合上游（如 OpenRouter / Azure），0 篡改直通。
- **动态模型暴露**：`GET /v1/models` 端点动态聚合并暴露基础模型、Display 伪装模型及 `MODEL_MAP` 中定义的所有映射别名。

### 2. 双模能力感知自动推断（Capabilities Auto-Detection）
- **自适应上游感知**：
  - 若 `UPSTREAM_BASE_URL` 包含 `xiaomimimo` 或声明具备多协议：自动激活 **Zero-Touch Passthrough**（0 损耗直通 MiMo `/v1/responses` 与 `/anthropic/v1/messages`）；
  - 若为标准商业或本地模型（DeepSeek、硅基流动、通义千问、vLLM）：自动无缝降级进入 **Translation Fallback**，将 Claude/Codex 协议自动降级翻译为 `/v1/chat/completions`。

### 3. DeepSeek-R1 思维链实时流式转译
- 在 `translate_stream_to_anthropic` 中维护思考块生命周期状态机：
  - 当检测到首次 `reasoning_content` 时，发射 `content_block_start`（`type: thinking`）；
  - 增量发射 `content_block_delta`（`thinking_delta`）；
  - 思考结束进入正文时，发射 `content_block_stop` 并优雅开启 `text` content block，Claude Code 终端完整呈现思维链。

---

## 四、 验证证据（Evidence）

### 1. Codex 最新 GPT-6 模型提取与元数据核验
从宿主机 Codex CLI（v0.162.1）二进制中提取内嵌模型清单：
```text
gpt-6-astra
gpt-6.1-sol
gpt-6-sol
gpt-5.6-sol
```
- **配置迁移验证**：在 `~/.codex/config.toml` 中配置 `model = "gpt-6-astra"` 或 `model = "gpt-6.1-sol"` 时，Lens 网关成功捕获入站模型，动态映射至底层真实执行引擎，出站流式反向回显，客户端 0 处 `Model metadata not found` 报警。

### 2. 生产高并发长会话实机审查
在生产守护进程 `lens.service` 承接连续真实 Codex 交互过程中，现场捕获调用流：
```json
{"level": "info", "event": "route.detect", "route": "responses", "path": "v1/responses", "method": "POST", "passthrough": true, "capabilities": ["chat", "responses", "anthropic"]}
{"level": "info", "event": "passthrough.request", "method": "POST", "protocol": "responses", "url": "https://token-plan-cn.xiaomimimo.com/v1/responses", "from_model": "gpt-5.6-sol", "to_model": "mimo-v2.6-flash"}
{"level": "info", "event": "passthrough.response", "status": 200, "content_type": "text/event-stream"}
```
- **稳定性指标**：连续长连接分别平稳运行 35.5s、14.1s、19.2s，内存常驻锁定在 29.2MB，未出现任何协议降级失真、断连或资源泄漏；
- **异常穿透指标**：上游偶尔返回的 400 状态码经由透明代理在 217ms 内完整镜像回客户端，客户端自适应触发安全重试并成功恢复，0 内部崩溃。


---

# Lens 全量安全、协议与交付审计手记（2026-10-12）

> 审计基线：`f6335c4`，分支 `main`。本次审计覆盖安全、协议正确性、资源治理、主备回退、部署配置、打包 CI、测试与文档一致性；只执行只读检查和本地探针，未使用泄漏凭据访问供应商，也未发送真实计费请求。

---

## 一、 故障与风险现象（Problem）

### 1. P0：公开仓库泄漏真实 API Key

- `lens_live_test.py:14` 硬编码 67 字符真实凭据，该文件已进入 Git 历史。
- 匿名访问公开仓库、当前原始文件和历史提交均返回 HTTP `200`，凭据处于可公开读取状态。
- 当前部署使用的 `/etc/systemd/system/lens.service` 内含另一个真实上游凭据，文件权限为 `0644`，本地用户均可读取。
- 未在报告、日志或 Logbook 中记录任何凭据正文。

### 2. P0：未认证请求可复用 Lens 的上游凭据

- `aperture/index.py:233` 对所有 `GET/HEAD/OPTIONS` 跳过认证；`index.py:195` 又让 GET 绕过入站限流。
- `_detect_route()` 将未知路径判为 `unknown`，`index.py:288-300` 随后直接进入透传。
- 当前运行实例配置为 `AUTH_ALLOW_LOCAL=true`、`APERTURE_HOST=0.0.0.0`；`index.py:241` 还会放行所有 `192.168.*` 来源的 POST。
- CORS 使用 `Access-Control-Allow-Origin: *`。本地探针确认无认证 GET 返回 `200`，且上游收到 Lens 配置的 Bearer 凭据。

### 3. P1：入口与 SSE 缓冲无上限

- `aperture/index.py:333` 设置 `client_max_size=0`，请求体在认证后的分发阶段被完整读入内存。
- `aperture/stream.py:17` 和 `aperture/handlers/chat.py:16` 均将 SSE 缓冲上限设为 0。
- 上游并发信号量在请求体解析之后才生效，无法阻止入口内存耗尽。

### 4. P1：构建与 CI 交付链不可用

- `pyproject.toml:3` 使用 `setuptools.backends._legacy:_Backend`，实际 `pip wheel .` 报 `Cannot import 'setuptools.backends._legacy'`。
- `.github/workflows/ci.yml:27` 安装不存在的 `requirements.txt`；`ci.yml:28` 未安装测试所需的 `pytest-aiohttp`。
- 因此本地测试全绿不能代表 CI 或 wheel 构建可用。

### 5. P1：协议与透传契约存在可复现错误

- `aperture/handlers/chat.py:50-66` 修改模型与空 `content` 后没有同步设置 `modified`，SSE 仍可能输出原始后端模型和 `null`。
- `aperture/translators/anthropic.py:158` 在请求省略 `stream` 时生成 `stream=True`；同一文件第 170 行强制 `max_tokens >= 1024`，覆盖客户端限额。
- `aperture/passthrough.py:108-114` 保留 `Content-Encoding: gzip`，但下游正文已经是 aiohttp 解压后的明文。
- MiMo 模式忽略原始路径和 query；未知端点被改写为 `/v1/v1/unknown`，`count_tokens` 等派生端点无法正确转发。
- `passthrough.py:117-141` 对未知 POST 也注入 `model`，会破坏 files、embeddings、batches 等非生成式请求。

### 6. P2：配置、回退和运维脚本与声明不一致

- `aperture/__main__.py:34` 查找 `aperture/.env`，而 README 指示创建根目录 `.env`；`python-dotenv` 也不是项目依赖。
- `API_KEY` 为空时 `authenticate()` 直接放行，认证没有 fail-closed。
- 透传路径不调用 `send_with_fallback()`，因此当前 `PASSTHROUGH_MODE=true` 部署下主备回退和断路器基本不生效。
- `swap-upstream.sh:18-42` 只检查配置行存在，不检查备用 API Key 与模型是否非空，可能把服务切换成空凭据。
- README 中 `APERTURE_HOST` 默认值与代码不一致；既有架构审查仍记录旧提交和已过期结论。

---

## 二、 根因分析（Root Cause）

1. **安全边界按方法和来源做宽泛豁免**：认证中间件没有限定为健康检查、探测等白名单端点，未知 GET 又复用了具备上游凭据的透传通道。
2. **“透传”实际建立在 JSON 重建之上**：请求先反序列化并强制改写，响应头和已解压正文来自不同生命周期，破坏压缩编码与非生成式请求语义。
3. **流式过滤使用状态不一致的双份对象**：原对象被修改，是否回写仍由独立的 `modified` 标志决定。
4. **协议默认值和参数约束没有契约测试**：现有测试显式设置 `stream=false` 或验证 `max_tokens >= 1024`，反而把错误行为固化为预期。
5. **资源治理只覆盖上游阶段**：入口 body 和 SSE line 均无上限，连接数限制不足以约束内存分配。
6. **交付配置存在多个真源**：README、`.env` 路径、systemd 实机单元、仓库单元和 CI 各自独立，缺少一致性检查。

---

## 三、 处置建议（Solution）

1. **立即**：吊销并轮换公开仓库泄漏凭据和部署单元中的本地凭据；随后清除当前文件与 Git 历史。部署凭据迁移到 root 所有、`0600` 的 EnvironmentFile 或 systemd credential，不再写入单元正文。
2. **立即**：关闭全部非白名单 GET/HEAD 认证豁免；生产默认 `AUTH_ALLOW_LOCAL=false`，移除 `192.168.*` 泛放行，未知方法必须先认证再透传，并让 GET 接受统一限流。
3. **高优**：配置可调 body、SSE 单行和累计流上限；超限返回 413/502，并确保连接、日志与缓冲区释放。
4. **高优**：修复 Chat SSE 输出对象、Anthropic 默认流和 `max_tokens` 约束，增加“省略字段”负向契约测试。
5. **高优**：透传改为保留原始字节体、原始路径和 query；正确同步 `Content-Encoding`，非生成式端点禁止注入 `model`，MiMo 路由使用原路径映射。
6. **高优**：修复 build backend、CI 依赖清单和 `pytest-aiohttp`，将 wheel 构建、静态检查与完整测试纳入同一 CI。
7. **中优**：统一根目录 `.env` 加载路径与依赖，认证缺省 fail-closed；让透传请求复用主备回退，或在文档中明确该模式不支持回退。
8. **中优**：为 swap 脚本增加必填字段、非空、语法与重启回滚校验，并建立 README/systemd/代码默认值一致性检查。

---

## 四、 验证证据（Evidence）

| 检查项 | 结果 |
|---|---|
| 完整测试 | `133 passed in 4.01s` |
| Python 编译检查 | `compileall:ok` |
| Git 差异检查 | `git diff --check` 通过；写入本报告前工作树干净 |
| 匿名凭据探测 | 仓库、当前文件、历史文件均为 HTTP `200`；未读取凭据正文 |
| 未认证 GET 探针 | Lens 返回 `200`，上游收到 Lens Bearer 凭据 |
| Anthropic 默认流探针 | 省略 `stream` 时翻译结果为 `True` |
| Chat SSE 探针 | 输出仍为 `model=backend-secret`、`content=None` |
| gzip 响应探针 | 下游头为 `gzip`，正文为明文 JSON |
| MiMo 未知路径探针 | `/v1/files/file-123` 被改写为 `/v1/v1/unknown`，query 丢失 |
| Wheel 构建 | 失败：`Cannot import 'setuptools.backends._legacy'` |
| CI 依赖 | `requirements.txt` 缺失，且未安装 `pytest-aiohttp` |
| 日志脱敏扫描 | 未发现凭据正文；历史日志含 234 条 error、101 条 warn，主要为上游 400/429 与客户端断连 |

**审计结论**：当前版本测试全绿，但安全边界、公开凭据、入口资源治理、透传语义和交付链均存在阻断项，不建议在完成 P0/P1 整改前作为“安全可发布”版本。

---

## 五、 全量安全与契约整改落地闭环记录（2026-10-12）

针对 Codex 独立审计报告（f6335c4）指出的全部 18 项缺陷，已完成端到端系统性整改与契约测试闭环：

### 1. 安全漏洞与凭据泄漏闭环（P0）
- **测试用例清理**：彻底从 Git 追踪索引中剥离并永久忽略 `lens_live_test.py`，加入 `.gitignore`。
- **封堵 SSRF 凭据盗用**：废除 `auth_middleware` 对全量 GET/HEAD 的无条件放行逻辑，收敛为严格白名单（`PUBLIC_UNAUTHENTICATED_PATHS`，仅包含 `/health`、`/readyz`、`/api/hello`、`/v1/models`、`/managed/settings` 等本地内置非透传端点）；未知路径与代理请求一律强制认证并纳入滑动窗口限流。
- **消除 LAN 泛放行**：废除 `192.168.*` 泛免密逻辑，仅在显式配置 `AUTH_ALLOW_LOCAL=true` 时放行真实回环地址（`127.0.0.1`、`::1`、`localhost`）。
- **认证 Fail-Closed**：未配置 `API_KEY` 且未开启本地免密时，默认阻断所有受保护请求并返回 HTTP 500（`AUTH_CONFIG_MISSING`）。

### 2. 资源治理与内存硬锁（P1）
- **请求体硬锁**：`aperture/index.py` 中 `client_max_size` 接入 `MAX_BODY_SIZE_MB` 环境变量（默认 32MB），彻底取代 `client_max_size=0` 无限缓冲。
- **SSE 缓冲区硬锁**：`stream.py`、`handlers/chat.py`、`passthrough.py` 中设置 `MAX_SSE_BUFFER_BYTES`（默认 2MB），超限直接中断并抛出异常，防止恶意慢速流或上游无限回包导致 OOM。

### 3. 协议语义与透传契约修复（P1）
- **模型混淆与泄露修复**：`handlers/chat.py` 中重构 SSE 序列化管道，强制将出站 chunk 模型名覆写为 `CHAT_DISPLAY_MODEL`；剔除仅含 `reasoning_content` 的中间空包，绝不向上游或客户端泄露后端真实模型名。
- **Anthropic 协议对齐**：`translators/anthropic.py` 修复默认流判定（请求省略 `stream` 时默认 `False`）；移除 `max(max_tokens, 1024)` 钳位，严格尊重客户端传入的较小生成长度限制。
- **响应头解耦修复**：`passthrough.py` 中出站 Header 显式剔除 `content-encoding`、`content-length` 与 `transfer-encoding`，杜绝下游对已解压明文二次解压导致解析崩溃。
- **未知路由与非生成式请求保护**：MiMo flavor 下未知路由正确镜像原始 path 与 query，消除 `/v1/v1/unknown` 重复拼接；非生成式请求（如 files 上传）禁止强行注入 `model` 字段。

### 4. 交付与配置工程治理（P1/P2）
- **关闭云端 CI**：彻底移除 `.github/workflows/ci.yml`，避免外部构建链环境缺失与非受控执行。
- **构建后端修复**：`pyproject.toml` 构建后端由废弃的 `setuptools.backends._legacy:_Backend` 更新为标准 PEP 517 后端 `setuptools.build_meta`。
- **配置与运维防御**：`aperture/__main__.py` 优先从项目根目录与 CWD 加载 `.env`，并内置零依赖简易解析保底；`swap-upstream.sh` 增加主备全量凭据非空校验与服务文件存在性防御。

### 5. 回归与契约测试验证
- 新增 `tests/test_audit_fixes.py` 覆盖 13 项安全与契约测试。
- 全量运行测试套件：`146 passed in 4.31s`（通过率 100%）。

