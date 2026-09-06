import em_scenes


def _assert_frame(frame):
    assert len(frame) == em_scenes.NUM_LEDS
    for i, led in enumerate(frame):
        assert led["id"] == i
        for ch in ("r", "g", "b"):
            assert 0 <= led[ch] <= 255


def test_every_preset_resolves_render_ready():
    for name in ("standard", "airy", "malevolent", "pride"):
        scene = em_scenes.resolve({"ledScene": name})
        assert scene["name"] == name
        _assert_frame(scene["listening"])
        _assert_frame(scene["spin_frame"](0))
        _assert_frame(scene["spin_frame"](7))
        # led_anim specs must carry the fields firmware keys on
        assert scene["listening_anim"]["pattern"] == "solid"
        assert scene["listening_anim"]["listening"] is True
        assert scene["spin_anim"]["pattern"] in ("spin", "rotate")
        assert scene["spin_anim"]["ttlSec"] > 0
        assert scene["meter_anim"]["pattern"] == "meter"


def test_unknown_scene_falls_back_to_standard():
    scene = em_scenes.resolve({"ledScene": "does-not-exist"})
    assert scene["listening"] == em_scenes.resolve({"ledScene": "standard"})["listening"]


def test_empty_config_is_standard():
    assert em_scenes.resolve({})["listening"] == \
        em_scenes.resolve({"ledScene": "standard"})["listening"]
    assert em_scenes.resolve(None)["listening"] == \
        em_scenes.resolve({"ledScene": "standard"})["listening"]


def test_custom_scene_uses_configured_colours():
    scene = em_scenes.resolve({
        "ledScene": "custom",
        "ledListenColor": "#102030",
        "ledThinkColor": "#405060",
    })
    led = scene["listening"][0]
    assert (led["r"], led["g"], led["b"]) == (0x10, 0x20, 0x30)
    spin = scene["spin_frame"](3)
    assert (spin[3]["r"], spin[3]["g"], spin[3]["b"]) == (0x40, 0x50, 0x60)


def test_custom_scene_bad_hex_falls_back_to_defaults():
    scene = em_scenes.resolve({"ledScene": "custom", "ledListenColor": "#zzz"})
    led = scene["listening"][0]
    assert (led["r"], led["g"], led["b"]) == (0, 180, 0)


def test_spinner_position_wraps():
    scene = em_scenes.resolve({"ledScene": "standard"})
    assert scene["spin_frame"](0) == scene["spin_frame"](em_scenes.NUM_LEDS)


def test_pride_rotates_whole_palette():
    scene = em_scenes.resolve({"ledScene": "pride"})
    f0, f1 = scene["spin_frame"](0), scene["spin_frame"](1)
    # rotation: LED i at pos 1 shows what LED i-1 showed at pos 0
    for i in range(em_scenes.NUM_LEDS):
        a, b = f1[i], f0[(i - 1) % em_scenes.NUM_LEDS]
        assert (a["r"], a["g"], a["b"]) == (b["r"], b["g"], b["b"])


# ─── meter response curve (dashboard-tunable) ────────────────────────────────

def test_meter_curve_omits_absent_keys():
    """
    Absent config keys must NOT be sent: an empty override means "use the
    firmware default", which lets the device move its defaults without every
    stored config pinning the old ones.
    """
    anim = em_scenes.resolve({})["meter_anim"]
    for k in ("attack", "decay", "floor", "gamma", "ref", "curve"):
        assert k not in anim


def test_meter_curve_passes_through_and_clamps():
    anim = em_scenes.resolve({
        "meterDecay": 0.5, "meterFloor": 0.0,
        "meterGamma": 99, "meterRef": -1, "meterCurve": "bogus",
    })["meter_anim"]
    assert anim["decay"] == 0.5
    assert anim["floor"] == 0.0          # 0 is a real value, not "absent"
    assert anim["gamma"] == 3.5          # clamped to the top of the range
    assert anim["ref"] == 0.02           # clamped to the bottom
    assert "curve" not in anim           # unparseable is dropped, not crashed


def test_turn_state_ttls_are_bounded():
    """
    A controller that dies mid-turn used to leave the ring animating for
    three minutes. Each phase's TTL must now be bounded by what that phase
    can legitimately take.
    """
    scene = em_scenes.resolve({})
    assert scene["listening_anim"]["ttlSec"] <= 30
    # The spinner spans HA think time AND the TTS fetch, so its TTL has to
    # outlast _fetch_tts_audio's timeout (60s, two attempts) or it clears
    # the ring during exactly the long responses that need it. Guard the
    # coupling so moving one without the other fails here.
    assert scene["spin_anim"]["ttlSec"] >= 120
    assert scene["spin_anim"]["ttlSec"] <= 180


def test_meter_ttl_scales_with_response_length():
    """
    The meter TTL must always exceed the response it is showing — a fixed
    value would clear the ring part-way through a long answer, which is the
    exact bug the playback_stats rendezvous fixes at the other end.
    """
    assert em_scenes.meter_ttl(0.5) >= 30           # short clips get a floor
    for seconds in (5, 30, 120, 600):
        assert em_scenes.meter_ttl(seconds) > seconds * 1.5


def test_outcome_cues_are_self_clearing_and_distinct():
    """
    Cues must retire on the device's own ticker (no follow-up message to
    lose) and must be told apart by rhythm, since colour is already spoken
    for by mute/link/volume.
    """
    scene = em_scenes.resolve({})
    ns, err = scene["nospeech_anim"], scene["error_anim"]
    for a in (ns, err):
        assert a["pattern"] == "pulse"
        assert 0 < a["ttlSec"] <= 2
    assert ns["periodMs"] != err["periodMs"]
    assert err["periodMs"] < ns["periodMs"]   # error reads as more agitated


def test_ack_cue_is_a_steady_hold_not_a_rhythm():
    """
    The acknowledgement cue says "heard you, nothing more to do" — it is not
    an outcome, so it must not borrow the rhythm the outcome cues use to mean
    something is wrong or was unheard.

    It exists because a turn can end in milliseconds: Home Assistant's
    satellite setup intercepts the wake word and ends the run immediately, so
    the ring lit and cleared inside ~100ms and read as a glitch on a device
    that was working. It stayed lit before only because the turn was hung.

    Self-clearing like the others, and short enough not to bleed into the
    next prompt — setup's two wake word tests were 3.0s and 5.6s apart on
    hardware.
    """
    scene = em_scenes.resolve({})
    ack = scene["ack_anim"]

    assert ack["pattern"] == "solid", \
        "a pulse would read as an outcome signal, which this is not"
    assert "periodMs" not in ack, "a steady hold has no rhythm to carry"
    assert 0 < ack["ttlSec"] <= 2, \
        "must retire on the device's own ticker, and well inside the gap " \
        "between the setup flow's two wake word prompts"


def test_no_ha_cue_is_orange_in_every_scene():
    """
    The only cue that ignores the scene palette, and deliberately.

    It does not report an outcome of the turn — it reports that there is
    nothing above the device to answer, and orange is what the device already
    pulses on its own while it cannot find a controller. HA not being
    connected is that condition one hop further up, so the colour is the
    message and a scene must not repaint it.
    """
    for name in ("standard", "airy", "malevolent", "pride"):
        scene = em_scenes.resolve({"ledScene": name})
        cue   = scene["no_ha_anim"]
        assert cue["colors"] == [list(em_scenes.LINK_ORANGE)], \
            "the fault colour must not follow the scene"
        assert cue["colors"] != scene["error_anim"]["colors"], \
            "and it must not land on the outcome cues' colour, or the two " \
            "reads become one signal"


def test_no_ha_cue_is_two_throbs_and_self_clearing():
    """
    Two throbs: runPulse starts and ends dim, so a 500ms period against the
    device's 1s TTL floor is exactly a double flash and then a clear. The
    count is the readable part — a longer period would leave one throb, a
    shorter one an indistinct flicker.
    """
    cue = em_scenes.resolve({})["no_ha_anim"]
    assert cue["pattern"] == "pulse"
    assert cue["ttlSec"] == 1, \
        "must retire on the device's own ticker with no follow-up message"
    assert cue["ttlSec"] * 1000 / cue["periodMs"] == 2
