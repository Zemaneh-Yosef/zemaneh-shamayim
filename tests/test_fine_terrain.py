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

        # per-place version: what the fine data adds to a horizon there (serve.Horizons.fine_version)
        import time
        from types import SimpleNamespace
        import serve
        check(ft.version(31.58, 35.28, 31.62, 35.32) == ft.sig, "version: the signature where it has data")
        check(ft.version(31.85, 35.85, 31.9, 35.9) is None, "version: None where no source covers")
        hz = SimpleNamespace(fine={"sig": ft.sig, "km": ft.max_km}, tiles=tiles)
        pt = {"lat": 31.6, "lon": 35.3, "bbox": None, "radius_km": 0.0}
        check(serve.Horizons.fine_version(hz, pt) == ft.sig, "a point inside the source's coverage")
        check(serve.Horizons.fine_version(hz, {**pt, "lat": 31.85, "lon": 35.9}) is None,
              "a point 15+ km from it: no fine data within max_km")
        check(serve.Horizons.fine_version(hz, {**pt, "lat": 31.78, "lon": 35.3}) == ft.sig,
              "a point 9 km north of its edge: within max_km, so it counts")
        check(serve.Horizons.fine_version(SimpleNamespace(fine=None, tiles=tiles), pt) is None, "fine terrain off")
        k = (math.floor(31.45 / 0.1), math.floor(35.05 / 0.1))   # never built so far
        ft.failed[k] = time.time()
        check(ft.chunk(k) is None and k not in ft.cache, "a failed chunk is not kept in memory")
        check(ft.version(31.42, 35.02, 31.47, 35.07) == ft.sig + "-partial", "version: '-partial' while a chunk failed")

        # Horizons.compute: the fine version in the answer, and an uncovered place's cache key as with it off
        import gzip
        import json
        from common import load_config
        cfg_path = tmp / "config.json"
        terr = {"tiles_dir": str(tiles_dir), "tiles_url": "", "max_km": 25.0}
        cfg_path.write_text(json.dumps({"data_dir": str(tmp / "h-on"), "terrain": {**terr, "fine": fine_cfg}}))
        on = serve.Horizons(load_config(str(cfg_path)))
        cfg_path.write_text(json.dumps({"data_dir": str(tmp / "h-off"), "terrain": terr}))
        off = serve.Horizons(load_config(str(cfg_path)))
        q_in, q_out = {"lat": ["31.6"], "lon": ["35.3"]}, {"lat": ["31.4"], "lon": ["35.68"]}
        r_in = json.loads(gzip.decompress(on.get(q_in)))
        r_out = json.loads(gzip.decompress(on.get(q_out)))
        check(r_in.get("terrain", {}).get("fine", {}).get("version") == on.fine["sig"],
              "covered place: terrain.fine.version in the horizon")
        check("fine" not in r_out.get("terrain", {}), "uncovered place: no terrain.fine")
        off.get(q_out)
        check({f.name for f in off.dir.iterdir()} <= {f.name for f in on.dir.iterdir()},
              "uncovered place: same cache key as with fine terrain off (its stored horizon stays valid)")
        check(on.terrain_version(q_in)["fine"] == on.fine["sig"] and on.terrain_version(q_out)["fine"] is None,
              "terrain_version agrees with the horizons")
        check(dem_layers.make_fine({"data_dir": str(tmp), "terrain": {"fine": {"sources": []}}}) is None,
              "off without sources")
    print("all passed" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
