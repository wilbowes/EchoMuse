"""Radar's stock playback tuning, read from a copy of an Echo 2's files.

The device reads these files from its own /system/vendor/etc/audio-algorithms
at start-up (device/internal/outchain/radartuning.go) and runs the stock
chain itself. This is the same loader for the controller's side of the chain
— the reference the device is tested against, and the path for a Radar
firmware without output_chain. None of the tuning is in this repository: the
controller has the files only when ECHOMUSE_RADAR_TUNING names a copy of that
directory, and without them it runs Radar without the stock curve, the way a
Radar whose files are missing does.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("echomuse.radar_tuning")

ENV = "ECHOMUSE_RADAR_TUNING"


@dataclass(frozen=True)
class MbclBand:
    """One row of MBCL.cfg's "Bands Definition"."""
    comp_in_vol_db: float
    comp_ratio: float
    comp_threshold_db: float
    comp_floor_db: float
    lim_in_vol_db: float
    lim_threshold_db: float
    lim_release_ms: float


@dataclass(frozen=True)
class Mbcl:
    in_vol_db: float
    crossovers_hz: tuple[float, float, float]
    bands: tuple[MbclBand, ...]
    full_band_in_vol_db: float
    full_band_threshold_db: float
    full_band_release_ms: float


@dataclass(frozen=True)
class RadarTuning:
    fir_bands: list = field(default_factory=list)      # [taps, ...]
    fir_bounds: list = field(default_factory=list)     # each band's upper volume value
    peq: list = field(default_factory=list)            # [(type, fc, q, gain_db)], no BYPASS
    trim_db: float = 0.0
    mbcl: Mbcl | None = None


def _strip_comments(text: str) -> str:
    """Remove /* */ and // comments outside strings (stock: cJSON_Minify)."""
    out, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _read_json(path: str):
    with open(path) as f:
        return json.loads(_strip_comments(f.read()))


_NUMBER = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _read_fir(path: str) -> list[float]:
    with open(path) as f:
        taps = [float(s) for s in _NUMBER.findall(_strip_comments(f.read()))]
    if not taps:
        raise ValueError(f"{os.path.basename(path)}: no taps")
    return taps


def _parse_peq(data) -> list:
    if data.get("Bypass"):
        return []
    out = []
    for b in data.get("Biquad Definitions", []):
        kind = b.get("FilterType")
        if kind == "BYPASS":
            continue
        if kind not in ("PEAK", "LOW_SHELF"):
            raise ValueError(f"filter type {kind!r} is not ported")
        fc, q = float(b["Fc"]), float(b["Q"])
        if fc <= 0 or q <= 0:
            raise ValueError(f"{kind} at {fc}Hz: invalid Fc/Q")
        out.append((kind, fc, q, float(b["GaindB"])))
    return out


def _parse_mbcl(data) -> Mbcl | None:
    if data.get("Bypass"):
        return None
    fc = [float(x) for x in data.get("FilterBank FC", [])]
    bands = data.get("Bands Definition", [])
    if len(fc) != 3 or len(bands) != 4:
        raise ValueError(f"want 3 crossover frequencies and 4 bands, got {len(fc)} and {len(bands)}")
    if any(f <= 0 for f in fc) or not fc[0] < fc[1] < fc[2]:
        raise ValueError(f"crossover frequencies must rise: {fc}")
    full = data.get("Full-band limiter", {})
    return Mbcl(
        in_vol_db=float(data.get("inVol", 0.0)),
        crossovers_hz=(fc[0], fc[1], fc[2]),
        bands=tuple(MbclBand(
            comp_in_vol_db=float(b.get("comp_inVol", 0.0)),
            comp_ratio=float(b.get("comp_ratio", 1.0)),
            comp_threshold_db=float(b.get("comp_thresh", 0.0)),
            comp_floor_db=float(b.get("comp_gainMin", 0.0)),
            lim_in_vol_db=float(b.get("lim_inVol", 0.0)),
            lim_threshold_db=float(b.get("lim_thresh", 0.0)),
            lim_release_ms=float(b.get("lim_release", 0.0)),
        ) for b in bands),
        full_band_in_vol_db=float(full.get("lim_inVol", 0.0)),
        full_band_threshold_db=float(full.get("lim_thresh", 0.0)),
        full_band_release_ms=float(full.get("lim_release", 0.0)),
    )


def load(directory: str) -> RadarTuning:
    """Read the tuning the way the device does: the stages AFE.cfg's Playback
    path lists, from the files it names. A listed stage whose file is missing
    or unreadable raises — part of the tuning is a different sound."""
    afe = _read_json(os.path.join(directory, "AFE.cfg"))
    algos = afe.get("Path Definition", {}).get("Playback", {}).get("Algorithms", {})
    defs = afe.get("Algorithm Definition", {})

    def stage(name):
        return defs.get(algos[name]) if name in algos else None

    fir_bands, fir_bounds, peq, trim_db, mbcl = [], [], [], 0.0, None
    d = stage("EQ")
    if d is not None and not d.get("Bypass"):
        files, bounds = d.get("External Coefficients", []), d.get("Volume Boundary", [])
        if not files or len(files) != len(bounds):
            raise ValueError(f"AFE.cfg: Equalizer FIR: {len(files)} files for {len(bounds)} boundaries")
        for name in files:
            taps = _read_fir(os.path.join(directory, os.path.basename(name)))
            if fir_bands and len(taps) != len(fir_bands[0]):
                raise ValueError(f"{name}: {len(taps)} taps, the others have {len(fir_bands[0])}")
            fir_bands.append(taps)
        fir_bounds = [float(b) for b in bounds]
    d = stage("ParametricEQ")
    if d is not None:
        peq = _parse_peq(_read_json(os.path.join(directory, os.path.basename(d["External Config"]))))
    d = stage("MBCL")
    if d is not None:
        files = d.get("External Config", [])
        if len(files) != 1:
            raise ValueError("AFE.cfg: MBCL: want one config file")
        mbcl = _parse_mbcl(_read_json(os.path.join(directory, os.path.basename(files[0]))))
    d = stage("OutputTrim")
    if d is not None:
        trim_db = float(d.get("GaindB", 0.0))
    return RadarTuning(fir_bands, fir_bounds, peq, trim_db, mbcl)


_cache: dict[str, RadarTuning | None] = {}


def current() -> RadarTuning | None:
    """The tuning from $ECHOMUSE_RADAR_TUNING, loaded once per directory;
    None when it is unset or the files do not load."""
    directory = os.environ.get(ENV, "")
    if not directory:
        return None
    if directory not in _cache:
        try:
            _cache[directory] = load(directory)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("radar tuning from %s: %s", directory, e)
            _cache[directory] = None
    return _cache[directory]
