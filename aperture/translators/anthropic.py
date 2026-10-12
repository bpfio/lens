"""Anthropic Messages API -> Chat Completions translation.

Mirrors JS src/translators/anthropic.js.
"""

import json
from typing import AsyncIterator

from aiohttp import ClientResponse

from ..helpers import uid, now, extract_text
from ..stream import stream_sse
from ..config import map_model_name


def translate_anthropic_to_chat(body: dict, env: dict | None = None) -> dict:
    """Translate an Anthropic Messages API request to Chat Completions format.

    Supports:
    - messages[] with role: user/assistant
    - system prompt (top-level or first system message)
    - content blocks (text, image, tool_use, tool_result)
    - tools[] definitions -> OpenAI function calling
    - streaming via stream: true
    - thinking config
    - Anthropic image blocks -> OpenAI image URL format
    """
    env = env or {}
    messages = []
    system_content = None

    # Extract system prompt (Anthropic puts it at top level)
    sys_val = body.get("system")
    if sys_val:
        if isinstance(sys_val, str):
            system_content = sys_val
        elif isinstance(sys_val, list):
            texts = []
            for block in sys_val:
                texts.append(extract_text(block))
            system_content = "\n".join(texts)

    # Process messages
    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            system_content = (system_content or "") + "\n" + extract_text(content)
            continue

        if role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            elif isinstance(content, list):
                user_parts = []
                has_user_text = False
                tool_messages = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        user_parts.append({"type": "text", "text": block.get("text", "")})
                        has_user_text = True

                    elif btype == "image":
                        source = block.get("source", {})
                        if source.get("data"):
                            user_parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{source.get('media_type', 'image/png')};base64,{source['data']}",
                                },
                            })

                    elif btype == "tool_result":
                        tc_content = (
                            block.get("content", "")
                            if isinstance(block.get("content"), str)
                            else extract_text(block.get("content"))
                        )
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", uid("call")),
                            "content": tc_content or "",
                        })

                if tool_messages:
                    messages.extend(tool_messages)
                has_image_url = any(p.get("type") == "image_url" for p in user_parts)
                if has_user_text or has_image_url:
                    if len(user_parts) == 1:
                        content = user_parts[0]
                        if content["type"] == "text":
                            messages.append({"role": "user", "content": content["text"]})
                        else:
                            messages.append({"role": "user", "content": user_parts})
                    else:
                        messages.append({"role": "user", "content": user_parts})
                elif not tool_messages:
                    messages.append({"role": "user", "content": ""})
            else:
                messages.append({"role": "user", "content": ""})
            continue

        if role == "assistant":
            tool_calls = []
            text = ""

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text += block.get("text", "")
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", uid("call")),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })

            msg_obj = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg_obj["tool_calls"] = tool_calls
            messages.append(msg_obj)
            continue

        if role in ("tool_result", "tool"):
            tc_content = (
                content if isinstance(content, str)
                else "\n".join(extract_text(b) for b in content) if isinstance(content, list)
                else ""
            )
            messages.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_use_id", uid("call")),
                "content": tc_content or "",
            })

    # Prepend system message
    if system_content:
        messages.insert(0, {"role": "system", "content": system_content.strip()})

    # Build chat request
    chat = {
        "model": _translate_model(body.get("model", ""), env),
        "messages": messages,
        "stream": body.get("stream", False) if "stream" in body else True,
    }

    # Map parameters
    if "temperature" in body:
        chat["temperature"] = body["temperature"]
    if "top_p" in body:
        chat["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        chat["stop"] = body["stop_sequences"]

    max_tokens = body.get("max_tokens", 8192)
    chat["max_tokens"] = max(max_tokens, 1024)

    # Tools -> function calling
    if "tools" in body and body["tools"]:
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type", "")
            tool_name = t.get("name") or (t.get("function") or {}).get("name") or ttype
            if tool_name:
                fn = t.get("function", t)
                params = (
                    fn.get("parameters")
                    or t.get("parameters")
                    or fn.get("input_schema")
                    or t.get("input_schema", {})
                )
                if not isinstance(params, dict) or not params.get("type"):
                    params = {"type": "object", "properties": {}}
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": fn.get("description", t.get("description", "")),
                        "parameters": params,
                        "input_schema": params,
                    },
                })
        if tools:
            chat["tools"] = tools
            chat["tool_choice"] = _map_anthropic_tool_choice(body.get("tool_choice"))

    # Thinking -> reasoning effort
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        chat["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking.get("budget_tokens", 2048),
        }

    # Metadata (user_id not forwarded — NVIDIA rejects non-standard params)

    return chat


async def translate_anthropic_stream(
    response: ClientResponse,
    request_id: str,
    model: str,
) -> AsyncIterator[dict]:
    """Translate upstream SSE stream to Anthropic Messages API stream events.

    Yields {event, data} dicts for pipe_sse().
    Events: message_start, content_block_start, content_block_delta,
            content_block_stop, message_delta, message_stop.
    """
    yield {
        "event": "message_start",
        "data": {
            "type": "message_start",
            "message": {
                "id": request_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    }

    content_index = 0
    text_block_index = -1  # -1 = no text block currently open
    thinking_block_index = -1
    tool_use_map: dict[int, dict] = {}
    last_finish_reason = None
    stream_usage = {"input_tokens": 0, "output_tokens": 0}

    async for chunk in stream_sse(response):
        if "usage" in chunk and isinstance(chunk.get("usage"), dict):
            stream_usage["input_tokens"] = (
                chunk["usage"].get("prompt_tokens", chunk["usage"].get("input_tokens", 0))
            )
            stream_usage["output_tokens"] = (
                chunk["usage"].get("completion_tokens", chunk["usage"].get("output_tokens", 0))
            )

        for choice in (chunk.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {}) or {}
            if not isinstance(delta, dict):
                delta = {}
            reasoning = delta.get("reasoning_content", "") or ""
            content = delta.get("content", "") or ""
            tool_calls = delta.get("tool_calls")
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                last_finish_reason = finish_reason

            # Thinking content
            if reasoning:
                if thinking_block_index == -1:
                    thinking_block_index = content_index
                    yield {
                        "event": "content_block_start",
                        "data": {
                            "type": "content_block_start",
                            "index": thinking_block_index,
                            "content_block": {"type": "thinking", "thinking": ""},
                        },
                    }
                yield {
                    "event": "content_block_delta",
                    "data": {
                        "type": "content_block_delta",
                        "index": thinking_block_index,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    },
                }
            elif thinking_block_index != -1 and (content or tool_calls):
                yield {
                    "event": "content_block_stop",
                    "data": {"type": "content_block_stop", "index": thinking_block_index},
                }
                content_index += 1
                thinking_block_index = -1

            # Text content: open block once, accumulate deltas, close once
            if content:
                if text_block_index == -1:
                    text_block_index = content_index
                    yield {
                        "event": "content_block_start",
                        "data": {
                            "type": "content_block_start",
                            "index": text_block_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    }
                yield {
                    "event": "content_block_delta",
                    "data": {
                        "type": "content_block_delta",
                        "index": text_block_index,
                        "delta": {"type": "text_delta", "text": content},
                    },
                }

            # Tool calls: close text block first, open tool_use block
            if tool_calls:
                if text_block_index != -1:
                    yield {
                        "event": "content_block_stop",
                        "data": {"type": "content_block_stop", "index": text_block_index},
                    }
                    content_index += 1
                    text_block_index = -1

                for tc in tool_calls:
                    fn = tc.get("function", {})
                    idx = tc.get("index", 0)
                    if idx not in tool_use_map:
                        tc_id = tc.get("id", uid("toolu"))
                        tool_use_map[idx] = {
                            "block_index": content_index,
                            "id": tc_id,
                            "name": fn.get("name", f"tool_{tc_id[:8]}"),
                            "input": "",
                        }
                        yield {
                            "event": "content_block_start",
                            "data": {
                                "type": "content_block_start",
                                "index": tool_use_map[idx]["block_index"],
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tool_use_map[idx]["id"],
                                    "name": tool_use_map[idx]["name"],
                                    "input": {},
                                },
                            },
                        }
                        content_index += 1

                    args = fn.get("arguments", "")
                    if args:
                        tool_use_map[idx]["input"] += args
                        yield {
                            "event": "content_block_delta",
                            "data": {
                                "type": "content_block_delta",
                                "index": tool_use_map[idx]["block_index"],
                                "delta": {"type": "input_json_delta", "partial_json": args},
                            },
                        }

            # Close tool_use blocks on finish
            if finish_reason:
                for key in list(tool_use_map.keys()):
                    tc_block = tool_use_map[key]
                    yield {
                        "event": "content_block_stop",
                        "data": {"type": "content_block_stop", "index": tc_block["block_index"]},
                    }
                    del tool_use_map[key]

    # Close any open thinking block
    if thinking_block_index != -1:
        yield {
            "event": "content_block_stop",
            "data": {"type": "content_block_stop", "index": thinking_block_index},
        }

    # Close any open text block
    if text_block_index != -1:
        yield {
            "event": "content_block_stop",
            "data": {"type": "content_block_stop", "index": text_block_index},
        }

    stop_reason = _map_finish_reason(last_finish_reason)

    yield {
        "event": "message_delta",
        "data": {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": stream_usage,
        },
    }

    yield {
        "event": "message_stop",
        "data": {"type": "message_stop"},
    }


async def translate_anthropic_json(
    response: ClientResponse,
    request_id: str,
    model: str,
) -> dict:
    """Translate a complete upstream Chat Completion response to Anthropic format."""
    data = await response.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = []

    # Thinking content
    reasoning = message.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})

    # Text content
    msg_content = message.get("content", "")
    if msg_content:
        content.append({"type": "text", "text": msg_content})

    # Tool calls -> tool_use blocks
    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        try:
            input_data = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            input_data = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", uid("toolu")),
            "name": fn.get("name", ""),
            "input": input_data,
        })

    finish_reason = choice.get("finish_reason")
    stop_reason = _map_finish_reason(finish_reason)
    usage = data.get("usage", {})

    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --- Internal helpers ---

def _translate_model(model: str, env: dict | None = None) -> str:
    """Map Anthropic request model to the configured DEFAULT_MODEL.

    Delegates to config.map_model_name() for consistent model resolution
    across all protocols (chat, responses, anthropic).
    """
    return map_model_name(model, env or {})


def _map_finish_reason(fr: str | None) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }
    return mapping.get(fr, "end_turn")


def _map_anthropic_tool_choice(choice: dict | str | None) -> str | dict:
    if not choice:
        return "auto"
    if isinstance(choice, str):
        return choice
    ctype = choice.get("type", "auto")
    if ctype == "auto":
        return "auto"
    if ctype == "any":
        return "required"
    if ctype == "tool":
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return "auto"
