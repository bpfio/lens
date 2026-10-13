"""OpenAI Responses API handler — translate, send, translate back."""

from aiohttp import web

from ..config import map_model_name, RESPONSES_DISPLAY_MODEL
from ..upstream import send_with_fallback
from ..stream import pipe_sse
from ..translators.responses import translate_to_chat, translate_stream_events, translate_response_json
from ..helpers import uid, now, cors_headers, extract_session_id


async def handle_responses_api(body: dict, request: web.Request) -> web.Response | web.StreamResponse:
    """Handle an OpenAI Responses API request."""
    app = request.app
    log = request.get("log")
    resp_id = uid("resp_")

    chat_req = translate_to_chat(body)
    original_model = chat_req.get("model", "")
    chat_req["model"] = map_model_name(chat_req.get("model"), app)
    if log:
        log.info("model.mapped", {"from": original_model, "to": chat_req["model"]})

    session_id = extract_session_id(request)
    query_str = request.rel_url.raw_query_string
    upstream_response = await send_with_fallback(
        app,
        chat_req,
        log,
        session_id=session_id,
        query_string=query_str,
        extra_headers=dict(request.headers),
    )

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
                "model": chat_req["model"],
                "detail": err_text[:500] if err_text else "",
            })
        msg = f"Upstream request failed: {err_text[:200]}" if err_text else "Upstream request failed"
        resp_headers = cors_headers()
        if "Retry-After" in upstream_response.headers:
            resp_headers["Retry-After"] = upstream_response.headers["Retry-After"]
        return web.json_response(
            {
                "id": resp_id,
                "object": "response",
                "created_at": now(),
                "model": RESPONSES_DISPLAY_MODEL,
                "output": [],
                "error": {
                    "message": msg,
                    "type": "invalid_request_error",
                    "code": "invalid_request_error",
                },
            },
            status=upstream_response.status,
            headers=resp_headers,
        )

    if chat_req.get("stream"):
        log and log.info("response.streaming", {"format": "sse"})
        return await pipe_sse(
            translate_stream_events(upstream_response, resp_id, RESPONSES_DISPLAY_MODEL),
            request,
        )

    log and log.info("response.ok", {"format": "json"})
    result = await translate_response_json(upstream_response, resp_id, RESPONSES_DISPLAY_MODEL)
    return web.json_response(result, headers=cors_headers())
