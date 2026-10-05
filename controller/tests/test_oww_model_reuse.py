"""
#512: a new OWWModel was built on every device reconnect and never freed.

A fresh Device object is created per connection, so a model cached on the
Device is discarded and rebuilt each reconnect — three ONNX sessions, tens of
MB, per reconnect, none freed. On a flaky link that leaked ~1 GB in 2.8 days
(34 reconnects → 34 models). The fix caches models module-side, keyed by
device_id, reused across reconnects.

em_controller is deliberately not importable here (see conftest) — the model
helpers construct an OWWModel, which needs openwakeword. These are shape guards
on the shipped source, matching test_wake_watchdog.py's approach.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _controller_src() -> str:
    return (CONTROLLER / "em_controller.py").read_text()


def _api_src() -> str:
    return (CONTROLLER / "em_api.py").read_text()


def _listener_src() -> str:
    src = _controller_src()
    start = src.index("async def _stream_listen")
    end = src.index("async def ", start + 1)
    return src[start:end]


def test_wake_and_barge_have_separate_module_caches():
    src = _controller_src()
    assert "_oww_models:" in src and "_oww_barge_models:" in src, \
        "wake and barge models need separate module-level caches"


def test_caches_are_keyed_by_device_id_alone():
    # Keying by (device_id, model_name[, speex]) would leave a renamed wake
    # word's old model cached for the process's life — a smaller version of
    # the leak this fixes. The store/lookup must use device_id alone.
    src = _controller_src()
    for store in ("_oww_models[device.device_id] =",
                  "_oww_barge_models[device.device_id] ="):
        assert store in src, f"expected a device_id-keyed write: {store}"


def test_wake_listener_acquires_from_cache_not_a_bare_constructor():
    # The whole bug was `OWWModel(...)` inline in the listener. Both the initial
    # load and the reload path must go through the cache helper instead.
    listener = _listener_src()
    assert "_acquire_wake_model(" in listener, \
        "the listener must acquire its model from the cache"
    assert "OWWModel(" not in listener, \
        "the listener must not construct OWWModel directly — that is the leak"


def test_reused_model_is_reset_but_a_fresh_one_is_not():
    # openwakeword's reset() re-seeds the feature buffer with random-noise
    # embeddings exactly as the constructor does, so a reused model must be
    # reset to equal a fresh build — but a freshly built one must NOT be, or
    # the un-warmed WarmupGate beside it would be wrong.
    src = _controller_src()
    acq = src.index("async def _acquire_wake_model")
    body = src[acq:src.index("async def ", acq + 1)]
    hit = body.index("_reset_wake_model(model)")
    build = body.index("OWWModel(")
    assert hit < build, \
        "the cache hit must reset() before the miss path builds a new model"


def test_reset_never_runs_on_the_event_loop():
    # reset() embeds 4s of noise: ~400ms on the HA host, which stalled every
    # Echo's audio and control plane on each controller-scored wake. Every
    # reset goes through the executor helper.
    src = _controller_src()
    assert src.count("run_in_executor(None, model.reset)") == 1
    assert ".reset()\n" not in "".join(
        line + "\n" for line in src.splitlines()
        if "model" in line and not line.lstrip().startswith("#")
        and "warmup" not in line)


def test_wake_routing_is_not_held_behind_the_reset():
    # Frames that arrive while the reset runs must already route to the turn;
    # awaiting it before oww_paused.set() drains the start of the command.
    listener = _listener_src()
    start = listener.index("model_reset = _reset_wake_model(model)")
    route = listener.index("device.oww_paused.set()", start)
    assert "await model_reset" not in listener[start:route]


def test_superseded_listener_stands_down_before_scoring():
    # The model is shared via the cache, so on a reconnect the old listener
    # must not score into it alongside the new one. Only the listener still in
    # _devices may run; a superseded one returns at the top of the loop, before
    # the frame fetch and predict below it.
    listener = _listener_src()
    guard = listener.index("if _devices.get(device.device_id) is not device:")
    predict = listener.index("model.predict")
    assert guard < predict, \
        "the staleness guard must sit above the predict call"
    # It must actually stop the loop, not just log.
    after = listener[guard:predict]
    assert "return" in after.split("\n", 3)[1], \
        "a superseded listener must return, not fall through to scoring"


def test_delete_device_evicts_the_cached_models():
    # A deleted device must not keep its models for the life of the process.
    api = _api_src()
    delete = api.index("async def _delete_device")
    body = api[delete:api.index("async def ", delete + 1)]
    assert "_forget_oww_models(" in body, \
        "_delete_device must evict the device's cached OWW models"


def test_barge_watcher_uses_the_cache():
    src = _controller_src()
    watcher = src[src.index("async def _barge_watcher"):]
    watcher = watcher[:watcher.index("async def ", 1)]
    assert "_acquire_barge_model(" in watcher, \
        "the barge watcher must acquire its model from the cache"
    assert "OWWModel(" not in watcher, \
        "the barge watcher must not construct OWWModel directly"
