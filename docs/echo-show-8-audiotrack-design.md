# crown speaker via AudioTrack — design spec (Scenario C)

Follows the scenario analysis in `docs/echo-show-8-journal.md`'s
2026-08-26 freeze entries (verdict: pursue routing playback through
Android's own audio APIs). Covers only the **playback** path —
`pcm_microphone_crown.go` and the mic side of the protocol are untouched,
staying raw ALSA, per that reasoning (pstore evidence implicates
DL1/playback, not capture).

## Why this instead of A

A (open DL1 on demand) narrows the collision *window* but doesn't remove the
collision *condition* — mediaserver still contends for the same exclusive
node, just less often. C removes the condition itself: playback goes through
`AudioTrack`, which `AudioFlinger` mixes with everything else in userspace
*before* touching the HAL/kernel node, so there's no second exclusive opener
of `mtk_pcm_I2S0dl1` at all. If this works, the "hold it open forever"
model A was trying to avoid becomes safe again — see below.

## Split of responsibility

| Layer | Stays the same | Changes |
|---|---|---|
| `data.go`, WS protocol, turn state machine | ✅ unchanged | — |
| `pcm_speaker_crown.go`: `audioStream`, `Mixer`, `duckTarget`, ring buffers | ✅ unchanged | — |
| `pcm_speaker_crown.go`: final write call | — | `p.pcm.Write(out)` → socket write |
| `pcm_microphone_crown.go` | ✅ unchanged (raw ALSA) | — |
| New: `crown_launcher` Java playback service | — | new |

Everything upstream of the current `p.pcm.Write(out)` call in `silenceLoop()`
(`device/internal/bindings/speaker/pcm_speaker_crown.go:163`) is reused
as-is. The `alsa.PCM` field becomes a small interface (`Write([]byte) (int,
error)`, `Close()`) so a socket-backed implementation can satisfy it without
touching the mixing loop.

## Format (fixed, no handshake)

Current ALSA config (`pcm_speaker_crown.go:88-92`): 48000 Hz, stereo, S16LE,
period = 1536 frames = 6144 bytes. The socket protocol reuses this exactly —
no negotiation needed, both sides are built from the same repo and deployed
together. If this ever needs to change, bump a version byte in the frame
header rather than adding a runtime handshake.

## Transport: filesystem-namespace Unix domain socket

Path: `<launcher's getFilesDir()>/pcm.sock` — e.g.
`/data/data/com.echomuse.crownlauncher/files/pcm.sock`. Created by the Java
service via `LocalServerSocket`/`LocalSocketAddress.Namespace.FILESYSTEM`
(not Android's default abstract namespace — a real path is what lets the Go
side use a plain `net.Dial("unix", path)` with no Android-specific socket
API on that end).

Framing: 4-byte little-endian length prefix + that many raw PCM bytes. One
frame per period (6144 bytes) is the expected steady state; the length
prefix exists so a future format-version bump or partial/short frame
doesn't have to guess.

## Q1 — does the socket work across both launch paths? **Answered, no device needed.**

Two ways the daemon gets launched today (handoff doc, both confirmed live):

1. **Via `ServerService`** (`ServerService.java:40`): `new
   ProcessBuilder(BINARY).start()` — the child inherits the **service's own
   uid** (the app's sandboxed uid, `u0_a148` per the handoff doc). The
   socket file lives under `getFilesDir()`, which Android creates as
   `0700`, owned by that same uid — a same-uid child can always open it, no
   special permission needed.
2. **Via plain `adb root` shell exec** (the "always worked" pre-APK method,
   also used throughout today's freeze testing): the process runs as
   **root**. Root bypasses DAC permission checks entirely — it can open any
   socket file regardless of owning uid.

Both paths connect successfully with **zero permission changes** needed
anywhere (no ueventd-style ownership patch, unlike the earlier `/dev/snd`
fix). This was fully answerable from the existing `ServerService.java` and
the handoff doc's uid findings — confirmed without touching hardware.

## Q3 — failure handling. **Designed, no device needed to validate the logic.**

The whole point of this change is to stop wedging the DSP when something
goes wrong; a design that can itself hang because the *socket* misbehaves
would be self-defeating. Rules:

- **Go side never blocks indefinitely on the socket.** `silenceLoop`'s
  current `p.pcm.Write(out)` is a blocking ALSA write paced by the hardware
  ring — the new socket writer gets a **write deadline** (one period's worth
  of wall-clock time, ~32ms at this rate) via `SetWriteDeadline`. A missed
  deadline drops that period (counts as an underrun, same accounting
  `report()` already does) rather than stalling the mix loop.
- **Reconnect, don't crash.** If the socket is closed or the Java service is
  mid-restart (it's `START_STICKY`, per `ServerService.java:47` — Android
  restarts it, but not instantly), the Go side keeps mixing and drops
  periods (writes to nothing / drops on deadline) while attempting a
  reconnect on a short backoff (e.g. 250ms, capped) in the background,
  swapping the live connection in once it succeeds. No period is ever
  queued waiting for a connection that might not come back.
- **Java side accepts one connection at a time**, closes any prior one on a
  new connect (the daemon only ever has one instance), and on socket error
  releases the `AudioTrack` and recreates it on the next connection —
  mirroring `ServerService`'s existing "no supervisor, minimal" philosophy
  rather than adding new retry/health-check machinery on that side.
- **Nothing here calls into `stop <service>` or touches any other app** —
  consistent with the mic binding's existing comment that "nothing in the
  audioserver family may ever be added here."

This is a straightforward reconnect-with-backoff + deadline-write pattern;
none of it needs a real device to design or code-review, only to confirm the
measured deadline value is sane once real latency numbers exist (see Q2).

## Q2 — is low-latency mode actually granted on this hardware? **Needs a device. Probe is ready.**

This is the one question that can kill the whole scenario: if
`AudioTrack` silently falls back to the shared/legacy mixer path on this
SoC/ROM instead of the fast/low-latency path, the extra buffering could
make barge-in noticeably worse than the current raw-ALSA behavior — and
there's no way to know without asking the actual device.

**Throwaway probe added**: `device/crown_launcher/src/com/echomuse/crownlauncher/AudioProbeReceiver.java`
(new, see below) — a broadcast receiver, triggered by
`adb shell am broadcast -a com.echomuse.crownlauncher.PROBE_AUDIO
-n com.echomuse.crownlauncher/.AudioProbeReceiver`, that:

1. Builds an `AudioTrack` with the exact format/usage this design would
   actually use (48kHz stereo S16LE, `USAGE_ASSISTANT`,
   `CONTENT_TYPE_SPEECH`, `PERFORMANCE_MODE_LOW_LATENCY` requested).
2. Logs (`adb logcat -s EchoMuseProbe`) what was **actually granted**:
   `getPerformanceMode()` (does it agree with what was requested, or fall
   back to `PERFORMANCE_MODE_NONE`?), `getBufferSizeInFrames()`, and the
   system-wide fast-mixer indicators `AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER`
   / `PROPERTY_OUTPUT_SAMPLE_RATE`.
3. Plays a short (~1s) test tone and releases — confirms the track actually
   produces sound at all on this build, not just that it constructs
   without throwing.

Answer takes under a minute once a device is attached: install the APK,
run the broadcast, read three log lines. **Run this before writing a single
line of the real socket/service code** — a `PERFORMANCE_MODE_NONE` result
doesn't kill Scenario C outright (legacy-mixer AudioTrack is still strictly
safer than raw ALSA contention), but it does mean the "keep it open
continuously, no latency regression" assumption above needs re-checking
against a measured number instead of an aspiration.

### Result (2026-08-26, run live on `G0916D10014507JS`)

```
AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE=48000
AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER=768
AudioTrack.getMinBufferSize=18464 bytes
requested PERFORMANCE_MODE_LOW_LATENCY, granted: LOW_LATENCY (granted as requested)
getBufferSizeInFrames=4616
getSampleRate=48000
getState=1 (1=INITIALIZED expected)
wrote 96000 of 96000 samples
```

**PROVEN: low-latency mode is granted, not simulated, not a fallback.**
`getPerformanceMode()` returned exactly what was requested — this ROM/SoC
does have a working fast-mixer path, contrary to the risk this section
flagged going in. `PROPERTY_OUTPUT_FRAMES_PER_BUFFER=768` at 48kHz is a
16ms native mixer period, in line with a real fast-mixer config, not the
larger legacy-path buffer sizes (typically 20-40ms+) that would have shown
up under `PERFORMANCE_MODE_NONE`. Test tone played and was **audibly heard**
on the actual device speaker (confirmed by the user live), not just
constructed without throwing.

One number to note, not yet fully explained: `getBufferSizeInFrames=4616`
(~96ms) is considerably larger than the requested `getMinBufferSize`
translated to frames (18464 bytes / 4 bytes-per-frame = 4616 frames —
**they're identical**, so this is simply what was asked for via
`.setBufferSizeInBytes(Math.max(minBuf, 4096))`, not something the low-latency
path silently inflated). The *native* low-latency period is the 768-frame
(16ms) figure above; the 4616-frame client buffer is just this probe's own
conservative sizing and isn't representative of what the real design should
request — the real service should size its `AudioTrack` buffer close to the
768-frame native period, not reuse this probe's oversized default, to get
the actual latency win this mode is proving is available.

**This clears the biggest open risk in Scenario C.** Barge-in/duck latency
still needs measuring end-to-end once the real socket path exists (this
probe only proves the mode is available, not the full round-trip cost
through our own mixer + socket + AudioFlinger), but the worst-case outcome
this section worried about — silent fallback to the shared legacy mixer —
is ruled out.

## Sequencing

1. **Run the Q2 probe** the moment a device is available — answers whether
   to budget for a latency regression before any other code is written.
2. Build the real `LocalServerSocket` + `AudioTrack` playback service in
   `crown_launcher`, using the format/framing/failure-handling above.
3. Swap `pcm_speaker_crown.go`'s `alsa.PCM` for the socket-backed writer
   behind the same small interface; mixing/ducking code untouched.
4. Live test: full voice turn + concurrent YouTube/browser load (the
   existing freeze repro) — this is the test that actually validates the
   scenario, everything before it is just de-risking getting there.
5. If 4 holds up, decide whether audio focus (`AudioManager.requestAudioFocus`,
   transient, per turn) should replace the daemon's own `duckDb` logic — a
   separate, smaller follow-up, not blocking the freeze fix itself.
