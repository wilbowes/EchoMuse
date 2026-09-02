"""
Asset-distribution planning.

The transport can only be exercised against a real device; the DECISIONS —
what to push, what to delete, what to refuse — are pure and are the part
that can quietly do damage, so they are tested here. A wrong prune deletes
the model a device is using; a wrong "keep" leaves it scoring against a
stale classifier that silently disagrees with the controller.
"""

import time

import pytest

import em_oww_assets as A


def _asset(name, md5, kind, size=1000):
    from pathlib import Path
    return A.Asset(name=name, source=Path("/src") / name, md5=md5, size=size, kind=kind)


def _base(md5_rt="rt1", md5_mel="mel1", md5_emb="emb1"):
    return [
        _asset(A.RUNTIME_NAME, md5_rt, "runtime", size=12_290_332),
        _asset("melspectrogram.onnx", md5_mel, "shared", size=1_087_958),
        _asset("embedding_model.onnx", md5_emb, "shared", size=1_326_578),
    ]


NOW = int(time.time())


def test_a_device_with_everything_is_a_noop():
    """The sync must be idempotent — it runs on every enable, not just once."""
    desired = _base() + [_asset("hey_mycroft_v0.1.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    p = A.plan_sync(desired, actual)
    assert p.is_noop
    assert sorted(p.keep) == sorted(a.name for a in desired)


def test_only_changed_files_are_pushed():
    """
    12.3MB over a shell transport is slow enough that re-pushing an unchanged
    runtime on every sync would make the feature feel broken.
    """
    desired = _base() + [_asset("hey_mycroft_v0.1.onnx", "c1", "classifier")]
    actual = {
        A.RUNTIME_NAME: ("rt1", NOW),
        "melspectrogram.onnx": ("mel1", NOW),
        "embedding_model.onnx": ("OLD", NOW),
        "hey_mycroft_v0.1.onnx": ("c1", NOW),
    }
    p = A.plan_sync(desired, actual)
    assert [a.name for a in p.push] == ["embedding_model.onnx"]
    assert A.RUNTIME_NAME in p.keep


def test_a_missing_device_gets_everything():
    desired = _base() + [_asset("hey_mycroft_v0.1.onnx", "c1", "classifier")]
    p = A.plan_sync(desired, {})
    assert len(p.push) == 4
    assert not p.prune


def test_the_selected_classifier_is_never_pruned():
    """
    Evicting the model the device is configured to use is the one outcome
    that breaks it outright rather than costing a re-push.
    """
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    for i in range(6):
        actual[f"old{i}.onnx"] = (f"x{i}", NOW - 10_000 * (i + 1))
    p = A.plan_sync(desired, actual)
    assert "selected.onnx" not in p.prune
    assert "selected.onnx" in p.keep


def test_extra_classifiers_are_evicted_oldest_first():
    """LRU by device mtime — no controller-side bookkeeping to lose."""
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    actual["newest.onnx"] = ("a", NOW - 100)
    actual["middle.onnx"] = ("b", NOW - 5_000)
    actual["oldest.onnx"] = ("c", NOW - 90_000)
    actual["ancient.onnx"] = ("d", NOW - 900_000)

    p = A.plan_sync(desired, actual, slots=3)
    # `slots` budgets the LEFTOVERS only (the desired model does not consume
    # one) — three kept, the oldest falls off.
    assert sorted(p.prune) == ["ancient.onnx"]
    assert "newest.onnx" in p.keep and "middle.onnx" in p.keep


def test_the_runtime_and_shared_models_are_never_evictable():
    """
    They are required by definition — pruning one would delete something the
    same sync is about to push back.
    """
    desired = _base() + [_asset("a.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW - 999_999) for a in desired}
    p = A.plan_sync(desired, actual, slots=1)
    assert not p.prune


def test_unrecognised_files_are_left_alone():
    """
    This deletes only what it positively recognises as an evictable
    classifier. A stray file in the directory is not ours to remove.
    """
    desired = _base() + [_asset("a.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    actual["notes.txt"] = ("z", NOW - 999_999)
    actual["libsomething.so"] = ("y", NOW - 999_999)
    p = A.plan_sync(desired, actual, slots=1)
    assert p.prune == []


def test_a_full_device_is_blocked_with_a_reason_not_half_written():
    desired = _base()
    p = A.plan_sync(desired, {}, free_mb=60)
    assert p.blocked and "free" in p.blocked
    assert not p.push, "a blocked plan must not push anything"


def test_space_is_only_checked_against_what_actually_needs_sending():
    """
    A device that already has everything must never be blocked for space —
    the sync runs on every enable, and refusing a no-op would report a
    problem that does not exist.
    """
    desired = _base()
    actual = {a.name: (a.md5, NOW) for a in desired}
    p = A.plan_sync(desired, actual, free_mb=1)
    assert p.blocked is None
    assert p.is_noop


# ─── Device inventory parsing ────────────────────────────────────────────────

def test_listing_parses_md5_mtime_name():
    text = ("aff8f1c6bee88425111e62aadf3c381c 1753900000 "
            f"{A.DEVICE_DIR}/libonnxruntime.so\n")
    got = A.parse_device_listing(text)
    assert got == {"libonnxruntime.so": ("aff8f1c6bee88425111e62aadf3c381c", 1753900000)}


@pytest.mark.parametrize("line", [
    "",
    "garbage",
    "nothexdigest 123 /x/a.onnx",
    "aff8f1c6bee88425111e62aadf3c381c notanint /x/a.onnx",
    "aff8f1c6 123 /x/a.onnx",
    "md5sum: can't open '/x/a.onnx': No such file or directory",
])
def test_unparseable_lines_are_skipped_not_guessed(line):
    """
    Shell noise (a prompt echo, an error from a missing file) must not become
    an inventory entry. Over-reporting a file as present makes the sync push
    nothing and claim success — the one failure mode with no visible symptom.
    """
    assert A.parse_device_listing(line) == {}


def test_a_partial_inventory_is_safe():
    """
    Missing an entry costs an unnecessary push. That is the right direction
    to fail in, and this pins it: an empty parse means push everything.
    """
    desired = _base()
    p = A.plan_sync(desired, A.parse_device_listing("junk\n"))
    assert len(p.push) == len(desired)


# ─── Contract with the device ────────────────────────────────────────────────

def test_device_dir_matches_the_firmware_constant():
    """
    DEVICE_DIR and shadow.DefaultDir are the same path in two languages. If
    they drift, the controller installs assets the device will not look for,
    and the only symptom is shadow mode silently never starting.
    """
    from pathlib import Path
    go = (Path(__file__).resolve().parents[2]
          / "device/internal/wakeword/shadow/open.go").read_text()
    assert f'DefaultDir = "{A.DEVICE_DIR}"' in go, (
        "em_oww_assets.DEVICE_DIR has drifted from shadow.DefaultDir"
    )


def test_shared_model_names_match_what_the_device_opens():
    from pathlib import Path
    go = (Path(__file__).resolve().parents[2]
          / "device/internal/wakeword/shadow/open.go").read_text()
    for name in A.SHARED_NAMES:
        assert f'"{name}"' in go, f"{name} is not what the device opens"
    assert f'"{A.RUNTIME_NAME}"' in go


def test_classifier_filename_matches_the_device_stem_rule():
    """
    The device derives the classifier filename with shadow.ModelStem, which
    mirrors em_oww_models.prediction_key. Both must agree, or we send
    hey_mycroft_v0.1.onnx and the device opens hey_mycroft_v0.onnx — the
    exact bug ModelStem was written to fix.
    """
    import em_oww_models
    assert em_oww_models.prediction_key("hey_mycroft_v0.1") == "hey_mycroft_v0.1"
    assert em_oww_models.prediction_key("/data/oww_models/clara.onnx") == "clara"


# ─── df parsing ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,want", [
    # Wrapped filesystem name — what a real device actually returned.
    ("                  1010       648       346  65% /data", 346),
    # Unwrapped, the shape the column index was originally written for.
    ("/dev/block/platform/mtk-msdc.0/by-name/userdata 1010 648 346 65% /data", 346),
    ("tmpfs 100 0 100 0% /data", 100),
])
def test_free_space_is_read_from_the_percentage_anchor_not_a_column_index(line, want):
    """
    busybox wraps a long filesystem name onto its own line, so the available
    column is $3 sometimes and $4 others. awk '{print $4}' returned "65%" on a
    real device — which parsed as no reading, silently disabling the
    free-space check rather than failing visibly.
    """
    assert A.parse_free_mb(line) == want


@pytest.mark.parametrize("line", ["", "garbage", "df: /data: No such file or directory"])
def test_unreadable_df_yields_no_measurement(line):
    """None means 'unknown', which plan_sync treats as 'do not check' — the
    opposite of 0, which would block every install."""
    assert A.parse_free_mb(line) is None


def test_unknown_free_space_does_not_block():
    desired = _base()
    p = A.plan_sync(desired, {}, free_mb=A.parse_free_mb("garbage"))
    assert p.blocked is None and p.push


# ── The stock set every device carries ────────────────────────────────────

def test_stock_models_match_what_the_dashboard_offers():
    """
    STOCK_MODELS is what we install; WW_MODELS is what a user can select.
    Drift either way is silent and lands on the user, not on CI: a wake word
    offered but not installed is the #191 failure (selectable, unscoreable,
    and under owwOnDevice=on a device with no wake word at all), and one
    installed but not offered is dead weight nobody can ever reach.
    """
    import re
    from pathlib import Path
    jsx = (Path(__file__).resolve().parents[1]
           / "static" / "dashboard.jsx").read_text()
    block = re.search(r"const WW_MODELS = \[(.*?)\];", jsx, re.S)
    assert block, "dashboard.jsx must still define WW_MODELS"
    offered = set(re.findall(r"value:\s*'([^']+)'", block.group(1)))
    assert offered == set(A.STOCK_MODELS), (
        f"dashboard offers {sorted(offered)}, "
        f"em_oww_assets installs {sorted(A.STOCK_MODELS)}"
    )


def test_the_stock_set_is_installed_alongside_the_selected_model():
    """
    A device provisioned for one wake word must still be able to score any
    other stock one, or changing the wake word later needs a manual trip to
    Updates -> Update assets (#191).
    """
    # Not `Path(... or "")`: Path("") is Path("."), which IS a directory, so
    # the skip never fired and the test ran against the CWD, finding no models.
    res = A.openwakeword_resources()
    if res is None:
        pytest.skip("openwakeword not installed in this environment")
    assets, problems = A.desired_assets(["alexa_v0.1"], resources=res)
    names = [a.name for a in assets if a.kind == "classifier"]
    assert names[0] == "alexa_v0.1.onnx", "the selected model must stay first (it is pinned)"
    assert set(names) == {f"{m}.onnx" for m in A.STOCK_MODELS}
    assert not [p for p in problems if "wake model" in p]


def test_include_stock_false_installs_only_what_was_asked_for():
    res = A.openwakeword_resources()
    if res is None:
        pytest.skip("openwakeword not installed in this environment")
    assets, _ = A.desired_assets(["alexa_v0.1"], resources=res, include_stock=False)
    assert [a.name for a in assets if a.kind == "classifier"] == ["alexa_v0.1.onnx"]


def test_installing_the_stock_set_does_not_evict_custom_models():
    """
    The four stock models exactly fill CLASSIFIER_SLOTS. Under the old rule
    (slots minus every desired classifier) that left no room at all, so
    installing them would have deleted every custom model on the device —
    including ones a user trained themselves and cannot re-download.
    """
    desired = _base() + [
        _asset("my_custom.onnx", "c1", "classifier"),
    ] + [_asset(f"{m}.onnx", m, "classifier") for m in A.STOCK_MODELS]
    actual = {
        A.RUNTIME_NAME: ("rt1", NOW), "melspectrogram.onnx": ("mel1", NOW),
        "embedding_model.onnx": ("emb1", NOW), "my_custom.onnx": ("c1", NOW),
        "older_custom.onnx": ("o1", NOW - 100),
    }
    plan = A.plan_sync(desired, actual)
    assert plan.prune == [], f"unexpectedly pruning {plan.prune}"
    assert "older_custom.onnx" in plan.keep


def test_leftover_custom_models_are_still_evicted_beyond_the_budget():
    """The budget still applies — it now governs leftovers only, not everything."""
    desired = _base() + [_asset(f"{m}.onnx", m, "classifier") for m in A.STOCK_MODELS]
    actual = {
        A.RUNTIME_NAME: ("rt1", NOW), "melspectrogram.onnx": ("mel1", NOW),
        "embedding_model.onnx": ("emb1", NOW),
        **{f"c{i}.onnx": (f"c{i}", NOW - i) for i in range(A.CLASSIFIER_SLOTS + 2)},
    }
    plan = A.plan_sync(desired, actual)
    assert len(plan.prune) == 2, plan.prune
    assert plan.prune == [f"c{A.CLASSIFIER_SLOTS}.onnx", f"c{A.CLASSIFIER_SLOTS + 1}.onnx"], \
        "eviction must still be oldest-first"


# ── Reconcile-on-connect: does the device have the model it was told to use ──

def test_the_selected_classifier_is_recognised_when_installed():
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    assert A.missing_selected_classifier(desired, actual) is None


def test_a_classifier_absent_from_the_device_is_named():
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in _base()}
    assert A.missing_selected_classifier(desired, actual) == "selected.onnx"


def test_the_right_name_with_the_wrong_bytes_counts_as_missing():
    """
    A re-trained custom model keeps its filename. Presence alone would call
    that installed and leave the device scoring against the old classifier,
    which disagrees with the controller silently.
    """
    desired = _base() + [_asset("selected.onnx", "c-new", "classifier")]
    actual = {**{a.name: (a.md5, NOW) for a in _base()},
              "selected.onnx": ("c-old", NOW)}
    assert A.missing_selected_classifier(desired, actual) == "selected.onnx"


def test_only_the_SELECTED_classifier_gates_readiness():
    """
    The stock set rides along on every sync, but a device is only deaf if it
    lacks the one it was told to use — a missing spare must not stand it down.
    """
    desired = _base() + [
        _asset("selected.onnx", "c1", "classifier"),
        _asset("spare.onnx", "c2", "classifier"),
    ]
    actual = {**{a.name: (a.md5, NOW) for a in _base()},
              "selected.onnx": ("c1", NOW)}
    assert A.missing_selected_classifier(desired, actual) is None


def test_no_classifier_to_check_is_not_a_missing_one():
    """Nothing configured is not the same as configured-and-absent."""
    assert A.missing_selected_classifier(_base(), {}) is None


# ─── missing_assets: the wider "is this device COMPLETE" question ─────────────
#
# missing_selected_classifier answers "is this device deaf", which is what
# stands a mode down. These cover the repair question beside it — the one
# nothing was asking while Office sat without three of the four stock
# classifiers for a fortnight, reported healthy the whole time.

def test_a_complete_device_has_nothing_missing():
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {a.name: (a.md5, NOW) for a in desired}
    assert A.missing_assets(desired, actual) == []


def test_a_spare_classifier_is_missing_even_though_the_device_can_score():
    """
    The exact Office shape: the selected model is present and correct, the
    other stock ones are not. Nothing is degraded — and selecting one of them
    tomorrow is a device with no wake word at all, which is what this catches.
    """
    desired = _base() + [
        _asset("selected.onnx", "c1", "classifier"),
        _asset("spare_a.onnx", "c2", "classifier"),
        _asset("spare_b.onnx", "c3", "classifier"),
    ]
    actual = {**{a.name: (a.md5, NOW) for a in _base()},
              "selected.onnx": ("c1", NOW)}
    assert A.missing_selected_classifier(desired, actual) is None
    assert A.missing_assets(desired, actual) == ["spare_a.onnx", "spare_b.onnx"]


def test_the_runtime_and_shared_models_count_too():
    """Not just classifiers — a device with no libonnxruntime scores nothing."""
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    actual = {"selected.onnx": ("c1", NOW)}
    assert A.missing_assets(desired, actual) == [
        A.RUNTIME_NAME, "melspectrogram.onnx", "embedding_model.onnx"]


def test_the_right_name_with_the_wrong_bytes_is_missing_here_too():
    """Same md5 rule as everywhere else in this module."""
    desired = _base() + [_asset("spare.onnx", "c-new", "classifier")]
    actual = {**{a.name: (a.md5, NOW) for a in _base()},
              "spare.onnx": ("c-old", NOW)}
    assert A.missing_assets(desired, actual) == ["spare.onnx"]


def test_an_unreadable_inventory_reports_everything_missing():
    """
    Deliberate, and the reason the docstring tells callers to establish the
    listing succeeded first: this function cannot tell an empty device from a
    shell that never answered, and must not pretend to.
    """
    desired = _base() + [_asset("selected.onnx", "c1", "classifier")]
    assert A.missing_assets(desired, {}) == [a.name for a in desired]
