# Lens

> *本项目从 [Aperture](https://github.com/YKaiXu/aperture)（TypeScript / Cloudflare Workers）移植到 Python，为 OpenAI Codex CLI、Claude Code CLI 与现代 Agentic 开发者工具提供统一的 AI 协议镜像与透传代理。*

**Lens** 是一个具备**能力感知、全路径镜像、双向头部透传与动态模型路由**的现代 AI 代理网关。它支持双模架构：上游具备原生多协议支持（如 Xiaomi MiMo）时实施 0 损耗直通，单协议上游（如 DeepSeek、OpenCode、vLLM）时实施降级转译。

```
  客户端 (Codex CLI / Claude Code / 任意应用)
       │
       │  POST /v1/responses (Codex)
       │  POST /v1/messages (Claude Code)
       │  POST /v1/chat/completions (OpenAI)
       │  GET/POST/DELETE /* (衍生端点)
       ▼
  ┌────────────────────────────────────────────────────────────┐
  │                           Lens                             │
  │                                                            │
  │   ┌────────────────────────────────────────────────────┐   │
  │   │  能力感知路由层 (Capabilities State Machine)       │   │
  │   └─────────────┬────────────────────────┬─────────────┘   │
  │                 │                        │                 │
  │   [原生多协议上游]                       [单模型 Chat 上游] │
  │   Zero-Touch Passthrough                 降级转译引擎       │
  │   • 全 HTTP 方法与 Query 严格镜像        • Responses ↔ Chat│
  │   • 双向 Headers 镜像 (追踪/限流)        • Anthropic ↔ Chat│
  │   • 字节级流式双向模型名伪装             • Thinking 流式转译│
  │   • 0 序列化开销，0 模型能力损耗         • DSML 强类型纠错  │
  └─────────────────┬────────────────────────┬─────────────────┘
                    │                        │
                    ▼                        ▼
           原生多协议提供商            标准 Chat 提供商
        (Xiaomi MiMo / OpenAI)      (DeepSeek / 阿里 / 本地vLLM)
```

---

## 核心特性

- **双模弹性架构** — 原生多协议上游 Zero-Touch 0 损耗直通；单协议上游全自动降级转译，无缝承接。
- **全方法与路径镜像** — 通配支持 GET/POST/DELETE/PUT/HEAD，完整保留 URL 相对路径与 Query 字符串（如分页游标 `cursor`、`limit`）。
- **双向 Headers 镜像** — 剥离 RFC 2616 Hop-by-hop 传输头，上行完整保留 `traceparent` 等分布式追踪头，下行镜像上游 `x-ratelimit-*` 与 `retry-after` 指标。
- **动态模型路由** — 支持 `force`（强制单模型）、`mapped`（按 JSON 别名映射）、`passthrough`（完全直通）三种模式；动态暴露 `/v1/models`。
- **主流 CLI 开箱即用**：
  - **OpenAI Codex CLI**：支持最新 GPT-6（`gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-sol`）及 GPT-5.6，透传 `responses-lite` 头，消除 `Model metadata not found` 报警。
  - **Anthropic Claude Code CLI**：内置拦截响应 `HEAD /api/hello` 与 `GET /managed/settings`，支持 DeepSeek-R1 思考流转官方 `thinking` block。
- **DSML 强类型解析** — 自动将 DeepSeek 等模型输出的 `<dsml>` XML 标签反序列化为强类型 JSON tool_calls（支持 boolean、number、null）。
- **极致轻量** — 仅依赖 `aiohttp`，生产常驻内存仅 **26~29MB**，CPU 消耗趋近于 0。

---

## 快速开始

```bash
pip install aiohttp

# 克隆并进入目录
git clone https://github.com/bpfio/lens.git
cd lens

# 启动服务（以接入 Xiaomi MiMo 为例）
API_KEY=your-mimo-key \
BACKEND_MODEL=mimo-v2.6-flash \
UPSTREAM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
PASSTHROUGH_MODE=true \
python3 -m aperture
```

---

## 核心配置项

可通过环境变量或创建私有 `.env` 文件进行配置：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `API_KEY` | — | 必填。上游访问凭证，同时用于客户端请求认证 |
| `UPSTREAM_BASE_URL` | `https://opencode.ai/zen/go/v1` | 上游 API 基础地址 |
| `BACKEND_MODEL` | `deepseek-v4-flash` | 上游真实调用的模型标识 |
| `UPSTREAM_CAPABILITIES` | `auto` | 上游原生支持的协议，逗号分隔（如 `responses,anthropic,chat`）。`auto` 会根据 URL 自动推断 |
| `UPSTREAM_FLAVOR` | `auto` | 上游协议风格（`standard` 或 `mimo`）。`auto` 下包含 `xiaomimimo` 自动识别为 mimo |
| `MODEL_OVERRIDE_MODE` | `force` | 模型改写模式：`force`（强制覆盖）、`mapped`（规则映射）、`passthrough`（完全直通） |
| `MODEL_MAP` | `""` | JSON 格式的模型别名映射字典（`mapped` 模式下生效） |
| `AUTH_ALLOW_LOCAL` | `false` | 是否放行本地回环（127.0.0.1）免密访问 |
| `APERTURE_HOST` | `127.0.0.1` | 监听地址 |
| `APERTURE_PORT` | `8080` | 监听端口 |
| `REQUEST_TIMEOUT_MS` | `120000` | 超时时间（毫秒） |
| `LOG_DIR` | `/var/log/lens` | 结构化日志写入目录 |

---

## 多模型接入场景示例

### 1. 接入 Xiaomi MiMo（原生多协议直通）
```bash
UPSTREAM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
API_KEY=tp-your-mimo-key
BACKEND_MODEL=mimo-v2.6-flash
PASSTHROUGH_MODE=true
```

### 2. 接入 DeepSeek 官方 API（自动转译与 Thinking 流式）
```bash
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
API_KEY=sk-your-deepseek-key
BACKEND_MODEL=deepseek-chat            # 或 deepseek-reasoner (R1 思考模型)
PASSTHROUGH_MODE=false
```

### 3. 接入本地私有 vLLM / Ollama（完全离线）
```bash
UPSTREAM_BASE_URL=http://127.0.0.1:8000/v1
API_KEY=none
BACKEND_MODEL=qwen2.5-coder-7b
AUTH_ALLOW_LOCAL=true
```

### 4. 多模型精细路由（动态映射）
```bash
MODEL_OVERRIDE_MODE=mapped
MODEL_MAP='{"gpt-5.6-sol": "deepseek-chat", "gpt-6-astra": "deepseek-reasoner", "claude-fable-5": "deepseek-reasoner"}'
```

---

## 客户端配置指南

### OpenAI Codex CLI

编辑 `~/.codex/config.toml`：
```toml
# 支持指定 gpt-6-astra, gpt-6.1-sol, gpt-6-sol 或 gpt-5.6-sol
model = "gpt-6-astra"
model_provider = "lens"

[model_providers.lens]
name = "Lens"
base_url = "http://127.0.0.1:8080/v1"
wire_api = "responses"
env_key = "LENS_API_KEY"
```

### Anthropic Claude Code CLI

编辑 `~/.claude/settings.json`：
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
    "ANTHROPIC_MODEL": "claude-fable-5[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-fable-5[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-5[1m]"
  }
}
```

---

## 自动化测试与验证

```bash
pip install pytest pytest-asyncio pytest-aiohttp
PYTHONDONTWRITEBYTECODE=1 pytest tests/ -v
```

当前内置 133 项自动化测试（覆盖全方法镜像、双向 Headers 透传、Responses 状态机、Anthropic thinking 流式、DSML 强类型解析等核心契约）。

---

## 守护进程部署 (systemd)

```bash
# 复制配置文件模板
cp .env.example .env
chmod 600 .env

# 配置 systemd 服务
sudo cp lens.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lens

# 查看实时日志
tail -f /home/yupeng/lens/logs/lens.log
```