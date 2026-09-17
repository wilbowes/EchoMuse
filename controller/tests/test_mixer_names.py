"""start_server.sh must name mixer controls, never number them (#546).

Control ids are positional and the FireOS 6 kernel shifts them from ~161 on,
so a numbered write can land on a different control and still succeed.
"""
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "device_payloads" / "start_server.sh"


def test_start_script_names_every_mixer_control():
    numbered = [
        line.strip()
        for line in SCRIPT.read_text().splitlines()
        if re.match(r"\s*tinymix\s+-D\s+0\s+\d", line)
    ]
    assert numbered == [], f"numbered mixer writes: {numbered}"


def test_start_script_still_sets_the_mixer():
    # The guard above passes vacuously if the writes disappear altogether.
    text = SCRIPT.read_text()
    for name in ("PCM Playback Volume", "Ext_Speaker_Amp_Switch", "Digital Volume Control"):
        assert f'tinymix -D 0 "' in text and name in text, name
