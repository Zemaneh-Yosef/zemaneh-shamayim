"""Write small NetCDF files laid out like the NCEP/NCAR Reanalysis 1 4x-daily long-term means that
build_prior.py downloads (same file names, variable names, descending latitudes, 0-360 longitudes,
1460 time steps = 365 days x 4), over a limited area so they stay small.

Built-in pattern (so the reduction can be checked):
  near-surface air = 285 + 8 cos(2 pi (doy - 200) / 365) + 5 cos(2 pi (hour - 20) / 24)   [K]
  lapse rate 5 K/km in January ... 7 K/km in July (pressure levels follow hydrostatically)
  land west of -74 deg (200 m terrain), sea east of it.
The pressure-level temperature file is written in degC to exercise unit handling.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import netCDF4
import numpy as np

LATS = np.arange(50.0, 29.9, -2.5)              # descending, like the real files
LONS = np.arange(270.0, 300.1, 2.5)             # 0..360 convention (-90 .. -60)
LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300]
R, G = 287.053, 9.80665


def near_surface(doy, hour):
    return 285 + 8 * math.cos(2 * math.pi * (doy - 200) / 365) + 5 * math.cos(2 * math.pi * (hour - 20) / 24)


def lapse(doy):          # K/m: 5 K/km in mid-January, 7 K/km in mid-July
    return (6.0 - 1.0 * math.cos(2 * math.pi * (doy - 15) / 365)) / 1000


def make(dirpath: Path) -> dict:
    dirpath.mkdir(parents=True, exist_ok=True)
    nt = 1460
    times = np.arange(nt) * 6.0                 # hours since 0001-01-01
    lon180 = (LONS + 180) % 360 - 180
    land = np.tile((lon180 < -74).astype(np.float32), (len(LATS), 1))
    orog = land * 200.0
    psfc = 101325.0 * (1 - 0.0065 * orog / 288.15) ** 5.2559

    def base(path, var, dims, units, extra_dims=()):
        ds = netCDF4.Dataset(path, "w")
        ds.createDimension("time", None if "time" in dims else 1)
        for d, vals in extra_dims:
            ds.createDimension(d, len(vals))
            v = ds.createVariable(d, "f4", (d,)); v[:] = vals
            if d == "level":
                v.units = "millibar"
        ds.createDimension("lat", len(LATS)); ds.createDimension("lon", len(LONS))
        ds.createVariable("lat", "f4", ("lat",))[:] = LATS
        ds.createVariable("lon", "f4", ("lon",))[:] = LONS
        t = ds.createVariable("time", "f8", ("time",))
        t.units, t.calendar = "hours since 1-1-1 00:00:0.0", "standard"
        t[:] = times if "time" in dims else [0.0]
        v = ds.createVariable(var, "f4", dims, fill_value=np.float32(-9.96921e36))
        v.units = units
        return ds, v

    out = {}
    # pressure levels: air (degC) and hgt (m)
    for name, var, units in (("air.4Xday.ltm.1991-2020.nc", "air", "degC"),
                             ("hgt.4Xday.ltm.1991-2020.nc", "hgt", "m")):
        ds, v = base(dirpath / name, var, ("time", "level", "lat", "lon"), units, (("level", LEVELS),))
        for ti in range(nt):
            doy, hour = ti // 4, (ti % 4) * 6
            T0 = near_surface(doy, hour)        # the column is anchored to this (sea-level) temperature
            L = lapse(doy)
            col_T = [T0 * (p / 1013.25) ** (R * L / G) for p in LEVELS]
            col_Z = [T0 / L * (1 - (p / 1013.25) ** (R * L / G)) for p in LEVELS]
            vals = np.array(col_T if var == "air" else col_Z, dtype=np.float32)
            if var == "air":
                vals = vals - 273.15
            v[ti] = np.broadcast_to(vals[:, None, None], (len(LEVELS), len(LATS), len(LONS)))
        ds.close()
        out[var] = dirpath / name
    # near-surface air (K) and surface pressure (Pa)
    ds, v = base(dirpath / "air.sig995.4Xday.ltm.1991-2020.nc", "air", ("time", "lat", "lon"), "degK")
    for ti in range(nt):
        v[ti] = np.full((len(LATS), len(LONS)), near_surface(ti // 4, (ti % 4) * 6) - 0.26 - land * 1.3, np.float32)
    ds.close()
    ds, v = base(dirpath / "pres.sfc.4Xday.ltm.1991-2020.nc", "pres", ("time", "lat", "lon"), "Pascals")
    for ti in range(nt):
        v[ti] = psfc
    ds.close()
    ds, v = base(dirpath / "hgt.sfc.nc", "hgt", ("time", "lat", "lon"), "m")
    v[0] = orog; ds.close()
    ds, v = base(dirpath / "land.nc", "land", ("time", "lat", "lon"), "")
    v[0] = land; ds.close()
    return out
