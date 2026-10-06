#!/usr/bin/env python3
"""Build the NOAA reanalysis climatology (the "ncep" source) for the configured regions.

Downloads NCEP/NCAR Reanalysis 1 long-term means (1991-2020, 4 times a day) from NOAA PSL - no key
or registration - and reduces them to each region: 12 months x 4 times of day x fields, a few MB.
About 2.1 GB is downloaded (the two pressure-level files are ~1 GB each); the raw files are deleted
afterwards unless --keep-raw.

    python3 build_prior.py [--config config.json] [--region NAME] [--source-dir DIR] [--keep-raw]

fetch_gfs.py runs this automatically for any region that has no prior yet (see "climatology" in the
config). Data: NCEP/NCAR Reanalysis 1, NOAA PSL, Boulder, Colorado, USA, https://psl.noaa.gov
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import urllib.request
from pathlib import Path

import numpy as np

from climatology import clim_dir
from common import load_config, write_json_atomic

log = logging.getLogger("build_prior")

PSL = "https://downloads.psl.noaa.gov/Datasets/ncep.reanalysis"
FILES = {   # role -> path under PSL
    "air": "Monthlies/pressure/air.4Xday.ltm.1991-2020.nc",
    "hgt": "Monthlies/pressure/hgt.4Xday.ltm.1991-2020.nc",
    "t_near": "Monthlies/surface/air.sig995.4Xday.ltm.1991-2020.nc",
    "psfc": "Monthlies/surface/pres.sfc.4Xday.ltm.1991-2020.nc",
    "orog": "surface/hgt.sfc.nc",
    "land": "surface/land.nc",
}
LEVELS = [1000, 925, 850, 700, 600, 500]     # Reanalysis 1 levels up to 500 mb
SLOT_HOURS = [0.0, 6.0, 12.0, 18.0]
SIGMA995_AGL = 40.0                          # sigma 0.995 is ~40 m above the ground


def fetch(url: str, dest: Path, contact: str) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        log.info("using already downloaded %s", dest.name)
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    ua = "zmanim-sky-server/0.1" + (f" ({contact})" if contact else "")
    log.info("downloading %s", url)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=300) as r, \
            open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
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


def build_region(cfg: dict, region: dict, raw: dict[str, Path]) -> Path:
    import netCDF4
    margin = 2.5
    out_fields = ["t2m", "psfc", "orog", "land"] + [f"t{p}" for p in LEVELS] + [f"z{p}" for p in LEVELS]
    values = None
    rows = cols = None
    for role in ("air", "hgt", "t_near", "psfc", "orog", "land"):
        with netCDF4.Dataset(raw[role]) as ds:
            ds.set_auto_maskandscale(True)
            v = data_var(ds)
            lats, lons = arr(ds.variables["lat"][:]), arr(ds.variables["lon"][:])
            if rows is None:
                rows, cols, rlats, rlons = region_index(lats, lons, region, margin)
                values = np.zeros((12, 4, len(out_fields), len(rows), len(cols)), dtype=np.float64)
                counts = np.zeros((12, 4), dtype=np.int64)
            units = getattr(v, "units", "")
            log.info("%s: %s %s dims=%s units=%r", raw[role].name, v.name, v.shape, v.dimensions, units)
            if role in ("orog", "land"):                       # constant fields: (time=1,) lat, lon
                a = arr(v[:]).reshape(-1, len(lats), len(lons))[0][np.ix_(rows, cols)]
                if role == "orog" and "geopotential" in (getattr(v, "long_name", "") or "").lower() \
                        and np.nanmax(a) > 20000:
                    a = a / 9.80665                            # stored as geopotential (m2/s2)
                values[:, :, out_fields.index(role)] = a
                continue
            n_time = v.shape[0]
            months, slots = month_and_slot(ds, n_time)
            if role in ("air", "hgt"):
                levs = list(np.asarray(ds.variables["level"][:]).astype(float))
                want = [(p, levs.index(float(p))) for p in LEVELS if float(p) in levs]
                if len(want) < 4:
                    raise ValueError(f"{raw[role].name}: expected levels {LEVELS}, file has {levs}")
            for ti in range(n_time):
                m, s = months[ti], slots[ti]
                if role in ("air", "hgt"):
                    blk = arr(v[ti, [k for _, k in want]])[:, rows][:, :, cols]
                    if role == "air":
                        blk = to_kelvin(blk, units)
                    for (p, _), b in zip(want, blk):
                        values[m, s, out_fields.index(("t" if role == "air" else "z") + str(p))] += b
                else:
                    blk = arr(v[ti])[np.ix_(rows, cols)]
                    if role == "t_near":
                        values[m, s, out_fields.index("t2m")] += to_kelvin(blk, units)
                    else:
                        values[m, s, out_fields.index("psfc")] += to_pa(blk, units)
                if role == "air":
                    counts[m, s] += 1
    if counts.min() == 0:
        raise ValueError(f"some month / time-of-day slots have no data: {counts.tolist()}")
    timevarying = [k for k, n in enumerate(out_fields) if n not in ("orog", "land")]
    values[:, :, timevarying] /= counts[:, :, None, None, None]
    # fields absent from the file (levels missing) stay 0 -> mark NaN so they are skipped
    for k, n in enumerate(out_fields):
        if n[0] in "tz" and n[1:].isdigit() and not values[:, :, k].any():
            values[:, :, k] = np.nan
    out = clim_dir(cfg, "ncep", region["name"])
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    np.save(tmp / "values.npy", values.astype(np.float32))
    write_json_atomic(tmp / "meta.json", {
        "lats": rlats.tolist(), "lons": rlons.tolist(), "fields": out_fields, "levels_mb": LEVELS,
        "slot_hours": SLOT_HOURS, "near_surface_agl": SIGMA995_AGL, "region": region,
        "source": "NCEP/NCAR Reanalysis 1 long-term mean 1991-2020, 4x daily (NOAA PSL)"})
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    p = values[:, :, out_fields.index("t2m")]
    log.info("region %s: %dx%d points; near-surface air %.1f..%.1f C (Jan 00Z mean %.1f C)", region["name"],
             len(rlats), len(rlons), np.nanmin(p) - 273.15, np.nanmax(p) - 273.15, np.nanmean(p[0, 0]) - 273.15)
    return out


def regions_missing(cfg: dict) -> list[dict]:
    import json
    out = []
    for r in cfg["regions"]:
        meta = clim_dir(cfg, "ncep", r["name"]) / "meta.json"
        if not meta.exists() or json.loads(meta.read_text()).get("region") != r:
            out.append(r)
    return out


def build(cfg: dict, regions: list[dict], source_dir: Path | None = None, keep_raw: bool = False) -> None:
    if not regions:
        log.info("NOAA reanalysis climatology already built for all regions")
        return
    raw_dir = source_dir or Path(cfg["data_dir"]) / "ncep-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = {}
    for role, rel in FILES.items():
        dest = raw_dir / Path(rel).name
        if source_dir is None:
            fetch(f"{cfg.get('ncep_prior_base', PSL)}/{rel}", dest, cfg.get("contact", ""))
        elif not dest.exists():
            raise FileNotFoundError(dest)
        raw[role] = dest
    for r in regions:
        build_region(cfg, r, raw)
    if source_dir is None and not keep_raw:
        shutil.rmtree(raw_dir, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--region", help="only this region (default: every region without a prior)")
    ap.add_argument("--source-dir", type=Path, help="use already downloaded files from this directory")
    ap.add_argument("--keep-raw", action="store_true", help="keep the ~2 GB of downloaded files")
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
