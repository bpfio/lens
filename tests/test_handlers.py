"""Integration tests for handlers using aiohttp TestClient."""

import json
import pytest
from aperture.index import create_app


@pytest.fixture
def app():
    import os
    os.environ["API_KEY"] = "sk-test"
    os.environ["DEFAULT_MODEL"] = "test-model"
    os.environ["UPSTREAM_BASE_URL"] = "http://localhost:99999"
    return create_app()


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_models_endpoint(client):
    resp = await client.get("/v1/models")
    assert resp.status == 200
    data = await resp.json()
    assert "data" in data
    assert isinstance(data["data"], list)
    assert len(data["data"]) > 0
    # Verify default model is listed
    default = data["data"][0]
    assert default["object"] == "model"


@pytest.mark.asyncio
async def test_health_endpoint(client):
    """Health check returns ok status."""
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_readyz_endpoint(client):
    """Readyz returns same as health."""
    resp = await client.get("/readyz")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_managed_settings_endpoint(client):
    """Managed settings returns expected schema for Claude Code."""
    resp = await client.get("/managed/settings")
    assert resp.status == 200
    data = await resp.json()
    assert "uuid" in data
    assert "checksum" in data
    assert "settings" in data
    assert isinstance(data["settings"], dict)


@pytest.mark.asyncio
async def test_api_hello_endpoint(client):
    """API hello probing returns ok for HEAD and GET."""
    resp_head = await client.head("/api/hello")
    assert resp_head.status == 200

    resp_get = await client.get("/api/hello")
    assert resp_get.status == 200
    text = await resp_get.text()
    assert text == "ok"


@pytest.mark.asyncio
async def test_chat_no_auth_returns_401(client):
    resp = await client.post("/v1/chat/completions", json={
        "model": "test",
        "messages": [{"role": "user", "content": "Hi"}],
    })
    assert resp.status == 401


@pytest.mark.asyncio
async def test_chat_with_valid_auth(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
        headers={"Authorization": "Bearer sk-test"},
    )
    # Auth passes — no real upstream so should get 502
    assert resp.status in (200, 502)


@pytest.mark.asyncio
async def test_anthropic_route_detection(client):
    resp = await client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status in (200, 502)


@pytest.mark.asyncio
async def test_request_id_in_logging(client, capsys):
    """Verify logging middleware runs and outputs structured logs."""
    resp = await client.get("/v1/models")
    assert resp.status == 200
    captured = capsys.readouterr()
    # Should have request.start and request.end log lines
    assert "request.start" in captured.out
    assert "request.end" in captured.out
    # Should contain a request_id
    lines = captured.out.strip().split("\n")
    parsed = [json.loads(l) for l in lines]
    request_ids = set(l["requestId"] for l in parsed if "requestId" in l)
    assert len(request_ids) >= 1
    for rid in request_ids:
        assert rid.startswith("req_")
