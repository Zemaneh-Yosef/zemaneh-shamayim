#!/usr/bin/env python3
"""Path profiles for past dates from ERA5, for checking the calculation against observed sunrises / sunsets.

/v1/path-profiles answers with GFS (a forecast, or typical values), so it can't say how well the whole
chain - air along the path, the 80 m / 100 m levels, columns stood on the real ground, the ray tracing, the
terrain - reproduces sunrises someone actually watched (Rabbi Druk's 1985-96 sunrises at Armon HaNatziv,
Yisroel Golan's at Nevei Yaakov, or your own). ERA5, ECMWF's reanalysis, has the air hour by hour back
to 1940 at 0.25 deg with 37 pressure levels. This writes, for the dates asked, exactly what
/v1/path-profiles would have served (same JSON, "source": "era5"), built by the same code (build_profile,
follow_terrain with the server's elevation tiles), for royzmanimwebsite's _denoDebug/validateSunrise.mjs.

    python3 era5_profiles.py --lat 31.7486 --lon 35.2374 --dates druk.csv --event sunrise --out era5.json
    python3 era5_profiles.py --lat 40.6093 --lon -73.9683 --from 2024-01-01 --to 2024-12-31 --out era5.json

--dates: a file with one date (YYYY-MM-DD) at the start of each line (a CSV of observations works: lines
that don't start with a date are skipped). Profiles reach api.distances_km (400 km) along the Sun's
azimuth, as the API's do; api.follow_terrain applies unless --no-follow-terrain.

The data comes from the Copernicus Climate Data Store (free; needs an account and its API key in
~/.cdsapirc, see https://cds.climate.copernicus.eu/how-to-api; pip install cdsapi). Only a box around the
paths is requested, one month at a time and only the hours around the events: a year of sunrises is a few
MB. Downloads are kept in --cache (default <data_dir>/era5), so a rerun downloads nothing. --nc-dir reads
files you downloaded yourself instead (named era5-pl-YYYYMM.nc and era5-sl-YYYYMM.nc; pressure levels:
temperature + geopotential; single levels: 2 m temperature, surface pressure, geopotential, land-sea
mask, skin temperature), which is also what the tests do.

ERA5's own terrain is smoothed like GFS's (follow_terrain corrects that), and it has no 80 m / 100 m levels:
its lowest pressure levels are 1000 / 975 / 950 / 925 mb. Data: Hersbach et al. (2020), ERA5 hourly data on
pressure / single levels, Copernicus Climate Change Service (C3S) Climate Data Store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from climatology import build_profile
from common import destination, load_config, sun_event

log = logging.getLogger("era5_profiles")

G0 = 9.80665
LEVELS = [1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 700, 650, 600, 550, 500]
PL_VARS = ["temperature", "geopotential"]
SL_VARS = ["2m_temperature", "surface_pressure", "geopotential", "land_sea_mask", "skin_temperature"]


def read_dates(path: Path) -> list[date]:
    out = []
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", line)
        if m:
            out.append(date.fromisoformat(m.group(1)))
    return sorted(set(out))


def plan(cfg: dict, lat: float, lon: float, dates: list[date], events: list[str]):
    """[(date, event, time, azimuth, [(distance, lat, lon)])] and the box (N, W, S, E) around all paths."""
    out = []
    lats, lons = [lat], [lon]
    for d in dates:
        for ev in events:
            se = sun_event(d, lat, lon, ev)
            if se is None:
                continue
            t, az = se
            pts = [(dist, *destination(lat, lon, az, dist)) for dist in cfg["api"]["distances_km"]]
            lats += [p[1] for p in pts]
            lons += [p[2] for p in pts]
            out.append((d, ev, t, az, pts))
    m = 0.5                                             # a grid point beyond every path point
    box = (math.ceil((max(lats) + m) * 4) / 4, math.floor((min(lons) - m) * 4) / 4,
           math.floor((min(lats) - m) * 4) / 4, math.ceil((max(lons) + m) * 4) / 4)
    return out, box


def hours_needed(events) -> dict[str, dict]:
    """{YYYYMM: {"days": [...], "hours": [...]}}: the hours on either side of each event."""
    out: dict[str, dict] = {}
    for _, _, t, _, _ in events:
        for h in (t.replace(minute=0, second=0, microsecond=0), t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)):
            k = h.strftime("%Y%m")
            e = out.setdefault(k, {"days": set(), "hours": set()})
            e["days"].add(h.day)
            e["hours"].add(h.hour)
    return {k: {"days": sorted(v["days"]), "hours": sorted(v["hours"])} for k, v in sorted(out.items())}


def download(cache: Path, month: str, need: dict, box) -> tuple[Path, Path]:
    """The month's two files for this box (from the cache, or the CDS)."""
    tag = hashlib.sha1(json.dumps([box, need]).encode()).hexdigest()[:10]
    pl, sl = cache / f"era5-pl-{month}-{tag}.nc", cache / f"era5-sl-{month}-{tag}.nc"
    if pl.exists() and sl.exists():
        return pl, sl
    try:
        import cdsapi
    except ImportError:
        raise SystemExit("ERA5 downloads need the CDS API client: pip install cdsapi, and your key in ~/.cdsapirc "
                         "(https://cds.climate.copernicus.eu/how-to-api); or pass --nc-dir") from None
    cache.mkdir(parents=True, exist_ok=True)
    c = cdsapi.Client(quiet=True)
    common_req = {"product_type": ["reanalysis"], "year": [month[:4]], "month": [month[4:]],
                  "day": [f"{d:02d}" for d in need["days"]], "time": [f"{h:02d}:00" for h in need["hours"]],
                  "area": list(box), "data_format": "netcdf", "download_format": "unarchived"}
    for dataset, target, extra in (
            ("reanalysis-era5-pressure-levels", pl, {"variable": PL_VARS, "pressure_level": [str(p) for p in LEVELS]}),
            ("reanalysis-era5-single-levels", sl, {"variable": SL_VARS})):
        if target.exists():
            continue
        log.info("requesting %s for %s (%d days x %d hours)", dataset, month, len(need["days"]), len(need["hours"]))
        tmp = target.with_suffix(".part")
        c.retrieve(dataset, {**common_req, **extra}, str(tmp))
        tmp.replace(target)
    return pl, sl


def _var(ds, *names):
    for n in names:
        if n in ds.variables:
            return ds.variables[n]
    raise KeyError(f"none of {names} in {list(ds.variables)}")


def _times(ds) -> list[datetime]:
    import netCDF4
    v = _var(ds, "valid_time", "time")
    return [datetime(t.year, t.month, t.day, t.hour, tzinfo=timezone.utc)
            for t in netCDF4.num2date(v[:], v.units, getattr(v, "calendar", "standard"))]


class Era5:
    """ERA5 fields by hour, [time][field] -> (lat ascending, lon ascending) arrays, under GFS-like names
    (t2m, tsfc, psfc, orog, land, t<p>, z<p>). Every month added must be on the same grid (one box)."""

    def __init__(self):
        self.fields: dict[datetime, dict[str, np.ndarray]] = {}
        self.lats = self.lons = None
        self.levels: list[int] = []

    def add(self, pl_path: Path, sl_path: Path) -> None:
        import netCDF4
        with netCDF4.Dataset(pl_path) as pl, netCDF4.Dataset(sl_path) as sl:
            lat = np.asarray(_var(pl, "latitude", "lat")[:], dtype=float)
            lon = (np.asarray(_var(pl, "longitude", "lon")[:], dtype=float) + 180) % 360 - 180
            fy, fx = np.argsort(lat), np.argsort(lon)
            if self.lats is not None and not (np.array_equal(self.lats, lat[fy]) and np.array_equal(self.lons, lon[fx])):
                raise ValueError(f"{pl_path}: not on the same grid as the other months")
            self.lats, self.lons = lat[fy], lon[fx]
            levels = [int(round(float(p))) for p in _var(pl, "pressure_level", "level")[:]]
            fix = lambda a: np.asarray(a, dtype=np.float64)[..., fy, :][..., fx]          # noqa: E731
            t, z = fix(_var(pl, "t")[:]), fix(_var(pl, "z")[:])
            sv = {k: fix(_var(sl, k)[:]) for k in ("t2m", "sp", "z", "lsm", "skt")}
            slt = {tt: i for i, tt in enumerate(_times(sl))}
            for i, tt in enumerate(_times(pl)):
                j = slt.get(tt)
                if j is None:
                    continue
                f = {"t2m": sv["t2m"][j], "tsfc": sv["skt"][j], "psfc": sv["sp"][j],
                     "orog": sv["z"][j] / G0, "land": sv["lsm"][j]}
                for k, p in enumerate(levels):
                    f[f"t{p}"], f[f"z{p}"] = t[i, k], z[i, k] / G0
                self.fields[tt] = f
        self.levels = sorted(set(self.levels) | set(levels), reverse=True)

    def at(self, t: datetime, lat: float, lon: float) -> dict | None:
        """Fields at a place and time (bilinear in space, linear in time between the hours around t)."""
        la, lo = self.lats, self.lons
        if not (la[0] <= lat <= la[-1] and lo[0] <= lon <= lo[-1]):
            return None
        h0 = t.replace(minute=0, second=0, microsecond=0)
        h1 = h0 + timedelta(hours=1)
        if h0 not in self.fields or h1 not in self.fields:
            return None
        w = (t - h0).total_seconds() / 3600
        i = min(int(np.searchsorted(la, lat, side="right") - 1), len(la) - 2)
        j = min(int(np.searchsorted(lo, lon, side="right") - 1), len(lo) - 2)
        fy = (lat - la[i]) / (la[i + 1] - la[i])
        fx = (lon - lo[j]) / (lo[j + 1] - lo[j])
        out = {}
        for k in self.fields[h0]:
            v = 0.0
            for hh, wt in ((h0, 1 - w), (h1, w)):
                a = self.fields[hh][k]
                v += wt * (a[i, j] * (1 - fy) * (1 - fx) + a[i, j + 1] * (1 - fy) * fx
                           + a[i + 1, j] * fy * (1 - fx) + a[i + 1, j + 1] * fy * fx)
            out[k] = float(v)
        return out


def make(cfg: dict, lat: float, lon: float, dates: list[date], events: list[str], files, ground=None) -> dict:
    """The /v1/path-profiles answer for these dates from ERA5. files(month, need, box) -> (pl, sl) paths."""
    evs, box = plan(cfg, lat, lon, dates, events)
    follow = cfg["api"].get("follow_terrain") or {}
    if not follow.get("enabled"):
        ground = None
    max_km = float(follow.get("max_km", 150.0))
    era5 = Era5()
    for month, need in hours_needed(evs).items():
        era5.add(*files(month, need, box))
    out = []
    for d, ev, t, az, pts in evs:
        near = [k for k, (dist, _, _) in enumerate(pts) if dist <= max_km]
        samples = [None] * len(pts)
        if ground is not None and near:
            for k, smp in zip(near, ground([pts[k][1:] for k in near])):
                samples[k] = smp
        profs = []
        for (dist, plat, plon), smp in zip(pts, samples):
            f = era5.at(t, plat, plon)
            if f is None:
                break
            kw = {"ground_samples": smp, "follow": follow} if smp is not None else {}
            profs.append(build_profile(f, era5.levels, dist, plat, plon, 2.0, **kw))
        if len(profs) == len(pts):
            out.append({"date": d.isoformat(), "event": ev, "source": "era5",
                        "timeUtc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "azimuthDeg": round(az, 2), "profiles": profs})
        else:
            log.warning("%s %s: no ERA5 data for the whole path, left out", d, ev)
    return {"cycle": None, "location": {"lat": lat, "lon": lon},
            "sources": {"era5": "ERA5 hourly reanalysis (Copernicus C3S), 0.25 deg, 16 pressure levels"},
            "events": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dates", type=Path, help="file with a date (YYYY-MM-DD) at the start of each line")
    g.add_argument("--from", dest="start", type=date.fromisoformat)
    ap.add_argument("--to", dest="end", type=date.fromisoformat, help="with --from (default: that day)")
    ap.add_argument("--event", choices=["sunrise", "sunset", "both"], default="both")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache", type=Path, help="downloads kept here (default <data_dir>/era5)")
    ap.add_argument("--nc-dir", type=Path, help="read era5-pl-YYYYMM.nc / era5-sl-YYYYMM.nc from here instead")
    ap.add_argument("--no-follow-terrain", action="store_true", help="leave the columns on ERA5's terrain")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    if a.dates:
        dates = read_dates(a.dates)
    else:
        end = a.end or a.start
        dates = [a.start + timedelta(days=k) for k in range((end - a.start).days + 1)]
    if not dates:
        ap.error("no dates")
    events = ["sunrise", "sunset"] if a.event == "both" else [a.event]
    if a.nc_dir:
        files = lambda month, need, box: (a.nc_dir / f"era5-pl-{month}.nc", a.nc_dir / f"era5-sl-{month}.nc")   # noqa: E731
    else:
        cache = a.cache or Path(cfg["data_dir"]) / "era5"
        files = lambda month, need, box: download(cache, month, need, box)   # noqa: E731
    ground = None
    if not a.no_follow_terrain:
        import serve
        ground = serve.Ground(serve.Horizons(cfg).tiles, cfg["api"].get("follow_terrain") or {})
    else:
        cfg["api"]["follow_terrain"] = {"enabled": False}
    body = make(cfg, a.lat, a.lon, dates, events, files, ground)
    a.out.write_text(json.dumps(body, separators=(",", ":")))
    log.info("%d events written to %s", len(body["events"]), a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
