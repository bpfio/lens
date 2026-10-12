"""Passthrough handler — direct proxy with model renaming for multi-protocol upstreams.

When upstream natively supports Responses API or Anthropic Messages API (e.g. Xiaomi MiMo),
this module proxies requests directly without translating to Chat Completions format.
It merely rewrites `model` in the request body to `BACKEND_MODEL` and pipes the upstream
response directly back to the client.
"""

import os
import aiohttp
from aiohttp import web, ClientTimeout

from .config import BACKEND_MODEL
from .helpers import cors_headers
from .middleware.logger import Logger


def get_upstream_urls(app: web.Application) -> dict:
    """Build endpoint URLs for passthrough mode."""
    base_url = app.get("upstream_base_url", os.environ.get("UPSTREAM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"))
    base_url = base_url.rstrip("/")

    # Check if base_url ends with /v1
    if base_url.endswith("/v1"):
        root_url = base_url[:-3]
        v1_url = base_url
    else:
        root_url = base_url
        v1_url = f"{base_url}/v1"

    responses_url = os.environ.get("RESPONSES_UPSTREAM_URL", f"{v1_url}/responses")
    chat_url = os.environ.get("CHAT_UPSTREAM_URL", f"{v1_url}/chat/completions")
    anthropic_url = os.environ.get("ANTHROPIC_UPSTREAM_URL", f"{root_url}/anthropic/v1/messages")

    return {
        "responses": responses_url,
        "chat": chat_url,
        "anthropic": anthropic_url,
    }


def get_upstream_headers(app: web.Application, protocol: str, request: web.Request | None = None) -> dict:
    """Build headers for upstream request."""
    api_key = app.get("api_key", os.environ.get("API_KEY", ""))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if protocol == "responses":
        headers["x-openai-internal-codex-responses-lite"] = "true"
        if request:
            for k, v in request.headers.items():
                if k.lower().startswith("x-openai-"):
                    headers[k.lower()] = v
    elif protocol == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        if request and "anthropic-version" in request.headers:
            headers["anthropic-version"] = request.headers["anthropic-version"]
    return headers


def rewrite_request_body(body: dict, target_model: str, protocol: str) -> tuple[dict, str]:
    """Rewrite model name in request body, returning (modified_body, original_model)."""
    body_copy = dict(body)
    original_model = body_copy.get("model", "")
    body_copy["model"] = target_model

    # Filter out unsupported tools for gateways that don't support web_search (e.g. MiMo)
    if protocol == "responses" and "tools" in body_copy and isinstance(body_copy["tools"], list):
        body_copy["tools"] = [
            t for t in body_copy["tools"]
            if not (isinstance(t, dict) and t.get("type") in ("web_search", "web_search_preview"))
        ]

    return body_copy, original_model


async def handle_passthrough(
    body: dict,
    request: web.Request,
    protocol: str,
) -> web.Response | web.StreamResponse:
    """Proxy request directly to upstream with model renaming."""
    app = request.app
    log: Logger | None = request.get("log")
    client: aiohttp.ClientSession = app.get("client")

    if client is None:
        return web.json_response(
            {"error": {"message": "Client not initialized", "type": "upstream_error"}},
            status=502,
            headers=cors_headers(),
        )

    urls = get_upstream_urls(app)
    url = urls.get(protocol)
    if not url:
        return web.json_response(
            {"error": {"message": f"Unsupported protocol {protocol}", "type": "invalid_request_error"}},
            status=400,
            headers=cors_headers(),
        )

    headers = get_upstream_headers(app, protocol, request)
    rewritten_body, original_model = rewrite_request_body(body, BACKEND_MODEL, protocol)

    if log:
        log.info("passthrough.request", {
            "protocol": protocol,
            "url": url,
            "from_model": original_model,
            "to_model": BACKEND_MODEL,
        })

    timeout_sec = app.get("request_timeout", 120000) / 1000.0
    is_stream = bool(body.get("stream"))
    timeout = ClientTimeout(
        total=None if is_stream else timeout_sec,
        sock_read=timeout_sec,
    )

    try:
        upstream_resp = await client.post(url, json=rewritten_body, headers=headers, timeout=timeout)
    except Exception as exc:
        if log:
            log.error("passthrough.error", {"error": str(exc), "url": url})
        return web.json_response(
            {"error": {"message": f"Upstream connection failed: {exc}", "type": "upstream_error"}},
            status=502,
            headers=cors_headers(),
        )

    if log:
        log.info("passthrough.response", {
            "status": upstream_resp.status,
            "content_type": upstream_resp.content_type,
        })

    # Error from upstream: return as is
    if not upstream_resp.ok:
        err_body = await upstream_resp.read()
        return web.Response(
            body=err_body,
            status=upstream_resp.status,
            content_type=upstream_resp.content_type or "application/json",
            headers=cors_headers(),
        )

    # Model bytes replacement targets
    backend_bytes = BACKEND_MODEL.encode("utf-8")
    display_bytes = original_model.encode("utf-8") if original_model else backend_bytes

    # Stream response
    content_type = upstream_resp.headers.get("Content-Type", "")
    if is_stream or "text/event-stream" in content_type:
        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                **cors_headers(),
            },
        )
        await resp.prepare(request)

        async for chunk in upstream_resp.content.iter_any():
            # Replace backend model name with original model name in SSE payloads
            if original_model and backend_bytes != display_bytes and backend_bytes in chunk:
                chunk = chunk.replace(backend_bytes, display_bytes)
            await resp.write(chunk)

        await resp.write_eof()
        return resp

    # Non-streaming JSON response
    resp_body = await upstream_resp.read()
    if original_model and backend_bytes != display_bytes and backend_bytes in resp_body:
        resp_body = resp_body.replace(backend_bytes, display_bytes)

    return web.Response(
        body=resp_body,
        status=upstream_resp.status,
        content_type=upstream_resp.content_type or "application/json",
        headers=cors_headers(),
    )
