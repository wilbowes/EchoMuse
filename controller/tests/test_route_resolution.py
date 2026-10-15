"""
Every route must resolve to its own handler.

aiohttp (3.10 on) indexes routes by their literal prefix and tries the most
specific first, so `/api/devices/pending` wins over `/api/devices/{id}`
whatever the order they were added in. Order still decides between
parameterised routes under the same prefix: `/api/devices/{id}/{what}` added
ahead of `/api/devices/{id}/config` takes its requests, and nothing fails.
With each route area registering its own routes, nobody sees the whole order
in one place, so it is checked here: a concrete URL is built for every route
and resolved through the real router.
"""

import asyncio
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import em_api


@pytest.fixture(scope="module")
def app():
    return asyncio.run(em_api.create_app())


def _routes(app):
    for resource in app.router.resources():
        info = resource.get_info()
        path = info.get("path") or info.get("formatter")
        if path is None:            # the static directory
            continue
        for route in resource:
            if route.method == "HEAD":   # added alongside every GET
                continue
            yield route.method, path, route.handler


def test_the_table_was_found(app):
    # Guards the guard: nothing to resolve must not read as nothing wrong.
    routes = list(_routes(app))
    assert len(routes) > 50
    assert any(p == "/api/devices/pending" for _, p, _ in routes)


def test_every_route_resolves_to_its_own_handler(app):
    wrong = []
    for method, path, handler in _routes(app):
        url = re.sub(r"\{[^}]+\}", "x1", path)
        match = asyncio.run(app.router.resolve(make_mocked_request(method, url)))
        if match.handler is not handler:
            got = getattr(match.handler, "__name__", match.handler)
            wrong.append(f"{method} {path} -> {got}, not {handler.__name__}")
    assert not wrong, "routes shadowed by an earlier registration: " + "; ".join(wrong)
