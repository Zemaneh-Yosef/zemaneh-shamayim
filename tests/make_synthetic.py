"""Write synthetic GRIB2 files shaped like NOMADS grib-filter GFS output (same parameter codes,
level types and north-to-south scanning) for testing the pipeline without network access.

Field: standard-lapse columns anchored at a 2 m temperature; land west of `coast_lon`, water east of
it with its own surface temperature."""
from __future__ import annotations

import math
from pathlib import Path

import eccodes
import numpy as np

LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500]


def column(t2m_k: float, psfc_pa: float, orog: float):
    """Standard-lapse column from the 2 m temperature: returns {p_mb: (T K, z m)}."""
    T0 = t2m_k + 0.0065 * 2  # at the surface
    out = {}
    for p in LEVELS:
        T = T0 * (p * 100 / psfc_pa) ** (1 / 5.2559)
        out[p] = (T, orog + (T0 - T) / 0.0065)
    return out


def write(path: Path, region: dict, t2m_land: float, t2m_sea: float, skin_sea: float,
          coast_lon: float, orog_land: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lats = np.arange(region["north"], region["south"] - 1e-9, -0.25)        # north -> south, like GFS
    lons = np.arange(region["west"], region["east"] + 1e-9, 0.25)
    LON, LAT = np.meshgrid(lons, lats)
    land = (LON < coast_lon).astype(float)
    t2m = np.where(land > 0, t2m_land, t2m_sea)
    orog = np.where(land > 0, orog_land, 0.0)
    psfc = 101325.0 * (1 - 0.0065 * orog / 288.15) ** 5.2559
    tsfc = np.where(land > 0, t2m_land + 3, skin_sea)
    fields = [
        ((0, 0, 0), "heightAboveGround", 2, t2m),
        ((0, 0, 0), "surface", 0, tsfc),
        ((0, 3, 0), "surface", 0, psfc),
        ((0, 3, 5), "surface", 0, orog),
        ((2, 0, 0), "surface", 0, land),
    ]
    cols = {}
    for p in LEVELS:
        T = np.zeros_like(t2m); Z = np.zeros_like(t2m)
        for idx in np.ndindex(t2m.shape):
            if (t2m[idx], psfc[idx], orog[idx]) not in cols:
                cols[(t2m[idx], psfc[idx], orog[idx])] = column(t2m[idx], psfc[idx], orog[idx])
            T[idx], Z[idx] = cols[(t2m[idx], psfc[idx], orog[idx])][p]
        fields.append(((0, 0, 0), "isobaricInhPa", p, T))
        fields.append(((0, 3, 5), "isobaricInhPa", p, Z))
    with open(path, "wb") as f:
        for (disc, cat, num), tol, lev, vals in fields:
            h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            eccodes.codes_set(h, "discipline", disc)
            eccodes.codes_set(h, "Ni", len(lons)); eccodes.codes_set(h, "Nj", len(lats))
            eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", float(lats[0]))
            eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", float(lats[-1]))
            eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", float(lons[0] % 360))
            eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", float(lons[-1] % 360))
            eccodes.codes_set(h, "iDirectionIncrementInDegrees", 0.25)
            eccodes.codes_set(h, "jDirectionIncrementInDegrees", 0.25)
            eccodes.codes_set(h, "jScansPositively", 0)
            eccodes.codes_set(h, "parameterCategory", cat)
            eccodes.codes_set(h, "parameterNumber", num)
            eccodes.codes_set(h, "typeOfLevel", tol)
            eccodes.codes_set(h, "level", lev)
            eccodes.codes_set(h, "bitsPerValue", 16)
            eccodes.codes_set_values(h, vals.astype(float).ravel())
            eccodes.codes_write(h, f)
            eccodes.codes_release(h)
