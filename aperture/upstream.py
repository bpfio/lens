"""Upstream API client.

Manages connection to upstream Chat Completions API via app's shared session.
No Gateway fallback (Cloudflare-specific logic removed).
"""

import asyncio
import os
import time
from aiohttp import web, ClientResponse, ClientError, ClientConnectionError, ClientTimeout

from .helpers import cors_headers
from .middleware.logger import Logger
from .upstream_limiter import UpstreamRateLimiter


def build_upstream_url(app: web.Application) -> str:
    """Build the upstream Chat Completions URL."""
    base_url = app.get("upstream_base_url", os.environ.get(
        "UPSTREAM_BASE_URL", "https://opencode.ai/zen/go/v1",
    ))
    return f"{base_url.rstrip('/')}/chat/completions"


def build_auth_headers(app: web.Application, session_id: str | None = None) -> dict:
    """Build auth headers for upstream requests."""
    api_key = app.get("api_key", os.environ.get("API_KEY", ""))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    sid = session_id or app.get("default_session_id")
    if sid:
        headers["x-opencode-session"] = sid
    return headers


async def send_chat_request(
    app: web.Application,
    chat_body: dict,
    log: Logger | None = None,
    url_override: str | None = None,
    headers_override: dict | None = None,
    session_id: str | None = None,
) -> ClientResponse | web.Response:
    """Send a Chat Completions request to the upstream API.

    Uses the app's shared aiohttp ClientSession.
    On network error, returns a 502 web.Response with a structured error body.

    Categorizes upstream errors:
    - Timeout → 504
    - Connection refused/DNS failure → 502 with UPSTREAM_UNREACHABLE
    - Other client errors → 502 with UPSTREAM_ERROR
    - Unexpected → 502 with INTERNAL_ERROR
    """
    url = url_override or build_upstream_url(app)
    headers = dict(headers_override) if headers_override else build_auth_headers(app, session_id=session_id)
    sid = session_id or app.get("default_session_id")
    if sid and "x-opencode-session" not in headers:
        headers["x-opencode-session"] = sid
    client = app.get("client")
    model = chat_body.get("model", "")

    if log:
        log.info("upstream.request", {
            "url": url,
            "model": model,
        })

    if client is None:
        msg = "Upstream client not initialized"
        if log:
            log.error("upstream.no_client", {"url": url})
        return web.json_response(
            {"error": {"message": msg, "type": "upstream_error", "code": "NO_CLIENT"}},
            status=502,
            headers=cors_headers(),
        )

    timeout_ms = app.get("request_timeout", 120000)
    nvidia_timeout_ms = app.get("nvidia_timeout_ms", 300000)

    # Rate limit — only for NVIDIA API (integrate.api.nvidia.com has 40 RPM limit)
    # Other upstreams (sensenova, opencode, etc.) are not rate-limited here.
    is_nvidia = "integrate.api.nvidia.com" in url
    limiter: UpstreamRateLimiter | None = app.get("_upstream_limiter")
    if limiter and is_nvidia:
        await limiter.acquire()

    # Concurrency limit — prevent exceeding NVIDIA's worker pool (48 max concurrent)
    semaphore: asyncio.Semaphore | None = app.get("_upstream_semaphore")
    if semaphore and is_nvidia:
        await semaphore.acquire()

    try:
        # Timeout semantics:
        # - Non-streaming: `total` bounds the whole request — fine, response
        #   arrives as one body.
        # - Streaming (SSE): `total` would kill long-running streams that keep
        #   producing chunks (e.g. deepseek reasoning + long replies) exactly at
        #   the limit, returning a 200 with a truncated body — which clients
        #   report as "empty or malformed response". Use `sock_read` instead:
        #   only fail when the upstream goes silent for the timeout window.
        stream = bool(chat_body.get("stream"))
        if is_nvidia:
            nv_sec = nvidia_timeout_ms / 1000.0
            timeout = ClientTimeout(
                total=None if stream else nv_sec,
                sock_read=nv_sec,
            )
            resp = await client.post(url, json=chat_body, headers=headers, timeout=timeout)
        else:
            sec = timeout_ms / 1000.0
            timeout = ClientTimeout(
                total=None if stream else sec,
                sock_read=sec,
            )
            resp = await client.post(url, json=chat_body, headers=headers, timeout=timeout)
        if log:
            log.info("upstream.response", {
                "url": url,
                "status": resp.status,
                "model": model,
            })
        return resp

    except asyncio.TimeoutError:
        effective_timeout = nvidia_timeout_ms if is_nvidia else timeout_ms
        if log:
            log.error("upstream.timeout", {
                "url": url,
                "timeout_ms": effective_timeout,
                "model": model,
            })
        return web.json_response(
            {
                "error": {
                    "message": f"Upstream request timed out after {effective_timeout}ms",
                    "type": "timeout_error",
                    "code": "UPSTREAM_TIMEOUT",
                },
            },
            status=504,
            headers=cors_headers(),
        )

    except ClientConnectionError as exc:
        if log:
            log.error("upstream.unreachable", {
                "url": url,
                "error": str(exc),
                "model": model,
            })
        return web.json_response(
            {
                "error": {
                    "message": f"Upstream unreachable: {exc}",
                    "type": "connection_error",
                    "code": "UPSTREAM_UNREACHABLE",
                },
            },
            status=502,
            headers=cors_headers(),
        )

    except ClientError as exc:
        if log:
            log.error("upstream.client_error", {
                "url": url,
                "error": str(exc),
                "model": model,
            })
        return web.json_response(
            {
                "error": {
                    "message": f"Upstream error: {exc}",
                    "type": "upstream_error",
                    "code": "UPSTREAM_CLIENT_ERROR",
                },
            },
            status=502,
            headers=cors_headers(),
        )

    except Exception as exc:
        if log:
            log.error("upstream.unexpected", {
                "url": url,
                "error": str(exc),
                "type": type(exc).__name__,
                "model": model,
            })
        return web.json_response(
            {
                "error": {
                    "message": "Internal upstream error",
                    "type": "internal_error",
                    "code": "INTERNAL_ERROR",
                },
            },
            status=502,
            headers=cors_headers(),
        )

    finally:
        if semaphore and is_nvidia:
            semaphore.release()


async def send_with_fallback(
    app: web.Application,
    chat_body: dict,
    log: Logger | None = None,
    session_id: str | None = None,
) -> ClientResponse | web.Response:
    """Send request with primary/backup fallback and circuit breaker.

    Tries primary upstream first. If the failure is retryable
    (timeout, connection error, 5xx) and a backup is configured,
    retries once on the backup upstream.

    Circuit breaker: after CB_THRESHOLD consecutive retryable failures
    on primary, the breaker opens and skips primary for CB_COOLDOWN_SEC
    seconds, falling back directly. After cooldown, one probe request
    is sent to primary to see if it recovered.
    """
    cb_threshold = app.get("cb_threshold", 3)
    cb_cooldown = app.get("cb_cooldown_sec", 300)

    # Check circuit breaker state
    cb_open_until = app.get("_cb_open_until", 0.0)
    cb_failures = app.get("_cb_failures", 0)
    now = time.time()

    if cb_open_until > now and _has_backup(app):
        # Breaker open — skip primary, go straight to backup
        log and log.warn("upstream.circuit_breaker_open", {
            "failures": cb_failures,
            "retry_at": cb_open_until,
            "fallback_to": app.get("backup_upstream_base_url", ""),
        })
        backup_url = _build_backup_url(app)
        backup_headers = _build_backup_headers(app, session_id=session_id)
        backup_body = _backup_chat_body(chat_body, app)
        return await send_chat_request(app, backup_body, log, url_override=backup_url, headers_override=backup_headers, session_id=session_id)

    # Step 1: Try primary
    resp = await send_chat_request(app, chat_body, log, session_id=session_id)
    if _is_success(resp):
        # Success — reset breaker
        app["_cb_failures"] = 0
        app["_cb_open_until"] = 0.0
        return resp

    if _is_retryable(resp) and _has_backup(app):
        # Retryable failure — increment counter
        app["_cb_failures"] = cb_failures + 1
        if app["_cb_failures"] >= cb_threshold:
            app["_cb_open_until"] = now + cb_cooldown
            log and log.warn("upstream.circuit_breaker_tripped", {
                "failures": app["_cb_failures"],
                "cooldown_sec": cb_cooldown,
                "open_until": app["_cb_open_until"],
            })

        log and log.warn("upstream.fallback", {
            "reason": _error_code(resp),
            "from": app.get("upstream_base_url", ""),
            "to": app.get("backup_upstream_base_url", ""),
        })
        backup_url = _build_backup_url(app)
        backup_headers = _build_backup_headers(app, session_id=session_id)
        backup_body = _backup_chat_body(chat_body, app)
        resp = await send_chat_request(app, backup_body, log, url_override=backup_url, headers_override=backup_headers, session_id=session_id)

    return resp


def _is_success(resp: ClientResponse | web.Response) -> bool:
    """< 400 from upstream is success; web.Response means send_chat_request already errored."""
    if isinstance(resp, web.Response):
        return False
    return resp.status < 400


def _is_retryable(resp: ClientResponse | web.Response) -> bool:
    """Timeout (504), connection error (502), or upstream 5xx."""
    if isinstance(resp, web.Response):
        return resp.status in (502, 504)
    return resp.status >= 500


def _error_code(resp: ClientResponse | web.Response) -> str:
    if isinstance(resp, web.Response):
        body = resp.body
        if isinstance(body, bytes):
            try:
                import json
                payload = json.loads(body)
                return payload.get("error", {}).get("code", f"HTTP_{resp.status}")
            except Exception:
                pass
        return f"HTTP_{resp.status}"
    return f"HTTP_{resp.status}"


def _has_backup(app: web.Application) -> bool:
    enabled = app.get("backup_enabled", True)
    return enabled and bool(app.get("backup_upstream_base_url", ""))


def _build_backup_url(app: web.Application) -> str:
    base_url = app.get("backup_upstream_base_url", "")
    return f"{base_url.rstrip('/')}/chat/completions"


def _build_backup_headers(app: web.Application, session_id: str | None = None) -> dict:
    api_key = app.get("backup_api_key", "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    sid = session_id or app.get("default_session_id")
    if sid:
        headers["x-opencode-session"] = sid
    return headers


def _backup_chat_body(chat_body: dict, app: web.Application) -> dict:
    """Create a copy of chat_body with backup upstream's model name."""
    body = dict(chat_body)
    backup_model = app.get("backup_backend_model", "")
    if backup_model:
        body["model"] = backup_model
    return body


def extract_usage(data: dict | None) -> dict | None:
    """Extract usage stats from upstream response data.

    Handles both OpenAI format (prompt_tokens/completion_tokens)
    and Responses API format (input_tokens/output_tokens).
    """
    if not data or not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not usage or not isinstance(usage, dict):
        return None
    return {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
        "total_tokens": usage.get("total_tokens", 0),
    }
