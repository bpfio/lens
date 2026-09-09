"""Bottom-layer utility functions for Aperture."""

import asyncio
import secrets
import time
from typing import Optional

from aiohttp import web
import aiohttp


def uid(prefix: str = "") -> str:
    """Generate a unique identifier using crypto randomness.

    Uses secrets.token_hex(12) for 24-character hex string.
    """
    return prefix + secrets.token_hex(12)


def now() -> int:
    """Return the current Unix timestamp as an integer."""
    return int(time.time())


def extract_text(content: str | list | None) -> str:
    """Extract text from various content formats.

    * str -> returned as-is
    * list[dict] -> joined text from 'text' blocks, skipping thinking blocks
    * None -> empty string
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                texts.append(block.get("text", ""))
            # 'thinking' and 'redacted_thinking' blocks are intentionally skipped
        return "".join(texts)
    return ""


def error_response(
    message: str,
    type_: str,
    code: str,
    status: int,
) -> web.Response:
    """Return a JSON error response with CORS headers."""
    return web.json_response(
        {"error": {"message": message, "type": type_, "code": code}},
        status=status,
        headers=cors_headers(),
    )


def cors_headers(extra: dict | None = None) -> dict:
    """Build CORS headers, optionally merging extra headers."""
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, x-api-key, x-opencode-session, x-claude-code-session-id, session-id, x-session-id",
    }
    if extra:
        headers.update(extra)
    return headers


def extract_session_id(request: web.Request) -> str:
    """Extract session ID from incoming request headers or fallback to app default."""
    for header in (
        "x-opencode-session",
        "x-claude-code-session-id",
        "session-id",
        "x-session-id",
    ):
        val = request.headers.get(header)
        if val:
            return val
    return request.app.get("default_session_id", "")


async def fetch_upstream(
    url: str,
    options: dict,
    timeout_ms: int,
) -> web.Response:
    """Compatibility helper for upstream HTTP requests.

    Returns a 504 web.Response on timeout; re-raises on other errors.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            method = options.get("method", "GET").upper()
            headers = options.get("headers", {})
            body = options.get("body")

            async with session.request(
                method, url, headers=headers, data=body
            ) as resp:
                response_body = await resp.read()
                return web.Response(
                    status=resp.status,
                    body=response_body,
                    headers=dict(resp.headers),
                )
    except asyncio.TimeoutError:
        return web.Response(status=504, text=b"Gateway Timeout")
