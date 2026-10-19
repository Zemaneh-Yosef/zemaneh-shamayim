"""Climatological (typical) temperature profiles, for dates beyond the forecast.

Two sources, both by month and time of day:

* "gfs":  your own climatology, built by fetch_gfs.py from the first 12 hours of every GFS run it
          stores (0.25 deg, 13 levels, water temperature). Grows by itself; a month / time-of-day slot
          is used once it holds `climatology.min_days` days of data. The air 80 m and 100 m above the
          ground (`near_surface_m`) is kept beside it in a store of its own, "gfs-near", with its own
          counts and list of runs: it was added later (and GFS has it only from March 2021), so the
          main store's years of data are kept as they are, and the profile gets those levels wherever
          "gfs-near" has enough data too. A third store, "gfs-moments", keeps the sums of the low-level
          temperatures (MOMENT_VARS) and of their pairwise products, i.e. their spread and how they vary
          together; sigma_members() turns that into a few representative profiles, so a calendar can be the
          typical outcome rather than the outcome of the typical air (see below).
* "ncep": NOAA NCEP/NCAR Reanalysis 1, last 10 complete years averaged at 00/06/12/18 UTC (2.5 deg, levels
          1000/925/850/700/600/500 mb, air ~40 m above ground). Built without any key by build_prior.py
          (the window moves forward each year); the fallback until your own climatology has filled in.
          backfill_gfs.py can fill the "gfs" source with past GFS runs so it covers every month at once.

Storage:  <data_dir>/climatology/<source>/<region>/meta.json + values.npy [+ counts.npy]
          (source "gfs", "gfs-near" or "ncep")
          values: [month, time-of-day slot, field, lat, lon]; "gfs" stores running sums + counts.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from common import Cycle, field_names, write_json_atomic

G0, R_DRY = 9.80665, 287.05
DEPRESSION_M = -20.0                     # as terrain.DEPRESSION_M: below this the low ground is real
NEAR_FIELD = re.compile(r"^t(\d+)m$")       # t80m: air temperature 80 m above the ground
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
                  near_surface_agl: float = 2.0, ground_samples: list[float] | None = None,
                  follow: dict | None = None) -> dict:
    """One column of the path. With ground_samples (elevations around the point, see real_ground) and
    `follow` (api.follow_terrain), its lower part is moved to stand on the real ground: see follow_terrain()."""
    surface_h = f["orog"] + near_surface_agl
    levels = [{"h": round(surface_h, 1), "t": round(f["t2m"] - 273.15, 2), "p": round(f["psfc"] / 100, 2)}]
    # fixed heights above the ground (t80m, t100m), when the source has them; their pressure is
    # hydrostatic from the surface pressure through the mean of the 2 m air and the level's
    near = []
    for name, temp in f.items():
        m = NEAR_FIELD.match(name)
        if m and temp is not None and math.isfinite(temp) and int(m.group(1)) > near_surface_agl:
            agl = int(m.group(1))
            p = f["psfc"] * math.exp(-G0 * agl / (R_DRY * (f["t2m"] + temp) / 2))
            near.append({"h": round(f["orog"] + agl, 1), "t": round(temp - 273.15, 2), "p": round(p / 100, 2)})
    near.sort(key=lambda lv: lv["h"])
    levels += near
    for p in levels_mb:
        z, temp = f.get(f"z{p}"), f.get(f"t{p}")
        if z is None or temp is None or not (math.isfinite(z) and math.isfinite(temp)):
            continue
        if p * 100 < f["psfc"] and z > surface_h + 1 and all(abs(z - lv["h"]) > 1 for lv in near):
            levels.append({"h": round(z, 1), "t": round(temp - 273.15, 2), "p": float(p)})
    levels.sort(key=lambda lv: lv["h"])
    water = f["land"] < 0.5
    prof = {"distanceKm": dist_km, "lat": round(lat, 4), "lon": round(lon, 4), "water": bool(water), "levels": levels}
    if water and "tsfc" in f and math.isfinite(f["tsfc"]):
        prof["skinC"] = round(f["tsfc"] - 273.15, 2)
    ground_m = real_ground(ground_samples, f["orog"]) if ground_samples else None
    if ground_m is not None and follow and not water and math.isfinite(ground_m):
        prof["levels"] = follow_terrain(levels, f["orog"], ground_m, float(follow.get("full_m", 500.0)),
                                        float(follow.get("fade_m", 2000.0)))
        prof["modelGroundM"], prof["groundM"] = round(f["orog"], 1), round(ground_m, 1)
    return prof


def real_ground(samples: list[float], model_ground: float) -> float:
    """Mean of elevation samples around a (land) column's point. Heights below sea level count where the
    ground there is a depression (the samples' mean, or the weather model's own ground, below DEPRESSION_M:
    the Dead Sea rift, which GFS has at ~-300 m but ERA5, smoother, at ~+30 m); otherwise they are the
    sea-floor depths the default elevation tiles carry just offshore of a coastal column, and count as 0."""
    mean = float(np.mean(samples))
    if model_ground >= DEPRESSION_M and mean >= DEPRESSION_M:
        return float(np.mean([max(v, 0.0) for v in samples]))
    return mean


def follow_terrain(levels: list[dict], model_ground: float, ground: float, full_m: float, fade_m: float) -> list[dict]:
    """Move a column's lower part onto the real ground. A 0.25 deg model smooths the terrain (GFS puts
    Jerusalem's ground at ~580 m, the city stands at ~750-800 m), but the air near the ground - a dawn
    inversion above all - lies along the real ground: an observer on a ridge stands in it, not hundreds of
    metres above it. Keller & Hall (2022, Computers & Geosciences 161, 105044, Fig. 8) shifted radiosonde
    profiles to follow the terrain along the ray and cut the winter error at Armon HaNatziv from 32 s to 12 s.

    Each level keeps its temperature and is raised (or lowered) by the ground difference times a weight that
    is 1 up to `full_m` above the model's ground and falls linearly to 0 at `fade_m` (widened to keep the
    levels in order where the difference is large); levels above are untouched. Pressures of moved levels
    are recomputed hydrostatically from the lowest unmoved level down (or from the surface pressure moved
    to the real ground, when every level moved)."""
    shift = ground - model_ground
    if abs(shift) < 1 or not levels:
        return levels
    fade_m = max(fade_m, full_m + 2 * abs(shift))
    out = []
    for lv in levels:
        a = lv["h"] - model_ground
        w = 1.0 if a <= full_m else max(0.0, 1.0 - (a - full_m) / (fade_m - full_m))
        out.append({**lv, "h": round(lv["h"] + shift * w, 1), "_moved": w > 0})
    fixed = [i for i, lv in enumerate(out) if not lv["_moved"]]
    tk = lambda lv: lv["t"] + 273.15                                    # noqa: E731
    if fixed:
        for i in range(fixed[0] - 1, -1, -1):
            hi, lo = out[i + 1], out[i]
            lo["p"] = round(hi["p"] * math.exp(G0 * (hi["h"] - lo["h"]) / (R_DRY * (tk(hi) + tk(lo)) / 2)), 2)
    else:
        b = out[0]
        b["p"] = round(b["p"] * math.exp(-G0 * shift / (R_DRY * tk(b))), 2)
        for i in range(1, len(out)):
            lo, hi = out[i - 1], out[i]
            hi["p"] = round(lo["p"] * math.exp(-G0 * (hi["h"] - lo["h"]) / (R_DRY * (tk(hi) + tk(lo)) / 2)), 2)
    for lv in out:
        del lv["_moved"]
    return out


# ---------------------------------------------------------------------------------------------
# Own GFS climatology: running sums by month and 3-hour slot (live runs and backfilled past runs)
# ---------------------------------------------------------------------------------------------
@contextmanager
def locked(out: Path):
    """Exclusive lock on one region's climatology (the fetcher and backfill_gfs.py may run at once)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out.parent / f".{out.name}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


GFS_STORES = ("gfs", "gfs-near", "gfs-moments")

# Low-level temperatures whose spread and co-variation "gfs-moments" keeps (those the cycle has): the 2 m
# and 80 m air and the pressure levels of the lowest ~1.5 km, where inversions decide horizon refraction.
MOMENT_VARS = ["t2m", "t80m", "t1000", "t975", "t950", "t925", "t900", "t850"]


def moment_vars(fields: list[str]) -> list[str]:
    v = [n for n in MOMENT_VARS if n in fields]
    return v if "t2m" in v and "t80m" in v and len(v) >= 4 else []


def moment_fields(vars_: list[str]) -> list[str]:
    """Sums of (T - 273.15) per variable, then of the products of every pair (a <= b)."""
    return [f"m:{a}" for a in vars_] + [f"m:{a}*{b}" for i, a in enumerate(vars_) for b in vars_[i:]]


def store_inputs(store: str, fields: list[str], levels_mb: list[int]) -> list[str]:
    """The forecast-cycle fields one of the own climatology's stores is made from."""
    if store == "gfs":
        return field_names(levels_mb)
    if store == "gfs-moments":
        return moment_vars(fields)
    main = set(field_names(levels_mb))                  # t2m looks like one too
    return [n for n in fields if NEAR_FIELD.match(n) and n not in main]


def store_fields(store: str, fields: list[str], levels_mb: list[int]) -> list[str]:
    """The fields of one of the own climatology's stores, out of a forecast cycle's `fields`."""
    inputs = store_inputs(store, fields, levels_mb)
    return moment_fields(inputs) if store == "gfs-moments" else inputs


def store_sample(store: str, arr: np.ndarray) -> np.ndarray:
    """What a store adds for one hour, from that hour's store_inputs (array[input, lat, lon])."""
    if store != "gfs-moments":
        return arr
    x = np.asarray(arr, dtype=np.float32) - np.float32(273.15)
    n = len(x)
    return np.concatenate([x] + [x[i:i + 1] * x[i:] for i in range(n)], axis=0)


def sigma_members(f: dict, mom: dict, modes: int = 3, kappa: float = 1.0) -> list[tuple[float, dict]]:
    """Representative variations of a column for a climatology date: [(weight, fields)].

    The profile from the mean air is not the typical sunrise: refraction grows faster than linearly with
    inversion strength, so a morning with a strong inversion moves the time more than a well-mixed one moves
    it back. From the low-level temperatures' means and covariance (mom: "gfs-moments" averaged, i.e. E[x]
    and E[xy] of T - 273.15) this takes the `modes` strongest ways they vary together (eigenvectors; the
    first is usually the whole column warmer or colder, the next an inversion stronger or weaker) and
    returns the mean column plus the column moved +-sqrt(modes + kappa) standard deviations along each, with
    the unscented-transform weights kappa / (modes + kappa) and 1 / (2 (modes + kappa)). The calculator traces
    each; the weighted mean of their times is the typical outcome to second order, and their spread how far
    a given year's morning strays. Mode signs: the first is "warmer", the rest have a warmer 2 m air."""
    vars_ = [v for v in MOMENT_VARS if f"m:{v}" in mom and v in f]
    n = len(vars_)
    if n < 2:
        return [(1.0, f)]
    mean = np.array([mom[f"m:{v}"] for v in vars_])
    cov = np.empty((n, n))
    for i, a in enumerate(vars_):
        for j, b in enumerate(vars_):
            key = f"m:{a}*{b}" if i <= j else f"m:{b}*{a}"
            cov[i, j] = mom[key] - mean[i] * mean[j]
    lam, vec = np.linalg.eigh((cov + cov.T) / 2)
    order = np.argsort(lam)[::-1][:modes]
    m = len(order)
    out = [(kappa / (m + kappa), f)]
    for k, idx in enumerate(order):
        v = vec[:, idx]
        if (v.sum() if k == 0 else v[0]) < 0:          # vars_[0] is t2m
            v = -v
        step = math.sqrt((m + kappa) * max(float(lam[idx]), 0.0)) * v
        for sign in (1, -1):
            g = dict(f)
            for q, name in enumerate(vars_):
                g[name] = f[name] + sign * step[q]
            out.append((1 / (2 * (m + kappa)), g))
    return out


def gfs_clim_meta(region: dict, grid_meta: dict, store: str = "gfs") -> dict:
    """The meta.json of a region's own GFS climatology store, from a stored cycle's region meta (whose
    fields may be more than the store keeps)."""
    nslots = 24 // GFS_SLOT_HOURS
    fields = store_fields(store, grid_meta["fields"], grid_meta["levels_mb"])
    source = "own GFS archive (first hours of each run)"
    if store == "gfs-near":
        source += ", air temperature at fixed heights above the ground"
    elif store == "gfs-moments":
        source += ", sums and pairwise products of the low-level temperatures"
    return {"lats": grid_meta["lats"], "lons": grid_meta["lons"], "fields": fields,
            "levels_mb": grid_meta["levels_mb"], "region": region,
            "slot_hours": [GFS_SLOT_HOURS * k + GFS_SLOT_HOURS / 2 for k in range(nslots)],
            "near_surface_agl": 2.0, "source": source}


def open_gfs_clim(cfg: dict, region: dict, grid_meta: dict, log, store: str = "gfs") -> Path:
    """The region's own climatology directory for one store, created (or restarted for a changed grid)
    as needed. Call with it locked."""
    out = clim_dir(cfg, store, region["name"])
    expected = gfs_clim_meta(region, grid_meta, store)
    if out.exists():
        old = json.loads((out / "meta.json").read_text())
        if any(old.get(k) != expected[k] for k in ("lats", "lons", "fields", "levels_mb", "region")):
            aside = out.with_name(out.name + f".replaced-{int(time.time())}")
            log.warning("region %s changed: keeping the old %s climatology at %s and starting a new one",
                        region["name"], store, aside)
            out.rename(aside)
    if not out.exists():
        nslots = 24 // GFS_SLOT_HOURS
        shape = (12, nslots, len(expected["fields"]), len(expected["lats"]), len(expected["lons"]))
        out.mkdir(parents=True)
        np.lib.format.open_memmap(out / "values.npy", mode="w+", dtype=np.float32, shape=shape)[:] = 0
        np.save(out / "counts.npy", np.zeros((12, nslots), dtype=np.int32))
        write_json_atomic(out / "meta.json", expected)
    return out


def archived_cycles(out: Path) -> set[str]:
    f = out / "archived_cycles.txt"
    return set(f.read_text().split()) if f.exists() else set()


def add_samples(out: Path, cycle_id: str, samples, weight: int = 1) -> int:
    """Add [(valid time, array[field, lat, lon])] to the running sums, each counted `weight` times,
    and record the cycle as archived. Call with the region locked. Returns the number added."""
    sums = np.load(out / "values.npy", mmap_mode="r+")
    counts = np.load(out / "counts.npy")
    added = 0
    for t, arr in samples:
        if arr.shape != sums.shape[2:] or np.isnan(arr).any():
            continue
        m, s = t.month - 1, t.hour // GFS_SLOT_HOURS
        sums[m, s] += arr * np.float32(weight)
        counts[m, s] += weight
        added += 1
    sums.flush()
    del sums
    np.save(out / "counts.npy", counts)
    with open(out / "archived_cycles.txt", "a") as fh:
        fh.write(cycle_id + "\n")
    os.utime(out / "meta.json")                 # tells the API to reload
    return added


def archive_cycle(cfg: dict, cycle: Cycle, cycle_dir: Path, log) -> None:
    ccfg = cfg.get("climatology", {})
    if not ccfg.get("archive", True):
        return
    hours = [h for h in range(int(ccfg.get("archive_hours", 12)))]
    for region in cfg["regions"]:
        rdir = cycle_dir / region["name"]
        meta = json.loads((rdir / "meta.json").read_text())
        arrays = {h: np.load(rdir / f"f{h:03d}.npy", mmap_mode="r") for h in hours if (rdir / f"f{h:03d}.npy").exists()}
        for store in GFS_STORES:
            names = store_inputs(store, meta["fields"], meta["levels_mb"])
            if not names:
                continue
            idx = [meta["fields"].index(n) for n in names]
            target = clim_dir(cfg, store, region["name"])
            with locked(target):
                out = open_gfs_clim(cfg, region, meta, log, store)
                if cycle.id in archived_cycles(out):
                    continue
                samples = [(cycle.time + timedelta(hours=h), store_sample(store, np.asarray(a[idx])))
                           for h, a in arrays.items()]
                added = add_samples(out, cycle.id, samples)
            log.info("climatology %s (%s): added %d hours of %s", region["name"], store, added, cycle.id)
