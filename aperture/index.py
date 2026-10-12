"""Aperture application factory and route dispatch.

Creates the aiohttp application, wires up middleware and routes.
Mirrors JS src/index.js.
"""

import json
import os
import sys
import time

from aiohttp import web

from .helpers import uid, error_response, cors_headers
from .middleware.auth import authenticate
from .middleware.rate_limiter import create_rate_limiter
from .middleware.logger import Logger, configure_log_file
from .handlers.chat import handle_chat_completions
from .handlers.responses import handle_responses_api
from .handlers.anthropic import handle_anthropic_messages
from .upstream_limiter import UpstreamRateLimiter
from .config import PASSTHROUGH_MODE
from .passthrough import handle_passthrough


VERSION = "1.0.0"


def _detect_route(path: str, body: dict) -> str:
    if path in ("/v1/chat/completions",) or path.endswith("/chat/completions"):
        return "chat"
    if path in ("/v1/messages",) or path.endswith("/messages"):
        return "anthropic"
    if "messages" in body:
        return "chat"
    if "input" in body or "instructions" in body:
        return "responses"
    if body.get("anthropic_version") or body.get("anthropic"):
        return "anthropic"
    return "responses"


def _handle_list_models(request: web.Request) -> web.Response:
    """Build and return the model list. Only shows the single hardcoded backend model."""
    backend_model = "deepseek-v4-flash"
    models = [{
        "id": backend_model,
        "object": "model",
        "created": 1780000000,
        "owned_by": "lens",
    }]
    return web.json_response({"data": models}, headers=cors_headers())


async def _handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response(
        {"status": "ok", "version": VERSION},
        headers=cors_headers(),
    )


# --- Logging middleware ---

@web.middleware
async def logging_middleware(request: web.Request, handler) -> web.Response:
    """Wraps every request with structured logging (request_id, timing, errors)."""
    request["request_id"] = uid("req_")
    log = Logger(request["request_id"])
    request["log"] = log

    log.info("request.start", {
        "method": request.method,
        "path": request.path,
        "remote": request.remote or request.headers.get("X-Forwarded-For", ""),
    })

    start = time.time()
    try:
        response = await handler(request)
        elapsed = int((time.time() - start) * 1000)
        log.info("request.end", {
            "status": response.status,
            "elapsed_ms": elapsed,
        })
        return response
    except web.HTTPException as exc:
        elapsed = int((time.time() - start) * 1000)
        log.warn("request.http_error", {
            "status": exc.status,
            "elapsed_ms": elapsed,
        })
        raise
    except Exception as exc:
        elapsed = int((time.time() - start) * 1000)
        log.error("request.error", {
            "error": str(exc),
            "type": type(exc).__name__,
            "elapsed_ms": elapsed,
        })
        raise


# --- CORS middleware ---

@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response(headers=cors_headers())
    try:
        response = await handler(request)
        for key, value in cors_headers().items():
            if key not in response.headers:
                response.headers[key] = value
        return response
    except web.HTTPException as exc:
        for key, value in cors_headers().items():
            if key not in exc.headers:
                exc.headers[key] = value
        raise


# --- Rate limit middleware ---

@web.middleware
async def rate_limit_middleware(request: web.Request, handler) -> web.Response:
    if request.method == "GET":
        return await handler(request)

    rate_limiter = request.app.get("rate_limiter")
    if rate_limiter is None:
        return await handler(request)

    client_ip = request.remote or request.headers.get("X-Forwarded-For", "unknown")
    allowed, reset_at = rate_limiter(client_ip)

    if not allowed:
        retry_after = max(1, int((reset_at - (time.time() * 1000)) / 1000))
        log = request.get("log")
        if log:
            log.warn("rate_limit.exceeded", {"client_ip": client_ip, "retry_after": retry_after})
        return web.json_response(
            {
                "error": {
                    "message": "Rate limit exceeded. Try again later.",
                    "type": "rate_limit_error",
                    "code": "RATE_LIMITED",
                },
            },
            status=429,
            headers={
                "Content-Type": "application/json",
                "Retry-After": str(retry_after),
                **cors_headers(),
            },
        )

    return await handler(request)


# --- Auth middleware ---

@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    if request.method in ("GET", "OPTIONS"):
        return await handler(request)

    # Allow localhost requests without API key (bypassed in production/local use, enforced during tests)
    remote = request.remote or ""
    allow_local = os.environ.get("AUTH_ALLOW_LOCAL", "true").lower() in ("true", "1", "yes")
    if os.environ.get("PYTEST_CURRENT_TEST"):
        allow_local = False
    if allow_local and (remote in ("127.0.0.1", "::1", "localhost") or remote.startswith("192.168.")):
        return await handler(request)

    api_key = request.app.get("api_key") or os.environ.get("API_KEY", "")
    auth_response = authenticate(request, api_key)
    if auth_response is not None:
        log = request.get("log")
        if log:
            log.warn("auth.failed", {
                "path": request.path,
                "has_auth_header": "Authorization" in request.headers,
                "has_x_api_key": "x-api-key" in request.headers,
            })
        return auth_response

    return await handler(request)


# --- POST dispatch ---

async def _handle_post(request: web.Request) -> web.Response:
    log = request.get("log")

    try:
        raw = await request.text()
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if log:
            log.error("request.parse_error", {"error": str(exc)})
        return error_response("Invalid JSON body", "invalid_request", "PARSE_ERROR", 400)

    if not isinstance(body, dict):
        if log:
            log.error("request.parse_error", {"detail": "body is not a dict"})
        return error_response("Invalid JSON body", "invalid_request", "PARSE_ERROR", 400)

    path = request.match_info.get("path", "")
    route = _detect_route(path, body)

    if log:
        log.info("route.detect", {"route": route, "path": path, "passthrough": PASSTHROUGH_MODE})

    if PASSTHROUGH_MODE:
        return await handle_passthrough(body, request, route)

    if route == "chat":
        return await handle_chat_completions(body, request)
    elif route == "responses":
        return await handle_responses_api(body, request)
    elif route == "anthropic":
        return await handle_anthropic_messages(body, request)

    if log:
        log.warn("route.unknown", {"path": path})
    return error_response("Unknown route", "invalid_request", "INVALID_ROUTE", 400)


# --- Client session lifecycle ---

async def _client_session_ctx(app: web.Application):
    import aiohttp
    timeout_sec = app.get("request_timeout", int(os.environ.get("REQUEST_TIMEOUT_MS", "120000"))) / 1000.0
    app["client"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec))
    yield
    await app["client"].close()


def create_app() -> web.Application:
    # No request body size limit (Claude Code sends large project context)
    logging_middleware_first = logging_middleware  # outermost — wraps everything
    app = web.Application(client_max_size=0, middlewares=[
        logging_middleware_first,
        cors_middleware,
        rate_limit_middleware,
        auth_middleware,
    ])

    app["upstream_base_url"] = os.environ.get("UPSTREAM_BASE_URL", "https://opencode.ai/zen/go/v1")
    app["api_key"] = os.environ.get("API_KEY", "")
    app["default_session_id"] = os.environ.get("OPENCODE_SESSION_ID") or f"lens-{uid()[:16]}"
    app["backup_upstream_base_url"] = os.environ.get("BACKUP_UPSTREAM_BASE_URL", "")
    app["backup_api_key"] = os.environ.get("BACKUP_API_KEY", "")
    app["backup_enabled"] = os.environ.get("BACKUP_ENABLED", "true") == "true"
    app["backup_backend_model"] = os.environ.get("BACKUP_BACKEND_MODEL", "")

    window_ms = int(os.environ.get("RATE_LIMIT_WINDOW_MS", "60000"))
    max_req = int(os.environ.get("RATE_LIMIT_MAX", "120"))
    app["rate_limiter"] = create_rate_limiter(window_ms, max_req)

    app["request_timeout"] = int(os.environ.get("REQUEST_TIMEOUT_MS", "120000"))
    app["nvidia_timeout_ms"] = int(os.environ.get("NVIDIA_TIMEOUT_MS", "300000"))
    app["cb_threshold"] = int(os.environ.get("CB_THRESHOLD", "3"))
    app["cb_cooldown_sec"] = int(os.environ.get("CB_COOLDOWN_SEC", "300"))

    upstream_rpm = int(os.environ.get("UPSTREAM_RPM", "39"))
    if upstream_rpm > 0:
        app["_upstream_limiter"] = UpstreamRateLimiter(upstream_rpm)
        print(f"Upstream rate limiter: {upstream_rpm} RPM", flush=True)

    upstream_concurrency = int(os.environ.get("UPSTREAM_CONCURRENCY", "8"))
    if upstream_concurrency > 0:
        import asyncio
        app["_upstream_semaphore"] = asyncio.Semaphore(upstream_concurrency)
        print(f"Upstream concurrency limiter: {upstream_concurrency}", flush=True)

    app.cleanup_ctx.append(_client_session_ctx)

    # Configure file logging
    log_dir = os.environ.get("LOG_DIR", "/var/log/lens")
    try:
        log_path = configure_log_file(log_dir)
        print(f"Logging to {log_path}", flush=True)
    except Exception as exc:
        print(f"Warning: could not set up file logging at {log_dir}: {exc}", file=sys.stderr, flush=True)

    app.router.add_get("/v1/models", _handle_list_models)
    app.router.add_get("/models", _handle_list_models)
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/readyz", _handle_health)
    app.router.add_post("/{path:.*}", _handle_post)

    return app
