"""Unit tests for passthrough module."""

import json
import pytest
from aiohttp import web
from aperture.passthrough import get_upstream_urls, get_upstream_headers, rewrite_request_body


def test_get_upstream_urls():
    app = {"upstream_base_url": "https://api.example.com/v1"}
    urls = get_upstream_urls(app)
    assert urls["responses"] == "https://api.example.com/v1/responses"
    assert urls["chat"] == "https://api.example.com/v1/chat/completions"
    assert urls["anthropic"] == "https://api.example.com/anthropic/v1/messages"


def test_get_upstream_headers_responses():
    app = {"api_key": "sk-secret-test"}
    headers = get_upstream_headers(app, "responses")
    assert headers["Authorization"] == "Bearer sk-secret-test"
    assert headers["x-openai-internal-codex-responses-lite"] == "true"


def test_get_upstream_headers_anthropic():
    app = {"api_key": "sk-secret-test"}
    headers = get_upstream_headers(app, "anthropic")
    assert headers["x-api-key"] == "sk-secret-test"
    assert headers["anthropic-version"] == "2023-06-01"


def test_rewrite_request_body_model():
    body = {"model": "gpt-5.6-sol", "input": "Hello"}
    rewritten, original = rewrite_request_body(body, "mimo-v2.6-flash", "responses")
    assert original == "gpt-5.6-sol"
    assert rewritten["model"] == "mimo-v2.6-flash"
    assert rewritten["input"] == "Hello"


def test_rewrite_request_body_preserves_web_search_by_default(monkeypatch):
    monkeypatch.delenv("FILTER_WEB_SEARCH", raising=False)
    body = {
        "model": "gpt-5.6-sol",
        "tools": [
            {"type": "function", "name": "bash"},
            {"type": "web_search"},
            {"type": "web_search_preview"},
        ],
    }
    rewritten, original = rewrite_request_body(body, "mimo-v2.6-flash", "responses")
    assert original == "gpt-5.6-sol"
    assert len(rewritten["tools"]) == 3


def test_rewrite_request_body_filters_web_search_when_enabled(monkeypatch):
    monkeypatch.setenv("FILTER_WEB_SEARCH", "true")
    body = {
        "model": "gpt-5.6-sol",
        "tools": [
            {"type": "function", "name": "bash"},
            {"type": "web_search"},
            {"type": "web_search_preview"},
        ],
    }
    rewritten, original = rewrite_request_body(body, "mimo-v2.6-flash", "responses")
    assert original == "gpt-5.6-sol"
    assert len(rewritten["tools"]) == 1
    assert rewritten["tools"][0]["name"] == "bash"
