"""
feature_flags.py — voice_assistant_feature_flags bit values
==============================================================

DeviceInfoResponse.voice_assistant_feature_flags (api_pb2.py, TYPE_UINT32
on the wire) is a bitflag, not a protobuf enum, so it isn't present in
the vendored api_pb2.py/api_options_pb2.py descriptors. The actual flag
values live in aioesphomeapi/model.py as a plain Python enum.IntFlag.

That file is ~2100 lines covering the full ESPHome entity model (climate,
bluetooth proxy, z-wave proxy, etc) — vendoring the whole module for one
7-line enum isn't worth it, so the values are reproduced directly here.
Source: aioesphomeapi==45.3.1, aioesphomeapi/model.py, class
VoiceAssistantFeature(enum.IntFlag). Cross-check against a newer
aioesphomeapi release if these ever need updating — the bit positions are
part of the wire protocol contract, not expected to change, but don't
assume that without checking.
"""

import enum


class VoiceAssistantFeature(enum.IntFlag):
    VOICE_ASSISTANT = 1 << 0
    SPEAKER = 1 << 1
    API_AUDIO = 1 << 2
    TIMERS = 1 << 3
    ANNOUNCE = 1 << 4
    START_CONVERSATION = 1 << 5
    MULTI_CHANNEL_AUDIO = 1 << 6


class BluetoothProxyFeature(enum.IntFlag):
    """
    Source: aioesphomeapi/model.py, class BluetoothProxyFeature(enum.IntFlag).
    Advertised via DeviceInfoResponse.bluetooth_proxy_feature_flags.

    EchoMuse advertises PASSIVE_SCAN | RAW_ADVERTISEMENTS only — passive
    advertisement forwarding (Bermuda, advert-based sensors). Active GATT
    connections are a future lift (the raw HCI transport supports it, the
    Go side doesn't).
    """

    PASSIVE_SCAN = 1 << 0
    ACTIVE_CONNECTIONS = 1 << 1
    REMOTE_CACHING = 1 << 2
    PAIRING = 1 << 3
    CACHE_CLEARING = 1 << 4
    RAW_ADVERTISEMENTS = 1 << 5
    FEATURE_STATE_AND_MODE = 1 << 6


class MediaPlayerEntityFeature(enum.IntFlag):
    """
    Source: aioesphomeapi/model.py, class MediaPlayerEntityFeature(enum.IntFlag).

    Added when we found (empirically, against a real HA Core instance —
    see EchoMuse session notes) that HA's ESPHome integration does not
    surface a device in Devices & Services at all if it reports zero
    entities via ListEntitiesRequest, even with voice_assistant_feature_flags
    set on DeviceInfoResponse. linux-voice-assistant's satellite.py always
    provisions a media_player entity for this reason — apparently load-
    bearing, not just a nice-to-have. We're reproducing the minimal set of
    flags linux-voice-assistant actually advertises (PAUSE, STOP, PLAY_MEDIA,
    VOLUME_SET, VOLUME_MUTE, MEDIA_ANNOUNCE), not the full enum, since most
    of these bit positions are irrelevant to a voice satellite.
    """

    PAUSE = 1 << 0
    VOLUME_SET = 1 << 2
    VOLUME_MUTE = 1 << 3
    STOP = 1 << 12
    PLAY = 1 << 14
    MEDIA_ANNOUNCE = 1 << 20


class MediaPlayerState(enum.IntEnum):
    """Source: aioesphomeapi/model.py, class MediaPlayerState(APIIntEnum)."""

    NONE = 0
    IDLE = 1
    PLAYING = 2
    PAUSED = 3
    ANNOUNCING = 4
    OFF = 5
    ON = 6
