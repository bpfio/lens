"""Tests for Anthropic Messages API -> Chat Completions translation."""

import json
import pytest
from unittest.mock import AsyncMock
from aiohttp import ClientResponse
from aperture.translators.anthropic import (
    translate_anthropic_to_chat,
    translate_anthropic_json,
)


class TestTranslateAnthropicToChat:
    def test_basic_text_message(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hello"}],
            "system": "Be concise.",
        }
        result = translate_anthropic_to_chat(body, {"DEFAULT_MODEL": "deepseek-v4-flash"})
        assert result["model"] == "deepseek-v4-flash"
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "Be concise."
        assert result["messages"][1]["role"] == "user"
        assert result["messages"][1]["content"] == "Hello"

    def test_image_block_converted(self):
        body = {
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "iVBORw0KGgo=",
                }},
            ]}],
        }
        result = translate_anthropic_to_chat(body)
        msg = result["messages"][0]
        assert isinstance(msg["content"], list)
        assert msg["content"][0]["type"] == "image_url"

    def test_tool_result_to_tool_role(self):
        body = {
            "messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_abc", "content": "Result: 42"},
                {"type": "text", "text": "Now what?"},
            ]}],
        }
        result = translate_anthropic_to_chat(body)
        roles = [m["role"] for m in result["messages"]]
        assert "tool" in roles
        assert "user" in roles

    def test_assistant_tool_use(self):
        body = {
            "messages": [{"role": "assistant", "content": [
                {"type": "text", "text": "Let me search"},
                {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "hello"}},
            ]}],
        }
        result = translate_anthropic_to_chat(body)
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "search"

    def test_tools_mapped(self):
        body = {
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [{"name": "search", "description": "Search", "parameters": {}}],
        }
        result = translate_anthropic_to_chat(body)
        assert len(result["tools"]) == 1
        assert result["tools"][0]["function"]["name"] == "search"

    def test_temp_and_top_p_passthrough(self):
        body = {
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
        }
        result = translate_anthropic_to_chat(body)
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9

    def test_stop_sequences(self):
        body = {
            "messages": [{"role": "user", "content": "Hi"}],
            "stop_sequences": ["\n\n", "stop"],
        }
        result = translate_anthropic_to_chat(body)
        assert result["stop"] == ["\n\n", "stop"]

    def test_max_tokens_preserved(self):
        body = {
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
        }
        result = translate_anthropic_to_chat(body)
        assert result["max_tokens"] == 50

    def test_anthropic_model_mapped(self):
        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = translate_anthropic_to_chat(body, {"DEFAULT_MODEL": "my-model"})
        # model is hardcoded to BACKEND_MODEL regardless of env
        from aperture.config import BACKEND_MODEL
        assert result["model"] == BACKEND_MODEL

    def test_thinking_config(self):
        body = {
            "messages": [{"role": "user", "content": "Hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }
        result = translate_anthropic_to_chat(body)
        assert result["thinking"]["type"] == "enabled"
        assert result["thinking"]["budget_tokens"] == 4096


@pytest.mark.asyncio
async def test_translate_anthropic_json():
    """Non-streaming Anthropic response translation."""
    mock_resp = AsyncMock(spec=ClientResponse)
    mock_resp.json = AsyncMock(return_value={
        "choices": [{
            "message": {"content": "Hello too!", "tool_calls": []},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    })
    result = await translate_anthropic_json(mock_resp, "msg_1", "model-x")
    assert result["type"] == "message"
    assert result["content"][0]["text"] == "Hello too!"
    assert result["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_translate_anthropic_json_with_tool_calls():
    """Anthropic response with tool_use blocks."""
    mock_resp = AsyncMock(spec=ClientResponse)
    mock_resp.json = AsyncMock(return_value={
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"q": "hello"}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    })
    result = await translate_anthropic_json(mock_resp, "msg_1", "model-x")
    assert result["stop_reason"] == "tool_use"
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "tool_use"
    assert result["content"][0]["input"] == {"q": "hello"}
