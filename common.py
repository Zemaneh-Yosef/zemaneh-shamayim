"""Shared helpers for zmanim-sky-server: config, storage layout, sun and geodesy."""
from __future__ import annotations

import contextlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Fields stored per forecast hour, in this order. Temperatures K, pressure Pa, heights m.
SURFACE_FIELDS = ["t2m", "tsfc", "psfc", "orog", "land"]


def field_names(levels_mb: list[int]) -> list[str]:
    """The fields of the own climatology's main store (and of forecast cycles, before near_fields)."""
    return SURFACE_FIELDS + [f"t{p}" for p in levels_mb] + [f"z{p}" for p in levels_mb]


def near_fields(near_m: list[int]) -> list[str]:
    """Air temperature at fixed heights above the ground (GFS's 80 m and 100 m): the bottom of the
    boundary layer, where the pressure levels (~250 m apart near the ground) see a dawn inversion only as
    a straight line from the 2 m air to 975 mb. Kept in a store of its own in the climatology."""
    return [f"t{h}m" for h in near_m]


def cycle_field_names(cfg: dict) -> list[str]:
    """The fields stored per forecast hour."""
    return field_names(cfg["pressure_levels_mb"]) + near_fields(cfg.get("near_surface_m", []))


DEFAULT_CONFIG = {
    "data_dir": "/var/lib/zmanim-sky",
    "contact": "",
    "regions": [
        # Include ~4 degrees of margin beyond the places you serve: profiles reach 400 km out.
        {"name": "conus", "north": 55.0, "south": 20.0, "west": -130.0, "east": -60.0},
    ],
    "pressure_levels_mb": [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500],
    # temperature at these heights above the ground too (GFS has 80 and 100 m, from GFS v16, March 2021)
    "near_surface_m": [80, 100],
    "forecast_hours": {"hourly_until": 120, "step_after": 3, "max": 168},
    "cycles": [0, 12],
    "cycles_to_keep": 2,
    "request_delay_s": 2.0,
    "climatology": {
        "archive": True,          # add the first hours of every GFS run to your own climatology
        "archive_hours": 12,
        "min_days": 8,            # days of data a month / time-of-day slot needs before it is used
        "ncep_prior": True,       # build the NOAA reanalysis climatology (the fallback) for each region
        "ncep_years": 10,         # ... averaged over the last N complete years (~600 MB download per year,
                                  # once; the window moves forward each March). 0 = NOAA's fixed
                                  # 1991-2020 long-term means (~2.1 GB, a couple of decades behind)
    },
    "grib_filter": {
        "hourly": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl",
        "three_hourly": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl",
    },
    # The GFS ensemble (fetch_gefs.py): how sure each forecast sunrise is (/v1/path-profiles?...&ensemble=1)
    "ensemble": {
        "enabled": False,
        "members": 10,            # perturbed members besides the control (GEFS has 30)
        "cycles": [0],            # runs fetched (each ~45 min of requests per region at 2 s apart)
        "max_hours": 168,
        "step_hours": 3,
        "cycles_to_keep": 1,
        "grib_filter": {
            "a": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p50a.pl",
            "b": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p50b.pl",
        },
    },
    "terrain": {
        "tiles_dir": "",          # default: <data_dir>/tiles (SRTM tiles are downloaded there as needed)
        "tiles_url": "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz",
        "eye_m": 1.7,             # eye above the ground when no height is given
        "min_km": 1.0,            # terrain nearer than this is the observer's own surroundings
        "max_km": 150.0,
        "max_radius_km": 3.0,     # largest vantage radius (or bbox centre-to-corner) a request may ask for
        "max_area_radius_km": 30.0,  # largest official area (area=auto) the server will search
                                  # (bigger areas get a coarser grid: max_candidates spots in all)
        "min_grid_m": 50.0,
        "max_candidates": 1200,   # larger areas get a coarser grid (~25 ms per candidate spot)
        "moon_percentile": 90.0,  # &moon=1 in an area: spots at this percentile of horizon height (high
                                  # horizon = late moonrise / early moonset; 100 = the single worst spot)
        "max_parallel": 2,        # horizon computations at once (each takes ~1-10 s of CPU)
        # Bare-earth elevation merged from several sources (dem_layers.py). Off while "sources" is empty:
        # then only the tiles above are used. Each source: {"name", "paths": [GDAL-readable paths, globs
        # or /vsicurl/ URLs], "enabled": true}; earlier sources win wherever they have data.
        "layers": {
            "dir": "",            # default: <data_dir>/dem-layers (a subfolder per source list)
            "auto_build": False,  # build missing tiles during requests (needs rasterio in the venv);
                                  # otherwise only tiles built with `dem_layers.py build` are used
            "sources": [],
        },
        # Finer elevation data for the terrain near the observer (dem_layers.FineTiles), where a height error
        # costs the most time. Off while "sources" is empty. Sources as in "layers" (e.g. USGS 3DEP 1/3",
        # lidar DTMs); built on demand in 0.1 degree chunks (needs rasterio), ~4.5 MB each at 1/3".
        "fine": {
            "dir": "",            # default: <data_dir>/dem-fine (a subfolder per source list)
            "max_km": 10.0,       # terrain this close comes from the fine data, sampled every cell
            "step_arcsec": 1 / 3,
            "auto_build": True,
            "sources": [],
        },
    },
    "light_pollution": {
        # World Atlas of Artificial Night Sky Brightness (Falchi et al. 2016, CC BY-NC 4.0), in the
        # 4096-pixel tiles of github.com/astertaylor/halakhic_calc (or re-compressed copies, e.g. LERC)
        "dir": "",                # default: <data_dir>/light-pollution
        "file": "lp_{row}_{col}_lerc.tif",   # tile file name in dir (your own copies, or converted downloads)
        # where a tile missing from dir is downloaded from ({row}, {col}; {file} = the name above); "" = never
        "url": "https://raw.githubusercontent.com/astertaylor/halakhic_calc/main/data/lp_{row}_{col}.tif",
        # GeoTIFF creation options a download is re-compressed with before it is saved as `file` (checked,
        # then the original deleted). Lossy LERC: ~13% of the original size; max_z_error is in mcd/m^2.
        # {} = keep downloads as they come, under the URL's own file name.
        "convert": {"compress": "LERC_ZSTD", "max_z_error": 0.001},
        # Bringing the 2014 atlas to today. Sky-meter readings (a JSON file, see sky-measurements.example.json)
        # within radius_km of a place, or inside its area, calibrate it there; elsewhere the measured growth of
        # skyglow since the atlas (Kyba et al. 2023) is applied. "trend": {} = the atlas as it is.
        "measurements": {"file": "", "radius_km": 15.0},     # file default: <data_dir>/sky-measurements.json
        "trend": {
            "base_year": 2014, "data_until": 2022, "rate": 0.096,
            "regions": [
                {"name": "north-america", "rate": 0.104, "south": 7.0, "north": 85.0, "west": -170.0, "east": -50.0},
                {"name": "europe", "rate": 0.065, "south": 35.0, "north": 72.0, "west": -25.0, "east": 45.0},
            ],
        },
        # blpCdM2 = the sky's average artificial luminance = this x the zenith (measured horizontal-illuminance /
        # zenith-luminance ratios peak at 2.25 pi under light pollution; calc_time.py itself uses pi)
        "sky_average_factor": 2.25,
        "area_percentile": 90.0,  # area=auto / bbox: this percentile of the area's pixels (brighter = later)
        "max_radius_km": 30.0,    # largest area / bbox (centre to corner) summarised
    },
    "haze_calibration": {
        # CAMS haze vs AERONET's measurements (haze_calibration.py; /v1/haze-calibration)
        "file": "",               # default: <data_dir>/haze-calibration.json
        "years": 2,               # period compared, back from now (CAMS on Open-Meteo starts Aug 2022)
        "level": "1.5",           # AERONET level: 1.5 cloud-screened (recent), 2.0 quality-assured (trails)
        "min_days": 60,           # days with both measurement and model a station needs
        "margin_deg": 2.0,        # stations this far outside the regions count too
        "radius_km": 300.0,       # stations blended into a place's factor
        "request_delay_s": 8.0,   # between CAMS requests (each counts as ~25 Open-Meteo calls a year)
        "max_sites_per_run": None,
    },
    "areas": {
        "file": "",               # default: <data_dir>/areas.json (written by build_areas.py)
        "tiger_year": 2024,       # US Census TIGER/Line vintage build_areas.py downloads
    },
    "api": {
        "host": "127.0.0.1",
        "port": 8787,
        "distances_km": [0, 10, 25, 50, 100, 150, 200, 300, 400],
        # Stand each column's lower air on the real ground (from the elevation tiles / bare-earth layers)
        # instead of the weather model's smoothed terrain (climatology.follow_terrain). Only columns within
        # max_km (beyond that the ray is far above the ground) and over land; ground_radius_m: the ground is
        # the mean of the elevation data within this distance of the column's point. OFF: on Rabbi Druk's
        # 1993-96 sunrises with ERA5 it made the fit worse (rms 15.2 s against 13.5 s; README).
        "follow_terrain": {"enabled": False, "max_km": 150.0, "full_m": 500.0, "fade_m": 2000.0,
                           "ground_radius_m": 1000.0},
        "max_days": 400,          # longest date range one request may ask for
        "allow_origin": "*",
    },
}


def load_config(path: str | None = None) -> dict:
    path = path or (os.environ.get("ZMANIM_SKY_CONFIG") or os.environ.get("REFRACTION_CONFIG")) or str(HERE / "config.json")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if Path(path).exists():
        user = json.loads(Path(path).read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def forecast_hours(cfg: dict) -> list[int]:
    fh = cfg["forecast_hours"]
    hours = list(range(0, min(fh["hourly_until"], fh["max"]) + 1))
    h = fh["hourly_until"] + fh["step_after"]
    while h <= fh["max"]:
        hours.append(h)
        h += fh["step_after"]
    return hours


# ---------------------------------------------------------------------------------------------
# Storage layout:  <data_dir>/gfs/<YYYYMMDDHH>/<region>/f<FFF>.npy  (+ meta.json per region)
#                  <data_dir>/gfs/current.json  -> {"cycle": "...", "hours": [...]}
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cycle:
    time: datetime  # UTC

    @property
    def id(self) -> str:
        return self.time.strftime("%Y%m%d%H")

    @staticmethod
    def parse(s: str) -> "Cycle":
        return Cycle(datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc))


def gfs_dir(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "gfs"


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------
# Sun (low precision; only used to pick the forecast hour and the azimuth, good to ~2 min / ~0.5 deg)
# ---------------------------------------------------------------------------------------------
def _sun_day(d, lon: float):
    day0 = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    n = (day0 - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400 + 0.0008 - lon / 360 + 0.5
    M = (357.5291 + 0.98560028 * n) % 360
    C = 1.9148 * math.sin(math.radians(M)) + 0.02 * math.sin(math.radians(2 * M))
    L = (M + C + 180 + 102.9372) % 360
    noon_j = 2451545 + n + 0.0053 * math.sin(math.radians(M)) - 0.0069 * math.sin(math.radians(2 * L))
    dec = math.asin(math.sin(math.radians(L)) * math.sin(math.radians(23.44)))
    return noon_j, dec


def sun_event(d, lat: float, lon: float, event: str):
    """Approximate (time UTC, azimuth deg east of north) of sunrise/sunset on solar date d, or None
    if the Sun does not rise/set."""
    noon_j, dec = _sun_day(d, lon)
    h0 = math.radians(-0.833)
    phi = math.radians(lat)
    cos_h = (math.sin(h0) - math.sin(phi) * math.sin(dec)) / (math.cos(phi) * math.cos(dec))
    if not -1 <= cos_h <= 1:
        return None
    H = math.degrees(math.acos(cos_h))
    j = noon_j + (-H if event == "sunrise" else H) / 360
    t = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=j - 2440587.5)
    cos_a = (math.sin(dec) - math.sin(h0) * math.sin(phi)) / (math.cos(h0) * math.cos(phi))
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
    return t, (az if event == "sunrise" else 360 - az)


def solar_date(t: datetime, lon: float):
    """Calendar date at the place, by mean solar time (matches the civil date for sunrise/sunset)."""
    return (t + timedelta(hours=lon / 15)).date()


# ---------------------------------------------------------------------------------------------
# Geodesy
# ---------------------------------------------------------------------------------------------
def destination(lat: float, lon: float, azimuth_deg: float, distance_km: float):
    R = 6371.0088
    d = distance_km / R
    p1, l1, a = math.radians(lat), math.radians(lon), math.radians(azimuth_deg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(a))
    l2 = l1 + math.atan2(math.sin(a) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


@contextlib.contextmanager
def single_run(lock_file: Path):
    """Yields True if this process holds the lock (released at exit), False if another process does: one
    fetcher at a time, whether started by its timer or by hand (two would write the same cycle's files)."""
    import fcntl
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
