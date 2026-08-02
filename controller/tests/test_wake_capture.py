"""
Unmatched wake-crossing capture.

The point of the feature is to answer one question — are the device's
unmatched crossings false accepts, or wakes the controller missed — so the
tests are about not destroying the evidence and not writing audio nobody
asked to be written.
"""

import em_shadow
import em_wake_capture as C


def _frames(n, at0=0.0, step=0.08, byte=1):
    """n consecutive 80ms frames, as the mic stream delivers them."""
    size = int(C.SAMPLE_RATE * step * C.SAMPLE_WIDTH)
    return [(at0 + i * step, bytes([byte]) * size) for i in range(n)]


class TestAudioRing:
    def test_it_keeps_the_most_recent_audio_and_bounds_itself(self):
        ring = C.AudioRing(seconds=2.0)
        for at, p in _frames(100):          # 8 seconds
            ring.append(at, p)
        assert 1.8 <= ring.seconds <= 2.0, "ring must stay within its bound"

    def test_it_bounds_by_duration_not_frame_count(self):
        """
        A frame is only nominally 80ms; a count-based bound would drift as
        soon as the device sent a short one.
        """
        ring = C.AudioRing(seconds=1.0)
        ring.append(0.0, b"\x00" * 320)     # 10ms
        for at, p in _frames(20, at0=0.1):  # 1.6s of full frames
            ring.append(at, p)
        assert ring.seconds <= 1.0

    def test_extract_returns_the_window_around_the_crossing(self):
        ring = C.AudioRing(seconds=20.0)
        for at, p in _frames(125):          # 10 seconds, t=0..10
            ring.append(at, p)
        pcm = ring.extract(centre=5.0, pre=1.0, post=1.0)
        # 2 seconds either side of centre, frames taken whole
        assert 1.9 <= C.duration_ms(len(pcm)) / 1000 <= 2.2

    def test_extract_is_empty_when_the_window_has_aged_out(self):
        """
        A real outcome, not an error: the hot path is never held up to
        guarantee a capture.
        """
        ring = C.AudioRing(seconds=1.0)
        for at, p in _frames(50):
            ring.append(at, p)
        assert ring.extract(centre=0.5) == b""

    def test_appending_nothing_is_harmless(self):
        ring = C.AudioRing(seconds=1.0)
        ring.append(1.0, b"")
        assert ring.seconds == 0


class TestMatching:
    def test_a_crossing_beside_a_controller_wake_is_matched(self):
        assert C.is_matched(100.0, 100.3, em_shadow.MATCH_WINDOW_S)
        assert C.is_matched(100.0, 99.2, em_shadow.MATCH_WINDOW_S)

    def test_a_crossing_with_no_wake_nearby_is_unmatched(self):
        assert not C.is_matched(100.0, 90.0, em_shadow.MATCH_WINDOW_S)
        assert not C.is_matched(100.0, None, em_shadow.MATCH_WINDOW_S)

    def test_the_decision_waits_longer_than_the_match_window(self):
        """
        The controller usually detects slightly LATER than the device — it
        scores the same frame after a network hop — so deciding at exactly
        the match window would call a real wake unmatched.
        """
        assert C.DECIDE_AFTER_S > em_shadow.MATCH_WINDOW_S

    def test_it_does_not_ask_whether_the_tracker_consumed_it(self):
        """
        The trap this feature could most easily fall into. A crossing is
        consumed at TURN-PERSIST time, which can be 30s after the wake, so a
        consumption check a few seconds later reports every genuine wake as
        unmatched — and would fill the disk with recordings of people
        talking to their assistant perfectly successfully.
        """
        tracker = em_shadow.ShadowTracker()
        at = em_shadow.now()
        tracker.record_cross(0.9, 0, at_now=at)
        # A turn is under way; nothing has been persisted yet, so the
        # tracker still holds the crossing...
        assert len(tracker._crossings) == 1
        # ...but the controller's wake instant already says it matched.
        assert C.is_matched(at, at + 0.15, em_shadow.MATCH_WINDOW_S)


class TestStorage:
    def test_filenames_carry_the_score_for_triage(self, tmp_path):
        name = C.filename("G090LF11803611NF", 1785660000.0, 0.734)
        assert name.endswith("_734.wav")
        dev, ts, score = C.parse_filename(name)
        assert dev == "G090LF11803611NF"
        assert abs(score - 0.734) < 0.001
        assert abs(ts - 1785660000.0) < 0.01

    def test_a_hostile_device_id_names_no_file(self):
        assert C.filename("../../etc/passwd", 1.0, 0.5) is None
        assert C.safe_device_id("a/b") is None

    def test_save_prunes_to_the_cap(self, tmp_path):
        db = str(tmp_path / "echomuse.db")
        pcm = b"\x00\x01" * 1600
        for i in range(8):
            C.save("dev1", pcm, 1785660000.0 + i, 0.5, db_path=db, keep=3)
        names = C.list_for("dev1", db_path=db)
        assert len(names) == 3, "retention is a hard count"
        # Newest kept, oldest dropped.
        assert names[0]["ts"] > names[-1]["ts"]
        assert names[0]["ts"] == 1785660007.0

    def test_save_writes_a_playable_wav(self, tmp_path):
        import wave
        db = str(tmp_path / "echomuse.db")
        pcm = b"\x00\x01" * 8000
        name = C.save("dev1", pcm, 1785660000.0, 0.5, db_path=db)
        path = C.captures_dir(db) / name
        with wave.open(str(path)) as w:
            assert w.getframerate() == C.SAMPLE_RATE
            assert w.getnchannels() == 1
            assert w.getnframes() == len(pcm) // 2

    def test_nothing_is_written_for_empty_audio(self, tmp_path):
        db = str(tmp_path / "echomuse.db")
        assert C.save("dev1", b"", 1785660000.0, 0.5, db_path=db) is None
        assert C.list_for("dev1", db_path=db) == []

    def test_one_device_cannot_read_anothers_capture(self, tmp_path):
        """
        The endpoint takes the device and the filename from the path, so the
        pairing is re-checked rather than trusted.
        """
        db = str(tmp_path / "echomuse.db")
        name = C.save("dev1", b"\x00\x01" * 1600, 1785660000.0, 0.5, db_path=db)
        assert C.resolve("dev1", name, db_path=db) is not None
        assert C.resolve("dev2", name, db_path=db) is None

    def test_a_missing_file_resolves_to_none_not_an_error(self, tmp_path):
        db = str(tmp_path / "echomuse.db")
        name = C.filename("dev1", 1785660000.0, 0.5)
        assert C.resolve("dev1", name, db_path=db) is None

    def test_path_traversal_in_the_name_resolves_to_none(self, tmp_path):
        db = str(tmp_path / "echomuse.db")
        for bad in ("../../etc/passwd", "dev1_1_1.wav/../../x", "..%2f.."):
            assert C.resolve("dev1", bad, db_path=db) is None

    def test_deleting_a_device_removes_its_captures(self, tmp_path):
        db = str(tmp_path / "echomuse.db")
        C.save("dev1", b"\x00\x01" * 1600, 1785660000.0, 0.5, db_path=db)
        C.save("dev2", b"\x00\x01" * 1600, 1785660001.0, 0.5, db_path=db)
        assert C.delete_device("dev1", db_path=db) == 1
        assert C.list_for("dev1", db_path=db) == []
        assert len(C.list_for("dev2", db_path=db)) == 1

    def test_captures_live_apart_from_utterance_recordings(self, tmp_path):
        """
        Different consent: an utterance was addressed to the assistant, an
        unmatched crossing was not.
        """
        import em_recordings
        db = str(tmp_path / "echomuse.db")
        assert C.captures_dir(db) != em_recordings.recordings_dir(db)
