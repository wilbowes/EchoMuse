# The Voice Pipeline, Explained

What actually happens between you saying "Hey Rhasspy, turn off the lights"
and the lights going off — stage by stage, in plain language, with the
benefits and trade-offs of each design choice.

The one-sentence version: **the Dot is deliberately dumb** — it captures
sound as cleanly as possible and streams it out; all the intelligence
(recognising the wake word, deciding when you've finished speaking,
understanding you) lives on the controller and in Home Assistant, where it
can be updated, tuned, and observed without touching the hardware.

```
 YOUR VOICE
    │
    ▼
┌─ On the Echo Dot ───────────────────────────────────────────┐
│  7 microphones → gain boost → echo cancel → mic selection   │
└──────────────────────────────│──────────────────────────────┘
                               │  continuous audio stream (WiFi)
                               ▼
┌─ On the controller ─────────────────────────────────────────┐
│  wake-word spotting → conversation management → sound shaping│
└──────────────────────────────│──────────────────────────────┘
                               │
                               ▼
┌─ In Home Assistant ─────────────────────────────────────────┐
│  speech-to-text → understanding → action → text-to-speech   │
└──────────────────────────────│──────────────────────────────┘
                               │  spoken response
                               ▼
                     back to the Dot's speaker
```

---

## Stage 1 — Seven microphones

The Dot has 6 microphones in a ring plus 1 in the centre, all captured
together, 16,000 times per second in high-precision 24-bit audio.

**Benefit:** hearing from every direction at once, plus the raw material for
knowing *which direction* you spoke from.

**Caveat:** they're tiny microphones in a small puck sitting in your room —
they hear the TV, the dishwasher, and the Dot's own speaker just as keenly
as they hear you. Most of the rest of the pipeline exists to deal with that.

## Stage 2 — Gain boost ("mic gain")

The raw capture is *extremely* quiet — measurements showed normal speech
using only a tiny fraction of the available signal range, and the old
processing threw the quietest (most information-rich) part away when
converting the audio for transmission. The fix: amplify the full-precision
24-bit signal by 24dB (≈16×) *before* that conversion, keeping detail that
would otherwise be lost forever.

**Benefit:** this single change took speech recognition from "fails on 1 in
3 requests" to "reliable" in real-room testing. It's the foundation
everything downstream stands on.

**Caveat:** a fixed boost means a very loud event (a shout next to the
device) can hit the ceiling and distort briefly. The device counts these
"clipped" moments in its log; in practice, even TV-at-movie-volume produces
zero.

## Stage 3 — Echo cancellation (AEC)

When the Dot is speaking, its microphones hear its own voice — loudly. AEC
keeps a copy of exactly what the speaker is playing and mathematically
subtracts it from what the mics hear, leaving only *other* sounds — like you
interrupting.

**Benefit:** follow-up questions work properly (the device can hear you over
the tail of its own response), and its own speech can't trigger or confuse
the listening logic. It's also the prerequisite for true "barge-in" —
interrupting the assistant mid-sentence — which is on the roadmap.

**Caveats:** it only removes the *Dot's own* sound — it does nothing about
the TV (that's a different problem; see Stage 8). And it needs a per-home
tune of one number (the delay between "played" and "heard" — see the
configuration guide). It ships disabled until you've turned it on and
sanity-checked it.

## Stage 4 — Microphone selection ("beamforming" + "lock-back")

For idle listening, the device always uses the centre microphone — it hears
all directions equally, so the wake word works wherever you stand. When you
*do* wake it, the device switches to the ring microphone facing you, which
hears you a little better and the rest of the room a little worse.

The subtle part is *how it picks*: by the time the controller has recognised
the wake word, half a second has passed and the sound of you saying it has
faded. So the device continuously keeps a two-second memory of how much
sound energy came from each direction, and when the wake arrives, it looks
**back** through that memory to find where the wake word actually came from
— not where sound is coming from right now. It also scores directions by
*sudden change* rather than raw loudness, so a voice beats a permanently
loud TV.

**Benefit:** better speech-to-text from the mic pointed at you, and the LED
direction indicator actually points at you.

**Caveats:** one selected mic is a modest improvement, not a magic zoom
lens. And in the gap between conversations, the device's own speech can
linger in that two-second memory — follow-up conversations get the weaker
version of this feature until barge-in/AEC work matures.

## Stage 5 — The continuous stream

Every 32 milliseconds, the processed audio is sent over WiFi to the
controller. Always. There is deliberately **no** "only send when it sounds
like speech" gate on this stream.

**Benefit:** the wake-word recogniser sees smooth, uninterrupted audio,
which measurably improves its accuracy — and there's no on-device logic
that can drift, misjudge your room, or degrade over days (both of which
actually happened with earlier, cleverer designs; boring won).

**Caveat:** a constant ~32KB/s per device on your WiFi — about 1/6th of
what streaming the *response* audio uses, so in practice a non-issue on any
home network. And to be clear about privacy: the stream goes to *your*
controller on *your* LAN and nowhere else.

## Stage 6 — Wake-word spotting

The controller runs openwakeword, a small neural network, over each
device's stream, scoring every moment: "how much did that sound like the
wake word?" Cross the sensitivity bar and the conversation starts.

**Benefit:** because this runs on the controller rather than the Dot, you
can change the wake word or sensitivity live from the dashboard, see every
detection *and* every near-miss in the Status tab, and future improvements
don't need firmware updates.

**Caveat:** it's a probability, not a certainty — the sensitivity slider is
a false-accepts vs. false-rejects trade-off you tune to your room (the
near-miss counter exists precisely to make that tuning informed rather than
vibes-based).

## Stage 7 — The conversation ("turn")

On wake: the LED goes green, the device's mic selection locks toward you,
and the controller pipes your audio to Home Assistant, which decides when
you've stopped talking (its own speech detector does this — with a
controller-side backstop that quietly ends things after 5 seconds if a
false wake meant nobody was speaking, judged against that room's measured
background noise level).

**Benefit:** endpointing ("has the user finished?") is done by Home
Assistant's well-maintained detector rather than home-grown logic, and the
false-wake backstop adapts to each room by itself — a quiet study and a
loud lounge get equally sensible behaviour with zero tuning.

**Caveat:** in a noisy room, the detector sometimes hangs on a beat too
long and the tail of TV dialogue rides along into speech-to-text (you'll
occasionally see a stray phrase appended to your transcript). Cleaning the
audio sent to speech-to-text is the next planned fix for this.

## Stage 8 — Speech-to-text, understanding, action

Home Assistant's Assist pipeline takes over: your speech becomes text
(Whisper or whichever STT you've configured), the text becomes intent
("turn off + kitchen lights"), the action happens, and a reply is composed.

**Benefit:** this is all standard, well-documented Home Assistant machinery
— every STT/LLM/TTS option HA supports works, and EchoMuse doesn't need to
know anything about it.

**Caveat:** it's also where most of the *time* goes (transcription and
response generation are the slow steps, especially on modest hardware), and
where background-noise transcription errors ultimately land. Better mics and
cleaner audio help; they can't fully substitute for a good STT model.

## Stage 9 — The response

The reply audio comes back through the controller, which shapes the sound
(the EQ from the configuration guide — the raw speaker is boomy),
resamples it to what the hardware wants, and streams it to the Dot, which
plays it while a copy is fed to the echo canceller (Stage 3) so the mics
can subtract it.

**Benefit:** centrally-applied EQ means every device gets consistent,
tuned sound, adjustable live from the dashboard.

**Caveat:** while the wake-word recogniser is suppressed during playback
(so the device can't wake itself), you currently can't interrupt a response
by voice — barge-in is the next major feature now that AEC provides its
foundation.

---

## Design principles, if you're wondering "why is it like this?"

1. **Dumb device, smart controller.** Anything that can drift, misjudge, or
   need tuning lives where it can be observed and updated without touching
   hardware. The Dot captures, amplifies, cancels its own echo, and streams
   — that's it.
2. **Measure, don't modify.** The controller tracks each room's noise floor
   and uses it to make *decisions* (is anyone speaking?), but never rewrites
   the audio on its way to speech-to-text. Adaptive audio-mangling is how
   the system's worst historical bugs happened.
3. **Boring and continuous beats clever and gated.** The always-on,
   unprocessed wake stream replaced a cleverer design that degraded over
   days. When in doubt, the pipeline chooses the predictable option.
