"""OpenAI Responses API -> Chat Completions translation.

Mirrors JS src/translators/responses.js.
"""

import json
from typing import AsyncIterator

from aiohttp import ClientResponse

from ..helpers import uid, now, extract_text
from ..stream import stream_sse


def _normalize_tool(t: dict) -> dict | None:
    """Normalize a tool definition to Chat Completions function format.

    Handles both flat {"name": ..., "parameters": ...} and nested
    {"function": {...}} forms, plus codex's {"type":"custom", "input_schema": ...}.

    Preserves strict schema validation mode if specified.
    """
    fn = t.get("function", t)
    name = fn.get("name", "")
    if not name:
        return None
    params = fn.get("parameters", fn.get("input_schema", {}))
    if not isinstance(params, dict) or not params.get("type"):
        params = {"type": "object", "properties": {}}
    tool_dict = {
        "type": "function",
        "function": {
            "name": name,
            "description": fn.get("description", ""),
            "parameters": params,
        },
    }
    # Preserve strict mode if specified in function or outer tool
    strict_val = fn.get("strict", t.get("strict"))
    if strict_val is not None:
        tool_dict["function"]["strict"] = bool(strict_val)
    return tool_dict


def translate_to_chat(body: dict) -> dict:
    """Translate an OpenAI Responses API request to Chat Completions format.

    Args:
        body: Parsed JSON body of a Responses API request.

    Returns:
        Chat Completions request body (dict).
    """
    messages = []

    # Instructions -> system message (prepended first)
    instructions = body.get("instructions", "")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # Input -> user messages
    inp = body.get("input", "")
    additional_tools: list[dict] = []
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})

    elif isinstance(inp, list):
        # Pre-scan: collect call_ids that have a function_call_output result.
        # Strict upstreams (opencode gateway since the DeepSeek 0731 routing
        # change) reject assistant tool_calls without a matching tool message.
        # codex failure loops can accumulate function_call items that never
        # produced a result (e.g. `exec` invoked with an incompatible payload);
        # those orphan calls must NOT be translated into visible tool_calls.
        fco_ids = {
            msg.get("call_id")
            for msg in inp
            if isinstance(msg, dict) and msg.get("type") == "function_call_output" and msg.get("call_id")
        }
        fc_ids = {
            msg.get("call_id")
            for msg in inp
            if isinstance(msg, dict) and msg.get("type") == "function_call" and msg.get("call_id")
        }
        for msg in inp:
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type", "")
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # codex CLI sends tool definitions as additional_tools (role: developer);
            # extract them so upstream knows tools are available.
            if mtype == "additional_tools":
                for t in msg.get("tools", []):
                    if isinstance(t, dict):
                        additional_tools.append(t)
                continue

            # codex sends back executed tool calls as top-level items (no role).
            # Map them to assistant tool_calls so the following tool result
            # has an anchor; without this, upstreams re-request the same tool
            # call in a loop (tool result appears orphaned).
            if mtype == "function_call":
                # Skip orphan calls that have no matching result — upstream
                # rejects assistant tool_calls without a following tool message.
                if msg.get("call_id") not in fco_ids:
                    continue
                tool_call = {
                    "id": msg.get("call_id", uid("call")),
                    "type": "function",
                    "function": {
                        "name": msg.get("name", ""),
                        "arguments": msg.get("arguments", ""),
                    },
                }
                # Merge with a preceding assistant message instead of emitting
                # consecutive assistant-tool_calls messages: strict upstreams
                # (e.g. DeepSeek official via opencode gateway, since the
                # 0731 routing change) reject N assistant(tool_calls) messages
                # back-to-back. Canonical form: ONE assistant message carrying
                # all tool_calls, followed by the tool results.
                if messages and messages[-1]["role"] == "assistant":
                    last = messages[-1]
                    if "tool_calls" in last:
                        last["tool_calls"].append(tool_call)
                    else:
                        last["tool_calls"] = [tool_call]
                else:
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call],
                    })
                continue

            # codex sends tool execution results back as function_call_output.
            # Map to a chat tool message so the upstream sees the result.
            if mtype == "function_call_output":
                # Skip results without a matching function_call — an unanchored
                # tool message is rejected by strict upstreams.
                if msg.get("call_id") not in fc_ids:
                    continue
                output = msg.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output)
                messages.append({
                    "role": "tool",
                    "tool_call_id": msg.get("call_id", ""),
                    "content": output,
                })
                continue

            # developer messages (codex system prompt) -> system, since many
            # compatible upstreams reject the developer role. content arrives as
            # [{"type":"input_text","text":"..."}] blocks — flatten to a string,
            # upstreams reject list content on system messages.
            if role == "developer":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "input_text"
                    )
                else:
                    text = ""
                if text:
                    messages.append({"role": "system", "content": text})
                continue

            if role == "user":
                if isinstance(content, str):
                    messages.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    user_parts = []
                    has_image = False
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype == "input_text":
                            user_parts.append({"type": "text", "text": block.get("text", "")})
                        elif btype == "input_image":
                            img_url = block.get("image_url", {})
                            if isinstance(img_url, dict) and img_url.get("url"):
                                has_image = True
                                user_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": img_url["url"]},
                                })
                    if has_image:
                        messages.append({"role": "user", "content": user_parts})
                    else:
                        messages.append({"role": "user", "content": "".join(p.get("text", "") for p in user_parts)})

            elif role == "assistant":
                if isinstance(content, str):
                    messages.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    text_parts = []
                    tool_calls = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype in ("output_text", "input_text"):
                            text_parts.append(block.get("text", ""))
                        elif btype == "function_call":
                            tool_calls.append({
                                "id": block.get("id", uid("call")),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("arguments", {})),
                                },
                            })
                    msg_obj = {"role": "assistant", "content": "".join(text_parts) or None}
                    if tool_calls:
                        msg_obj["tool_calls"] = tool_calls
                    messages.append(msg_obj)

    # Build chat body
    chat = {
        "model": body.get("model", ""),
        "messages": messages,
        "stream": body.get("stream", False),
    }

    # Map parameters
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    if "temperature" in body:
        chat["temperature"] = body["temperature"]
    if "top_p" in body:
        chat["top_p"] = body["top_p"]
    if "stop" in body:
        chat["stop"] = body["stop"]

    # Map reasoning (effort / thinking intensity)
    if "reasoning" in body:
        reasoning_cfg = body["reasoning"]
        if isinstance(reasoning_cfg, dict):
            if "effort" in reasoning_cfg:
                chat["reasoning_effort"] = reasoning_cfg["effort"]
            chat["reasoning"] = reasoning_cfg
        elif isinstance(reasoning_cfg, str):
            chat["reasoning_effort"] = reasoning_cfg

    # Map text format (structured outputs)
    if "text" in body and isinstance(body["text"], dict):
        text_cfg = body["text"]
        fmt = text_cfg.get("format")
        if isinstance(fmt, dict):
            chat["response_format"] = fmt
        elif fmt == "json":
            chat["response_format"] = {"type": "json_object"}

    # Map parallel_tool_calls
    if "parallel_tool_calls" in body:
        chat["parallel_tool_calls"] = bool(body["parallel_tool_calls"])

    # Map caching and tracking parameters
    if "prompt_cache_key" in body:
        chat["prompt_cache_key"] = body["prompt_cache_key"]
    if "safety_identifier" in body:
        chat["safety_identifier"] = body["safety_identifier"]
    if "service_tier" in body:
        chat["service_tier"] = body["service_tier"]

    # Map tools
    tools = []
    if "tools" in body and body["tools"]:
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type", "")
            if ttype == "function" or (t.get("name") and not ttype) or not ttype:
                norm = _normalize_tool(t)
                if norm:
                    tools.append(norm)
            elif ttype in ("web_search", "web_search_preview", "file_search", "code_interpreter"):
                # Forward hosted tools
                tools.append(t)
    for t in additional_tools:
        norm = _normalize_tool(t)
        if norm:
            tools.append(norm)
    if tools:
        chat["tools"] = tools

    # Normalize tool_choice to Chat Completions format
    raw_tc = body.get("tool_choice")
    if isinstance(raw_tc, dict):
        # Responses format: {"type": "function", "name": "foo"} -> Chat format: {"type": "function", "function": {"name": "foo"}}
        if raw_tc.get("type") == "function" and "function" not in raw_tc:
            func_name = raw_tc.get("name", "")
            chat["tool_choice"] = {"type": "function", "function": {"name": func_name}}
        else:
            chat["tool_choice"] = raw_tc
    elif raw_tc is not None:
        chat["tool_choice"] = raw_tc
    elif tools:
        chat["tool_choice"] = "auto"

    # Metadata
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and "user_id" in metadata:
        chat["user"] = metadata["user_id"]

    return chat


async def translate_stream_events(
    response: ClientResponse,
    resp_id: str,
    model: str,
) -> AsyncIterator[dict]:
    """Translate upstream Chat Completions SSE to Responses API events.

    Yields {event, data} dicts for pipe_sse().
    Emits reasoning output items and deltas if reasoning_content is present.
    """
    content_index = 0
    item_index = 0
    text_item_added = False
    text_item_id = f"{resp_id}/item_0"
    accumulated_text = ""

    reasoning_item_added = False
    reasoning_item_done = False
    reasoning_item_id = f"{resp_id}/reasoning_0"
    accumulated_reasoning = ""
    reasoning_output_index = 0

    last_finish_reason = None
    tool_call_items: dict[int, dict] = {}
    arguments_done_sent: set[int] = set()
    latest_usage: dict = {}

    async for chunk in stream_sse(response):
        if "usage" in chunk and isinstance(chunk["usage"], dict):
            latest_usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {}) or {}
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                last_finish_reason = finish_reason

            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
            content = delta.get("content", "")
            tool_calls = delta.get("tool_calls")

            # 1. Reasoning content stream
            if reasoning:
                if not reasoning_item_added:
                    reasoning_item_added = True
                    yield {
                        "event": "response.output_item.added",
                        "data": {
                            "type": "response.output_item.added",
                            "output_index": reasoning_output_index,
                            "item": {
                                "id": reasoning_item_id,
                                "type": "reasoning",
                                "status": "in_progress",
                                "summary": [],
                            },
                        },
                    }
                accumulated_reasoning += reasoning
                yield {
                    "event": "response.reasoning.delta",
                    "data": {
                        "type": "response.reasoning.delta",
                        "output_index": reasoning_output_index,
                        "delta": reasoning,
                    },
                }

            # If reasoning was active and we now see text content, tool calls, or finish_reason, finalize reasoning
            if (content or tool_calls or finish_reason) and (reasoning_item_added and not reasoning_item_done):
                reasoning_item_done = True
                yield {
                    "event": "response.reasoning.done",
                    "data": {
                        "type": "response.reasoning.done",
                        "output_index": reasoning_output_index,
                        "text": accumulated_reasoning,
                    },
                }
                yield {
                    "event": "response.output_item.done",
                    "data": {
                        "type": "response.output_item.done",
                        "output_index": reasoning_output_index,
                        "item": {
                            "id": reasoning_item_id,
                            "type": "reasoning",
                            "status": "completed",
                            "summary": [],
                            "text": accumulated_reasoning,
                        },
                    },
                }

            # 2. Text content stream
            if content:
                text_output_idx = 1 if reasoning_item_added else 0
                if not text_item_added:
                    text_item_added = True
                    yield {
                        "event": "response.output_item.added",
                        "data": {
                            "type": "response.output_item.added",
                            "output_index": text_output_idx,
                            "item": {
                                "id": text_item_id,
                                "type": "message",
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        },
                    }
                    yield {
                        "event": "response.content_part.added",
                        "data": {
                            "type": "response.content_part.added",
                            "output_index": text_output_idx,
                            "part": {
                                "type": "output_text",
                                "text": "",
                            },
                        },
                    }
                accumulated_text += content
                yield {
                    "event": "response.output_text.delta",
                    "data": {
                        "type": "response.output_text.delta",
                        "output_index": text_output_idx,
                        "content_index": content_index,
                        "delta": content,
                    },
                }

            # 3. Tool calls stream
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    tc_id = tc.get("id", uid("call"))
                    idx = tc.get("index", 0)
                    if idx not in tool_call_items:
                        item_index += 1
                        tool_output_idx = (1 if reasoning_item_added else 0) + (1 if text_item_added else 0) + len(tool_call_items)
                        tool_call_items[idx] = {
                            "item_id": f"{resp_id}/item_{item_index}",
                            "output_index": tool_output_idx,
                            "name": fn.get("name", ""),
                            "call_id": tc_id,
                            "arguments": "",
                        }
                        yield {
                            "event": "response.output_item.added",
                            "data": {
                                "type": "response.output_item.added",
                                "output_index": tool_output_idx,
                                "item": {
                                    "id": tool_call_items[idx]["item_id"],
                                    "type": "function_call",
                                    "status": "in_progress",
                                    "name": tool_call_items[idx]["name"],
                                    "call_id": tool_call_items[idx]["call_id"],
                                    "arguments": "",
                                },
                            },
                        }

                    arg_delta = fn.get("arguments", "")
                    if arg_delta:
                        tool_call_items[idx]["arguments"] += arg_delta
                        yield {
                            "event": "response.function_call_arguments.delta",
                            "data": {
                                "type": "response.function_call_arguments.delta",
                                "output_index": tool_call_items[idx]["output_index"],
                                "item_id": tool_call_items[idx]["item_id"],
                                "delta": arg_delta,
                            },
                        }

                    if finish_reason in ("tool_calls", "stop"):
                        arguments_done_sent.add(idx)
                        yield {
                            "event": "response.function_call_arguments.done",
                            "data": {
                                "type": "response.function_call_arguments.done",
                                "item_id": tool_call_items[idx]["item_id"],
                                "name": tool_call_items[idx]["name"],
                                "arguments": tool_call_items[idx]["arguments"],
                            },
                        }

    # Finalize reasoning if not yet done
    output_items = []
    if reasoning_item_added:
        if not reasoning_item_done:
            yield {
                "event": "response.reasoning.done",
                "data": {
                    "type": "response.reasoning.done",
                    "output_index": reasoning_output_index,
                    "text": accumulated_reasoning,
                },
            }
            yield {
                "event": "response.output_item.done",
                "data": {
                    "type": "response.output_item.done",
                    "output_index": reasoning_output_index,
                    "item": {
                        "id": reasoning_item_id,
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [],
                        "text": accumulated_reasoning,
                    },
                },
            }
        output_items.append({
            "id": reasoning_item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "text": accumulated_reasoning,
        })

    # Finalize text output
    if text_item_added and accumulated_text:
        text_output_idx = 1 if reasoning_item_added else 0
        yield {
            "event": "response.output_text.done",
            "data": {
                "type": "response.output_text.done",
                "output_index": text_output_idx,
                "content_index": content_index,
                "text": accumulated_text,
            },
        }
        yield {
            "event": "response.content_part.done",
            "data": {
                "type": "response.content_part.done",
                "output_index": text_output_idx,
                "content_index": content_index,
                "part": {"type": "output_text", "text": accumulated_text},
            },
        }
        yield {
            "event": "response.output_item.done",
            "data": {
                "type": "response.output_item.done",
                "output_index": text_output_idx,
                "item": {
                    "id": text_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": accumulated_text}],
                },
            },
        }
        output_items.append({
            "id": text_item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": accumulated_text}],
        })

    # Finalize tool call items
    for idx in sorted(tool_call_items):
        item = tool_call_items[idx]
        if idx not in arguments_done_sent:
            yield {
                "event": "response.function_call_arguments.done",
                "data": {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["item_id"],
                    "name": item["name"],
                    "arguments": item["arguments"],
                },
            }
        yield {
            "event": "response.output_item.done",
            "data": {
                "type": "response.output_item.done",
                "output_index": item["output_index"],
                "item": {
                    "id": item["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": item["name"],
                    "call_id": item["call_id"],
                    "arguments": item["arguments"],
                },
            },
        }
        output_items.append({
            "id": item["item_id"],
            "type": "function_call",
            "status": "completed",
            "name": item["name"],
            "call_id": item["call_id"],
            "arguments": item["arguments"],
        })

    # Build final response object
    final_response = {
        "id": resp_id,
        "object": "response",
        "created_at": now(),
        "status": "completed",
        "model": model,
        "output": output_items,
    }
    if latest_usage:
        usage_dict = {
            "input_tokens": latest_usage.get("prompt_tokens", latest_usage.get("input_tokens", 0)),
            "output_tokens": latest_usage.get("completion_tokens", latest_usage.get("output_tokens", 0)),
            "total_tokens": latest_usage.get("total_tokens", 0),
        }
        if "prompt_tokens_details" in latest_usage:
            usage_dict["input_token_details"] = latest_usage["prompt_tokens_details"]
        if "completion_tokens_details" in latest_usage:
            usage_dict["output_token_details"] = latest_usage["completion_tokens_details"]
        final_response["usage"] = usage_dict

    yield {
        "event": "response.completed",
        "data": {
            "type": "response.completed",
            "response": final_response,
        },
    }

    yield {
        "event": "response.done",
        "data": {
            "type": "response.done",
            "response": final_response,
        },
    }


async def translate_response_json(
    response: ClientResponse,
    resp_id: str,
    model: str,
) -> dict:
    """Translate a complete Chat Completion response to Responses API JSON."""
    data = await response.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})

    output = []

    # 1. Reasoning output
    reasoning_content = message.get("reasoning_content") or message.get("reasoning", "")
    if reasoning_content:
        output.append({
            "id": f"{resp_id}/reasoning",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "text": reasoning_content,
        })

    # 2. Text output
    content = message.get("content", "")
    if content:
        output.append({
            "id": f"{resp_id}/text",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": content},
            ],
        })

    # 3. Tool call output
    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        output.append({
            "id": tc.get("id", uid("call")),
            "type": "function_call",
            "status": "completed",
            "name": fn.get("name", ""),
            "call_id": tc.get("id", uid("call")),
            "arguments": fn.get("arguments", "{}"),
        })

    usage = data.get("usage", {})
    usage_dict = {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
        "total_tokens": usage.get("total_tokens", 0),
    }
    if "prompt_tokens_details" in usage:
        usage_dict["input_token_details"] = usage["prompt_tokens_details"]
    if "completion_tokens_details" in usage:
        usage_dict["output_token_details"] = usage["completion_tokens_details"]

    return {
        "id": resp_id,
        "object": "response",
        "created_at": now(),
        "model": model,
        "status": "completed",
        "output": output,
        "usage": usage_dict,
    }
