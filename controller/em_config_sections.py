"""
Config sections — the unit of fleet-vs-device scoping.

A device used to be all-or-nothing: `use_global_config` was one boolean, so
you either inherited the entire fleet config or overrode every key of it.
That got unwieldy as the config grew — wanting a device-specific ring scene
meant forking its mic gain, wake threshold and EQ too, and they then stopped
tracking fleet changes forever.

Scoping is now per section. A device stores the SET of sections it overrides;
its effective config is the fleet config with the device's own values layered
over it for those sections only. Everything else keeps following the fleet.

This module is the single source of truth for which key belongs to which
section. `controller/static/dashboard.jsx` carries a mirror of SECTIONS for
rendering, and tests/test_config_sections.py fails if the two drift or if any
config key ends up belonging to no section.
"""

# Section id → (display label, config keys). Ids are stored in the DB, so
# they are API surface: renaming one needs a migration. Labels are display
# only and must match the dashboard Stage titles.
SECTIONS: dict[str, dict] = {
    "playback": {
        "label": "Playback",
        "keys": ["eqBands", "eqLoudness", "duckDb",
                 "limiterEnabled", "limiterThreshold", "limiterRelease",
                 "bassGuardEnabled", "bassGuardDb"],
    },
    "wakeword": {
        "label": "Wake word",
        "keys": [
            "owwModel", "owwThreshold", "owwSpeexNs",
            "bargeInEnabled", "bargeInThreshold", "wakeArbitrationMs",
            "owwOnDevice", "wakeSound",
        ],
    },
    "microphones": {
        "label": "Microphones",
        "keys": [
            "adcMicpga", "adcDigitalGain", "micGainDb",
            "beamformingEnabled", "beamAngle",
            "aecEnabled", "aecDelayMs", "aecTailMs", "nsAsr",
            "saveUtterances",
        ],
    },
    "ring": {
        "label": "Ring",
        "keys": [
            "ledScene", "ledListenColor", "ledThinkColor",
            "meterAttack", "meterDecay", "meterFloor",
            "meterGamma", "meterRef", "meterCurve",
        ],
    },
    "advanced": {
        "label": "Advanced",
        "keys": [
            "agcEnabled", "vadThreshold", "vadSpeechMs", "vadSilenceMs",
            # Already the button-turn section; these decide whether they happen.
            "buttonSingleTapEvent", "buttonMultiTapMs",
        ],
    },
    "bluetooth": {
        "label": "Bluetooth",
        "keys": ["bleProxyEnabled"],
    },
}

# Keys that live in the config dict but are NOT user-facing settings, and so
# belong to no section and are never fleet-inherited.
#
# startupVolume is persisted device STATE wearing a config key's clothes: the
# controller writes it automatically from every volume_state report, and the
# device re-applies it via SeedVolume on the first config push per run. That
# round trip is what makes volume survive a reboot, so the key has to stay —
# but it is not something you set, and it was never meaningfully editable
# (SeedVolume ignores later pushes, so the old dashboard slider did nothing
# until the device restarted and was overwritten by any real volume change).
# Fleet default 85 still does one real job: the starting point for a device
# that has never reported.
STATE_KEYS: frozenset[str] = frozenset({"startupVolume"})

SECTION_IDS: tuple[str, ...] = tuple(SECTIONS)


def keys_for(section_ids) -> set[str]:
    """Every config key belonging to the given sections. Unknown ids ignored."""
    out: set[str] = set()
    for sid in section_ids or ():
        section = SECTIONS.get(sid)
        if section:
            out.update(section["keys"])
    return out


def normalise(section_ids) -> list[str]:
    """
    Filter to known section ids, in canonical SECTIONS order.

    Stored values survive a controller downgrade/upgrade, so an id we no
    longer recognise must be dropped rather than propagated — otherwise a
    stale id would silently widen or narrow an override.
    """
    if not section_ids:
        return []
    given = set(section_ids)
    return [sid for sid in SECTION_IDS if sid in given]


def merge(global_cfg: dict, device_cfg: dict, section_ids) -> dict:
    """
    Effective config: fleet, with the device's values layered over it for the
    sections it overrides.

    STATE_KEYS always come from the device when present, whatever the section
    scoping says — they are that device's own hardware state, and inheriting
    one from the fleet would mean a device coming back at another room's
    volume.
    """
    effective = dict(global_cfg)
    overridden = keys_for(section_ids)
    for key in overridden:
        if key in device_cfg:
            effective[key] = device_cfg[key]
    for key in STATE_KEYS:
        if key in device_cfg:
            effective[key] = device_cfg[key]
    return effective


def summarise(section_ids) -> str:
    """Dashboard-facing one-liner for the Status panel's Config row."""
    n = len(normalise(section_ids))
    if n == 0:
        return "Fleet"
    return f"Local override ({n} of {len(SECTION_IDS)})"
