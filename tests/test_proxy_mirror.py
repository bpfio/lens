"""Comprehensive tests for Lens transparent proxy mirroring and protocol routing."""

import json
import pytest
from aiohttp import web
from aperture.index import create_app


@pytest.fixture
async def mock_proxy_upstream(aiohttp_server):
    """Start a multi-method mock upstream server recording incoming requests."""
    recorded_requests = []

    async def generic_handler(request: web.Request):
        body = None
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = await request.text()

        req_info = {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": dict(request.headers),
            "body": body,
        }
        recorded_requests.append(req_info)

        resp_headers = {
            "x-ratelimit-remaining": "88",
            "x-ratelimit-limit": "100",
            "retry-after": "5",
            "x-trace-id": "trace-upstream-123",
        }

        if request.method == "DELETE":
            return web.Response(status=204, headers=resp_headers)

        if request.path.endswith("/stream"):
            stream_resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", **resp_headers},
            )
            await stream_resp.prepare(request)
            chunk = json.dumps({"model": body.get("model", "") if isinstance(body, dict) else "", "content": "hello stream"})
            await stream_resp.write(f"data: {chunk}\n\n".encode())
            await stream_resp.write(b"data: [DONE]\n\n")
            return stream_resp

        # Return json echoing request info
        resp_data = {
            "upstream_echo": True,
            "method": request.method,
            "path": request.path,
            "model": body.get("model", "") if isinstance(body, dict) else "",
        }
        return web.json_response(resp_data, headers=resp_headers)

    up_app = web.Application()
    up_app.router.add_route("*", "/{path:.*}", generic_handler)
    server = await aiohttp_server(up_app, port=None)
    server.recorded = recorded_requests
    return server


@pytest.mark.asyncio
async def test_full_method_and_query_mirroring(aiohttp_client, mock_proxy_upstream, monkeypatch):
    """Verify GET, DELETE, and Query parameters are cleanly mirrored to upstream."""
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("UPSTREAM_BASE_URL", f"http://localhost:{mock_proxy_upstream.port}/v1")
    monkeypatch.setenv("AUTH_ALLOW_LOCAL", "false")

    app = create_app()
    client = await aiohttp_client(app)

    # 1. Test GET with query parameters
    get_resp = await client.get(
        "/v1/models/custom-model-42?limit=10&cursor=cur_abc",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert get_resp.status == 200
    assert get_resp.headers.get("x-ratelimit-remaining") == "88"
    assert get_resp.headers.get("x-trace-id") == "trace-upstream-123"

    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["method"] == "GET"
    assert last_req["path"] == "/v1/models/custom-model-42"
    assert last_req["query"] == {"limit": "10", "cursor": "cur_abc"}

    # 2. Test DELETE
    del_resp = await client.delete(
        "/v1/files/file-xyz-999",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert del_resp.status == 204
    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["method"] == "DELETE"
    assert last_req["path"] == "/v1/files/file-xyz-999"


@pytest.mark.asyncio
async def test_bidirectional_header_mirroring(aiohttp_client, mock_proxy_upstream, monkeypatch):
    """Verify tracing and tenant headers pass upstream, and rate-limit headers pass downstream."""
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("UPSTREAM_BASE_URL", f"http://localhost:{mock_proxy_upstream.port}/v1")

    app = create_app()
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/embeddings",
        json={"input": "test embedding", "model": "text-embedding-3-small"},
        headers={
            "Authorization": "Bearer sk-test",
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "x-custom-tenant": "org-unit-dev",
        },
    )
    assert resp.status == 200
    # Downstream headers received by client
    assert resp.headers.get("x-ratelimit-remaining") == "88"
    assert resp.headers.get("retry-after") == "5"

    # Upstream headers received by mock upstream
    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["headers"].get("traceparent") == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert last_req["headers"].get("x-custom-tenant") == "org-unit-dev"


@pytest.mark.asyncio
async def test_upstream_capabilities_native_passthrough(aiohttp_client, mock_proxy_upstream, monkeypatch):
    """Verify that when upstream supports responses/anthropic, requests pass natively without translation."""
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("UPSTREAM_BASE_URL", f"http://localhost:{mock_proxy_upstream.port}/v1")
    monkeypatch.setenv("UPSTREAM_CAPABILITIES", "responses,anthropic")
    monkeypatch.setenv("MODEL_OVERRIDE_MODE", "passthrough")

    app = create_app()
    client = await aiohttp_client(app)

    # Responses API should pass natively to /v1/responses
    resp = await client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-sol", "input": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status == 200
    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["path"] == "/v1/responses"
    assert last_req["body"]["model"] == "gpt-5.6-sol"
    assert "x-openai-internal-codex-responses-lite" in last_req["headers"]

    # Anthropic Messages API should pass natively to /v1/messages
    resp_anthropic = await client.post(
        "/v1/messages",
        json={"model": "claude-3-7-sonnet-20250219", "messages": [{"role": "user", "content": "hello"}]},
        headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
    )
    assert resp_anthropic.status == 200
    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["path"] == "/v1/messages"
    assert last_req["body"]["model"] == "claude-3-7-sonnet-20250219"
    assert last_req["headers"].get("anthropic-version") == "2023-06-01"


@pytest.mark.asyncio
async def test_model_override_mode_mapped(aiohttp_client, mock_proxy_upstream, monkeypatch):
    """Verify MODEL_OVERRIDE_MODE='mapped' rewrites only mapped models and preserves unmapped models."""
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("UPSTREAM_BASE_URL", f"http://localhost:{mock_proxy_upstream.port}/v1")
    monkeypatch.setenv("MODEL_OVERRIDE_MODE", "mapped")
    monkeypatch.setenv("MODEL_MAP", json.dumps({"claude-special": "deepseek-target-model"}))

    app = create_app()
    client = await aiohttp_client(app)

    # 1. Mapped model
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "claude-special", "input": "text"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status == 200
    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["body"]["model"] == "deepseek-target-model"

    # 2. Unmapped model preserved
    resp2 = await client.post(
        "/v1/embeddings",
        json={"model": "keep-my-original-model", "input": "text"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp2.status == 200
    last_req = mock_proxy_upstream.recorded[-1]
    assert last_req["body"]["model"] == "keep-my-original-model"


@pytest.mark.asyncio
async def test_models_list_endpoint_includes_mapped_aliases(aiohttp_client, monkeypatch):
    """Verify GET /v1/models exposes backend model, display models, and configured aliases."""
    monkeypatch.setenv("BACKEND_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ANTHROPIC_DISPLAY_MODEL", "claude-fable-5")
    monkeypatch.setenv("RESPONSES_DISPLAY_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("MODEL_MAP", json.dumps({"my-custom-alias": "deepseek-v4-flash"}))

    app = create_app()
    client = await aiohttp_client(app)

    resp = await client.get("/v1/models")
    assert resp.status == 200
    data = await resp.json()
    model_ids = {m["id"] for m in data.get("data", [])}

    assert "deepseek-v4-flash" in model_ids
    assert "claude-fable-5" in model_ids
    assert "gpt-5.6-sol" in model_ids
    assert "my-custom-alias" in model_ids
