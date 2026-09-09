"""Anthropic Messages API handler — translate, send, translate back."""

from aiohttp import web

from ..config import map_model_name, ANTHROPIC_DISPLAY_MODEL
from ..upstream import send_with_fallback
from ..stream import pipe_sse
from ..translators.anthropic import (
    translate_anthropic_to_chat,
    translate_anthropic_stream,
    translate_anthropic_json,
)
from ..helpers import uid, cors_headers, extract_session_id


async def handle_anthropic_messages(body: dict, request: web.Request) -> web.Response | web.StreamResponse:
    """Handle an Anthropic Messages API request."""
    app = request.app
    log = request.get("log")
    request_id = uid("msg_")

    display_model = ANTHROPIC_DISPLAY_MODEL

    chat_req = translate_anthropic_to_chat(body, dict(app))
    original_model = chat_req.get("model", "")
    chat_req["model"] = map_model_name(chat_req.get("model"), app)
    if log:
        log.info("model.mapped", {"from": original_model, "to": f"{chat_req['model']} (display: {display_model})"})

    session_id = extract_session_id(request)
    upstream_response = await send_with_fallback(app, chat_req, log, session_id=session_id)

    if isinstance(upstream_response, web.Response):
        return upstream_response

    if not upstream_response.ok:
        if log:
            log.error("upstream.bad_status", {
                "status": upstream_response.status,
                "model": chat_req["model"],
            })
        return web.json_response(
            {
                "id": request_id,
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Upstream request failed"},
            },
            status=upstream_response.status,
            headers={
                "x-request-id": request_id,
                "request-id": request_id,
                **cors_headers(),
            },
        )

    if chat_req.get("stream"):
        log and log.info("response.streaming", {"format": "sse"})
        return await pipe_sse(
            translate_anthropic_stream(upstream_response, request_id, display_model),
            request,
        )

    log and log.info("response.ok", {"format": "json"})
    result = await translate_anthropic_json(upstream_response, request_id, display_model)
    return web.json_response(
        result,
        headers={
            "x-request-id": request_id,
            "request-id": request_id,
            **cors_headers(),
        },
    )
