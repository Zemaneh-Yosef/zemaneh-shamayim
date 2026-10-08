#!/usr/bin/env python3
"""Build the NOAA reanalysis climatology (the "ncep" source) for the configured regions.

By default this averages the most recent `climatology.ncep_years` complete years (10) of NCEP/NCAR
Reanalysis 1, 4 times a day, from NOAA PSL's yearly files - no key or registration - and reduces them
to each region: 12 months x 4 times of day x fields, a few MB. Each year is downloaded (~600 MB: the
two pressure-level files are ~250-310 MB each), reduced to a global summary of ~25 MB that is kept
under <data_dir>/climatology/ncep-years/, and deleted. So:

* the first build downloads about 6 GB (10 years), one year at a time (~600 MB of disk at most);
* the window moves forward once a year (from March, when NOAA has finished the previous year's
  files): only the new year is downloaded;
* adding a region needs no download at all.

`climatology.ncep_years: 0` uses NOAA's fixed 1991-2020 long-term means instead (about 2.1 GB,
centred on ~2005, so a couple of decades behind today's climate).

    python3 build_prior.py [--config config.json] [--region NAME] [--source-dir DIR] [--keep-raw]

fetch_gfs.py runs this automatically for any region that has no prior yet, or whose prior covers an
older window than the current one (see "climatology" in the config). Data: NCEP/NCAR Reanalysis 1,
NOAA PSL, Boulder, Colorado, USA, https://psl.noaa.gov
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np

from climatology import clim_dir
from common import load_config, write_json_atomic

log = logging.getLogger("build_prior")

PSL = "https://downloads.psl.noaa.gov/Datasets/ncep.reanalysis"
STATIC_FILES = {
    "orog": "surface/hgt.sfc.nc",
    "land": "surface/land.nc",
}
LTM_FILES = {   # role -> path under PSL (NOAA's fixed 1991-2020 long-term means)
    "air": "Monthlies/pressure/air.4Xday.ltm.1991-2020.nc",
    "hgt": "Monthlies/pressure/hgt.4Xday.ltm.1991-2020.nc",
    "t_near": "Monthlies/surface/air.sig995.4Xday.ltm.1991-2020.nc",
    "psfc": "Monthlies/surface/pres.sfc.4Xday.ltm.1991-2020.nc",
}
YEAR_FILES = {  # role -> path under PSL for one year of 4x-daily data
    "air": "pressure/air.{year}.nc",
    "hgt": "pressure/hgt.{year}.nc",
    "t_near": "surface/air.sig995.{year}.nc",
    "psfc": "surface/pres.sfc.{year}.nc",
}
TIME_ROLES = ("air", "hgt", "t_near", "psfc")
LEVELS = [1000, 925, 850, 700, 600, 500]     # Reanalysis 1 levels up to 500 mb
SLOT_HOURS = [0.0, 6.0, 12.0, 18.0]
SIGMA995_AGL = 40.0                          # sigma 0.995 is ~40 m above the ground
OUT_FIELDS = ["t2m", "psfc", "orog", "land"] + [f"t{p}" for p in LEVELS] + [f"z{p}" for p in LEVELS]
DEFAULT_YEARS = 10
FINISHED_BY_MONTH = 3        # NOAA has finished the previous year's files by about the end of January
CHUNK = 124                  # time steps read at once (~30 MB for the pressure-level files)


class MissingFile(Exception):
    """The file is not on the server (or not in --source-dir)."""


def fetch(url: str, dest: Path, contact: str) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        log.info("using already downloaded %s", dest.name)
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    ua = "zmanim-sky-server/0.1" + (f" ({contact})" if contact else "")
    log.info("downloading %s", url)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=300) as r, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code == 404:
            raise MissingFile(url) from None
        raise
    tmp.rename(dest)


def data_var(ds):
    """The file's main data variable (the one that is not a coordinate / bounds variable)."""
    skip = {"time", "level", "lat", "lon", "time_bnds", "climatology_bounds", "valid_yr_count", "nbnds"}
    cands = [v for n, v in ds.variables.items() if n not in skip and v.ndim >= 2]
    if not cands:
        raise ValueError(f"{ds.filepath()}: no data variable found")
    return max(cands, key=lambda v: v.ndim)


def arr(x) -> np.ndarray:
    """netCDF data (possibly masked) as a float array with NaN for missing values."""
    return np.ma.filled(np.ma.asarray(x, dtype=float), np.nan)


def to_kelvin(a, units: str):
    u = (units or "").strip().lower()
    return a + 273.15 if u in ("degc", "deg c", "c", "celsius", "degrees c", "degrees_celsius") else a


def to_pa(a, units: str):
    u = (units or "").strip().lower()
    if u in ("mb", "millibar", "millibars", "hpa"):
        return a * 100.0
    return a


def month_and_slot(ds, n_time: int):
    """For each time step: (month index 0-11, slot index 0-3)."""
    import netCDF4
    try:
        t = ds.variables["time"]
        dates = netCDF4.num2date(t[:], t.units, getattr(t, "calendar", "standard"),
                                 only_use_cftime_datetimes=True)
        months = np.array([d.month - 1 for d in dates])
        slots = np.array([SLOT_HOURS.index(float(d.hour)) for d in dates])
        return months, slots
    except Exception as e:                    # fall back to "4 per day, starting 1 Jan 00 UTC"
        if n_time % 4:
            raise ValueError(f"cannot interpret the time axis ({e})") from None
        log.warning("time axis not decodable (%s); assuming 4 steps per day from 1 Jan", e)
        doy = np.arange(n_time) // 4
        ndays = n_time // 4
        cum = np.cumsum([31, 29 if ndays == 366 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])
        months = np.searchsorted(cum, doy, side="right")
        return months, np.arange(n_time) % 4


def region_index(lats: np.ndarray, lons: np.ndarray, region: dict, margin: float):
    """Row / column indices covering the region (+margin), lats ascending, lons -180..180 monotonic."""
    lat_order = np.argsort(lats)
    la = lats[lat_order]
    rows = lat_order[(la >= region["south"] - margin) & (la <= region["north"] + margin)]
    rows = rows[np.argsort(lats[rows])]
    lon180 = (lons + 180) % 360 - 180
    w, e = region["west"] - margin, region["east"] + margin
    sel = [k for k in np.argsort(lon180) if w <= lon180[k] <= e]
    cols = np.array(sel, dtype=int)
    if len(rows) < 2 or len(cols) < 2:
        raise ValueError(f"region {region['name']} is too small for the 2.5 deg grid")
    return rows, cols, lats[rows], lon180[cols]


# ---------------------------------------------------------------------------------------------
# Which years
# ---------------------------------------------------------------------------------------------
def n_years(cfg: dict) -> int:
    return int(cfg.get("climatology", {}).get("ncep_years", DEFAULT_YEARS))


def target(cfg: dict, today: date | None = None):
    """The window the prior should cover: [first year, last year], or "ltm" for NOAA's 1991-2020 means."""
    n = n_years(cfg)
    if n <= 0:
        return "ltm"
    today = today or date.today()
    end = today.year - 1 if today.month >= FINISHED_BY_MONTH else today.year - 2
    return [end - n + 1, end]


def regions_missing(cfg: dict, today: date | None = None) -> list[dict]:
    """Regions without a prior, built for another box, or built for an older window of years."""
    want = target(cfg, today)
    out = []
    for r in cfg["regions"]:
        meta_path = clim_dir(cfg, "ncep", r["name"]) / "meta.json"
        if not meta_path.exists():
            out.append(r)
            continue
        meta = json.loads(meta_path.read_text())
        have = meta.get("target", "ltm")          # priors built before yearly windows were the 1991-2020 means
        if meta.get("region") != r or have != want:
            out.append(r)
    return out


# ---------------------------------------------------------------------------------------------
# Reducing NOAA files to global month x time-of-day means (cached per year)
# ---------------------------------------------------------------------------------------------
def cache_dir(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "climatology" / "ncep-years"


def reduce_files(paths: dict[str, Path], require_full_year: bool):
    """Global means by month and time of day of the four time-varying files.

    Returns (means float32 [12, 4, field, lat, lon], counts int [role, 12, 4], lats, lons), or None
    if require_full_year and a file holds less than a whole year."""
    import netCDF4
    sums = counts = lats = lons = None
    for ri, role in enumerate(TIME_ROLES):
        with netCDF4.Dataset(paths[role]) as ds:
            ds.set_auto_maskandscale(True)
            v = data_var(ds)
            la, lo = arr(ds.variables["lat"][:]), arr(ds.variables["lon"][:])
            if sums is None:
                lats, lons = la, lo
                sums = np.zeros((12, 4, len(OUT_FIELDS), len(lats), len(lons)), dtype=np.float64)
                counts = np.zeros((len(TIME_ROLES), 12, 4), dtype=np.int64)
            elif not (np.array_equal(la, lats) and np.array_equal(lo, lons)):
                raise ValueError(f"{paths[role].name}: grid differs from {paths['air'].name}")
            units = getattr(v, "units", "")
            n_time = v.shape[0]
            log.info("%s: %s %s dims=%s units=%r", paths[role].name, v.name, v.shape, v.dimensions, units)
            if require_full_year and n_time < 365 * 4:
                log.warning("%s holds only %d time steps (%.0f days): not a complete year",
                            paths[role].name, n_time, n_time / 4)
                return None
            months, slots = month_and_slot(ds, n_time)
            if role in ("air", "hgt"):
                levs = list(np.asarray(ds.variables["level"][:]).astype(float))
                want = [(p, levs.index(float(p))) for p in LEVELS if float(p) in levs]
                if len(want) < 4:
                    raise ValueError(f"{paths[role].name}: expected levels {LEVELS}, file has {levs}")
                kidx = [k for _, k in want]
                fidx = [OUT_FIELDS.index(("t" if role == "air" else "z") + str(p)) for p, _ in want]
            else:
                fidx = [OUT_FIELDS.index("t2m" if role == "t_near" else "psfc")]
            for t0 in range(0, n_time, CHUNK):
                t1 = min(n_time, t0 + CHUNK)
                blk = arr(v[t0:t1, kidx]) if role in ("air", "hgt") else arr(v[t0:t1])[:, None]
                if role in ("air", "t_near"):
                    blk = to_kelvin(blk, units)
                elif role == "psfc":
                    blk = to_pa(blk, units)
                for k in range(t1 - t0):
                    m, s = months[t0 + k], slots[t0 + k]
                    sums[m, s, fidx] += blk[k]
                    counts[ri, m, s] += 1
    if counts.min() == 0:
        raise ValueError(f"some month / time-of-day slots have no data: {counts[0].tolist()}")
    means = np.full(sums.shape, np.nan, dtype=np.float32)
    for ri, role in enumerate(TIME_ROLES):
        if role in ("air", "hgt"):
            ks = [k for k, n in enumerate(OUT_FIELDS) if n[0] == ("t" if role == "air" else "z") and n[1:].isdigit()]
        else:
            ks = [OUT_FIELDS.index("t2m" if role == "t_near" else "psfc")]
        for k in ks:
            if sums[:, :, k].any():                # levels absent from the file stay NaN
                means[:, :, k] = sums[:, :, k] / counts[ri][:, :, None, None]
    return means, counts, lats, lons


def save_cache(path: Path, means, counts, lats, lons, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, means=means, counts=counts, lats=lats, lons=lons,
             fields=np.array(OUT_FIELDS), source=np.array(source))
    tmp.replace(path)


def load_cache(path: Path):
    if not path.exists():
        return None
    with np.load(path) as z:
        if list(z["fields"]) != OUT_FIELDS:
            return None                            # made by an older version: rebuild it
        return z["means"], z["counts"], z["lats"], z["lons"]


def get_file(cfg: dict, rel: str, raw_dir: Path, source_dir: Path | None) -> Path:
    dest = (source_dir or raw_dir) / Path(rel).name
    if source_dir is None:
        fetch(f"{cfg.get('ncep_prior_base', PSL)}/{rel}", dest, cfg.get("contact", ""))
    elif not dest.exists():
        raise MissingFile(str(dest))
    return dest


def reduced_period(cfg: dict, key, raw_dir: Path, source_dir: Path | None, keep_raw: bool):
    """One year (key = int) or the long-term means (key = "ltm"): cached global means, downloading
    and reducing the NOAA files if needed. None if NOAA does not have that whole year (yet)."""
    files = LTM_FILES if key == "ltm" else {r: p.format(year=key) for r, p in YEAR_FILES.items()}
    cpath = cache_dir(cfg) / ("ltm-1991-2020.npz" if key == "ltm" else f"{key}.npz")
    cached = load_cache(cpath)
    if cached is not None:
        log.info("using the stored summary of %s", key)
        return cached
    paths = {}
    try:
        for role in TIME_ROLES:
            paths[role] = get_file(cfg, files[role], raw_dir, source_dir)
    except MissingFile as e:
        if key == "ltm":
            raise
        log.warning("year %s is not available (%s)", key, e)
        return None
    got = reduce_files(paths, require_full_year=key != "ltm")
    if source_dir is None and not keep_raw:
        for p in paths.values():
            p.unlink(missing_ok=True)
    if got is None:
        return None
    save_cache(cpath, *got, source=str(key))
    return got


def combine(cfg: dict, raw_dir: Path, source_dir: Path | None, keep_raw: bool, want):
    """Means over the window: (means [12, 4, field, lat, lon], lats, lons, years used)."""
    if want == "ltm":
        means, _, lats, lons = reduced_period(cfg, "ltm", raw_dir, source_dir, keep_raw)
        return means, lats, lons, "1991-2020 (long-term mean)"
    first, last = want
    n = last - first + 1
    used, total, weight, grid = [], None, None, None
    for year in range(last, first - 6, -1):        # a year NOAA lacks is replaced by an earlier one
        if len(used) == n:
            break
        got = reduced_period(cfg, year, raw_dir, source_dir, keep_raw)
        if got is None:
            continue
        means, counts, lats, lons = got
        if grid is None:
            grid = (lats, lons)
            total = np.zeros(means.shape, dtype=np.float64)
            weight = np.zeros(means.shape, dtype=np.float64)
        elif not (np.array_equal(lats, grid[0]) and np.array_equal(lons, grid[1])):
            raise ValueError(f"year {year}: grid differs from the other years")
        w = np.zeros(means.shape[:3])
        for ri, role in enumerate(TIME_ROLES):        # each field weighted by its own time steps
            for k, name in enumerate(OUT_FIELDS):
                if role_of(name) == role:
                    w[:, :, k] = counts[ri]
        ok = np.isfinite(means)
        total += np.where(ok, means, 0.0) * w[..., None, None]
        weight += ok * w[..., None, None]
        used.append(year)
    if len(used) < max(1, (n + 1) // 2):
        raise RuntimeError(f"only {len(used)} of {n} years available from NOAA ({sorted(used)}); will retry")
    if len(used) < n:
        log.warning("using %d years instead of %d: %s", len(used), n, sorted(used))
    with np.errstate(invalid="ignore", divide="ignore"):
        means = total / weight                     # NaN where no year had the value
    used.sort()
    label = f"{used[0]}-{used[-1]}" if used == list(range(used[0], used[-1] + 1)) else ", ".join(map(str, used))
    return means.astype(np.float32), grid[0], grid[1], label


def role_of(field: str) -> str | None:
    if field == "t2m":
        return "t_near"
    if field == "psfc":
        return "psfc"
    if field[0] in "tz" and field[1:].isdigit():
        return "air" if field[0] == "t" else "hgt"
    return None


def static_fields(cfg: dict, raw_dir: Path, source_dir: Path | None):
    """Terrain height (m) and land fraction on the reanalysis grid: {role: (array, lats, lons)}."""
    import netCDF4
    out = {}
    for role, rel in STATIC_FILES.items():
        with netCDF4.Dataset(get_file(cfg, rel, raw_dir, source_dir)) as ds:
            ds.set_auto_maskandscale(True)
            v = data_var(ds)
            la, lo = arr(ds.variables["lat"][:]), arr(ds.variables["lon"][:])
            a = arr(v[:]).reshape(-1, len(la), len(lo))[0]
            if role == "orog" and "geopotential" in (getattr(v, "long_name", "") or "").lower() \
                    and np.nanmax(a) > 20000:
                a = a / 9.80665                            # stored as geopotential (m2/s2)
            out[role] = (a, la, lo)
    return out


def build_region(cfg: dict, region: dict, means, lats, lons, static, label: str, want) -> Path:
    margin = 2.5
    rows, cols, rlats, rlons = region_index(lats, lons, region, margin)
    values = np.array(means[:, :, :, rows][:, :, :, :, cols], dtype=np.float32)
    for role in ("orog", "land"):
        a, la, lo = static[role]
        if not (np.array_equal(la, lats) and np.array_equal(lo, lons)):
            raise ValueError(f"{STATIC_FILES[role]}: grid differs from the time-varying files")
        values[:, :, OUT_FIELDS.index(role)] = a[np.ix_(rows, cols)]
    out = clim_dir(cfg, "ncep", region["name"])
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    np.save(tmp / "values.npy", values)
    write_json_atomic(tmp / "meta.json", {
        "lats": rlats.tolist(), "lons": rlons.tolist(), "fields": OUT_FIELDS, "levels_mb": LEVELS,
        "slot_hours": SLOT_HOURS, "near_surface_agl": SIGMA995_AGL, "region": region,
        "target": want, "years": label,
        "source": f"NCEP/NCAR Reanalysis 1, {label} average, 4x daily (NOAA PSL)"})
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    p = values[:, :, OUT_FIELDS.index("t2m")]
    log.info("region %s (%s): %dx%d points; near-surface air %.1f..%.1f C (Jan 00Z mean %.1f C)", region["name"],
             label, len(rlats), len(rlons), np.nanmin(p) - 273.15, np.nanmax(p) - 273.15,
             np.nanmean(p[0, 0]) - 273.15)
    return out


def build(cfg: dict, regions: list[dict], source_dir: Path | None = None, keep_raw: bool = False,
          today: date | None = None) -> None:
    if not regions:
        log.info("NOAA reanalysis climatology already built for all regions")
        return
    want = target(cfg, today)
    raw_dir = Path(cfg["data_dir"]) / "ncep-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log.info("NOAA reanalysis climatology for %s: %s", [r["name"] for r in regions],
             "1991-2020 long-term means" if want == "ltm" else f"years {want[0]}-{want[1]}")
    means, lats, lons, label = combine(cfg, raw_dir, source_dir, keep_raw, want)
    static = static_fields(cfg, raw_dir, source_dir)
    for r in regions:
        build_region(cfg, r, means, lats, lons, static, label, want)
    if want != "ltm":                              # summaries of years that left the window
        keep = set(range(want[0] - 5, want[1] + 1))
        for p in cache_dir(cfg).glob("*.npz"):
            if not p.stem.isdigit() or int(p.stem) not in keep:
                p.unlink(missing_ok=True)
    if not keep_raw:
        shutil.rmtree(raw_dir, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--region", help="only this region (default: every region without an up-to-date prior)")
    ap.add_argument("--source-dir", type=Path, help="use already downloaded files from this directory")
    ap.add_argument("--keep-raw", action="store_true", help="keep the downloaded NOAA files")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    regions = [r for r in cfg["regions"] if r["name"] == args.region] if args.region else regions_missing(cfg)
    if args.region and not regions:
        log.error("no region named %s in the config", args.region)
        return 1
    build(cfg, regions, args.source_dir, args.keep_raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
