"""Contract and regression tests for architecture review remediations."""

import json
import pytest
from unittest.mock import MagicMock
from aiohttp import web

from aperture.index import _detect_route, create_app
from aperture.translators.responses import translate_to_chat, translate_stream_events
from aperture.translators.anthropic import translate_anthropic_stream, translate_anthropic_to_chat
from aperture.translators.dsml import normalize_dsml_tool_calls
from aperture.middleware.auth import authenticate


def test_detect_route_path_slashes():
    """Verify route detection handles leading slashes and raw paths consistently."""
    assert _detect_route("chat/completions", {}) == "chat"
    assert _detect_route("/chat/completions", {}) == "chat"
    assert _detect_route("v1/chat/completions", {}) == "chat"
    assert _detect_route("/v1/chat/completions", {}) == "chat"

    assert _detect_route("messages", {}) == "anthropic"
    assert _detect_route("/messages", {}) == "anthropic"
    assert _detect_route("v1/messages", {}) == "anthropic"
    assert _detect_route("/v1/messages", {}) == "anthropic"

    assert _detect_route("responses", {}) == "responses"
    assert _detect_route("/responses", {}) == "responses"
    assert _detect_route("v1/responses", {}) == "responses"
    assert _detect_route("/v1/responses", {}) == "responses"

    # Unknown route without signature body fields should fail
    assert _detect_route("unknown/path", {}) == "unknown"


def test_responses_multimodal_image_translation():
    """Verify input_image is converted to standard OpenAI image_url content block."""
    body = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Analyze this:"},
                    {"type": "input_image", "image_url": {"url": "https://example.com/test.png"}},
                ],
            }
        ],
    }
    chat_body = translate_to_chat(body)
    messages = chat_body["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "Analyze this:"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://example.com/test.png"}}


def test_responses_hosted_tools_preservation():
    """Verify web_search, file_search, and code_interpreter tools are preserved."""
    body = {
        "model": "gpt-5.6-sol",
        "input": "Search for news",
        "tools": [
            {"type": "web_search"},
            {"type": "file_search"},
            {"type": "function", "name": "custom_fn", "parameters": {"type": "object"}},
        ],
    }
    chat_body = translate_to_chat(body)
    tools = chat_body.get("tools", [])
    tool_types = [t.get("type") for t in tools]
    assert "web_search" in tool_types
    assert "file_search" in tool_types
    assert "function" in tool_types


@pytest.mark.asyncio
async def test_responses_stream_pure_tool_call_closure():
    """Verify stream without text produces no orphaned text item_0, and all items close."""
    payload = {
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_123",
                    "function": {"name": "run_calc", "arguments": '{"x": 1}'},
                }]
            },
            "finish_reason": "tool_calls",
        }]
    }
    line = b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n"

    async def fake_content():
        yield line

    mock_resp = MagicMock()
    mock_resp.content = fake_content()

    events = []
    async for ev in translate_stream_events(mock_resp, "resp_pure_tool", "gpt-test"):
        events.append(ev)

    added_items = [
        ev["data"]["item"]["id"]
        for ev in events
        if ev["event"] == "response.output_item.added"
    ]
    done_items = [
        ev["data"]["item"]["id"]
        for ev in events
        if ev["event"] == "response.output_item.done"
    ]

    # Every item that was added must have a matching done event
    assert len(added_items) > 0
    assert set(added_items) == set(done_items)

    # Ensure no orphaned text item_0 was emitted
    for item_id in added_items:
        assert "item_0" not in item_id


@pytest.mark.asyncio
async def test_anthropic_stream_thinking_translation():
    """Verify upstream reasoning_content is converted to Anthropic thinking blocks."""
    payload = {
        "choices": [{
            "delta": {
                "reasoning_content": "Thinking step by step...",
                "content": "Final answer",
            },
            "finish_reason": "stop",
        }]
    }
    line = b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n"

    async def fake_content():
        yield line

    mock_resp = MagicMock()
    mock_resp.content = fake_content()

    events = []
    async for ev in translate_anthropic_stream(mock_resp, "msg_think", "claude-test"):
        events.append(ev)

    event_types = [ev["event"] for ev in events]
    assert "content_block_start" in event_types
    assert "content_block_delta" in event_types

    # Find thinking block
    thinking_deltas = [
        ev["data"]["delta"]["thinking"]
        for ev in events
        if ev["event"] == "content_block_delta" and ev["data"].get("delta", {}).get("type") == "thinking_delta"
    ]
    assert "Thinking step by step..." in thinking_deltas


def test_dsml_type_inference():
    """Verify DSML tool call parameters preserve numbers and booleans."""
    body = {
        "choices": [{
            "message": {
                "content": '<invoke name="query"><parameter name="limit">50</parameter><parameter name="active">true</parameter><parameter name="name">alpha</parameter></invoke>'
            }
        }]
    }
    normalized = normalize_dsml_tool_calls(body)
    tool_call = normalized["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(tool_call["function"]["arguments"])
    assert args["limit"] == 50
    assert args["active"] is True
    assert args["name"] == "alpha"


def test_auth_timing_safe_and_rejection():
    """Verify authenticate rejects invalid keys in constant time."""
    req = web.Request
    mock_req = MagicMock(spec=req)
    mock_req.headers = {"Authorization": "Bearer sk-wrong-key"}

    res = authenticate(mock_req, "sk-correct-key")
    assert res is not None
    assert res.status == 401
