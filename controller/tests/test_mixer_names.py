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


def test_start_and_stop_preserve_other_boards(tmp_path):
    """Execute the actual shell blocks with recorded mixer writes and fake idme."""
    import subprocess

    text = SCRIPT.read_text()
    startup = text.split('# Speaker mixer init', 1)[1].split('# Mic gain', 1)[0]
    startup = '# Speaker mixer init' + startup
    shutdown = re.search(r'amp_off\(\) \{.*?\n\}', text, re.S).group()
    old_start = [
        '-D 0 Audio_I2S1_Setting On',
        '-D 0 HP DAC Playback Switch 1 1',
        '-D 0 MFP Gpio Mute On',
        '-D 0 PCM Playback Volume 100 100',
    ]
    old_stop = ['-D 0 PCM Playback Volume 0 0', '-D 0 Ext_Speaker_Amp_Switch Off']
    for identity in (None, b'A3S5BH2HU6VAYF\0', b'unknown\0', b'', b'A7WXQPH584YP-extra\0', b'A7WXQPH584YP\0'):
        idme = tmp_path / 'device_type_id'
        if identity is None:
            idme.unlink(missing_ok=True)
        else:
            idme.write_bytes(identity)
        script = startup.replace('/proc/idme/device_type_id', str(idme))
        result = subprocess.run(
            ['sh', '-c', 'tinymix() { printf "%s\\n" "$*"; }; '
             'busybox() { command "$@"; };\n' + script + shutdown + '\namp_off\n'],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        if identity == b'A7WXQPH584YP\0':
            assert result == [
                '-D 0 MFP Gpio Mute On', '-D 0 PCM Playback Volume 0 0',
                *old_start[:2], '-D 0 MFP Gpio Mute On', *old_stop,
            ]
        else:
            assert result == old_start + old_stop, identity
