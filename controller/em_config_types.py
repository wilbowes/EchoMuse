"""
The JSON type each config value must have, and what happens when one does not.

Config values were stored exactly as the API received them, and two readers
disagree about what a wrong type means:

  - The device decodes the push into `config.ConfigMessage` and applies it only
    if `json.Unmarshal` returns no error. One mistyped field — `"0.5"` for a
    float, `30.0` for an int, `NaN` — fails the decode, and the WHOLE push is
    dropped with nothing reported. Measured against Go's decoder, not assumed.
  - The controller converts values with `float()`/`int()`/`bool()` when a
    device registers. `float("abc")` raised there, after the device had been
    added, so it was torn down and redialled into the same failure for good.
    `bool("false")` is True, which for `saveUtterances` means recording.

So: the API refuses a new value of the wrong type (`problems`), and both push
paths drop any stored one before sending (`drop_invalid`). A dropped key is one
the device leaves unchanged and the controller reads as its default, which is
the existing meaning of an absent key at both ends.

Keys not listed here are read defensively already (em_scenes' colours and
meter curve) or validated where they are handled (consolePassword,
consoleTimeoutMin, controllerEndpoints). `tests/test_config_types.py` holds the
table against `device/internal/config/config.go` and makes every default key
say which it is.
"""

from __future__ import annotations

import math

INT = "an integer"
FLOAT = "a number"
BOOL = "true or false"
STR = "a string"
FLOAT_LIST = "a list of numbers"

KINDS: dict[str, str] = {
    # Decoded by the device (device/internal/config ConfigMessage).
    "adcDigitalGain": INT,
    "adcMicpga": INT,
    "micGainDb": INT,
    "startupVolume": INT,
    "vadThreshold": FLOAT,
    "vadSpeechMs": INT,
    "vadSilenceMs": INT,
    "owwThreshold": FLOAT,
    "owwModel": STR,
    "owwOnDevice": STR,
    "bargeInEnabled": BOOL,
    "bargeInThreshold": FLOAT,
    "duckDb": FLOAT,
    "beamAngle": FLOAT,
    "beamformingEnabled": BOOL,
    "agcEnabled": BOOL,
    "aecEnabled": BOOL,
    "aecDelayMs": INT,
    "aecTailMs": INT,
    "aecRefSource": STR,
    "bleProxyEnabled": BOOL,
    "wakeSound": BOOL,
    "wakeSoundLevel": STR,
    "eqBands": FLOAT_LIST,
    "eqLoudness": BOOL,
    "bassGuardEnabled": BOOL,
    "bassGuardDb": FLOAT,
    "limiterEnabled": BOOL,
    "limiterThreshold": FLOAT,
    "limiterRelease": FLOAT,
    # Consumed only by the controller.
    "buttonSingleTapEvent": BOOL,
    "buttonMultiTapMs": INT,
    "wakeArbitrationMs": INT,
    "streamReply": BOOL,
    "owwSpeexNs": BOOL,
    "nsAsr": BOOL,
    "saveUtterances": BOOL,
}


def _number(v) -> bool:
    # bool is an int subclass; True is not a threshold.
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def fits(kind: str, value) -> bool:
    """Whether `value` is acceptable for `kind`. None never is."""
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == FLOAT:
        return _number(value)
    if kind == BOOL:
        return isinstance(value, bool)
    if kind == STR:
        return isinstance(value, str)
    if kind == FLOAT_LIST:
        return isinstance(value, list) and all(_number(x) for x in value)
    raise ValueError(f"unknown kind: {kind}")


def invalid_keys(config: dict) -> list[str]:
    """Listed keys in `config` whose values do not fit, sorted."""
    return sorted(k for k, kind in KINDS.items()
                  if k in config and not fits(kind, config[k]))


def problems(body: dict, stored: dict) -> list[str]:
    """
    Why a config write should be refused: one message per bad key.

    Only values the write CHANGES are judged. A client doing read-modify-write
    sends back whatever is stored, and a value stored before this check
    existed must not make every later save fail; drop_invalid covers those.
    """
    out = []
    for key in invalid_keys(body):
        if key in stored and stored[key] == body[key] and type(stored[key]) is type(body[key]):
            continue
        out.append(f"{key} must be {KINDS[key]}, got {body[key]!r}")
    return out


def drop_invalid(config: dict) -> tuple[dict, list[str]]:
    """`config` without the listed keys whose values do not fit, and those keys."""
    bad = invalid_keys(config)
    if not bad:
        return config, []
    return {k: v for k, v in config.items() if k not in bad}, bad
