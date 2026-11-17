#!/usr/bin/env python3
"""The climate envelope: the range of near-surface air a place can have in a month, for the website's guardrails.

The website refuses a forecast more than 45 C from the place's normal for the date (atmosphere-guard.js), but only
when it has the place's monthly normals (Open-Meteo's ERA5, else NOAA's station normals). When neither downloads, a
broken forecast (-50 C in a New York July) would be used. GET /v1/climate-envelope gives the website a reference
for that case, for any place on Earth.

Per 5 degree cell and month: the lowest and highest near-surface temperature in NOAA's 1991-2020 daily long-term
means (4 times a day) over every 2.5 degree reanalysis point on or inside the cell, carried to sea level along the
standard lapse rate (6.5 K/km over the reanalysis terrain; the website carries it back up to the forecast's height).
An envelope rather than a normal, so a coarse cell never refuses real air in a valley or on a plateau inside it; the
website refuses only a value more than 45 C outside the whole range.

    python3 climate_envelope.py [--config config.json] [--source-dir DIR]

fetch_gfs.py runs this once when <data_dir>/climate-envelope.npz is missing (~70 MB from NOAA PSL, deleted after);
the long-term means change once a decade. Data: NCEP/NCAR Reanalysis 1, NOAA PSL, Boulder, Colorado, USA.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import warnings
from pathlib import Path

import numpy as np

from build_prior import LTM_FILES, arr, data_var, get_file, month_and_slot, static_fields, to_kelvin
from common import load_config

log = logging.getLogger("climate_envelope")

RES = 5.0
LAPSE = 0.0065                       # K/m; the website carries the envelope up with the same rate
EMPTY_LO, EMPTY_HI = 127, -128       # a cell with no reanalysis point (lo > hi): no answer there


def envelope_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "climate-envelope.npz"


def envelope(t_sl: np.ndarray, months: np.ndarray, lats: np.ndarray, lons: np.ndarray, res: float = RES):
    """t_sl: sea-level temperature (C) [time, lat, lon]; months: month index 0-11 per time step.
    Returns int8 (lo, hi) [nLat, nLon, 12]: floor of the minimum, ceiling of the maximum; EMPTY_LO / EMPTY_HI
    where a cell holds no point with data."""
    n_lat, n_lon = int(round(180 / res)), int(round(360 / res))
    lon180 = (lons + 180) % 360 - 180
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)              # all-NaN points
        mlo = np.stack([np.nanmin(t_sl[months == m], axis=0) for m in range(12)])   # [12, lat, lon]
        mhi = np.stack([np.nanmax(t_sl[months == m], axis=0) for m in range(12)])
    lo = np.full((n_lat, n_lon, 12), EMPTY_LO, dtype=np.int16)
    hi = np.full((n_lat, n_lon, 12), EMPTY_HI, dtype=np.int16)
    eps = 1e-6
    for i in range(n_lat):
        a, b = -90 + i * res, -90 + (i + 1) * res
        rows = np.where((lats >= a - eps) & (lats <= b + eps))[0]          # edges count for both cells
        if not len(rows):
            continue
        for j in range(n_lon):
            c, d = -180 + j * res, -180 + (j + 1) * res
            on = (lon180 >= c - eps) & (lon180 <= d + eps)
            if j == n_lon - 1:
                on |= lon180 <= -180 + eps                                 # 180 E is -180
            cols = np.where(on)[0]
            if not len(cols):
                continue
            sub_lo = mlo[:, rows][:, :, cols].reshape(12, -1)
            sub_hi = mhi[:, rows][:, :, cols].reshape(12, -1)
            ok = np.isfinite(sub_lo).any(axis=1) & np.isfinite(sub_hi).any(axis=1)
            if not ok.all():
                continue                                                   # a month without data: no answer
            lo[i, j] = np.floor(np.nanmin(sub_lo, axis=1))
            hi[i, j] = np.ceil(np.nanmax(sub_hi, axis=1))
    filled = lo <= hi
    if not filled.any():
        raise ValueError("climate envelope: no cell has data")
    if lo[filled].min() < -127 or hi[filled].max() > 126:
        raise ValueError("climate envelope: temperatures out of range (wrong units?)")
    return lo.astype(np.int8), hi.astype(np.int8)


def build(cfg: dict, source_dir: Path | None = None) -> Path:
    import netCDF4
    raw = Path(cfg["data_dir"]) / "envelope-raw"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        with netCDF4.Dataset(get_file(cfg, LTM_FILES["t_near"], raw, source_dir)) as ds:
            ds.set_auto_maskandscale(True)
            v = data_var(ds)
            lats, lons = arr(ds.variables["lat"][:]), arr(ds.variables["lon"][:])
            t = to_kelvin(arr(v[:]).reshape(-1, len(lats), len(lons)), getattr(v, "units", "")) - 273.15
            months, _ = month_and_slot(ds, t.shape[0])
        orog, olat, olon = static_fields(cfg, raw, source_dir)["orog"]
    finally:
        if source_dir is None:
            shutil.rmtree(raw, ignore_errors=True)
    if orog.shape != t.shape[1:] or not np.allclose(olat, lats) or not np.allclose(olon, lons):
        raise ValueError("surface height is not on the temperature's grid")
    lo, hi = envelope(t + LAPSE * orog[None], months, lats, lons)
    out = envelope_path(cfg)
    tmp = out.with_name(out.stem + ".part.npz")
    np.savez_compressed(tmp, lo=lo, hi=hi, res=np.float32(RES))
    tmp.replace(out)
    filled = lo <= hi
    log.info("wrote %s: %d of %d cells, sea-level %d..%d C", out, int(filled.all(axis=2).sum()),
             filled.shape[0] * filled.shape[1], lo[filled].min(), hi[filled].max())
    return out


def load(path: Path) -> dict:
    with np.load(path) as z:
        return {"lo": z["lo"], "hi": z["hi"], "res": float(z["res"])}


def at(env: dict, lat: float, lon: float) -> dict:
    """The /v1/climate-envelope answer for a place. LookupError where the envelope has no data."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    res, lo, hi = env["res"], env["lo"], env["hi"]
    i = min(lo.shape[0] - 1, int((lat + 90) // res))
    j = int((lon + 180) // res) % lo.shape[1]
    if (lo[i, j] > hi[i, j]).any():
        raise LookupError("no climate envelope for this place")
    return {
        "cell": {"south": -90 + i * res, "west": -180 + j * res, "res": res},
        "seaLevelC": {"min": [int(x) for x in lo[i, j]], "max": [int(x) for x in hi[i, j]]},
        "lapseKPerM": LAPSE,
        "source": "NCEP/NCAR Reanalysis 1 daily long-term means 1991-2020 (air.sig995), NOAA PSL",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--source-dir", type=Path, help="use already downloaded NOAA files from this directory")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build(load_config(args.config), args.source_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
