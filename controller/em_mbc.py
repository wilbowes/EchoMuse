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
import em_radar_tuning

# Crossover, Hz. Measured off stock (#229), not chosen. This is biscuit's
# band 1 (0-115Hz) from /system/vendor/etc/audio-algorithms/MBCL.cfg.
CROSSOVER_HZ = 115.0

def _signal():
    """scipy.signal on first use; see em_eq._signal and em_eq.preload."""
    import scipy.signal
    return scipy.signal


# Bass band law, also from stock (biscuit).
BASS_RATIO        = 20.0
BASS_THRESHOLD_DB = -50.0
BASS_RELEASE_MS   = 200.0

# Radar's own MBCL.cfg ("Radar Tuning V4.5") is a real 4-band multiband
# compressor, not a copy of biscuit's. Only band 1 is ported here, same as
# biscuit — see the module docstring for why bands 2-4 are not. Its crossover
# (70Hz, not 115) and threshold (-25dB, not -50) are measured off a Radar
# unit's own firmware; ratio and release are the same on both boards.
RADAR_CROSSOVER_HZ     = 70.0
RADAR_BASS_THRESHOLD_DB = -25.0

# Per-board tuning, keyed the same way device/pkg/board.Board.ID is on the
# other side of this port. An unrecognised id (including None) gets
# biscuit's numbers — the only board this was ever measured against until
# Radar, and the safer of the two to default an unknown unit to.
_BOARD_TUNING = {
    "biscuit": (CROSSOVER_HZ, BASS_THRESHOLD_DB),
    "radar":   (RADAR_CROSSOVER_HZ, RADAR_BASS_THRESHOLD_DB),
}


def _tuning_for(board_id: str | None) -> tuple[float, float]:
    return _BOARD_TUNING.get(board_id or "biscuit", _BOARD_TUNING["biscuit"])

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

_FULL_SCALE = 32768.0
_EPS = 1e-9


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

    def __init__(self, ratio, threshold_db, release_ms, floor_db, fs):
        self.ratio = max(1.0, float(ratio))
        self.threshold_db = float(threshold_db)
        self.floor_db = min(0.0, float(floor_db))
        self._slew = (em_limiter.RELEASE_REFERENCE_DB
                      / (max(0.1, float(release_ms)) / 1000.0) / fs)
        self._gain_db = 0.0
        self.max_reduction_db = 0.0

    def gains_db(self, x: np.ndarray) -> np.ndarray:
        """Per-sample gain in dB (<= 0) for this band's samples."""
        level_db = 20.0 * np.log10(np.maximum(np.abs(x) / _FULL_SCALE, _EPS))

        # Static curve: above the threshold, keep 1/ratio of the excess.
        over = np.maximum(level_db - self.threshold_db, 0.0)
        target_db = np.maximum(-over * (1.0 - 1.0 / self.ratio), self.floor_db)

        # Instant attack, slew-limited release. Instant attack is the right
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
                 threshold_db: float = BASS_THRESHOLD_DB,
                 enabled: bool = True):
        self.sample_rate = int(sample_rate)
        # Bypassed rather than absent, so a stream can be toggled without
        # dropping the instance — see set_params.
        self.enabled = bool(enabled)
        self.bass_guard_db = min(0.0, float(bass_guard_db))
        self.crossover_hz = float(crossover_hz)
        self.threshold_db = float(threshold_db)

        self._lp = _lr4(self.crossover_hz, self.sample_rate, "low")
        self._hp = _lr4(self.crossover_hz, self.sample_rate, "high")
        self._zl = np.zeros((self._lp.shape[0], 2))
        self._zh = np.zeros((self._hp.shape[0], 2))

        self._bass = _BandGain(BASS_RATIO, self.threshold_db,
                               BASS_RELEASE_MS, self.bass_guard_db,
                               self.sample_rate)

    @property
    def max_reduction_db(self) -> float:
        """Worst reduction so far — the instrument for whether this is doing
        anything, and for whether it is doing too much."""
        return round(self._bass.max_reduction_db, 2)

    @property
    def raw_max_reduction_db(self) -> float:
        """Same, full precision — what test vectors are generated from, and
        the counterpart gen_vectors.py uses uniformly across both this
        class and RadarMultiband."""
        return self._bass.max_reduction_db

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
            self._bass.floor_db = self.bass_guard_db
        if enabled is not None:
            self.enabled = bool(enabled)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Compress one chunk. Returns exactly as many samples as given."""
        if samples.size == 0:
            return samples
        x = np.asarray(samples, dtype=np.float64)
        low, self._zl = _signal().sosfilt(self._lp, x, zi=self._zl)
        high, self._zh = _signal().sosfilt(self._hp, x, zi=self._zh)
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


def for_stream(sample_rate: int,
               enabled: bool,
               bass_guard_db: float = DEFAULT_BASS_GUARD_DB,
               board_id: str | None = None,
               ) -> "BassGuard | RadarMultiband | None":
    """
    Build one for a stream, or None when disabled.

    Takes plain values rather than a Device, for em_limiter.for_stream's
    reason: em_player cannot import em_controller, and the test suite cannot
    import either.

    board_id picks the class — see build_guard. In practice this path is
    controller-side only, i.e. a device that has NOT negotiated
    output_chain — a device that has moved its own processing on-device is
    unaffected by anything here.
    """
    if not enabled:
        return None
    return build_guard(sample_rate, board_id, bass_guard_db=bass_guard_db)


def build_guard(sample_rate: int,
                board_id: str | None,
                bass_guard_db: float = DEFAULT_BASS_GUARD_DB,
                enabled: bool = True,
                ) -> "BassGuard | RadarMultiband":
    """
    One guard/compressor instance for a stream, picking the class the board
    actually has. Radar runs its own real 4-band MBCL (RadarMultiband) when
    its tuning is loaded (em_radar_tuning); every other board, and a Radar
    without it, keeps the single-band BassGuard above. bass_guard_db
    is the one depth control the dashboard exposes either way — on Radar it
    reaches only band 1's floor, same as before; bands 2-4 have no control,
    same reasoning as the limiter override (see em_limiter.build_limiter).

    The single call site both em_player.py and em_controller.py's
    _guard_for now share, so a board's class can never drift between a
    voice turn and a music stream.
    """
    tuning = em_radar_tuning.current() if board_id == "radar" else None
    if tuning is not None and tuning.mbcl is not None:
        return RadarMultiband(sample_rate, tuning.mbcl,
                              bass_guard_db=bass_guard_db, enabled=enabled)
    crossover_hz, threshold_db = _tuning_for(board_id)
    return BassGuard(sample_rate, bass_guard_db=bass_guard_db,
                     crossover_hz=crossover_hz, threshold_db=threshold_db,
                     enabled=enabled)


# ═══════════════════════════════════════════════════════════════════════════
# Radar's full 4-band MBCL
# ═══════════════════════════════════════════════════════════════════════════
#
# Configured from the Echo's own MBCL.cfg (em_radar_tuning.Mbcl), which is not
# in this repository: crossovers, a system gain ahead of the split, and four
# bands of compressor then limiter, each with its own input trim. Biscuit's
# own bands 2-4 are deliberately NOT ported (see the module docstring).
#
# The DYNAMICS are read out of libasp.so rather than this file, which names
# no compressor timing at all: StockCompressor and em_limiter.StockLimiter
# (2026-10-10). The limiter clamps its release to 180..400ms.


def _f32(hexbits: str) -> float:
    """A float32 constant read out of libasp.so, as the float64 it widens to.
    Both halves of the port use the same bits (device/internal/outchain
    mirrors these with math.Float32frombits), so the constant cannot be the
    reason they disagree."""
    import struct
    return struct.unpack(">f", bytes.fromhex(hexbits))[0]


# Stock's MBCL band compressor, decoded from Radar's libasp.so (class ctor
# 0xecea0, process 0xed168, gate 0xed018, gain computer 0xed0a8, reset
# 0xed44c), 2026-10-10. It is NOT the peak detector with instant attack that
# _BandGain is, and that difference is the "muddy" bass: stock measures POWER
# over 1ms blocks, smooths it (attack ~43ms, release ~435ms), smooths the
# GAIN again (~654ms, both directions), and applies it to audio delayed 16ms
# so the gain arrives ahead of the transient. A per-sample peak compressor at
# 10:1 on 70-200Hz pumps on every kick; stock's barely moves within a beat.
STOCK_COMP_BLOCK_MS = 1          # block = fs/1000 samples (48 at 48kHz)
STOCK_COMP_DELAY = 768           # 16ms look-ahead ring, fixed in samples
_COMP_GATE_FAST   = _f32("3c2aaaab")   # 1/96: fast power smoother A
_COMP_FLOOR_UP    = _f32("382ec33e")   # floor tracker B rises this slowly
_COMP_FLOOR_DOWN  = _f32("39da740e")   # ...and falls this fast
_COMP_GATE_RATIO  = _f32("3916feb5")   # level moves only if A > B * this (-38.4dB)
_COMP_ATTACK      = _f32("3cbbbbbc")   # level smoother, rising (~43ms)
_COMP_RELEASE     = _f32("3b162fc9")   # level smoother, falling (~435ms)
_COMP_GAIN_SMOOTH = _f32("3ac83fb7")   # gain smoother, both ways (~654ms)
_COMP_INIT        = _f32("3c23d70a")   # 0.01: A, B and the level at reset
_COMP_IN_VOL_MIN, _COMP_IN_VOL_MAX = _f32("3dcccccd"), _f32("40b3f300")


class StockCompressor:
    """
    One MBCL band compressor exactly as stock runs it, in S16 units.

    Per 1ms block: y = x * inVol; P = mean(y^2) on full-scale-normalised
    samples; a gate (A fast, B a slow floor) decides whether the level L may
    move toward P; the gain computer turns L into g (ratio above a power
    threshold, never below gainMin); G eases toward g; the block's output is
    G times y delayed 768 samples.

    Streaming adds ONE block of latency on top of stock's 16ms: the block's
    gain needs the whole block, and chunks here do not land on block
    boundaries, so output is held one block (48 samples). Every band carries
    the same delay, so the sum stays aligned. Pure Python per block (1000/s)
    — cheap enough; Radar runs this on the device anyway (output_chain).

    Bypass (enabled=False) keeps the delay and the trim and freezes the
    dynamics, so the bass guard toggle cannot misalign the bands or click.
    """

    def __init__(self, sample_rate: int, ratio: float, threshold_db: float,
                 floor_db: float, in_vol_db: float = 0.0, enabled: bool = True):
        self.block = int(sample_rate) * STOCK_COMP_BLOCK_MS // 1000
        self.latency = STOCK_COMP_DELAY + self.block
        self.enabled = bool(enabled)
        self._pow_scale = 1.0 / (self.block * _FULL_SCALE * _FULL_SCALE)
        ratio = min(max(float(ratio), 1.0), 20.0)
        self._k_half = (1.0 - 1.0 / ratio) * 0.5
        self._tpow = 10.0 ** (min(max(float(threshold_db), -90.0), 0.0) / 10.0)
        self.set_floor_db(floor_db)
        self._in_vol = min(max(10.0 ** (float(in_vol_db) / 20.0),
                               _COMP_IN_VOL_MIN), _COMP_IN_VOL_MAX)
        self.min_gain = 1.0
        self.reset()

    def set_floor_db(self, floor_db: float) -> None:
        self.floor_db = min(max(float(floor_db), -40.0), 0.0)
        self._floor = 10.0 ** (self.floor_db / 20.0)

    def reset(self) -> None:
        self._a = self._b = self._level = _COMP_INIT
        self._gain = 1.0
        self._ring = np.zeros(STOCK_COMP_DELAY)
        self._pending = np.zeros(0)
        self._fifo = np.zeros(self.block)

    @property
    def max_reduction_db(self) -> float:
        return -20.0 * math.log10(self.min_gain)

    def _block(self, xb: np.ndarray) -> np.ndarray:
        y = xb * self._in_vol
        delayed = self._ring[:self.block]
        self._ring = np.concatenate((self._ring[self.block:], y))
        if not self.enabled:
            return delayed
        p = np.cumsum(y * y)[-1] * self._pow_scale   # sequential, as the Go
        a = self._a + _COMP_GATE_FAST * (p - self._a)
        b = self._b
        b = b + (_COMP_FLOOR_DOWN if a < b else _COMP_FLOOR_UP) * (a - b)
        self._a, self._b = a, b
        lvl = self._level
        if a > b * _COMP_GATE_RATIO:
            lvl = lvl + (_COMP_ATTACK if lvl < p else _COMP_RELEASE) * (p - lvl)
            self._level = lvl
        if lvl <= self._tpow:
            g = 1.0
        else:
            g = (self._tpow / lvl) ** self._k_half
            if g <= self._floor:
                g = self._floor
        gain = self._gain + _COMP_GAIN_SMOOTH * (g - self._gain)
        self._gain = gain
        if gain < self.min_gain:
            self.min_gain = gain
        return gain * delayed

    def process(self, x: np.ndarray) -> np.ndarray:
        """Exactly as many samples out as in, delayed by `latency`."""
        n = x.size
        buf = np.concatenate((self._pending, x))
        nb = buf.size // self.block
        outs = [self._fifo]
        for k in range(nb):
            outs.append(self._block(buf[k * self.block:(k + 1) * self.block]))
        self._pending = buf[nb * self.block:]
        fifo = np.concatenate(outs)
        self._fifo = fifo[n:]
        return fifo[:n]


def four_band_flatness_db(crossovers_hz: tuple[float, float, float],
                          fs: int = 48000) -> float:
    """
    Peak-to-peak deviation of the four allpass-compensated bands' sum, in
    dB — the multi-band equivalent of crossover_flatness_db.

    A naive recursive split (keep halving the high branch, as
    crossover_flatness_db's single-crossover case does) does NOT sum flat
    once nested: measured 1.59dB of ripple near the crossovers, because an
    LR4 split's sum is flat only in MAGNITUDE — its PHASE rotates with
    frequency (measured up to 111 degrees near a crossover) — so summing
    bands that went through a DIFFERENT NUMBER of filter stages adds
    mismatched phases, not just flat gain.

    The fix is standard multi-way crossover design: give every band the
    SAME total number of stages by running the earlier ones through an
    ALLPASS version (that crossover's own LP+HP, summed and used only for
    its phase) of every later crossover they did not actually split on —
    see RadarMultiband. That makes the whole network end to end a cascade
    of allpass filters, each contributing |H|=1, so the product is exactly
    1: measured 4.6e-11dB, at the floor of float64 rather than merely small.
    """
    fc1, fc2, fc3 = crossovers_hz
    f = np.geomspace(5.0, fs * 0.4999, 8192)

    def resp(fc, kind):
        _, h = _signal().sosfreqz(_lr4(fc, fs, kind), worN=f, fs=fs)
        return h

    lp1, hp1 = resp(fc1, "low"), resp(fc1, "high")
    lp2, hp2 = resp(fc2, "low"), resp(fc2, "high")
    lp3, hp3 = resp(fc3, "low"), resp(fc3, "high")
    ap2, ap3 = lp2 + hp2, lp3 + hp3

    band1 = ap3 * ap2 * lp1
    band2 = ap3 * lp2 * hp1
    band3 = lp3 * hp2 * hp1
    band4 = hp3 * hp2 * hp1

    mag = np.abs(band1 + band2 + band3 + band4)
    return float(20.0 * np.log10(mag.max() / max(mag.min(), _EPS)))


class RadarMultiband:
    """
    Radar's real 4-band MBCL, in full, configured from the Echo's own
    MBCL.cfg (spec, an em_radar_tuning.Mbcl).

    Three crossovers split the signal into four bands; each band runs its
    own compressor then its own peak limiter, both with their own input
    trim (comp_inVol/lim_inVol); the four are summed. The combined
    full-band limiter (em_limiter.build_limiter) is NOT
    part of this class — it is MBCL's own "Full-band limiter" entry and
    stays exactly where it already runs, downstream of this, in
    em_player.py/em_eq.py.

    EXACT FLATNESS, THROUGH ALLPASS COMPENSATION
    ----------------------------------------------
    See four_band_flatness_db's docstring for why a naive recursive split
    does not sum flat and what fixes it. In this network: band 1 (which
    only ever sees the fc1 split) is additionally run through an allpass
    of fc2 and then of fc3; band 2 (fc1 and fc2) is additionally run
    through an allpass of fc3; bands 3 and 4 already carry all three
    splits' worth of filtering and need no compensation. Every
    compensation filter below is an INDEPENDENT instance of the same
    coefficients as the real split it stands in for — same transfer
    function, separate state, because it is filtering a different signal.
    """

    def __init__(self, sample_rate: int,
                 spec: "em_radar_tuning.Mbcl",
                 bass_guard_db: float = DEFAULT_BASS_GUARD_DB,
                 enabled: bool = True):
        self.spec = spec
        self.sample_rate = int(sample_rate)
        self.enabled = bool(enabled)
        self.bass_guard_db = min(0.0, float(bass_guard_db))
        fs = self.sample_rate

        fc1, fc2, fc3 = spec.crossovers_hz
        self._lp1 = _lr4(fc1, fs, "low")
        self._hp1 = _lr4(fc1, fs, "high")
        self._lp2 = _lr4(fc2, fs, "low")
        self._hp2 = _lr4(fc2, fs, "high")
        self._lp3 = _lr4(fc3, fs, "low")
        self._hp3 = _lr4(fc3, fs, "high")

        # One zi state per USE of a filter. lp2/hp2 and lp3/hp3 are each
        # used twice — once for the real split, once purely for phase
        # compensation on an earlier band — and each use needs its own
        # history, hence the separate keys sharing the same coefficients.
        def z(sos):
            return np.zeros((sos.shape[0], 2))
        self._z = {
            "lp1": z(self._lp1), "hp1": z(self._hp1),
            "lp2": z(self._lp2), "hp2": z(self._hp2),      # split on high1
            "lp2c": z(self._lp2), "hp2c": z(self._hp2),    # compensation, on low1
            "lp3": z(self._lp3), "hp3": z(self._hp3),      # split on high2 -> band3/4
            "lp3c1": z(self._lp3), "hp3c1": z(self._hp3),  # compensation, on low2 -> band2
            "lp3c2": z(self._lp3), "hp3c2": z(self._hp3),  # compensation, on ap2(low1) -> band1
        }

        # Stock's own compressor (StockCompressor); its time constants are
        # libasp's, so the "comp_release" the config never names is no
        # longer a guess.
        self._comp = [
            StockCompressor(fs, b.comp_ratio, b.comp_threshold_db,
                            b.comp_floor_db if i > 0 else self.bass_guard_db,
                            in_vol_db=b.comp_in_vol_db, enabled=self.enabled)
            for i, b in enumerate(spec.bands)
        ]
        # Samples the bands are delayed by (stock's look-ahead plus one
        # block of streaming), so a one-shot or a flush can drain it.
        self.latency = self._comp[0].latency
        # Stock's own limiter (em_limiter.StockLimiter), lim_inVol inside.
        self._lim = [em_limiter.StockLimiter(fs, threshold_db=b.lim_threshold_db,
                                             release_ms=b.lim_release_ms,
                                             in_vol_db=b.lim_in_vol_db,
                                             enabled=self.enabled)
                     for b in spec.bands]
        self.latency += self._lim[0].latency

    @property
    def max_reduction_db(self) -> float:
        """Worst reduction seen so far, across every band's compressor and
        limiter — the instrument for whether any of this is doing
        anything."""
        return round(self.raw_max_reduction_db, 2)

    @property
    def raw_max_reduction_db(self) -> float:
        """Same, full precision — what test vectors are generated from."""
        return max(s.max_reduction_db for s in (*self._comp, *self._lim))

    def set_params(self,
                   bass_guard_db: float | None = None,
                   enabled: bool | None = None) -> None:
        """Change band 1's floor and/or bypass the law, without touching
        carried filter or gain state — see BassGuard.set_params, same
        reasoning. Bands 2-4 have no control, same as the limiter
        override: there is nothing today to leave untouched."""
        if bass_guard_db is not None:
            self.bass_guard_db = min(0.0, float(bass_guard_db))
            self._comp[0].set_floor_db(self.bass_guard_db)
        if enabled is not None:
            self.enabled = bool(enabled)
            for c in self._comp:
                c.enabled = self.enabled
            for lim in self._lim:
                lim.set_params(enabled=self.enabled)

    def _filt(self, sos: np.ndarray, key: str, x: np.ndarray) -> np.ndarray:
        y, self._z[key] = _signal().sosfilt(sos, x, zi=self._z[key])
        return y

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Compress one chunk. Returns exactly as many samples as given."""
        if samples.size == 0:
            return samples
        x = np.asarray(samples, dtype=np.float64)

        # The crossover network runs unconditionally, enabled or not — see
        # the class docstring on BassGuard.process for why: keeping the
        # filters warm and the output an allpass of the input either way is
        # what makes the toggle click-free.
        low1  = self._filt(self._lp1, "lp1", x)
        high1 = self._filt(self._hp1, "hp1", x)

        low2  = self._filt(self._lp2, "lp2", high1)
        high2 = self._filt(self._hp2, "hp2", high1)

        ap2_low1 = (self._filt(self._lp2, "lp2c", low1)
                  + self._filt(self._hp2, "hp2c", low1))

        band3_raw = self._filt(self._lp3, "lp3", high2)
        band4_raw = self._filt(self._hp3, "hp3", high2)

        band2_raw = (self._filt(self._lp3, "lp3c1", low2)
                   + self._filt(self._hp3, "hp3c1", low2))
        band1_raw = (self._filt(self._lp3, "lp3c2", ap2_low1)
                   + self._filt(self._hp3, "hp3c2", ap2_low1))

        # mbcl_inVol and every band's comp_inVol/lim_inVol apply EITHER
        # WAY: they are fixed gain stages, not dynamics ones, and gating
        # any of them on `enabled` would make the "bass guard" toggle
        # also step the overall level by as much as 6dB (band 3's
        # comp_inVol+lim_inVol) on top of whatever the law itself was
        # doing — a bigger, more noticeable click than any other toggle in
        # this chain produces. Real stock's own "mbcl_bypass" drops all of
        # it too (bypass skips the whole block), but nothing here can
        # toggle that flag live the way this dashboard control can, so
        # this follows the rest of the chain's own convention instead:
        # only the LAW engaging or not should be audible across the
        # toggle. Only gains_db() — the compression/limiting ITSELF — is
        # skipped while disabled, which also freezes its gain state, same
        # as BassGuard/Limiter bypass.
        sys_gain = 10.0 ** (self.spec.in_vol_db / 20.0)
        out = 0.0
        for i, (raw, spec) in enumerate(zip(
                (band1_raw, band2_raw, band3_raw, band4_raw), self.spec.bands)):
            y = self._comp[i].process(raw * sys_gain)   # comp_inVol inside
            y = self._lim[i].process(y)                 # lim_inVol inside
            out = out + y
        return out
