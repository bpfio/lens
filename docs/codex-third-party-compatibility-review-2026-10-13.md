# Codex CLI 第三方模型兼容性审查报告

- 审查日期：2026-10-13
- 审查对象：`bpfio/lens`
- 代码基线：`e5206ba`
- 审查范围：Codex CLI 第三方 Provider 接入、Responses 协议翻译、模型能力、流式事件、传输策略与测试契约
- 审查方式：official OpenAI documentation 核对、静态代码审查、最小协议探针、完整回归测试
- 结论等级：基础接入可用，但距离“多 Provider 无损融合”仍有结构性缺口

## 一、 执行摘要

Lens 已完成上一轮安全与资源治理整改，当前完整测试为 `146 passed`。透传模式可以承载基础 Responses 请求，翻译模式也覆盖文本、多模态和普通函数工具。

但 Lens 目前仍不是 Codex CLI 意义上的完整第三方 Provider 网关：它缺少逐 Provider 的鉴权、headers、query、重试与超时配置；Responses 的推理、缓存、状态化和结构化输出参数被静默丢弃；流式响应只重建文本与工具事件；模型目录只是静态别名列表；能力协商只区分协议，不区分模型功能。

因此，当前适合“已知单上游、基础文本与工具调用”的场景，不适合直接承担多厂商、推理模型、托管工具、状态化 Responses 和高可靠流式任务。

## 二、 official OpenAI documentation 基线

本次审查以以下 official OpenAI documentation 为 Codex 侧能力口径：

1. [Codex Advanced Config](https://developers.openai.com/codex/config-file/config-advanced)
   - 自定义 `model_providers.<id>`；
   - `base_url`、`env_key`、命令式 Token 刷新；
   - 静态及环境变量 HTTP headers；
   - Ollama、LM Studio、Mistral、Azure、Amazon Bedrock 等 Provider 示例；
   - Provider 级 query、重试和流配置。

2. [Codex Configuration Reference](https://developers.openai.com/codex/config-file/config-reference)
   - 当前自定义 Provider 的 `wire_api` 仅支持 `responses`；
   - `query_params`、`request_max_retries`、`stream_max_retries`、`stream_idle_timeout_ms`；
   - `supports_standalone_web_search`、`supports_websockets` 等能力声明。

这意味着 Lens 应重点优化“Responses 原生透传能力”和“Chat-only 上游的 Responses 语义适配”，而不是继续扩展已经废弃的 Codex Chat wire API。

## 三、 关键发现

### P1-1：Responses 关键参数被静默丢弃

代码位置：`aperture/translators/responses.py:225-260`

最小探针中以下请求字段没有进入 Chat Completions 请求：

```text
background
include
parallel_tool_calls
prompt_cache_key
reasoning
safety_identifier
service_tier
store
text
```

影响：

- `reasoning` 丢失后，推理强度和摘要策略无法传递到第三方模型；
- `text.format` 丢失后，结构化输出可能降级为普通文本；
- `prompt_cache_key`、`safety_identifier` 丢失后，缓存和归属策略失效；
- `store/background/previous_response_id` 是有状态语义，不能用无状态 Chat 请求等价替代。

整改要求：每个参数必须进入明确的能力策略：原生透传、字段翻译、受控降级或返回 `unsupported_parameter`，禁止静默丢弃。

### P1-2：工具选择与 strict 工具映射不完整

代码位置：`aperture/translators/responses.py:15-38`、`aperture/translators/responses.py:235-255`

当前探针结果：

```text
tool_choice = {'type': 'function', 'name': 'run'}
strict_preserved = False
```

问题：

- Responses 的 `tool_choice` 被直接放入 Chat 请求，没有转换为 Chat 的函数选择结构；
- `_normalize_tool()` 丢弃 `strict`；
- `web_search/file_search/code_interpreter` 被原样放入 Chat `tools`，只有上游真正支持托管工具时才成立。

整改要求：建立 Responses 到 Chat 的显式工具 schema，并按 Provider 能力决定托管工具是透传、本地适配还是拒绝。

### P1-3：推理与高级流事件缺失

代码位置：`aperture/translators/responses.py:265-494`、`aperture/translators/responses.py:497-547`

包含 `reasoning_content` 的流式探针只产生：

```text
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
response.done
```

没有任何 reasoning 事件。非流式响应也只构造文本和函数调用。

整改要求：

- 增加 reasoning delta、summary、refusal、annotations；
- 补齐 usage、incomplete、failed、cancelled 状态；
- 对未知事件采用可配置的透传或记录策略；
- 输出完整 usage details，而不仅是三个总量字段。

### P1-4：缺少逐 Provider 注册表和传输策略

代码位置：`aperture/config.py:48-82`、`aperture/upstream.py:17-118`、`aperture/index.py:366-381`

Lens 当前只有单一主备 URL、全局 API Key、字符串 `MODEL_MAP` 和全局超时。与 Codex Provider 配置相比缺少：

```text
provider_id
base_url
model_routes
auth.mode / token_command / refresh_interval
http_headers / env_http_headers
query_params
request_max_retries
stream_max_retries
stream_idle_timeout_ms
wire_api
capabilities
```

整改要求：实现 `provider -> model -> capability/transport` 三层注册表，使不同模型可以拥有独立上游、凭据、headers、query、超时和重试策略。

### P1-5：能力协商粒度不足

代码位置：`aperture/config.py:61-68`、`aperture/passthrough.py:150-156`

`UPSTREAM_CAPABILITIES` 目前只区分 `responses/chat/anthropic`，`FILTER_WEB_SEARCH` 是全局布尔值。

建议能力模型至少包含：

```text
reasoning_summary
hosted_tools
web_search
structured_output
prompt_cache
background
strict_tools
stream_retries
websocket
```

每项能力应有 `passthrough / translate / degrade / reject` 四种策略，并产生可观察的降级日志和指标。

### P2-1：翻译模式丢失原始 headers、query 与错误语义

代码位置：`aperture/upstream.py:25-35`、`aperture/handlers/responses.py:30-60`

翻译模式只构造 Authorization 和 session header，不接收 Codex/OpenAI 的组织、项目、Beta、追踪等请求头，也不追加 Provider query。上游错误会被截断并重新包装，可能丢失原始错误 code/type/param、`Retry-After` 和限流信息。

整改要求：将请求上下文提取、静态 Provider header 注入、query 合并和错误映射统一到 Provider 层，透传与翻译共用。

### P2-2：模型目录是静态展示，不是能力目录

代码位置：`aperture/index.py:68-105`

当前 `/v1/models` 只返回后端模型、三个显示模型和字符串别名，没有上下文窗口、推理能力、工具能力、结构化输出或 tokenizer 信息。

整改要求：引入结构化模型注册表，由同一数据源驱动模型发现、模型改写、能力协商、上下文窗口和降级策略，避免通过伪造已知模型名来规避元数据警告。

### P2-3：SSE 空闲超时和中断恢复不足

代码位置：`aperture/passthrough.py:197-212`、`aperture/upstream.py:95-125`

Lens 只有全局请求/SSE 空闲超时，没有 Provider 级 `stream_max_retries`，也没有对“已向客户端输出后中断”的重放保护。

整改要求：实现有界指数退避；首包前可安全重试，首包后必须采用断点续传或终止策略，避免重复 token、重复工具调用和乱序事件。

### P2-4：状态化 Responses 端点与翻译模式边界不清

代码位置：`aperture/index.py:285-338`

翻译模式只处理新的 `/v1/responses` POST。读取、取消、后台任务、历史回复等状态化能力没有统一语义。

整改要求：

- Responses 原生上游：完整透传状态化端点；
- Chat-only 上游：明确拒绝 `store/background/previous_response_id` 等能力；
- 若未来支持状态化翻译，需要独立的响应存储、轮询、取消和过期机制。

### P3：缺少 Codex 版本化黄金契约

现有 `146 passed` 覆盖基础转换，但没有覆盖完整 Codex Provider 行为。

建议新增 Codex `0.162.1` 黄金语料：

1. 推理 effort、summary 与 reasoning SSE；
2. strict tools、tool_choice 和托管工具；
3. prompt cache、structured output、metadata；
4. 自定义 headers、query 和命令式鉴权；
5. 429、Retry-After、流中断、取消和未知事件；
6. Responses-native、Chat-only、Ollama/LM Studio 三类上游的端到端验收。

## 四、 推荐实施路线

### 阶段一：Provider 与能力模型

1. 定义 Provider、模型路由、传输策略和能力矩阵；
2. 将凭据、headers、query、超时和重试从全局配置下沉到 Provider；
3. 为不支持的 Responses 参数建立显式拒绝或降级规则。

### 阶段二：Responses 语义适配

1. 修复 tool_choice、strict 和托管工具映射；
2. 实现 reasoning、结构化输出、缓存与 metadata 映射；
3. 补齐流式和非流式事件、usage 与错误语义。

### 阶段三：模型目录与可靠性

1. 建立结构化模型注册表；
2. 实现 Provider 级流重试与安全恢复策略；
3. 建立兼容性降级日志、指标和告警。

### 阶段四：契约验证

1. 建立 Codex 版本化黄金请求与 SSE 快照；
2. 对 Responses-native、Chat-only 和本地模型执行端到端矩阵；
3. 每次 Codex CLI 升级前运行兼容性回归。

## 五、 验证证据

| 检查项 | 结果 |
|---|---|
| 完整回归测试 | `146 passed in 4.44s` |
| 工作树基线 | `e5206ba`，审查前干净 |
| Responses 字段探针 | 9 个关键字段被丢弃 |
| tool_choice 探针 | 保持 Responses 格式，未转为 Chat 格式 |
| strict 工具探针 | `strict=False` |
| reasoning 流探针 | 0 个 reasoning 事件 |
| Codex wire API | official OpenAI documentation：仅 `responses` |
| 真实计费请求 | 未执行 |

## 六、 结论

Lens 已经具备可用的单模型翻译和基础透传能力，但当前仍以“路径代理 + 有限字段翻译”为主，尚未形成 Codex CLI 所需的 Provider 注册表、模型能力目录和 Responses 全语义适配层。

下一轮工作的核心不是继续增加零散字段补丁，而是建立统一的 `Provider -> Model -> Capability -> Translation Policy` 架构。完成该架构后，Lens 才能从“能接第三方模型”提升为“可稳定融合第三方模型”。
