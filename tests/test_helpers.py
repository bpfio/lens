"""Tests for aperture/helpers.py."""

from aperture.helpers import (
    cors_headers,
    extract_text,
    now,
    uid,
)


class TestUid:
    def test_uid_generates_unique(self):
        ids = {uid() for _ in range(100)}
        assert len(ids) == 100

    def test_uid_with_prefix(self):
        result = uid(prefix="test-")
        assert result.startswith("test-")


class TestNow:
    def test_now_returns_int(self):
        result = now()
        assert isinstance(result, int)


class TestExtractText:
    def test_extract_text_string(self):
        assert extract_text("hello") == "hello"

    def test_extract_text_list_blocks(self):
        blocks = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]
        assert extract_text(blocks) == "Hello world"

    def test_extract_text_ignores_thinking(self):
        blocks = [
            {"type": "text", "text": "Hello "},
            {"type": "thinking", "text": "I should think about this"},
            {"type": "text", "text": "world"},
            {"type": "redacted_thinking", "text": "redacted"},
        ]
        assert extract_text(blocks) == "Hello world"

    def test_extract_text_none(self):
        assert extract_text(None) == ""


class TestCorsHeaders:
    def test_cors_headers_default(self):
        headers = cors_headers()
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
        assert "Access-Control-Allow-Headers" in headers

    def test_cors_headers_with_extra(self):
        headers = cors_headers(extra={"X-Custom": "value"})
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert headers["X-Custom"] == "value"


class TestExtractSessionId:
    def test_extract_from_opencode_header(self):
        from unittest.mock import MagicMock
        from aperture.helpers import extract_session_id
        req = MagicMock()
        req.headers = {"x-opencode-session": "sess-opencode"}
        req.app = {}
        assert extract_session_id(req) == "sess-opencode"

    def test_extract_from_claude_code_header(self):
        from unittest.mock import MagicMock
        from aperture.helpers import extract_session_id
        req = MagicMock()
        req.headers = {"x-claude-code-session-id": "sess-claude"}
        req.app = {}
        assert extract_session_id(req) == "sess-claude"

    def test_extract_fallback_to_default(self):
        from unittest.mock import MagicMock
        from aperture.helpers import extract_session_id
        req = MagicMock()
        req.headers = {}
        req.app = {"default_session_id": "sess-default"}
        assert extract_session_id(req) == "sess-default"

