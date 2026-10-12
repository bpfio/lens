"""Tests for audit report fixes (P0 security, P1 protocol contracts, resource limits)."""

import json
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aperture.index import create_app, PUBLIC_UNAUTHENTICATED_PATHS, auth_middleware
from aperture.handlers.chat import filter_chat_stream
from aperture.translators.anthropic import translate_anthropic_to_chat
from aperture.passthrough import (
    filter_downstream_headers,
    build_passthrough_url,
    rewrite_request_body,
)
from aperture.config import CHAT_DISPLAY_MODEL


class MockContentStream:
    """Mock aiohttp response content for SSE streaming tests."""
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class MockClientResponse:
    """Mock ClientResponse for filter_chat_stream."""
    def __init__(self, chunks):
        self.content = MockContentStream(chunks)


@pytest.mark.asyncio
async def test_auth_blocks_unauthenticated_get_for_non_whitelist():
    """Ensure non-whitelisted GET requests are rejected with 401."""
    app = create_app()
    app["api_key"] = "test-secret"

    for path in ["/v1/chat/completions", "/v1/responses", "/v1/unknown", "/anything"]:
        req = make_mocked_request("GET", path, app=app)
        async def dummy_handler(request):
            return web.Response(text="ok")

        resp = await auth_middleware(req, dummy_handler)
        assert resp.status == 401, f"Path {path} should require auth"


@pytest.mark.asyncio
async def test_auth_allows_whitelisted_paths():
    """Ensure whitelisted paths allow unauthenticated GET."""
    app = create_app()
    app["api_key"] = "test-secret"

    for path in ["/health", "/readyz", "/v1/models", "/managed/settings"]:
        req = make_mocked_request("GET", path, app=app)
        async def dummy_handler(request):
            return web.Response(text="ok")

        resp = await auth_middleware(req, dummy_handler)
        assert resp.status == 200, f"Path {path} should be whitelisted"


@pytest.mark.asyncio
async def test_auth_no_lan_bypass():
    """Ensure 192.168.x.x addresses cannot bypass authentication."""
    app = create_app()
    app["api_key"] = "test-secret"

    req = make_mocked_request(
        "POST",
        "/v1/chat/completions",
        headers={"X-Forwarded-For": "192.168.1.100"},
        app=app,
    )
    async def dummy_handler(request):
        return web.Response(text="ok")

    resp = await auth_middleware(req, dummy_handler)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_auth_fail_closed_when_no_api_key(monkeypatch):
    """When API_KEY is unset and local bypass is disabled, requests must fail with 500."""
    monkeypatch.setenv("AUTH_ALLOW_LOCAL", "false")
    monkeypatch.delenv("API_KEY", raising=False)
    app = create_app()
    app["api_key"] = ""

    req = make_mocked_request("POST", "/v1/chat/completions", app=app)
    async def dummy_handler(request):
        return web.Response(text="ok")

    resp = await auth_middleware(req, dummy_handler)
    assert resp.status == 500


@pytest.mark.asyncio
async def test_chat_sse_model_override_without_reasoning():
    """Ensure parsed model is replaced with CHAT_DISPLAY_MODEL even when chunk has no reasoning."""
    upstream_chunk = (
        b'data: {"id":"1","object":"chat.completion.chunk","model":"qwen-backend",'
        b'"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
    )
    resp = MockClientResponse([upstream_chunk])

    out = []
    async for line in filter_chat_stream(resp):
        out.append(line)

    non_empty = [line for line in out if line.strip()]
    assert len(non_empty) == 1
    assert non_empty[0].startswith("data: ")
    data = json.loads(non_empty[0][6:])
    assert data["model"] == CHAT_DISPLAY_MODEL
    assert "qwen-backend" not in non_empty[0]
    assert data["choices"][0]["delta"]["content"] == "Hello"


@pytest.mark.asyncio
async def test_chat_sse_drops_pure_reasoning_chunk():
    """Ensure chunk carrying only reasoning_content is cleanly dropped."""
    reasoning_chunk = (
        b'data: {"id":"1","object":"chat.completion.chunk","model":"qwen-backend",'
        b'"choices":[{"index":0,"delta":{"reasoning_content":"Thinking..."},"finish_reason":null}]}\n\n'
    )
    resp = MockClientResponse([reasoning_chunk])

    out = []
    async for line in filter_chat_stream(resp):
        out.append(line)

    non_empty = [line for line in out if line.strip()]
    assert len(non_empty) == 0


def test_anthropic_default_stream_is_false():
    """Ensure missing stream field defaults to False, not True."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    result = translate_anthropic_to_chat(body)
    assert result["stream"] is False


def test_anthropic_max_tokens_not_clamped():
    """Ensure small max_tokens is preserved and not clamped to 1024."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 15,
    }
    result = translate_anthropic_to_chat(body)
    assert result["max_tokens"] == 15


def test_filter_downstream_headers_strips_content_encoding():
    """Ensure content-encoding is stripped so client doesn't decompress uncompressed body."""
    upstream_headers = {
        "content-encoding": "gzip",
        "content-type": "application/json",
        "x-request-id": "req-123",
        "transfer-encoding": "chunked",
    }
    filtered = filter_downstream_headers(upstream_headers)
    assert "content-encoding" not in filtered
    assert "transfer-encoding" not in filtered
    assert "content-type" not in filtered
    assert filtered.get("x-request-id") == "req-123"


def test_build_passthrough_url_mimo_unknown_route(monkeypatch):
    """Ensure mimo flavor preserves route without duplicating /v1."""
    monkeypatch.setenv("UPSTREAM_FLAVOR", "mimo")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
    app = create_app()

    req = make_mocked_request("GET", "/v1/models?limit=10", app=app)
    url = build_passthrough_url(app, req, "models")
    assert url == "https://token-plan-cn.xiaomimimo.com/v1/models?limit=10"
    assert "/v1/v1/" not in url


def test_rewrite_request_body_no_model_injection():
    """Ensure non-generative POST body without model does not get a model field injected."""
    body = {"purpose": "fine-tune", "file_id": "file-123"}
    rewritten, orig = rewrite_request_body(body, "mimo-v2.6-flash", "files")
    assert "model" not in rewritten
    assert orig == ""


def test_app_client_max_size_configured(monkeypatch):
    """Ensure create_app sets client_max_size to configured MAX_BODY_SIZE_MB."""
    monkeypatch.setenv("MAX_BODY_SIZE_MB", "16")
    app = create_app()
    assert app._client_max_size == 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_filter_chat_stream_buffer_overflow(monkeypatch):
    """Ensure filter_chat_stream aborts with RuntimeError when chunk stream exceeds MAX_SSE_BUFFER_BYTES."""
    monkeypatch.setenv("MAX_SSE_BUFFER_BYTES", "64")
    # Feed a chunk that exceeds 64 bytes
    huge_chunk = b"data: " + (b"x" * 100) + b"\n\n"
    resp = MockClientResponse([huge_chunk])

    with pytest.raises(RuntimeError, match="SSE buffer exceeded"):
        async for _ in filter_chat_stream(resp):
            pass
