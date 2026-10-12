"""API key authentication middleware.

Mirrors JS src/middleware/auth.js.

Validates requests via Authorization: Bearer <key> or x-api-key header.
Returns None (allowed) or a 401 web.Response (denied).
"""

import hmac
from aiohttp import web
from ..helpers import error_response


def authenticate(request: web.Request, api_key: str | None) -> web.Response | None:
    """Validate the client's API key.

    Args:
        request: The incoming HTTP request.
        api_key: The configured API_KEY from env (None/empty = allow all).

    Returns:
        None if authenticated, or a 401 web.Response.
    """
    if not api_key:
        return None

    auth_header = request.headers.get("Authorization", "")
    token = request.headers.get("x-api-key", "")

    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        return error_response(
            "Missing API key. Provide via Authorization: Bearer <key> or x-api-key header.",
            "auth_error",
            "AUTH_REQUIRED",
            401,
        )

    if not hmac.compare_digest(token, api_key):
        return error_response(
            "Invalid API key.",
            "auth_error",
            "AUTH_INVALID",
            401,
        )

    return None
