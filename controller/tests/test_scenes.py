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
