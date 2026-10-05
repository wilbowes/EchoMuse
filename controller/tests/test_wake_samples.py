import json
import wave

import pytest

import em_db as db
import em_wake_samples as samples


def _pcm(frames=1, byte=1):
    return bytes([byte, 0]) * (samples.SAMPLE_RATE * 80 // 1000) * frames


def test_sample_files_are_wav_and_device_scoped(tmp_path):
    path = str(tmp_path / "db.sqlite")
    name = samples.filename("dev1")
    assert name and samples.save("dev1", name, _pcm(), db_path=path)
    saved = samples.resolve("dev1", name, db_path=path)
    assert saved and saved.is_file()
    assert samples.resolve("dev2", name, db_path=path) is None
    with wave.open(str(saved), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == samples.SAMPLE_RATE * 80 // 1000


def test_capture_includes_preroll_and_postroll_and_coalesces_score_run():
    capture = samples.WakeCapture()
    for i in range(10):
        capture.feed_audio(_pcm(byte=i), now=i * .08)
    capture.consider(enabled=True, minimum=.2, score=.25, threshold=.5,
                     model="hey_vanessa", now=.8)
    # A sustained score run updates one candidate, rather than opening a new
    # clip each scoring frame.
    capture.consider(enabled=True, minimum=.2, score=.3, threshold=.5,
                     model="hey_vanessa", now=.9)
    assert capture.active["kind"] == "candidate"
    assert capture.active["score"] == .3
    assert capture.feed_audio(_pcm(), now=2.3) is not None
    assert capture.active is None


def test_trigger_promotes_candidate_and_floor_rearms_after_score_drops():
    capture = samples.WakeCapture()
    capture.feed_audio(_pcm(), now=0)
    capture.consider(enabled=True, minimum=.2, score=.21, threshold=.6,
                     model="m", now=.1)
    capture.consider(enabled=True, minimum=.2, score=.7, threshold=.6,
                     model="m", trigger_source="controller", now=.2)
    assert capture.active["kind"] == "trigger"
    assert capture.active["trigger_source"] == "controller"
    capture.feed_audio(_pcm(), now=2)
    capture.consider(enabled=True, minimum=.2, score=.01, threshold=.6,
                     model="m", now=2)
    capture.consider(enabled=True, minimum=.2, score=.22, threshold=.6,
                     model="m", now=2.1)
    assert capture.active is not None


def test_trigger_uses_the_short_post_roll_even_with_continued_speech():
    capture = samples.WakeCapture()
    capture.feed_audio(_pcm(), now=0)
    capture.consider(enabled=True, minimum=.2, score=.7, threshold=.5,
                     model="m", trigger_source="controller", now=.5)
    assert capture.active["until"] == .5 + samples.TRIGGER_POST_ROLL_SECONDS
    # Ordinary speech right after the wake word, still above the (low) floor,
    # must not re-arm with the long candidate window instead.
    capture.consider(enabled=True, minimum=.2, score=.25, threshold=.5, model="m", now=.6)
    assert capture.active["until"] == .6 + samples.TRIGGER_POST_ROLL_SECONDS
    assert capture.active["until"] < .6 + samples.POST_ROLL_SECONDS
    done = capture.feed_audio(_pcm(), now=.6 + samples.TRIGGER_POST_ROLL_SECONDS + .01)
    assert done is not None and done["kind"] == "trigger"


def test_candidate_keeps_the_long_post_roll():
    capture = samples.WakeCapture()
    capture.feed_audio(_pcm(), now=0)
    capture.consider(enabled=True, minimum=.2, score=.3, threshold=.6, model="m", now=.5)
    assert capture.active["kind"] == "candidate"
    assert capture.active["until"] == .5 + samples.POST_ROLL_SECONDS


def test_tail_trim_shortens_a_trigger_clip_but_not_a_candidate():
    frame = samples.SAMPLE_RATE * 80 // 1000 * samples.SAMPLE_WIDTH

    trigger = samples.WakeCapture()
    for i in range(30):
        trigger.feed_audio(_pcm(), now=i * .08)
    trigger.consider(enabled=True, minimum=.2, score=.7, threshold=.5,
                      model="m", trigger_source="controller", now=2.4)
    before = len(trigger.active["pcm"])
    done = trigger.feed_audio(_pcm(), now=2.4 + samples.TRIGGER_POST_ROLL_SECONDS + .01)
    assert done is not None
    assert len(done["pcm"]) == before + frame - samples.TRIGGER_TAIL_TRIM_BYTES

    candidate = samples.WakeCapture()
    for i in range(30):
        candidate.feed_audio(_pcm(), now=i * .08)
    candidate.consider(enabled=True, minimum=.2, score=.3, threshold=.6, model="m", now=2.4)
    before = len(candidate.active["pcm"])
    done = candidate.feed_audio(_pcm(), now=2.4 + samples.POST_ROLL_SECONDS + .01)
    assert done is not None
    assert len(done["pcm"]) == before + frame  # untrimmed


def test_tail_trim_does_not_empty_a_short_trigger_clip():
    capture = samples.WakeCapture()
    capture.feed_audio(_pcm(), now=0)
    capture.consider(enabled=True, minimum=.2, score=.9, threshold=.5,
                      model="m", trigger_source="controller", now=.05)
    done = capture.feed_audio(_pcm(), now=.05 + samples.TRIGGER_POST_ROLL_SECONDS + .01)
    assert done is not None and len(done["pcm"]) > 0


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db.init(str(tmp_path / "samples.db"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "samples.db"))
    db.register_new_device("dev1", "127.0.0.1", "v1")
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_sample_labels_are_scoped_and_validated(fresh_db):
    name = samples.filename("dev1")
    db._conn.execute(
        "INSERT INTO wake_samples (device_id, ts, audio_file) VALUES (?, ?, ?)",
        ("dev1", 1, name),
    )
    db._conn.commit()
    row = db.get_wake_samples("dev1")[0]
    assert db.set_wake_sample_label("dev1", row["id"], "not_wake")
    assert not db.set_wake_sample_label("dev1", row["id"], "bad")
    assert not db.set_wake_sample_label("other", row["id"], "wake")
    assert db.get_wake_samples("dev1")[0]["label"] == "not_wake"


def test_retention_is_bounded_per_device(fresh_db):
    for i in range(samples.KEEP_PER_DEVICE + 3):
        db.insert_wake_sample("dev1", {
            "ts": i, "audio_file": samples.filename("dev1"), "kind": "candidate",
            "score": .3, "threshold": .5,
        })
    rows = db.get_wake_samples("dev1", 100)
    assert len(rows) == samples.KEEP_PER_DEVICE
    assert rows[0]["ts"] == samples.KEEP_PER_DEVICE + 2


def test_retention_unlinks_audio_while_its_row_still_exists(fresh_db, monkeypatch):
    first = samples.filename("dev1")
    assert samples.save("dev1", first, _pcm())
    db.insert_wake_sample("dev1", {
        "ts": 0, "audio_file": first, "kind": "candidate",
    })
    for i in range(1, samples.KEEP_PER_DEVICE):
        db.insert_wake_sample("dev1", {
            "ts": i, "audio_file": samples.filename("dev1"), "kind": "candidate",
        })

    unlink = samples.unlink
    observed = []

    def check_then_unlink(device_id, name, db_path=None):
        row = db._conn.execute(
            "SELECT 1 FROM wake_samples WHERE device_id = ? AND audio_file = ?",
            (device_id, name),
        ).fetchone()
        observed.append(row is not None)
        return unlink(device_id, name, db_path)

    monkeypatch.setattr(samples, "unlink", check_then_unlink)
    db.insert_wake_sample("dev1", {
        "ts": samples.KEEP_PER_DEVICE, "audio_file": samples.filename("dev1"),
        "kind": "candidate",
    })

    assert observed == [True]
    assert not samples.resolve("dev1", first)
    assert len(db.get_wake_samples("dev1", 100)) == samples.KEEP_PER_DEVICE


def test_retention_keeps_metadata_when_audio_cannot_be_unlinked(fresh_db, monkeypatch):
    names = []
    for i in range(samples.KEEP_PER_DEVICE):
        name = samples.filename("dev1")
        names.append(name)
        db.insert_wake_sample("dev1", {
            "ts": i, "audio_file": name, "kind": "candidate",
        })
    monkeypatch.setattr(samples, "unlink", lambda *_a, **_kw: False)

    with pytest.raises(OSError, match="could not remove expired wake sample"):
        db.insert_wake_sample("dev1", {
            "ts": samples.KEEP_PER_DEVICE,
            "audio_file": samples.filename("dev1"), "kind": "candidate",
        })

    rows = db.get_wake_samples("dev1", 100)
    assert len(rows) == samples.KEEP_PER_DEVICE
    assert {row["audio_file"] for row in rows} == set(names)


def test_delete_keeps_metadata_when_audio_cannot_be_unlinked(fresh_db, monkeypatch):
    name = samples.filename("dev1")
    assert samples.save("dev1", name, _pcm())
    db.insert_wake_sample("dev1", {"ts": 1, "audio_file": name, "kind": "candidate"})
    row = db.get_wake_samples("dev1")[0]
    monkeypatch.setattr(samples, "unlink", lambda *_a, **_kw: False)

    with pytest.raises(OSError, match="could not remove wake sample"):
        db.delete_wake_sample("dev1", row["id"])

    assert db.get_wake_samples("dev1")[0]["id"] == row["id"]
    assert samples.resolve("dev1", name).is_file()


def test_delete_unlinks_audio_while_its_row_still_exists(fresh_db, monkeypatch):
    name = samples.filename("dev1")
    assert samples.save("dev1", name, _pcm())
    db.insert_wake_sample("dev1", {"ts": 1, "audio_file": name, "kind": "candidate"})
    row = db.get_wake_samples("dev1")[0]
    unlink = samples.unlink
    observed = []

    def check_then_unlink(device_id, sample_name, db_path=None):
        found = db._conn.execute(
            "SELECT 1 FROM wake_samples WHERE device_id = ? AND audio_file = ?",
            (device_id, sample_name),
        ).fetchone()
        observed.append(found is not None)
        return unlink(device_id, sample_name, db_path)

    monkeypatch.setattr(samples, "unlink", check_then_unlink)
    assert db.delete_wake_sample("dev1", row["id"]) == name

    assert observed == [True]
    assert db.get_wake_samples("dev1") == []
    assert not samples.resolve("dev1", name)


def test_labeling_archives_the_sample_with_its_metadata(fresh_db):
    name = samples.filename("dev1")
    assert samples.save("dev1", name, _pcm())
    db.insert_wake_sample("dev1", {
        "ts": 1000, "audio_file": name, "kind": "trigger", "model": "hey_vanessa",
        "score": .8, "threshold": .5, "device_score": .7, "trigger_source": "controller",
    })
    row = db.get_wake_samples("dev1")[0]

    assert db.set_wake_sample_label("dev1", row["id"], "wake")

    archived = samples.archive_dir("wake") / name
    sidecar = samples.archive_dir("wake") / (name[:-4] + ".json")
    assert archived.is_file()
    meta = json.loads(sidecar.read_text())
    assert meta["model"] == "hey_vanessa" and meta["score"] == .8


def test_archive_survives_retention_pruning_of_the_original(fresh_db):
    name = samples.filename("dev1")
    assert samples.save("dev1", name, _pcm())
    row_id = db.insert_wake_sample("dev1", {"ts": 0, "audio_file": name, "kind": "candidate"})
    assert db.set_wake_sample_label("dev1", row_id, "not_wake")
    archived = samples.archive_dir("not_wake") / name

    for i in range(1, samples.KEEP_PER_DEVICE + 2):
        db.insert_wake_sample("dev1", {
            "ts": i, "audio_file": samples.filename("dev1"), "kind": "candidate",
        })

    assert not samples.resolve("dev1", name)  # original pruned
    assert archived.is_file()  # archived copy untouched


def test_archive_survives_explicit_deletion_of_the_original(fresh_db):
    name = samples.filename("dev1")
    assert samples.save("dev1", name, _pcm())
    row_id = db.insert_wake_sample("dev1", {"ts": 1, "audio_file": name, "kind": "candidate"})
    assert db.set_wake_sample_label("dev1", row_id, "uncertain")
    archived = samples.archive_dir("uncertain") / name

    assert db.delete_wake_sample("dev1", row_id) == name

    assert not samples.resolve("dev1", name)
    assert archived.is_file()


def test_deleting_device_removes_sample_rows_and_audio(fresh_db, monkeypatch):
    name = samples.filename("dev1")
    assert samples.save("dev1", name, _pcm())
    db.insert_wake_sample("dev1", {"ts": 1, "audio_file": name, "kind": "candidate"})
    path = samples.resolve("dev1", name)
    assert path and path.is_file()

    unlink = samples.unlink
    observed = []

    def check_then_unlink(device_id, sample_name, db_path=None):
        found = db._conn.execute(
            "SELECT 1 FROM wake_samples WHERE device_id = ? AND audio_file = ?",
            (device_id, sample_name),
        ).fetchone()
        observed.append(found is not None)
        return unlink(device_id, sample_name, db_path)

    monkeypatch.setattr(samples, "unlink", check_then_unlink)
    db.delete_device("dev1")

    assert observed == [True]
    assert db.get_wake_samples("dev1") == []
    assert not path.exists()
