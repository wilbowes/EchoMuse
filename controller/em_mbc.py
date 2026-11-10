"""
em_mbc.py — dynamic bass guard for the speaker path.

Sits between the EQ and the peak limiter. The limiter stops the SIGNAL
clipping; this stops the DRIVER being asked for excursion it does not have.

WHY THIS EXISTS, AND WHY IT IS THE BAND IT IS
---------------------------------------------
The parameters are not invented. They are what the stock firmware runs on
this exact speaker, read off a device from
`/system/vendor/etc/audio-algorithms/MBCL.cfg` (#229):

    crossovers  115 / 500 / 7500 Hz
    band 1    0-115 Hz     ratio 20:1  threshold -50dB  floor -40dB
    band 2  115-500 Hz     ratio  2:1  threshold -10dB  floor -40dB
    band 3  500-7500 Hz    ratio  2:1  threshold -10dB  floor -40dB
    band 4  7500Hz-Nyq     ratio  2:1  threshold -10dB  floor -40dB

**Band 1 is not compression, it is dynamic bass removal**, and it is the
whole point. 20:1 from a threshold of -50dBFS means essentially nothing below
115Hz survives at a normal listening level, while quiet content keeps its low
end. That is the correct answer for a driver this small, and it is the
opposite of what the symptom suggests: "tin-can" sounds like missing bass, so
the instinct is to boost it — which spends excursion on frequencies the
driver cannot produce, and the resulting cone movement intermodulates
everything above it into mud. Removing that content is what makes the
midrange clean.

**Bands 2-4 are deliberately not implemented.** They are one gentle 2:1 law
at -10dB repeated three times, which is broadband compression for loudness
rather than protection — a taste decision that wants a listening test and a
measured driver response behind it, neither of which exists yet. Implementing
them would also mean a three-crossover tree with allpass compensation on
every branch, for a benefit nobody has heard. One crossover is exact and
provable; see below.

THE CROSSOVER IS LINKWITZ-RILEY, AND THE FIRST ATTEMPT WAS NOT
--------------------------------------------------------------
Two bands split by an LR4 pair (Butterworth 2nd order applied twice). LR4's
defining property is that its lowpass and highpass SUM FLAT — measured across
the spectrum at 4096 points, |LP+HP| deviates by 0.0000dB — so with no
compression active this processor does not colour anything.

The first implementation split subtractively (`rest = x - lowpass`), which is
exactly reconstructing by construction and looks obviously right. It does not
work: at 60Hz the lowpass passes 0.998 of the signal and the residual is
**1.279**, larger than the input, because subtracting a phase-shifted copy is
not the same as removing a band. Bass reduction of 20dB in band 1 produced a
measured 0.4dB at the output. Found by measurement, not by reading it.
"""

import math

import numpy as np

import em_limiter

# Crossover, Hz. Measured off stock (#229), not chosen.
CROSSOVER_HZ = 115.0


def _signal():
    """scipy.signal on first use; see em_eq._signal and em_eq.preload."""
    import scipy.signal
    return scipy.signal


# Bass band law, also from stock.
BASS_RATIO        = 20.0
BASS_THRESHOLD_DB = -50.0
BASS_RELEASE_MS   = 200.0

# How far the bass band may be pulled down. Stock uses -40dB.
#
# We default SHALLOWER, deliberately: stock's -40 sits in front of stock's own
# EQ, which we do not have and have not measured, so copying the depth without
# the curve it was tuned against is not the same setting.
#
# -30 rather than -20 after the first listening test (2026-08-20, drum and
# bass on Test Device 01): it sits mid-range, so there is room to move in
# BOTH directions without hitting an end stop. Note the choice is nearly
# free either way — across the entire -40..0 range this moves the OVERALL
# level 0.14dB, so no one will hear the default change. What is audible is
# the guard being on at all (-5.0dB overall, -17.7dB at 50Hz), which is
# `bassGuardEnabled`, not this. Tune with the toggle; this is headroom.
DEFAULT_BASS_GUARD_DB = -30.0

# The three bands above the guard, for the stock-sound profile (#229): stock's
# MBCL splits at 500Hz and 7.5kHz and gives each band a gentle 2:1 law from
# -10dB with a -40dB floor. This is what lets stock run its top volume steps
# into the driver correction without crushing the whole signal at the
# limiter. Release is NOT in stock's config; 100ms is our choice, to be
# checked against a stock Echo by ear.
UPPER_CROSSOVERS_HZ = (500.0, 7500.0)
# (ratio, threshold dBFS, attack ms) per band: 115-500Hz, 500Hz-7.5kHz,
# above. The
# first is harder than stock's 2:1 from -10: this driver's 2nd harmonic at
# 150-300Hz rises with level (250Hz: 7.5% at -18dBFS, 37% at -3, VVV
# 2026-10-18), and at 85-100% volume 2:1 from -10 left the limiter doing the
# work, heard as pumping and some distortion. 3:1 from -16 was picked by ear
# over that and over 4:1 from -20 (thinner) on voice and music. Its attack
# is 20ms: at 10ms the harder law put -48dB of junk on the rising-syllable
# test, 20ms gives -53 (as 2:1 did) for 1dB more peak into the limiter.
UPPER_LAWS = ((3.0, -16.0, 20.0), (2.0, -10.0, 10.0), (2.0, -10.0, 10.0))
UPPER_FLOOR_DB = -40.0
UPPER_RELEASE_MS = 100.0
# Stock's band 1 floors at -40dB too. The profile's +26dB correction at
# 150-200Hz puts speech far over the bass threshold, so a -30dB floor left
# the low band 10dB hotter than stock and the driver distorted (VVV,
# 2026-10-16). The deeper of this and the user's guard depth applies.
STOCK_BASS_FLOOR_DB = -40.0
# Hold before release, stock-sound profile only. Without it the gain
# recovers between a waveform's peaks and is cut again on the next, a
# ripple at the signal's own frequency: 1% THD at 150-200Hz and 3.5% at
# 60Hz through the profile at half volume, heard as distortion on VVV
# (2026-10-16). Each is longer than half a period of the band's lowest
# content (40Hz for the bass band, 115Hz above it).
STOCK_BASS_HOLD_MS = 20.0
STOCK_UPPER_HOLD_MS = 30.0
# The three upper bands do not cut instantly: with an instant attack the gain
# stepped down on every new peak of a rising syllable, which is broadband
# grit at any volume where speech crosses -10dB (about -40dB of junk on two
# tones, VVV 2026-10-16). Their held target is smoothed by a one-pole with
# the attack in UPPER_LAWS (-47..-57dB), and the 30ms hold covers what the slower
# attack lets through between peaks. The bass band keeps the guard's instant
# attack: it is protection, and was never the grit.

_FULL_SCALE = 32768.0
_EPS = 1e-9


def _allpass2(fc: float, fs: int) -> np.ndarray:
    """The second-order allpass an LR4 crossover's two halves sum to
    (Butterworth poles, Q = 1/sqrt(2)), as one SOS row. Run on a band that
    skips a crossover, it gives that band the same phase as the bands that
    went through it, so the split still sums flat."""
    w0 = 2 * math.pi * fc / fs
    alpha = math.sin(w0) / (2 * (1 / math.sqrt(2)))
    a0, a1, a2 = 1 + alpha, -2 * math.cos(w0), 1 - alpha
    return np.array([[a2 / a0, a1 / a0, 1.0, 1.0, a1 / a0, a2 / a0]])


def _lr4(fc: float, fs: int, kind: str) -> np.ndarray:
    """
    Linkwitz-Riley 4th order = Butterworth 2nd order applied twice.

    Clamped below Nyquist so an odd sample rate degrades rather than raising.
    """
    wn = min(fc / (fs * 0.5), 0.99)
    sos = _signal().butter(2, wn, btype=kind, output="sos")
    return np.vstack([sos, sos])


def crossover_flatness_db(fc: float = CROSSOVER_HZ, fs: int = 48000) -> float:
    """
    Peak-to-peak deviation of |LP + HP| across the spectrum, in dB.

    Exposed so the flat-sum property is a measurement in the test suite
    rather than a claim in a comment.
    """
    _, hl = _signal().sosfreqz(_lr4(fc, fs, "low"), worN=4096, fs=fs)
    _, hh = _signal().sosfreqz(_lr4(fc, fs, "high"), worN=4096, fs=fs)
    mag = np.abs(hl + hh)
    return float(20.0 * np.log10(mag.max() / max(mag.min(), _EPS)))


class _BandGain:
    """
    A band's detector and gain computer.

    Separate from the filtering so the gain law can be tested on its own: it
    is a static curve plus a release, and both are easy to get subtly wrong
    in ways that surface as a pumping artefact rather than as an error.
    """

    def __init__(self, ratio, threshold_db, release_ms, floor_db, fs, hold_ms=0.0,
                 attack_ms=0.0):
        self.ratio = max(1.0, float(ratio))
        # The target is held at its minimum over this many trailing samples
        # (1 = no hold); the tail carries the window across chunks.
        self._hold = max(1, int(round(float(hold_ms) * fs / 1000.0)))
        self._tail = np.zeros(self._hold - 1)
        self.threshold_db = float(threshold_db)
        self.floor_db = min(0.0, float(floor_db))
        self._slew = (em_limiter.RELEASE_REFERENCE_DB
                      / (max(0.1, float(release_ms)) / 1000.0) / fs)
        # One-pole smoothing of the held target (0 = instant attack), and
        # its carried output.
        self._attack = (1.0 - math.exp(-1.0 / (float(attack_ms) * fs / 1000.0))
                        if attack_ms > 0 else 0.0)
        self._smooth_db = 0.0
        self._gain_db = 0.0
        self.max_reduction_db = 0.0

    def gains_db(self, x: np.ndarray) -> np.ndarray:
        """Per-sample gain in dB (<= 0) for this band's samples."""
        level_db = 20.0 * np.log10(np.maximum(np.abs(x) / _FULL_SCALE, _EPS))

        # Static curve: above the threshold, keep 1/ratio of the excess.
        over = np.maximum(level_db - self.threshold_db, 0.0)
        target_db = np.maximum(-over * (1.0 - 1.0 / self.ratio), self.floor_db)
        if self._hold > 1:
            buf = np.concatenate([self._tail, target_db])
            # A trailing window: element i is the minimum of buf[i-hold+1..i].
            from scipy.ndimage import minimum_filter1d
            held = minimum_filter1d(buf, self._hold, origin=(self._hold - 1) // 2, mode="nearest")
            self._tail = buf[-(self._hold - 1):]
            target_db = held[self._hold - 1:]
        if self._attack:
            a = self._attack
            target_db, _ = _signal().lfilter([a], [1.0, -(1.0 - a)], target_db,
                                             zi=[(1.0 - a) * self._smooth_db])
            self._smooth_db = float(target_db[-1])

        # Instant attack (onto the smoothed target, where there is one),
        # slew-limited release. Instant attack is the right
        # choice for PROTECTION: the excursion happens on the transient, so a
        # compressor that takes 10ms to respond has already let it through.
        # Written as a running minimum in a sheared coordinate system rather
        # than the sequential recursion it describes — see em_limiter.
        n = np.arange(target_db.size, dtype=np.float64)
        sheared = target_db - self._slew * n
        sheared[0] = min(sheared[0], self._gain_db + self._slew)
        gain_db = np.minimum(self._slew * n + np.minimum.accumulate(sheared), 0.0)

        self._gain_db = float(gain_db[-1])
        self.max_reduction_db = max(self.max_reduction_db, float(-gain_db.min()))
        return gain_db


class BassGuard:
    """
    Streaming two-band compressor: a hard dynamic law below the crossover,
    unity above it.

    One instance per audio stream — it carries filter and gain state, so
    sharing one would let a voice response compress the music underneath it.
    """

    def __init__(self, sample_rate: int,
                 bass_guard_db: float = DEFAULT_BASS_GUARD_DB,
                 crossover_hz: float = CROSSOVER_HZ,
                 enabled: bool = True,
                 upper_bands: bool = False):
        self.sample_rate = int(sample_rate)
        # Bypassed rather than absent, so a stream can be toggled without
        # dropping the instance — see set_params. The stock-sound profile
        # keeps it on: the voicing was tuned with it (outchain chain.apply).
        self.enabled = bool(enabled) or bool(upper_bands)
        self.bass_guard_db = min(0.0, float(bass_guard_db))
        self.crossover_hz = float(crossover_hz)

        self._lp = _lr4(self.crossover_hz, self.sample_rate, "low")
        self._hp = _lr4(self.crossover_hz, self.sample_rate, "high")
        self._zl = np.zeros((self._lp.shape[0], 2))
        self._zh = np.zeros((self._hp.shape[0], 2))

        # Fixed at construction: the extra crossovers own filter state, so
        # turning them on mid-stream would start them cold (a phase step).
        self.upper_bands = bool(upper_bands)
        self._bass = _BandGain(BASS_RATIO, BASS_THRESHOLD_DB,
                               BASS_RELEASE_MS, self._bass_floor_db(),
                               self.sample_rate,
                               STOCK_BASS_HOLD_MS if self.upper_bands else 0.0)
        if self.upper_bands:
            f2, f3 = UPPER_CROSSOVERS_HZ
            self._xo = [_lr4(f2, self.sample_rate, "low"), _lr4(f2, self.sample_rate, "high"),
                        _lr4(f3, self.sample_rate, "low"), _lr4(f3, self.sample_rate, "high")]
            self._xz = [np.zeros((s.shape[0], 2)) for s in self._xo]
            # The bass band skips both upper crossovers; their allpass keeps
            # its phase with the rest (0.64dB of ripple without; 0.0000 with
            # both allpasses, crossover_flatness of the 4-band sum).
            self._lo_ap = np.vstack([_allpass2(f2, self.sample_rate), _allpass2(f3, self.sample_rate)])
            self._lo_apz = np.zeros((2, 2))
            # Likewise the 115-500Hz band skips the 7.5kHz crossover.
            self._b2_ap = _allpass2(f3, self.sample_rate)
            self._b2_apz = np.zeros((1, 2))
            self._upper = [_BandGain(ratio, threshold, UPPER_RELEASE_MS,
                                     UPPER_FLOOR_DB, self.sample_rate, STOCK_UPPER_HOLD_MS,
                                     attack)
                           for ratio, threshold, attack in UPPER_LAWS]

    def _bass_floor_db(self) -> float:
        if self.upper_bands:
            return min(self.bass_guard_db, STOCK_BASS_FLOOR_DB)
        return self.bass_guard_db

    @property
    def max_reduction_db(self) -> float:
        """Worst reduction so far — the instrument for whether this is doing
        anything, and for whether it is doing too much."""
        return round(self._bass.max_reduction_db, 2)

    def set_params(self,
                   bass_guard_db: float | None = None,
                   enabled: bool | None = None) -> None:
        """
        Change the guard mid-stream, without touching carried state.

        Depth is the parameter that wants tuning by ear in a real room, and
        the chain used to be built once per stream — so hearing a change meant
        skipping the track, which makes an A/B nearly impossible to judge.

        The crossover is deliberately NOT settable: it owns the filter state,
        so moving it mid-stream would mean rebuilding the biquads and either
        carrying incompatible state or zeroing it, which is an audible thump
        at exactly the moment someone is listening for a difference. It is a
        measured value off the hardware, not a taste one.
        """
        if bass_guard_db is not None:
            self.bass_guard_db = min(0.0, float(bass_guard_db))
            self._bass.floor_db = self._bass_floor_db()
        if enabled is not None:
            self.enabled = bool(enabled) or self.upper_bands

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Compress one chunk. Returns exactly as many samples as given."""
        if samples.size == 0:
            return samples
        x = np.asarray(samples, dtype=np.float64)
        low, self._zl = _signal().sosfilt(self._lp, x, zi=self._zl)
        high, self._zh = _signal().sosfilt(self._hp, x, zi=self._zh)
        if self.upper_bands:
            return self._process_upper(low, high)
        if not self.enabled:
            # Bypassed, but still filtered. LR4's two halves sum MAGNITUDE-
            # flat (crossover_flatness_db measures 0.0000dB); the sum is an
            # allpass, not the identity, so this is not `return x` and must
            # not be simplified into one. That is the point: the signal takes
            # the same path in both states, so toggling changes only the gain
            # law and cannot click. Returning x instead would step the phase
            # at the toggle, and skipping the filters would leave them cold to
            # ring on re-enable — both audible as a thump at exactly the
            # moment someone is listening for the difference.
            return low + high
        return low * (10.0 ** (self._bass.gains_db(low) / 20.0)) + high


    def _upper_split(self, high: np.ndarray):
        b2, self._xz[0] = _signal().sosfilt(self._xo[0], high, zi=self._xz[0])
        b2, self._b2_apz = _signal().sosfilt(self._b2_ap, b2, zi=self._b2_apz)
        rest, self._xz[1] = _signal().sosfilt(self._xo[1], high, zi=self._xz[1])
        b3, self._xz[2] = _signal().sosfilt(self._xo[2], rest, zi=self._xz[2])
        b4, self._xz[3] = _signal().sosfilt(self._xo[3], rest, zi=self._xz[3])
        return b2, b3, b4

    def _process_upper(self, low: np.ndarray, high: np.ndarray) -> np.ndarray:
        """Four bands: the guard's law below the crossover, the gentle law
        on the three above. Bypassed, the bands are still split and summed,
        for the reason the two-band path does (no step at a toggle)."""
        bands = self._upper_split(high)
        low, self._lo_apz = _signal().sosfilt(self._lo_ap, low, zi=self._lo_apz)
        if not self.enabled:
            return low + sum(bands)
        out = low * (10.0 ** (self._bass.gains_db(low) / 20.0))
        for b, law in zip(bands, self._upper):
            out = out + b * (10.0 ** (law.gains_db(b) / 20.0))
        return out


def for_stream(sample_rate: int,
               enabled: bool,
               bass_guard_db: float = DEFAULT_BASS_GUARD_DB
               ) -> "BassGuard | None":
    """
    Build one for a stream, or None when disabled.

    Takes plain values rather than a Device, for em_limiter.for_stream's
    reason: em_player cannot import em_controller, and the test suite cannot
    import either.
    """
    if not enabled:
        return None
    return BassGuard(sample_rate, bass_guard_db=bass_guard_db)
