"""Guardrails for the weather data the server takes in: GFS and GEFS forecasts (fetch_gfs.decode, also used for
the archive backfill_gfs.py builds the climatology from) and NOAA's reanalysis climatology (build_prior.py).

A field holding a value no air or ground can have makes the whole file count as damaged: the forecast run is not
published (the previous run keeps serving and is retried on the next fetch), the archive skips that hour, and the
reanalysis climatology is not replaced. The limits are the extremes the Earth has recorded, with a margin, so real
weather is never refused - only values that cannot be (a damaged download, a changed unit or encoding):

    2 m air            -89.2 C (Vostok, 1983) .. 56.7 C (Death Valley, 1913)        -> 178 .. 334 K
    80 / 100 m air     the same with room for model extremes                         -> 170 .. 340 K
    ground skin        ice as cold as the air above; ground to ~71 C (Lut, satellite)  -> 178 .. 348 K
    pressure levels    including GFS's extrapolation below high ground (hot "air" under
                       the Tibetan plateau at 1000 mb) and the coldest stratosphere   -> 160 .. 350 K
    surface pressure   Everest's ~330 mb .. the 1083.8 mb record (Agata, 1968)       -> 300 .. 1100 mb
    ground height      the Dead Sea shore (-430 m) .. Everest (8,849 m)             -> -500 .. 9,000 m
    level heights      rising level by level as the pressure falls (below the ground too)
    land fraction      0 .. 1

check_run_jump() also compares a new GFS run with the one serving, where their forecasts overlap within the first
day: consecutive runs differ there by well under 1 K and 1 mb on most of the grid, so a median difference over
the whole region of more than 3 K (2 m air), 3 mb (surface pressure) or 40 m (500 mb height) is not new weather
but a damaged or misread run, and it is held back.
"""
from __future__ import annotations

import re

import numpy as np

# field name -> (low, high), in the stored units (K, Pa, m, fraction)
FIXED = {
    "t2m": (178.0, 334.0),
    "tsfc": (178.0, 348.0),
    "psfc": (30000.0, 110000.0),
    "orog": (-500.0, 9000.0),
    "land": (-0.01, 1.01),
}
NEAR_AIR = (170.0, 340.0)        # t80m, t100m
LEVEL_AIR = (160.0, 350.0)       # t1000 .. t500 (and above)
LEVEL_HEIGHT = (-2000.0, 40000.0)

JUMP = {"t2m": 3.0, "psfc": 300.0, "z500": 40.0}   # median |new - serving|, K / Pa / m
JUMP_WITHIN_H = 24


def limits(name: str) -> tuple[float, float] | None:
    if name in FIXED:
        return FIXED[name]
    if re.fullmatch(r"t\d+m", name):
        return NEAR_AIR
    if re.fullmatch(r"t\d+", name):
        return LEVEL_AIR
    if re.fullmatch(r"z\d+", name):
        return LEVEL_HEIGHT
    return None


def problems(names: list[str], fields: np.ndarray, axis: int = 0) -> list[str]:
    """What in these fields (`fields` with the field axis `axis`, in `names` order) no air or ground can have; [] if
    nothing. Missing values (NaN) are not counted."""
    out = []
    a = np.moveaxis(np.asarray(fields), axis, 0)
    for name, v in zip(names, a):
        lim = limits(name)
        if lim is None:
            continue
        finite = np.isfinite(v)
        bad = finite & ((v < lim[0]) | (v > lim[1]))
        if bad.any():
            out.append(f"{name}: {int(bad.sum())} values outside {lim[0]:g}..{lim[1]:g} "
                       f"(from {float(np.nanmin(v)):.6g} to {float(np.nanmax(v)):.6g})")
        elif np.isinf(v).any():
            out.append(f"{name}: infinite values")
    # level heights rise as the pressure falls
    levels = sorted((int(n[1:]) for n in names if re.fullmatch(r"z\d+", n)), reverse=True)
    for lo, hi in zip(levels, levels[1:]):
        zl, zh = a[names.index(f"z{lo}")], a[names.index(f"z{hi}")]
        both = np.isfinite(zl) & np.isfinite(zh)
        wrong = both & (zh <= zl)
        if wrong.any():
            out.append(f"z{hi} not above z{lo} at {int(wrong.sum())} points")
    return out


def check(names: list[str], fields: np.ndarray, where: str, axis: int = 0) -> None:
    """Raises ValueError naming what in the fields cannot be real (see problems)."""
    p = problems(names, fields, axis)
    if p:
        raise ValueError(f"{where}: impossible values, not used: " + "; ".join(p))


def run_jump(names: list[str], new: np.ndarray, serving: np.ndarray) -> list[str]:
    """Where a new run's fields differ from the serving run's (same valid time, same grid) by more than any real
    update: the median over the grid of the absolute difference, per field in JUMP. [] if none."""
    out = []
    for name, limit in JUMP.items():
        if name not in names:
            continue
        i = names.index(name)
        d = np.abs(np.asarray(new[i], dtype=np.float64) - np.asarray(serving[i], dtype=np.float64))
        d = d[np.isfinite(d)]
        if d.size and float(np.median(d)) > limit:
            out.append(f"{name}: median change {float(np.median(d)):.3g} (more than {limit:g})")
    return out
