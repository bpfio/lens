import json
import os
from typing import AsyncIterator
from aiohttp import web

from ..config import map_model_name, CHAT_DISPLAY_MODEL
from ..upstream import send_with_fallback
from ..stream import pipe_sse_raw
from ..translators.dsml import normalize_dsml_tool_calls
from ..helpers import error_response, cors_headers, extract_session_id


async def filter_chat_stream(response) -> AsyncIterator[str]:
    """Filter SSE stream to strip non-standard fields (reasoning_content, null content) and enforce display model."""
    MAX_BUF = int(os.environ.get("MAX_SSE_BUFFER_BYTES", "2097152"))  # 2MB default guard
    buf = b""
    total = 0

    async for chunk in response.content:
        total += len(chunk)
        if MAX_BUF > 0 and total > MAX_BUF:
            raise RuntimeError("SSE buffer exceeded maximum size")

        buf += chunk
        lines = buf.split(b"\n")
        buf = lines.pop() if lines else b""

        for line in lines:
            trimmed = line.strip()
            if not trimmed.startswith(b"data: "):
                yield line.decode("utf-8", errors="replace")
                continue

            payload = trimmed[6:].strip()
            if payload == b"[DONE]":
                yield line.decode("utf-8", errors="replace")
                continue

            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                yield line.decode("utf-8", errors="replace")
                continue

            if "choices" not in parsed or not isinstance(parsed.get("choices"), list):
                yield line.decode("utf-8", errors="replace")
                continue

            # Override model to display name (never leak backend model)
            parsed["model"] = CHAT_DISPLAY_MODEL

            reasoning_stripped = False
            for choice in parsed["choices"]:
                delta = choice.get("delta")
                if not delta:
                    continue
                if "reasoning_content" in delta:
                    del delta["reasoning_content"]
                    reasoning_stripped = True
                if delta.get("content") is None and "tool_calls" not in delta:
                    delta["content"] = ""

            has_payload = any(
                c.get("finish_reason")
                or (
                    c.get("delta")
                    and (
                        bool(c["delta"].get("role"))
                        or (isinstance(c["delta"].get("content"), str) and len(c["delta"]["content"]) > 0)
                        or c["delta"].get("tool_calls") is not None
                    )
                )
                for c in parsed["choices"]
            )

            # Drop chunk if it only carried reasoning_content and has no other visible delta
            if reasoning_stripped and not has_payload:
                continue

            yield f"data: {json.dumps(parsed)}"


async def handle_chat_completions(body: dict, request: web.Request) -> web.Response | web.StreamResponse:
    """Handle a Chat Completions request: override model, send upstream, filter/normalize."""
    app = request.app
    log = request.get("log")

    original_model = body.get("model", "")
    body["model"] = map_model_name(body.get("model"), app)
    if log:
        log.info("model.mapped", {"from": original_model, "to": body["model"]})

    session_id = extract_session_id(request)
    upstream_response = await send_with_fallback(app, body, log, session_id=session_id)

    if isinstance(upstream_response, web.Response):
        return upstream_response

    if not upstream_response.ok:
        err_text = ""
        try:
            err_text = await upstream_response.text()
        except Exception:
            pass
        finally:
            if hasattr(upstream_response, "release"):
                upstream_response.release()
        if log:
            log.error("upstream.bad_status", {
                "status": upstream_response.status,
                "model": body["model"],
                "detail": err_text[:500] if err_text else "",
            })
        msg = f"Upstream request failed: {err_text[:200]}" if err_text else "Upstream request failed"
        return error_response(msg, "upstream_error", "UPSTREAM", upstream_response.status)

    if body.get("stream"):
        log and log.info("response.streaming", {"format": "sse"})
        return await pipe_sse_raw(filter_chat_stream(upstream_response), request)

    try:
        response_text = await upstream_response.text()
        response_body = json.loads(response_text)
        for choice in response_body.get("choices", []):
            msg = choice.get("message", {})
            if "reasoning_content" in msg:
                del msg["reasoning_content"]
        response_body["model"] = CHAT_DISPLAY_MODEL
        normalized = normalize_dsml_tool_calls(response_body)
        log and log.info("response.ok", {"tokens": response_body.get("usage")})
        return web.json_response(normalized, headers=cors_headers())
    except json.JSONDecodeError as exc:
        if log:
            log.error("response.parse_error", {"error": str(exc), "body_preview": response_text[:500]})
        return error_response("Upstream returned invalid JSON", "upstream_error", "UPSTREAM_PARSE_ERROR", 502)
    except Exception as exc:
        if log:
            log.error("response.unexpected", {"error": str(exc), "type": type(exc).__name__})
        return error_response("Failed to process upstream response", "internal_error", "INTERNAL_ERROR", 502)
