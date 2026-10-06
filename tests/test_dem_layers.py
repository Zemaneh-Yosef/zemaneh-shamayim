"""Merged bare-earth tiles (dem_layers.py), without network: synthetic sources with known heights.

    python3 tests/test_dem_layers.py          (needs rasterio)

Sources, best first, over tile N31E035:
  national  EPSG:2039 (Israeli TM grid), covers lon < 35.5 only (nodata east of a "border")
  fabdem    EPSG:4326 1x1 degree tile (pixel-is-area like FABDEM), nodata (sea) north of 31.9
  skadi     .hgt surface model: everything +40 m, plus a 30 m "building block"
Heights are linear in lat/lon, so bilinear sampling must reproduce them almost exactly; this checks both
the priority rule and the georeferencing (including the reprojection from EPSG:2039).
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import dem_layers  # noqa: E402
import terrain  # noqa: E402

LAT_I, LON_I = 31, 35
BORDER_LON = 35.5
SEA_LAT = 31.9


def national_h(lat, lon):
    return 100 + 200 * (lat - LAT_I) + 50 * (lon - LON_I)


def fabdem_h(lat, lon):
    return 300 + 100 * (lat - LAT_I) - 80 * (lon - LON_I)


def skadi_h(lat, lon):
    h = 500 + 40 + 0 * lat
    building = (abs(lat - 31.95) < 0.01) & (abs(lon - 35.8) < 0.01)
    return h + 30 * building


def write_sources(tmp: Path):
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin
    from pyproj import Transformer

    # skadi .hgt: 3601 x 3601, row 0 = north edge, big-endian int16
    n = 3601
    lat = LAT_I + 1 - np.arange(n)[:, None] / 3600
    lon = LON_I + np.arange(n)[None, :] / 3600
    tiles = tmp / "tiles"
    tiles.mkdir()
    a = np.round(skadi_h(lat, lon)).astype(">i2")
    (tiles / "N31E035.hgt.gz").write_bytes(gzip.compress(a.tobytes()))

    # FABDEM-like: 3600 x 3600 pixel-is-area, EPSG:4326, nodata -9999 over the sea strip
    m = 3600
    lat_c = LAT_I + 1 - (np.arange(m)[:, None] + 0.5) / 3600
    lon_c = LON_I + (np.arange(m)[None, :] + 0.5) / 3600
    f = fabdem_h(lat_c, lon_c).astype(np.float32)
    f[np.broadcast_to(lat_c > SEA_LAT, f.shape)] = -9999
    fab = tmp / "fabdem"
    fab.mkdir()
    with rasterio.open(fab / "N31E035_FABDEM_V1-2.tif", "w", driver="GTiff", width=m, height=m, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999,
                       transform=from_origin(LON_I, LAT_I + 1, 1 / 3600, 1 / 3600)) as ds:
        ds.write(f, 1)

    # national DTM in EPSG:2039, 25 m cells, covering the tile; nodata east of BORDER_LON
    to_itm = Transformer.from_crs("EPSG:4326", "EPSG:2039", always_xy=True)
    to_ll = Transformer.from_crs("EPSG:2039", "EPSG:4326", always_xy=True)
    xs, ys = to_itm.transform([LON_I - 0.02, LON_I + 1.02, LON_I - 0.02, LON_I + 1.02],
                              [LAT_I - 0.02, LAT_I - 0.02, LAT_I + 1.02, LAT_I + 1.02])
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    res = 25.0
    w, h = int((x1 - x0) / res), int((y1 - y0) / res)
    gx = x0 + (np.arange(w) + 0.5) * res
    gy = y1 - (np.arange(h) + 0.5) * res
    GX, GY = np.meshgrid(gx, gy)
    LON, LAT = to_ll.transform(GX, GY)
    nat = national_h(LAT, LON).astype(np.float32)
    nat[LON >= BORDER_LON] = -32767
    with rasterio.open(tmp / "israel_dtm.tif", "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs=CRS.from_epsg(2039), nodata=-32767, transform=from_origin(x0, y1, res, res)) as ds:
        ds.write(nat, 1)
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
        layers = {"dir": str(tmp / "layers"), "auto_build": True, "sources": [
            {"name": "israel-dtm", "paths": [str(tmp / "israel_dtm.tif")]},
            {"name": "fabdem", "paths": [str(tmp / "fabdem" / "{name}_FABDEM_V1-2.tif")]},   # per-tile template
        ]}
        cfg = {"data_dir": str(tmp), "terrain": {"tiles_dir": str(tiles_dir), "tiles_url": "", "layers": layers,
                                                  "max_km": 150.0}}
        base = terrain.Tiles(tiles_dir, None)
        builder = dem_layers.make_builder(cfg, base)
        tile = builder.build(LAT_I, LON_I)
        info = json.loads(builder.path(LAT_I, LON_I).with_suffix(".json").read_text())
        frac = info["fraction_by_source"]
        print("    composition:", frac)
        check(set(frac) == {"israel-dtm", "fabdem", "skadi (fallback)"}, "all three sources used")
        check(abs(frac["israel-dtm"] - 0.5) < 0.01, f"national DTM fills its half ({frac['israel-dtm']:.1%})")
        check(abs(frac["skadi (fallback)"] - 0.05) < 0.01, f"skadi only where neither covers ({frac['skadi (fallback)']:.1%})")

        t = terrain.Tiles(tiles_dir, None, layered_dir=dem_layers.layers_dir(cfg), builder=builder)
        rng = np.random.default_rng(1)
        lat = LAT_I + 0.02 + rng.random(400) * 0.96
        lon = LON_I + 0.02 + rng.random(400) * 0.96
        got = t.heights(lat, lon)
        west = lon < BORDER_LON - 0.002
        east_land = (lon > BORDER_LON + 0.002) & (lat < SEA_LAT - 0.002)
        east_sea = (lon > BORDER_LON + 0.002) & (lat > SEA_LAT + 0.002)
        err_nat = np.abs(got[west] - national_h(lat[west], lon[west])).max()
        err_fab = np.abs(got[east_land] - fabdem_h(lat[east_land], lon[east_land])).max()
        err_sk = np.abs(got[east_sea] - skadi_h(lat[east_sea], lon[east_sea])).max()
        check(err_nat < 0.05, f"west of border = national DTM, reprojected from EPSG:2039 (max error {err_nat:.3f} m)")
        check(err_fab < 0.05, f"east, on land = FABDEM (max error {err_fab:.3f} m)")
        check(err_sk <= 0.5, f"east, at sea = skadi fallback (max error {err_sk:.3f} m)")
        check(t.layered_used == 1, "Tiles used the merged tile")
        b = t.height(31.95, 35.8)
        check(abs(b - fabdem_h(31.95, 35.8)) > 20, "building block only where skadi is the fallback")

        # horizon code runs on merged tiles
        hz = terrain.horizon_for_point(t, 31.3, 35.2, max_km=40, sides=("sunrise",))
        check(len(hz["sunrise"]) > 0 and abs(hz["ground"] - national_h(31.3, 35.2)) < 0.2,
              f"horizon_for_point on merged data (ground {hz['ground']} m)")

        # all-sea tile -> marker, None (like a missing skadi tile; skadi marks known sea with .sea)
        (tiles_dir / "N33E030.sea").write_text("")
        check(builder.build(33, 30) is None and builder.path(33, 30).with_suffix(".sea").exists(),
              "open-sea tile stored as a marker, not as zeros")

        # a source that fails for a reason other than "no file" aborts the build: nothing saved
        (tmp / "fabdem" / "N31E034_FABDEM_V1-2.tif").write_bytes(b"not a tiff")
        (tiles_dir / "N31E034.sea").write_text("")
        try:
            builder.build(31, 34)
            aborted = False
        except dem_layers.SourceError:
            aborted = True
        check(aborted and not builder.path(31, 34).exists(), "unreadable source file -> build aborted, no tile saved")
        t2 = terrain.Tiles(tiles_dir, None, layered_dir=dem_layers.layers_dir(cfg), builder=builder)
        check(t2.tile(31, 34) is None and t2.layered_used == 0, "Tiles then falls back to the plain tiles")

        # signature changes with the source list -> separate folder (old tiles never reused)
        sig1 = dem_layers.signature(layers)
        sig2 = dem_layers.signature({**layers, "sources": layers["sources"][1:]})
        check(sig1 != sig2 and dem_layers.signature({"sources": []}) is None,
              "signature follows the source list; none when layering is off")

    print("all passed" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
