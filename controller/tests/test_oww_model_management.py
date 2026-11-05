"""
#905: custom wake word models say who uses them, refuse a delete by naming the
Echo rather than its serial, and stop a runaway uploader at 32.
"""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import em_db as db
import em_oww_models as owm
import em_routes_oww

DEVICE = "G090LF11803611NF"


@pytest.fixture()
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    db.init(str(tmp_path / "echomuse.db"))
    yield tmp_path / owm.MODELS_SUBDIR
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _model(directory, name, size=2048):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    return path


def _device(label, model_path):
    db.register_new_device(DEVICE, "10.0.0.9", "v2.9.0")
    db.set_device_label(DEVICE, label)
    db.set_device_config(
        DEVICE, {**db.DEFAULT_DEVICE_CONFIG, "owwModel": str(model_path)})


def _get_models():
    resp = asyncio.run(em_routes_oww._get_oww_models.__wrapped__(None))
    return {m["file"]: m for m in json.loads(resp.text)["models"]}


def _delete(name):
    req = make_mocked_request(
        "DELETE", f"/api/oww_models/{name}", match_info={"file": name})
    return asyncio.run(em_routes_oww._delete_oww_model.__wrapped__(req))


# ─── who uses each model ─────────────────────────────────────────────────────

def test_the_list_names_the_fleet_default_and_an_echo_by_label(models_dir):
    fleet = _model(models_dir, "hey_house.onnx")
    device = _model(models_dir, "hey_kitchen.onnx", size=4096)
    _model(models_dir, "hey_leftover.onnx")
    db.set_global_device_config({"owwModel": str(fleet)})
    _device("Kitchen", device)

    models = _get_models()
    assert models["hey_house.onnx"]["used_by"] == [owm.FLEET_DEFAULT]
    assert models["hey_kitchen.onnx"]["used_by"] == ["Kitchen"]
    assert models["hey_leftover.onnx"]["used_by"] == []


def test_the_list_carries_the_upload_time_and_size(models_dir):
    _model(models_dir, "hey_house.onnx", size=3072)
    entry = _get_models()["hey_house.onnx"]
    assert entry["size"] == 3072
    assert entry["mtime"] > 0


def test_an_unlabelled_echo_falls_back_to_its_serial(models_dir):
    model = _model(models_dir, "hey_kitchen.onnx")
    db.register_new_device(DEVICE, "10.0.0.9", "v2.9.0")
    db.set_device_config(
        DEVICE, {**db.DEFAULT_DEVICE_CONFIG, "owwModel": str(model)})
    assert _get_models()["hey_kitchen.onnx"]["used_by"] == [DEVICE]


# ─── the delete refusal ──────────────────────────────────────────────────────

def test_delete_refuses_a_model_in_use_and_names_the_echo_by_label(models_dir):
    fleet = _model(models_dir, "hey_house.onnx")
    device = _model(models_dir, "hey_kitchen.onnx")
    db.set_global_device_config({"owwModel": str(fleet)})
    _device("Kitchen", device)

    resp = _delete("hey_kitchen.onnx")
    body = json.loads(resp.text)
    assert resp.status == 409 and body["code"] == "model_in_use"
    assert "Kitchen" in body["error"]
    assert DEVICE not in body["error"], "the refusal must name the label, not the serial"
    assert device.is_file()


def test_an_unused_model_deletes(models_dir):
    leftover = _model(models_dir, "hey_leftover.onnx")
    resp = _delete("hey_leftover.onnx")
    assert resp.status == 200
    assert not leftover.is_file()


# ─── the cap ─────────────────────────────────────────────────────────────────

def test_the_32nd_upload_lands_and_the_33rd_is_refused(models_dir, monkeypatch):
    for i in range(31):
        _model(models_dir, f"old{i}.onnx")
    monkeypatch.setattr(em_routes_oww.em_oww_check, "problem", lambda data: None)

    async def run():
        app = web.Application()
        app.router.add_post(
            "/upload", em_routes_oww._post_oww_model_upload.__wrapped__)

        async def post(client, name):
            form = aiohttp.FormData()
            form.add_field("model", b"model", filename=name)
            r = await client.post("/upload", data=form)
            return r.status, await r.json()

        async with TestClient(TestServer(app)) as client:
            return [await post(client, "new32.onnx"),
                    await post(client, "new33.onnx"),
                    await post(client, "old0.onnx")]

    second, third, replace = asyncio.run(run())
    assert second[0] == 201, second
    assert third[0] == 409 and third[1]["code"] == "model_limit"
    assert str(owm.MAX_MODELS) in third[1]["error"]
    assert len(list(models_dir.glob("*.onnx"))) == owm.MAX_MODELS
    assert not (models_dir / "new33.onnx").exists()
    assert replace[0] == 201, "replacing a model already in the list adds nothing"
    assert len(list(models_dir.glob("*.onnx"))) == owm.MAX_MODELS
