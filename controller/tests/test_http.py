"""
em_http: one response shape for the whole API. The dashboard reads `code` to
decide what to show, so an error without it is an error it cannot explain.
"""

import asyncio
import json

import pytest
from aiohttp import web

import em_http


def test_ok_is_the_payload_itself():
    r = em_http.ok([1, 2], status=201)
    assert r.status == 201 and r.content_type == "application/json"
    assert json.loads(r.text) == [1, 2]


def test_error_carries_message_and_code():
    r = em_http.error("not_found", "No such device", 404)
    assert r.status == 404
    assert json.loads(r.text) == {"error": "No such device", "code": "not_found"}


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return json.loads(self._body)


def test_a_body_that_is_not_json_is_a_400_with_a_code():
    with pytest.raises(web.HTTPBadRequest) as e:
        asyncio.run(em_http.json_body(_Req("{not json")))
    assert json.loads(e.value.text)["code"] == "invalid_json"


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": 3}])
def test_a_missing_or_blank_field_is_a_400_naming_it(body):
    with pytest.raises(web.HTTPBadRequest) as e:
        em_http.require_str(body, "name")
    assert json.loads(e.value.text) == {
        "error": "Missing or empty required field: name", "code": "missing_field"}


def test_a_present_field_is_returned_trimmed():
    assert em_http.require_str({"name": "  Kitchen "}, "name") == "Kitchen"
