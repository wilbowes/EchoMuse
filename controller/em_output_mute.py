"""
Mute for the media player's OUTPUT, as Home Assistant means it: silence now,
and the same volume back on unmute.

We advertised VOLUME_MUTE to HA and then dropped the command as unhandled,
while reporting muted=False — so HA and Music Assistant both showed a mute
button that did nothing (#641). Music Assistant can fake a mute by setting 0
and restoring, but it uses the player's own mute when HA says there is one.

Done here rather than on the device so it works on every firmware in the
field: mute sends volume 0 and remembers the level; unmute sends it back.
Any other volume while muted — HA's slider, the Echo's buttons — unmutes at
that level, which is what a volume change on a muted player normally does.

This is not the Dot's mute button. That mutes the MICROPHONES, is sovereign
on the device, and is reported separately (#438).

Pure: the caller sends the levels it returns and persists only what it is
told to.
"""


# One press of the Echo's volume-up button, in device levels (4dB; volumeStep
# in device/internal/server/volume.go).
BUTTON_STEP = 8
DEVICE_MAX = 127


class OutputMute:
    def __init__(self):
        self.muted = False
        self._restore: int | None = None   # level to put back on unmute

    def mute(self, current_level: int) -> int | None:
        """Level to send now, or None if already muted."""
        if self.muted:
            return None
        self.muted = True
        self._restore = int(current_level)
        return 0

    def unmute(self) -> int | None:
        """Level to send now, or None if not muted."""
        if not self.muted:
            return None
        self.muted = False
        level, self._restore = self._restore, None
        return level

    def volume_set(self, level: int) -> None:
        """An explicit volume from HA ends a mute."""
        self.muted = False
        self._restore = None

    def device_report(self, level: int) -> tuple[bool, int | None]:
        """
        The device reported its level. Returns (keep, send):

        keep: a real volume to keep (persist as startupVolume, show HA).
              False for our own mute echoing back as 0, which must not
              overwrite the level unmute restores.
        send: a level to send instead, or None.

        A non-zero level while muted is the Echo's volume-up button. The
        device stepped up from the 0 we sent, which lands on its button
        floor (47, near silent) — not what a volume-up on a muted player
        means. So it unmutes one step above the level from before the mute
        (UAT 2026-09-27: it came back at 37%, and 47 replaced the stored
        volume). Volume-down from 0 stays 0 and stays muted.
        """
        if not self.muted:
            return True, None
        if int(level) == 0:
            return False, None
        restore = self._restore
        self.muted = False
        self._restore = None
        if restore is None:
            return True, None
        return False, min(DEVICE_MAX, restore + BUTTON_STEP)

    def on_reconnect(self) -> int | None:
        """Level to send after the device reconnects: it boots at its stored
        startupVolume, which is the pre-mute level, so a mute must be
        re-applied."""
        return 0 if self.muted else None

    @property
    def restore_level(self) -> int | None:
        return self._restore
