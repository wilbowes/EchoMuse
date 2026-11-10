"""
The controller's live, process-wide state: the device registry, shell
session coordination, the OWW model caches, the wake arbiter, the loop
lag peak, and this controller's link ports and TLS directory.

It is a module of its own because em_controller cannot hold it. em_start.py
execvp's em_controller.py, so the running controller is `__main__`, and any
other module that does `import em_controller` loads a SECOND copy whose
state is all defaults (#306: /api/system/status read a loop lag peak of 0.0
while the log beside it showed an 881ms stall). This module is only ever
imported by name, so every reader and writer gets the same object.

Holds state and nothing that acts on it beyond keeping it consistent. No
aiohttp, websockets or openwakeword import, so the suite can import it
without stubs.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import TYPE_CHECKING, Any

import em_arbiter

if TYPE_CHECKING:
    from em_controller import Device
    from openwakeword.model import Model as OWWModel


# The live device registry — keyed by device_id (ro.serialno).
devices: dict[str, "Device"] = {}

# Device id -> the board its firmware reported (em_boards), connected or not.
# Seeded from the database at startup so satellites started before their Echo
# connects show the right model.
boards: dict[str, str] = {}


# Shell session coordination — keyed by device_id.
#
# shell_pending:   Future resolved with the device ws when handle_shell receives it.
# shell_dashboard: dashboard WebSocket set by em_api for interactive sessions.
shell_pending:   dict[str, asyncio.Future] = {}
shell_dashboard: dict[str, Any]            = {}


# OWW model caches — keyed by device_id ALONE (#512).
#
# An OWWModel is expensive: three ONNX InferenceSessions (melspectrogram,
# embedding, wakeword), tens of MB. A NEW Device object is created per
# connection (see handle_control), so a model held on the Device is thrown
# away and rebuilt on every reconnect. On a flaky link that is a leak — 34
# reconnects built 34 models in 2.8 days and none were freed, ~1 GB retained.
#
# Caching module-side, keyed by device_id, lets a reconnecting device reuse its
# model. openwakeword's AudioFeatures.reset() clears the buffers and re-seeds
# feature_buffer with random-noise embeddings exactly as the constructor does,
# so a reset+reused model is equivalent to a freshly built one (and the
# un-warmed WarmupGate beside it stays correct — a reset model is noise-seeded
# just like a fresh one).
#
# device_id ALONE, with the variant stored beside the model: keying by
# (device_id, model_name[, speex]) would leave a renamed wake word's old model
# cached for the life of the process — a smaller version of the same leak.
# Changing the wake word therefore REPLACES the entry, dropping the old model.
# Evicted on device delete and when the device listens privately (see
# forget_oww_models).
#
# The wake listener and the barge watcher get SEPARATE slots: they build the
# model differently (the wake listener with enable_speex_noise_suppression, the
# barge watcher without) and can be live at once, so they must not share one.
oww_models:       dict[str, tuple[str, bool, "OWWModel"]] = {}   # id -> (name, speex, model)
oww_barge_models: dict[str, tuple[str, "OWWModel"]]       = {}   # id -> (name, model)


# Every wake model built, held weakly: the count of models still alive, for
# the log line after a release. Being weak, it keeps nothing alive itself.
live_oww_models: "weakref.WeakSet[OWWModel]" = weakref.WeakSet()


def forget_oww_models(device_id: str) -> bool:
    """Drop a device's cached OWW models. Called when a device is deleted, and
    when it listens privately (nothing here scores it then), so neither keeps
    its models (and their ONNX sessions) for the life of the process."""
    wake = oww_models.pop(device_id, None)
    barge = oww_barge_models.pop(device_id, None)
    return wake is not None or barge is not None


# One arbiter for the fleet — elects a single responder when one
# utterance wakes several Echos (see em_arbiter.py).
wake_arbiter = em_arbiter.WakeArbiter()


# Peak event-loop lag observed since start, in ms (see
# em_controller.event_loop_lag_monitor). Read by the API for
# /api/system/status and the support bundle.
loop_lag_peak_ms: float = 0.0


# Device-link TLS material directory — set by em_controller.main() once
# em_pki.ensure_pki() succeeds. None = TLS listener not running (no
# cryptography package / setup failure); credential endpoints then 503.
tls_dir: str | None = None


def set_tls_dir(path: str) -> None:
    global tls_dir
    tls_dir = path


# This controller's own address and device ports: the defaults for an entry
# in controllerEndpoints, and what the dashboard offers to fill in.
# tls_port is 0 when the wss listener is not running.
link = {"ip": "", "port": 8767, "tls_port": 0}


def set_link_ports(ip: str, port: int, tls_port: int) -> None:
    link.update(ip=ip or "", port=port, tls_port=tls_port)
