"""Passthrough handler — full-path reverse proxy and model router.

When upstream natively supports Responses API or Anthropic Messages API,
this module proxies requests directly without translating to Chat Completions format.
It supports:
- Full HTTP method proxying (GET, POST, DELETE, PUT)
- Complete path and query string mirroring
- Bi-directional header mirroring (distributed tracing, rate limits, metadata)
- Flexible model routing (passthrough, alias mapping, or forced rewrite)
"""

import os
import aiohttp
from aiohttp import web, ClientTimeout

from .config import BACKEND_MODEL, get_model_map
from .helpers import cors_headers
from .middleware.logger import Logger

# RFC 2616 Hop-by-hop headers to strip
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def get_upstream_urls(app: web.Application) -> dict:
    """Build endpoint URLs for passthrough mode (backward compatibility)."""
    base_url = app.get("upstream_base_url", os.environ.get("UPSTREAM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"))
    base_url = base_url.rstrip("/")

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


def build_passthrough_url(app: web.Application, request: web.Request, protocol: str) -> str:
    """Build the exact upstream URL preserving original path and query."""
    base_url = app.get("upstream_base_url", os.environ.get("UPSTREAM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"))
    base_url = base_url.rstrip("/")

    flavor = os.environ.get("UPSTREAM_FLAVOR", "auto").lower()

    if flavor == "mimo" or (flavor == "auto" and ("xiaomimimo" in base_url or "mimo" in base_url)):
        urls = get_upstream_urls(app)
        return urls.get(protocol, f"{base_url}/v1/{protocol}")

    # Standard flavor: mirror request path and query string exactly
    req_path = request.rel_url.path
    if base_url.endswith("/v1"):
        if req_path == "/v1":
            req_path = ""
        elif req_path.startswith("/v1/"):
            req_path = req_path[3:]

    query_str = request.rel_url.raw_query_string
    url = f"{base_url}{req_path}"
    if query_str:
        url = f"{url}?{query_str}"
    return url


def get_upstream_headers(app: web.Application, protocol: str, request: web.Request | None = None) -> dict:
    """Build headers for upstream request with tracing and auth."""
    api_key = app.get("api_key", os.environ.get("API_KEY", ""))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    if request:
        for k, v in request.headers.items():
            if k.lower() not in HOP_BY_HOP_HEADERS:
                headers[k.lower()] = v

    if protocol == "responses":
        headers["x-openai-internal-codex-responses-lite"] = "true"
    elif protocol == "anthropic":
        headers["x-api-key"] = api_key
        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"

    headers["authorization"] = f"Bearer {api_key}" if protocol != "anthropic" else headers.get("authorization", f"Bearer {api_key}")
    return headers


def filter_downstream_headers(upstream_headers) -> dict:
    """Filter hop-by-hop headers while mirroring rate-limits and tracing to client."""
    headers = cors_headers()
    for k, v in upstream_headers.items():
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "content-type":
            headers[k] = v
    return headers


def rewrite_request_body(body: dict, target_model: str, protocol: str) -> tuple[dict, str]:
    """Rewrite model name in request body based on MODEL_OVERRIDE_MODE and MODEL_MAP."""
    body_copy = dict(body)
    original_model = body_copy.get("model", "")
    mode = os.environ.get("MODEL_OVERRIDE_MODE", "force").lower()

    if mode == "passthrough":
        effective_model = original_model or target_model
    elif mode == "mapped":
        mmap = get_model_map()
        effective_model = mmap.get(original_model, original_model or target_model)
    else:
        effective_model = target_model

    body_copy["model"] = effective_model

    # Filter out unsupported tools only if explicitly enabled
    filter_search = os.environ.get("FILTER_WEB_SEARCH", "false").lower() in ("true", "1", "yes")
    if filter_search and protocol == "responses" and "tools" in body_copy and isinstance(body_copy["tools"], list):
        body_copy["tools"] = [
            t for t in body_copy["tools"]
            if not (isinstance(t, dict) and t.get("type") in ("web_search", "web_search_preview"))
        ]

    return body_copy, original_model


async def handle_passthrough(
    body: dict | None,
    request: web.Request,
    protocol: str,
) -> web.Response | web.StreamResponse:
    """Proxy request directly to upstream with path mirroring and header preservation."""
    app = request.app
    log: Logger | None = request.get("log")
    client: aiohttp.ClientSession = app.get("client")

    if client is None:
        return web.json_response(
            {"error": {"message": "Client not initialized", "type": "upstream_error"}},
            status=502,
            headers=cors_headers(),
        )

    url = build_passthrough_url(app, request, protocol)
    headers = get_upstream_headers(app, protocol, request)

    method = request.method.upper()
    rewritten_body = None
    original_model = ""

    if method not in ("GET", "HEAD", "DELETE") and body is not None:
        rewritten_body, original_model = rewrite_request_body(body, BACKEND_MODEL, protocol)

    if log:
        log.info("passthrough.request", {
            "method": method,
            "protocol": protocol,
            "url": url,
            "from_model": original_model,
            "to_model": (rewritten_body.get("model") if rewritten_body else ""),
        })

    timeout_sec = app.get("request_timeout", 120000) / 1000.0
    is_stream = bool(body and body.get("stream"))
    timeout = ClientTimeout(
        total=None if is_stream else timeout_sec,
        sock_read=timeout_sec,
    )

    if method in ("GET", "HEAD", "DELETE"):
        headers.pop("content-type", None)
        headers.pop("Content-Type", None)

    try:
        if method in ("GET", "HEAD", "DELETE"):
            upstream_resp = await client.request(method, url, headers=headers, timeout=timeout)
        else:
            upstream_resp = await client.request(method, url, json=rewritten_body, headers=headers, timeout=timeout)
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

    downstream_headers = filter_downstream_headers(upstream_resp.headers)

    # Error from upstream: return as is with mirrored headers
    if not upstream_resp.ok:
        err_body = await upstream_resp.read()
        return web.Response(
            body=err_body,
            status=upstream_resp.status,
            content_type=upstream_resp.content_type or "application/json",
            headers=downstream_headers,
        )

    effective_model = rewritten_body.get("model", "") if rewritten_body else ""
    backend_bytes = effective_model.encode("utf-8") if effective_model else b""
    display_bytes = original_model.encode("utf-8") if original_model else backend_bytes

    def _replace_model_field(data: bytes) -> bytes:
        if not original_model or backend_bytes == display_bytes or not backend_bytes:
            return data
        p1 = b'"model":"' + backend_bytes + b'"'
        r1 = b'"model":"' + display_bytes + b'"'
        p2 = b'"model": "' + backend_bytes + b'"'
        r2 = b'"model": "' + display_bytes + b'"'
        if p1 in data:
            data = data.replace(p1, r1)
        if p2 in data:
            data = data.replace(p2, r2)
        return data

    # Stream response
    content_type = upstream_resp.headers.get("Content-Type", "")
    if is_stream or "text/event-stream" in content_type:
        downstream_headers["Content-Type"] = "text/event-stream"
        downstream_headers["Cache-Control"] = "no-cache"
        downstream_headers["Connection"] = "keep-alive"

        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers=downstream_headers,
        )
        await resp.prepare(request)

        buf = b""
        async for chunk in upstream_resp.content.iter_any():
            buf += chunk
            lines = buf.split(b"\n")
            buf = lines.pop() if lines else b""
            for line in lines:
                await resp.write(_replace_model_field(line) + b"\n")

        if buf:
            await resp.write(_replace_model_field(buf))

        await resp.write_eof()
        return resp

    # Non-streaming JSON response
    resp_body = await upstream_resp.read()
    resp_body = _replace_model_field(resp_body)

    return web.Response(
        body=resp_body,
        status=upstream_resp.status,
        content_type=upstream_resp.content_type or "application/json",
        headers=downstream_headers,
    )
