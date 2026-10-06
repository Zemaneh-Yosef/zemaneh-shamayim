"""Climatological (typical) temperature profiles, for dates beyond the forecast.

Two sources, both by month and time of day:

* "gfs":  your own climatology, built by fetch_gfs.py from the first 12 hours of every GFS run it
          stores (0.25 deg, 13 levels, water temperature). Grows by itself; a month / time-of-day slot
          is used once it holds `climatology.min_days` days of data.
* "ncep": NOAA NCEP/NCAR Reanalysis 1, 1991-2020 averages at 00/06/12/18 UTC (2.5 deg, levels
          1000/925/850/700/600/500 mb, air ~40 m above ground). Downloaded once without any key by
          build_prior.py; the fallback until your own climatology has filled in.

Storage:  <data_dir>/climatology/<source>/<region>/meta.json + values.npy [+ counts.npy]
          values: [month, time-of-day slot, field, lat, lon]; "gfs" stores running sums + counts.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from common import Cycle, write_json_atomic

MID_MONTH_DOY = [15.5, 45, 74.5, 105, 135.5, 166, 196.5, 227.5, 258, 288.5, 319, 349.5]
GFS_SLOT_HOURS = 3                      # own climatology: 8 slots of 3 h (UTC)


def clim_dir(cfg: dict, source: str, region: str) -> Path:
    return Path(cfg["data_dir"]) / "climatology" / source / region


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
class ClimGrid:
    """One source for one region."""

    def __init__(self, path: Path):
        self.path = path
        self.meta = json.loads((path / "meta.json").read_text())
        self.lats = np.asarray(self.meta["lats"], dtype=float)
        self.lons = np.asarray(self.meta["lons"], dtype=float)
        self.fields = self.meta["fields"]
        self.levels_mb = self.meta["levels_mb"]
        self.slot_hours = self.meta["slot_hours"]          # UTC hour at the centre of each slot
        self.near_surface_agl = self.meta.get("near_surface_agl", 2.0)
        self.values = np.load(path / "values.npy", mmap_mode="r")
        cpath = path / "counts.npy"
        self.counts = np.load(cpath) if cpath.exists() else None   # None: values are means
        self.stamp = self.version(path)

    @staticmethod
    def version(path: Path):
        try:
            return tuple(os.stat(path / n).st_mtime_ns for n in ("meta.json", "values.npy"))
        except OSError:
            return None

    def contains(self, lat: float, lon: float) -> bool:
        return self.lats[0] <= lat <= self.lats[-1] and self.lons[0] <= lon <= self.lons[-1]

    def fields_at(self, t: datetime, lat: float, lon: float, min_samples: int = 1):
        """Field values (dict) at time t (month + time of day) and place, or None if outside the grid
        or a needed month / slot has too little data."""
        if not self.contains(lat, lon):
            return None
        la, lo = self.lats, self.lons
        i = min(int(np.searchsorted(la, lat, side="right") - 1), len(la) - 2)
        j = min(int(np.searchsorted(lo, lon, side="right") - 1), len(lo) - 2)
        fy = (lat - la[i]) / (la[i + 1] - la[i])
        fx = (lon - lo[j]) / (lo[j + 1] - lo[j])
        acc, wsum = None, 0.0
        for m, wm in month_weights(t):
            for s, ws in slot_weights(t, self.slot_hours):
                w = wm * ws
                if w <= 0:
                    continue
                n = 1
                if self.counts is not None:
                    n = int(self.counts[m, s])
                    if n < min_samples:
                        continue                         # this month / slot is not ready yet
                blk = np.asarray(self.values[m, s, :, i:i + 2, j:j + 2], dtype=np.float64) / n
                v = (blk[:, 0, 0] * (1 - fy) * (1 - fx) + blk[:, 0, 1] * (1 - fy) * fx
                     + blk[:, 1, 0] * fy * (1 - fx) + blk[:, 1, 1] * fy * fx)
                acc = v * w if acc is None else acc + v * w
                wsum += w
        # use the slots that are ready if they carry most of the weight (e.g. only the nearer month)
        if acc is None or wsum < 0.5:
            return None
        return {n: float(acc[k] / wsum) for k, n in enumerate(self.fields)}


def month_weights(t: datetime):
    """[(month index, weight)] interpolating between mid-month values (wraps over the new year)."""
    doy = t.timetuple().tm_yday - 1 + (t.hour + t.minute / 60) / 24
    for k in range(12):
        a = MID_MONTH_DOY[k]
        b = MID_MONTH_DOY[k + 1] if k < 11 else MID_MONTH_DOY[0] + 365
        d = doy + 365 if doy < MID_MONTH_DOY[0] else doy
        if a <= d <= b:
            w = (d - a) / (b - a)
            return [(k, 1 - w), ((k + 1) % 12, w)]
    return [(0, 1.0)]


def slot_weights(t: datetime, slot_hours: list[float]):
    """[(slot index, weight)] interpolating in UTC time of day between slot centres (wraps)."""
    h = t.hour + t.minute / 60 + t.second / 3600
    n = len(slot_hours)
    for k in range(n):
        a = slot_hours[k]
        b = slot_hours[k + 1] if k < n - 1 else slot_hours[0] + 24
        x = h + 24 if h < slot_hours[0] else h
        if a <= x <= b:
            w = (x - a) / (b - a)
            return [(k, 1 - w), ((k + 1) % n, w)]
    return [(0, 1.0)]


# ---------------------------------------------------------------------------------------------
# Turning field values into a profile (shared with the forecast path)
# ---------------------------------------------------------------------------------------------
def build_profile(f: dict, levels_mb: list[int], dist_km: float, lat: float, lon: float,
                  near_surface_agl: float = 2.0) -> dict:
    surface_h = f["orog"] + near_surface_agl
    levels = [{"h": round(surface_h, 1), "t": round(f["t2m"] - 273.15, 2), "p": round(f["psfc"] / 100, 2)}]
    for p in levels_mb:
        z, temp = f.get(f"z{p}"), f.get(f"t{p}")
        if z is None or temp is None or not (math.isfinite(z) and math.isfinite(temp)):
            continue
        if p * 100 < f["psfc"] and z > surface_h + 1:
            levels.append({"h": round(z, 1), "t": round(temp - 273.15, 2), "p": float(p)})
    water = f["land"] < 0.5
    prof = {"distanceKm": dist_km, "lat": round(lat, 4), "lon": round(lon, 4), "water": bool(water), "levels": levels}
    if water and "tsfc" in f and math.isfinite(f["tsfc"]):
        prof["skinC"] = round(f["tsfc"] - 273.15, 2)
    return prof


# ---------------------------------------------------------------------------------------------
# Own GFS climatology: add the first hours of a stored cycle to running sums
# ---------------------------------------------------------------------------------------------
def archive_cycle(cfg: dict, cycle: Cycle, cycle_dir: Path, log) -> None:
    ccfg = cfg.get("climatology", {})
    if not ccfg.get("archive", True):
        return
    hours = [h for h in range(int(ccfg.get("archive_hours", 12)))]
    for region in cfg["regions"]:
        rdir = cycle_dir / region["name"]
        meta = json.loads((rdir / "meta.json").read_text())
        out = clim_dir(cfg, "gfs", region["name"])
        nslots = 24 // GFS_SLOT_HOURS
        shape = (12, nslots, len(meta["fields"]), len(meta["lats"]), len(meta["lons"]))
        expected = {"lats": meta["lats"], "lons": meta["lons"], "fields": meta["fields"],
                    "levels_mb": meta["levels_mb"], "region": region,
                    "slot_hours": [GFS_SLOT_HOURS * k + GFS_SLOT_HOURS / 2 for k in range(nslots)],
                    "near_surface_agl": 2.0, "source": "own GFS archive (first hours of each run)"}
        if out.exists():
            old = json.loads((out / "meta.json").read_text())
            if any(old.get(k) != expected[k] for k in ("lats", "lons", "fields", "levels_mb", "region")):
                aside = out.with_name(out.name + f".replaced-{int(time.time())}")
                log.warning("region %s changed: keeping the old climatology at %s and starting a new one",
                            region["name"], aside)
                out.rename(aside)
        if not out.exists():
            out.mkdir(parents=True)
            np.lib.format.open_memmap(out / "values.npy", mode="w+", dtype=np.float32, shape=shape)[:] = 0
            np.save(out / "counts.npy", np.zeros((12, nslots), dtype=np.int32))
            write_json_atomic(out / "meta.json", expected)
        done_file = out / "archived_cycles.txt"
        done = set(done_file.read_text().split()) if done_file.exists() else set()
        if cycle.id in done:
            continue
        sums = np.load(out / "values.npy", mmap_mode="r+")
        counts = np.load(out / "counts.npy")
        added = 0
        for h in hours:
            src = rdir / f"f{h:03d}.npy"
            if not src.exists():
                continue
            arr = np.load(src)
            if np.isnan(arr).any():
                continue
            t = cycle.time + timedelta(hours=h)
            m, s = t.month - 1, t.hour // GFS_SLOT_HOURS
            sums[m, s] += arr
            counts[m, s] += 1
            added += 1
        sums.flush()
        del sums
        np.save(out / "counts.npy", counts)
        with open(done_file, "a") as fh:
            fh.write(cycle.id + "\n")
        os.utime(out / "meta.json")                 # tells the API to reload
        log.info("climatology %s: added %d hours of %s", region["name"], added, cycle.id)
