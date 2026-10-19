"""Fine near-range terrain (terrain.fine, dem_layers.FineTiles), without network: a synthetic 1/3" source
with a narrow ridge the 1" tiles don't have.

    python3 tests/test_fine_terrain.py          (needs rasterio)

Plain tile N31E035: flat 100 m. Fine source (EPSG:4326, 1/3", covering 31.5..31.7 N, 35.2..35.5 E, nodata
elsewhere): flat 100 m, a 15 m wide ridge 60 m high 3 km east of the observer, and a 400 m one 15 km east
- beyond terrain.fine.max_km (10), so it must come from the plain tiles (flat there) and not count.
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import dem_layers  # noqa: E402
import terrain  # noqa: E402

OBS = (31.6, 35.3)
KX = 111195.0 * math.cos(math.radians(OBS[0]))          # m per degree of longitude
NEAR_LON = OBS[1] + 3000 / KX
FAR_LON = OBS[1] + 15000 / KX


def write_sources(tmp: Path):
    import rasterio
    from rasterio.transform import from_origin

    n = 3601
    tiles = tmp / "tiles"
    tiles.mkdir()
    (tiles / "N31E035.hgt").write_bytes(np.full((n, n), 100, dtype=">i2").tobytes())

    step = 1 / 3 / 3600
    south, north, west, east = 31.5, 31.7, 35.2, 35.5
    h, w = int(round((north - south) / step)), int(round((east - west) / step))
    lon_c = west + (np.arange(w) + 0.5) * step
    a = np.full((h, w), 100.0, dtype=np.float32)
    a[:, np.abs(lon_c - NEAR_LON) * KX < 7.5] = 160.0
    a[:, np.abs(lon_c - FAR_LON) * KX < 7.5] = 500.0
    with rasterio.open(tmp / "fine.tif", "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs="EPSG:4326", nodata=-9999, transform=from_origin(west, north, step, step)) as ds:
        ds.write(a, 1)
    return tiles


def main():
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tiles_dir = write_sources(tmp)
        fine_cfg = {"max_km": 10.0, "step_arcsec": 1 / 3, "auto_build": True,
                    "sources": [{"name": "fine", "paths": [str(tmp / "fine.tif")]}]}
        cfg = {"data_dir": str(tmp), "terrain": {"fine": fine_cfg}}
        tiles = terrain.Tiles(tiles_dir, None)

        plain = terrain.horizon_for_point(tiles, *OBS, sides=("sunrise",), max_km=30)
        tiles.fine = dem_layers.make_fine(cfg)
        fine = terrain.horizon_for_point(tiles, *OBS, sides=("sunrise",), max_km=30)
        east = lambda hz: next(q for q in hz["sunrise"] if abs(q["azimuthDeg"] - 90) < 0.05)   # noqa: E731
        p, f = east(plain), east(fine)
        check(p["heightM"] == 100.0, f"plain tiles: no ridge ({p['heightM']} m at {p['distanceKm']} km)")
        check(abs(f["distanceKm"] - 3.0) < 0.02 and f["heightM"] > 150,
              f"fine data: the 15 m wide ridge at {f['distanceKm']} km, {f['heightM']} m")
        check(all(q["distanceKm"] < 10 for q in fine["sunrise"] if abs(q["azimuthDeg"] - 90) < 20),
              "the 400 m ridge 15 km out is beyond max_km: from the plain (flat) tiles")
        ft = tiles.fine
        hh = ft.heights(np.array([31.6, 31.6, 31.8]), np.array([NEAR_LON, 35.25, 35.3]))
        check(hh[0] > 150 and abs(hh[1] - 100) < 1e-3 and np.isnan(hh[2]),
              f"FineTiles.heights: ridge, plain, NaN outside the source ({hh.round(1).tolist()})")
        check(any(p.suffix == ".none" for p in ft.dir.iterdir()), "a chunk with no fine data is a marker file")
        built = sorted(p.name for p in ft.dir.glob("*.npy"))
        check(built and all(p.stat().st_size < 5_000_000 for p in ft.dir.glob("*.npy")),
              f"{len(built)} chunks of 0.1 deg ({built[:3]}...)")
        check(fine["ground"] == 100.0, "observer's ground from the fine data")
        check(dem_layers.fine_signature(fine_cfg) != dem_layers.fine_signature({**fine_cfg, "step_arcsec": 1}),
              "signature follows the grid and sources")
        check(dem_layers.make_fine({"data_dir": str(tmp), "terrain": {"fine": {"sources": []}}}) is None,
              "off without sources")
    print("all passed" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
