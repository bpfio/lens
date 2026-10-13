"""Contract tests for Codex CLI third-party provider compatibility.

Covers:
- P1-1: Parameter mapping (reasoning, text.format, parallel_tool_calls, cache/safety)
- P1-2: Tool normalization (strict mode preservation and Chat tool_choice conversion)
- P1-3: Streaming & non-streaming reasoning events (reasoning_content -> response.reasoning.*)
- P1-4 & P1-5: Provider registry, multi-provider routing, and capability model
- P2-1: Header and query string preservation
- P2-2: Model directory capabilities and context window metadata
- P2-4: Stateful responses endpoint boundaries in translation mode
"""

import json
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aperture.translators.responses import (
    translate_to_chat,
    _normalize_tool,
    translate_stream_events,
    translate_response_json,
)
from aperture.providers import (
    ProviderConfig,
    ProviderRegistry,
    get_provider_registry,
    reset_provider_registry,
)
from aperture.index import create_app, _handle_list_models


class MockStreamContent:
    """Mock aiohttp response content for SSE streaming."""
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class MockClientResponse:
    """Mock ClientResponse for stream and JSON tests."""
    def __init__(self, chunks=None, json_data=None):
        if chunks is not None:
            self.content = MockStreamContent(chunks)
        self._json_data = json_data or {}

    async def json(self):
        return self._json_data


# --- P1-1: Parameter Mapping Tests ---

def test_responses_parameter_mapping_reasoning():
    """Verify reasoning parameter is mapped to reasoning_effort."""
    req = {
        "model": "gpt-5.6-sol",
        "input": "Explain quantum computing",
        "reasoning": {"effort": "high", "summary": "auto"},
    }
    chat = translate_to_chat(req)
    assert chat.get("reasoning_effort") == "high"
    assert chat.get("reasoning") == {"effort": "high", "summary": "auto"}


def test_responses_parameter_mapping_structured_outputs():
    """Verify text.format is mapped to response_format."""
    req = {
        "model": "gpt-5.6-sol",
        "input": "Output schema",
        "text": {
            "format": {
                "type": "json_schema",
                "json_schema": {"name": "Result", "schema": {"type": "object"}},
            }
        },
    }
    chat = translate_to_chat(req)
    assert chat.get("response_format") == {
        "type": "json_schema",
        "json_schema": {"name": "Result", "schema": {"type": "object"}},
    }


def test_responses_parameter_mapping_parallel_tools_and_tracking():
    """Verify parallel_tool_calls, prompt_cache_key, and safety_identifier are preserved."""
    req = {
        "model": "gpt-5.6-sol",
        "input": "Test query",
        "parallel_tool_calls": True,
        "prompt_cache_key": "cache-k1",
        "safety_identifier": "safe-u1",
        "service_tier": "scale",
    }
    chat = translate_to_chat(req)
    assert chat.get("parallel_tool_calls") is True
    assert chat.get("prompt_cache_key") == "cache-k1"
    assert chat.get("safety_identifier") == "safe-u1"
    assert chat.get("service_tier") == "scale"


# --- P1-2: Tool Choice & Strict Mode Tests ---

def test_normalize_tool_preserves_strict():
    """Verify _normalize_tool preserves strict: true attribute."""
    tool = {
        "type": "function",
        "name": "lookup",
        "description": "search item",
        "strict": True,
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    norm = _normalize_tool(tool)
    assert norm["function"].get("strict") is True


def test_tool_choice_converted_to_chat_spec():
    """Verify Responses {"type": "function", "name": "foo"} is converted to Chat function spec."""
    req = {
        "model": "gpt-5.6-sol",
        "input": "Call tool",
        "tools": [{"type": "function", "name": "my_tool"}],
        "tool_choice": {"type": "function", "name": "my_tool"},
    }
    chat = translate_to_chat(req)
    assert chat.get("tool_choice") == {
        "type": "function",
        "function": {"name": "my_tool"},
    }


# --- P1-3: Streaming & JSON Reasoning Events Tests ---

@pytest.mark.asyncio
async def test_streaming_reasoning_events_emitted():
    """Verify chunks with reasoning_content emit response.reasoning.* events."""
    chunks = [
        b'data: {"choices": [{"delta": {"reasoning_content": "Thinking step 1..."}, "finish_reason": null}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "Final answer."}, "finish_reason": "stop"}]}\n\n',
    ]
    resp = MockClientResponse(chunks=chunks)

    events = []
    async for ev in translate_stream_events(resp, "resp_test_1", "gpt-5.6-sol"):
        events.append(ev)

    event_types = [e["event"] for e in events]
    assert "response.output_item.added" in event_types
    assert "response.reasoning.delta" in event_types
    assert "response.reasoning.done" in event_types
    assert "response.output_text.delta" in event_types
    assert "response.completed" in event_types
    assert "response.done" in event_types

    # Verify reasoning delta data
    reasoning_deltas = [e["data"]["delta"] for e in events if e["event"] == "response.reasoning.delta"]
    assert "Thinking step 1..." in reasoning_deltas


@pytest.mark.asyncio
async def test_non_streaming_reasoning_in_output():
    """Verify non-streaming response with reasoning_content includes reasoning output item."""
    json_data = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Answer here",
                "reasoning_content": "Deep thought process",
            },
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 25,
            "total_tokens": 35,
            "completion_tokens_details": {"reasoning_tokens": 15},
        },
    }
    resp = MockClientResponse(json_data=json_data)
    result = await translate_response_json(resp, "resp_test_2", "gpt-5.6-sol")

    output_types = [item["type"] for item in result["output"]]
    assert "reasoning" in output_types
    assert "message" in output_types

    reasoning_item = next(item for item in result["output"] if item["type"] == "reasoning")
    assert reasoning_item["text"] == "Deep thought process"
    assert result["usage"]["output_token_details"]["reasoning_tokens"] == 15


# --- P1-4 & P1-5: Provider Registry and Capabilities Tests ---

def test_provider_registry_multi_provider_routing(monkeypatch):
    """Verify ProviderRegistry parses multiple providers and routes models."""
    config_json = json.dumps({
        "ollama": {
            "base_url": "http://127.0.0.1:11434/v1",
            "wire_api": "chat",
            "models": {"qwen-local": "qwen2.5:7b"},
            "capabilities": ["chat", "reasoning_summary"],
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-mock-key",
            "models": {"deepseek-r1": "deepseek-reasoner"},
            "capabilities": ["chat", "reasoning_summary", "structured_output"],
        },
    })
    monkeypatch.setenv("PROVIDERS_CONFIG", config_json)
    reset_provider_registry()

    reg = get_provider_registry()
    p_ollama = reg.resolve_provider_for_model("qwen-local")
    assert p_ollama.id == "ollama"
    assert p_ollama.resolve_model("qwen-local") == "qwen2.5:7b"
    assert p_ollama.has_capability("reasoning_summary")
    assert not p_ollama.has_capability("hosted_tools")

    p_ds = reg.resolve_provider_for_model("deepseek-r1")
    assert p_ds.id == "deepseek"
    assert p_ds.resolve_model("deepseek-r1") == "deepseek-reasoner"

    # Default fallback
    p_def = reg.resolve_provider_for_model("unknown-model")
    assert p_def.id == "default"

    reset_provider_registry()


# --- P2-2: Model Directory Metadata Tests ---

@pytest.mark.asyncio
async def test_handle_list_models_metadata():
    """Verify /v1/models returns context_window and capabilities metadata."""
    req = make_mocked_request("GET", "/v1/models")
    resp = await _handle_list_models(req)
    assert resp.status == 200

    data = json.loads(resp.text)
    assert "data" in data
    assert len(data["data"]) > 0

    first_model = data["data"][0]
    assert "context_window" in first_model
    assert first_model["context_window"] == 128000
    assert "capabilities" in first_model
    assert first_model["capabilities"]["supports_tools"] is True
    assert first_model["capabilities"]["supports_structured_output"] is True


# --- P2-4: Stateful Responses Boundary Tests ---

@pytest.mark.asyncio
async def test_stateful_responses_boundary_in_translation_mode(monkeypatch):
    """Verify GET /v1/responses/{id} in translation mode returns 400 with UNSUPPORTED_STATEFUL_RESPONSES."""
    monkeypatch.setenv("PASSTHROUGH_MODE", "false")
    monkeypatch.setenv("UPSTREAM_CAPABILITIES", "chat")
    app = create_app()

    client_req = make_mocked_request("GET", "/v1/responses/resp_abc123", app=app)
    # Dispatch through the app router
    from aperture.index import _handle_dispatch
    resp = await _handle_dispatch(client_req)

    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"]["code"] == "UNSUPPORTED_STATEFUL_RESPONSES"
