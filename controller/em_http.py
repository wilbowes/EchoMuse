"""
JSON responses and request-body checks shared by every HTTP route.

One response shape for the whole API: success is the payload itself, failure
is `{"error": <message>, "code": <code>}` with the status set. The dashboard
reads `code` to decide what to show, so a handler never builds its own.
"""

from __future__ import annotations

import json

from aiohttp import web


def ok(data, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps(data),
    )


def error(code: str, message: str, status: int) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps({"error": message, "code": code}),
    )


async def json_body(request: web.Request) -> dict:
    """
    Parse the request body as JSON.
    Returns 400 if body is missing or not valid JSON.
    """
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            content_type="application/json",
            text=json.dumps({
                "error": "Request body must be valid JSON",
                "code":  "invalid_json",
            }),
        )


def require_str(body: dict, key: str) -> str:
    """Extract a required string field from a parsed JSON body."""
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise web.HTTPBadRequest(
            content_type="application/json",
            text=json.dumps({
                "error": f"Missing or empty required field: {key}",
                "code":  "missing_field",
            }),
        )
    return value.strip()
