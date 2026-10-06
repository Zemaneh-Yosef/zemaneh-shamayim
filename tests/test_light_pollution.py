"""light_pollution.py and /v1/light-pollution on synthetic atlas tiles, without network.

Two tiles are written on the atlas grid (rows 4096-8191; columns 24576-28671 and 28672-32767, which
include Israel and their shared edge at 58.93 E), one zstd as in halakhic_calc and one LERC. Every
pixel holds its own tile-local index (row * 4096 + col, exact in float32), so each read can be checked
against the pixel rasterio itself says contains the point.

    python3 tests/test_light_pollution.py          (needs rasterio)
"""
from __future__ import annotations

import gzip
import json
import math
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import Affine  # noqa: E402

import areas  # noqa: E402
import light_pollution as lpm  # noqa: E402
import serve  # noqa: E402
from common import load_config  # noqa: E402

T = lpm.TILE
TILES = [(1, 6), (1, 7)]                              # (tile row, tile col)


def write_tile(path: Path, tr: int, tc: int, compress: str, shift_px: float = 0.0, realistic: bool = False):
    """Each pixel holds its tile-local index; or (realistic) smooth-ish brightness 0..~50 mcd/m^2."""
    r, c = np.mgrid[0:T, 0:T]
    a = (r * T + c).astype(np.float32)
    if realistic:
        rng = np.random.default_rng(2)
        a = (np.exp(3 * np.sin(r / 300.0) * np.cos(c / 200.0)) * rng.lognormal(0, 0.3, (T, T))).astype(np.float32)
    tf = Affine(lpm.PX, 0.0, lpm.LEFT + (tc * T + shift_px) * lpm.PX, 0.0, -lpm.PX, lpm.TOP - tr * T * lpm.PX)
    prof = dict(driver="GTiff", width=T, height=T, count=1, dtype="float32", crs="EPSG:4326", transform=tf,
                tiled=True, blockxsize=256, blockysize=256, compress=compress,
                nodata=float(np.finfo(np.float32).min))
    if compress == "LERC_ZSTD":
        prof["max_z_error"] = 0
    with rasterio.open(path, "w", **prof) as d:
        d.write(a, 1)


def decode(v: float):
    v = int(round(v))
    return v // T, v % T


def check_pixels(tmp: Path):
    """Indices match rasterio's own georeferenced index; reads cross the tile edge correctly."""
    lp = lpm.LightPollution({"data_dir": str(tmp), "light_pollution": {"dir": str(tmp / "orig"), "url": ""}})
    rng = np.random.default_rng(1)
    with rasterio.open(tmp / "orig" / "lp_4096_24576.tif") as ds:
        for _ in range(300):
            lat = round(float(rng.uniform(17.0, 50.9)), 5)       # point() works to 5 decimals
            lon = round(float(rng.uniform(24.81, 58.92)), 5)
            want = tuple(ds.index(lon, lat))
            got = lpm.pixel_of(lat, lon)
            assert (got[0] - 4096, got[1] - 24576) == want, (lat, lon, got, want)
            assert decode(lp.point(lat, lon)["atlasMcdM2"]) == want, (lat, lon)
    # calc_time.py's index math is off by several rows here; ours agrees with the georeferencing
    lat, lon = 31.7767, 35.2345
    old_row = int((-lat + 85) * (17406 / 145))
    assert lpm.pixel_of(lat, lon)[0] - old_row == 5
    # a window across the tiles' shared edge
    a = lp.window(5000, 5002, 24576 + T - 2, 24576 + T + 1)
    assert a.shape == (3, 4)
    for i, r in enumerate(range(5000, 5003)):
        for j, c in enumerate(range(24576 + T - 2, 24576 + T + 2)):
            assert decode(a[i, j]) == (r - 4096, c % T), (r, c, a[i, j])
    print("pixels ok")


def check_area(tmp: Path):
    """Polygon mask = areas.area_contains per pixel centre; stats are percentiles of those pixels."""
    lp = lpm.LightPollution({"data_dir": str(tmp), "light_pollution": {"dir": str(tmp / "orig"), "url": ""}})
    outer = [35.10, 31.70, 35.30, 31.72, 35.25, 31.86, 35.12, 31.80]            # flat lon, lat
    hole = [35.18, 31.76, 35.22, 31.76, 35.22, 31.79, 35.18, 31.79]
    polys = [[outer, hole]]
    bbox = [31.70, 35.10, 31.86, 35.30]
    area = {"bbox": bbox, "polygons": polys}
    r0, r1 = lpm.pixel_of(bbox[2], 35.1)[0], lpm.pixel_of(bbox[0], 35.1)[0]
    c0, c1 = lpm.pixel_of(31.8, bbox[1])[1], lpm.pixel_of(31.8, bbox[3])[1]
    vals = []
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            la, lo = lpm.pixel_centre(r, c)
            if areas.area_contains(area, la, lo):
                vals.append((r - 4096) * T + (c - 24576))
    vals = np.array(vals, dtype=np.float64)
    st = lp.area(bbox, polys, 90)["stats"]
    assert st["pixels"] == len(vals) > 100, (st, len(vals))
    assert abs(st["value"] - np.percentile(vals, 90)) < 1e-6 * vals.max()
    assert abs(st["min"] - vals.min()) < 1e-3 and abs(st["max"] - vals.max()) < 1e-3
    st50 = lp.area(bbox, polys, 50)["stats"]
    assert abs(st50["value"] - np.median(vals)) < 1e-6 * vals.max()
    # an area smaller than a pixel holds no pixel centre
    c = lpm.pixel_centre(5000, 25000)
    tiny = [c[0] + 0.001, c[1] + 0.001, c[0] + 0.003, c[1] + 0.003]
    assert lp.area(tiny)["stats"] is None
    print(f"area ok ({st['pixels']} pixels)")


def check_files(tmp: Path):
    """LERC copies, gaps filled by download under the original's name, offline errors, bad grids."""
    served = tmp / "remote"
    hits = []

    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            hits.append(self.path)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(H, directory=str(served)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/lp_{{row}}_{{col}}.tif"
    cfg = {"data_dir": str(tmp), "light_pollution": {"dir": str(tmp / "lerc"), "file": "lp_{row}_{col}_lerc.tif",
                                                     "url": url}}
    lp = lpm.LightPollution(cfg)
    v1 = lp.point(31.7767, 35.2345)                     # tile (1, 6): a LERC copy is there
    assert not hits and v1["coverage"]
    v2 = lp.point(31.7767, 60.0)                        # tile (1, 7): not in the LERC folder -> download
    assert len(hits) == 1 and (tmp / "lerc" / "lp_4096_28672.tif").exists(), hits
    lp2 = lpm.LightPollution(cfg)                       # a new process finds the downloaded original
    assert lp2.point(31.7767, 60.0) == v2 and len(hits) == 1
    with rasterio.open(tmp / "orig" / "lp_4096_24576.tif") as a, rasterio.open(tmp / "lerc" / "lp_4096_24576_lerc.tif") as b:
        assert np.array_equal(a.read(1), b.read(1))     # lossless LERC: same values
    check_convert(tmp, url, hits)
    # a missing tile with downloads off
    off = lpm.LightPollution({"data_dir": str(tmp), "light_pollution": {"dir": str(tmp / "orig"), "url": ""}})
    try:
        off.point(0.0, 0.0)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as e:
        assert "downloads disabled" in str(e)
    # a missing tile that the server does not have either
    try:
        lp.point(0.0, 0.0)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "HTTP 404" in str(e)
    # a file that is not on the atlas grid
    (tmp / "bad").mkdir()
    write_tile(tmp / "bad" / "lp_4096_24576.tif", 1, 6, "ZSTD", shift_px=3)
    bad = lpm.LightPollution({"data_dir": str(tmp), "light_pollution": {"dir": str(tmp / "bad"), "url": ""}})
    try:
        bad.point(31.7767, 35.2345)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "not tile" in str(e)
    # outside the atlas's latitudes: no artificial light, flagged
    o = off.estimate(-75.0, 0.0)
    assert not o["coverage"] and o["artificialMcdM2"] == 0 and o["totalMagArcsec2"] > 21.9
    srv.shutdown()
    print("files ok")


def check_convert(tmp: Path, url: str, hits: list):
    """light_pollution.convert: downloads (and originals already there) re-compressed, checked, original gone."""
    conv = tmp / "conv"
    conv.mkdir()
    opts = {"compress": "LERC_ZSTD", "max_z_error": 0.01}
    cfg = {"data_dir": str(tmp), "light_pollution": {"dir": str(conv), "file": "lp_{row}_{col}_lerc.tif",
                                                     "url": url, "convert": opts}}
    lp = lpm.LightPollution(cfg)
    n = len(hits)
    with rasterio.open(tmp / "remote" / "lp_8192_0.tif") as s:
        truth = s.read(1)
        want = float(truth[tuple(s.index(-160.0, 0.0))])
    v = lp.point(0.0, -160.0)                           # tile (2, 0): downloaded, then converted
    assert len(hits) == n + 1, hits
    assert abs(v["atlasMcdM2"] - want) <= 0.01 + 1e-4, (v, want)
    assert sorted(f.name for f in conv.iterdir()) == ["lp_8192_0_lerc.tif"], list(conv.iterdir())
    with rasterio.open(conv / "lp_8192_0_lerc.tif") as c:
        assert c.profile["compress"] == "lerc_zstd" and c.transform == rasterio.open(tmp / "remote" / "lp_8192_0.tif").transform
        err = np.abs(c.read(1).astype(np.float64) - truth).max()
        assert 0 < err <= 0.01 + 1e-5, err            # really lossy, and within the bound (+ float32 rounding)
    assert (conv / "lp_8192_0_lerc.tif").stat().st_size < (tmp / "remote" / "lp_8192_0.tif").stat().st_size / 2
    # an original downloaded before convert was set: converted on first use, no download
    import shutil
    shutil.copy(tmp / "remote" / "lp_4096_28672.tif", conv / "lp_4096_28672.tif")
    lp.point(31.7767, 60.0)
    assert len(hits) == n + 1 and not (conv / "lp_4096_28672.tif").exists()
    assert (conv / "lp_4096_28672_lerc.tif").exists()
    # a conversion that fails leaves nothing half-written (and is retried next time)
    bad = lpm.LightPollution({"data_dir": str(tmp), "light_pollution": {
        "dir": str(tmp / "conv-bad"), "file": "lp_{row}_{col}_lerc.tif", "url": url,
        "convert": {**opts, "blockxsize": 100}}})          # GeoTIFF blocks must be multiples of 16
    try:
        bad.point(0.0, -160.0)
        raise AssertionError("expected the conversion to fail")
    except Exception as e:
        assert not isinstance(e, AssertionError), e
    assert list((tmp / "conv-bad").iterdir()) == [], list((tmp / "conv-bad").iterdir())
    print("convert ok")


def check_correction(tmp: Path):
    """Trend by region and year; sky-meter readings override it (nearby, or inside the area)."""
    t = lpm.Trend(None)
    assert t.region(40.7, -74.0) == ("north-america", 0.104) and t.region(48.9, 2.35) == ("europe", 0.065)
    assert t.region(31.8, 35.2) == ("world", 0.096)                      # Israel: worldwide rate
    assert math.isclose(t.factor(40.7, -74.0, 2014, 2024), 1.104 ** 10)
    assert lpm.Trend({}).factor(40.7, -74.0, 2014, 2024) == 1.0            # {} = off
    assert lpm.Trend({"enabled": False}).factor(40.7, -74.0, 2014, 2024) == 1.0
    assert abs(lpm.sqm_to_artificial(22.0)) < 0.005 and abs(lpm.sqm_to_artificial(lpm.mag_arcsec2(5.174)) - 5.0) < 1e-9
    mfile = tmp / "meas.json"
    base = {"data_dir": str(tmp), "light_pollution": {
        "dir": str(tmp / "remote"), "file": "lp_{row}_{col}.tif", "url": "",
        "measurements": {"file": str(mfile), "radius_km": 5}}}
    lp = lpm.LightPollution(base)
    a0 = lp.point(0.0, -160.0)["atlasMcdM2"]
    one = lpm.LightPollution({**base, "light_pollution": {**base["light_pollution"], "sky_average_factor": 1.0}})
    r1 = one.estimate(0.0, -160.0, year=2026)
    assert r1["blpCdM2"] == r1["artificialCdM2"]                         # configurable
    r = lp.estimate(0.0, -160.0, year=2026)                               # no file yet: the trend
    assert r["correction"]["method"] == "trend" and math.isclose(r["artificialMcdM2"], a0 * 1.096 ** 12, rel_tol=1e-4)
    am = lp.point(0.01, -160.01)["atlasMcdM2"]                            # a reading 1.6 km away, 2026
    want_art = 2 * am
    g0 = 1.096 ** (2026.5 - (2026 + 2.5 / 12))                              # March reading -> mid-2026
    mfile.write_text(json.dumps({"measurements": [
        {"lat": 0.01, "lon": -160.01, "date": "2026-03-01", "sqm": lpm.mag_arcsec2(want_art + lpm.NATURAL_MCD_M2)},
        {"lat": 0.5, "lon": -160.0, "date": "2026-03-01", "sqm": 15.0},     # 55 km away: not used
        {"lat": "x", "lon": 1, "date": "2026"}]}))                         # broken: skipped
    r = lp.estimate(0.0, -160.0, year=2026)
    c = r["correction"]
    f = (want_art * g0 + lpm.NATURAL_MCD_M2) / (am + lpm.NATURAL_MCD_M2)
    assert c["method"] == "measured" and c["count"] == 1 and math.isclose(c["factorOnTotal"], f, rel_tol=1e-3), c
    assert math.isclose(r["artificialMcdM2"], (a0 + lpm.NATURAL_MCD_M2) * f - lpm.NATURAL_MCD_M2, rel_tol=1e-3)
    assert r["atlasMcdM2"] == a0
    # an older reading is carried forward by the trend
    import os, time
    time.sleep(0.01)
    mfile.write_text(json.dumps({"measurements": [
        {"lat": 0.01, "lon": -160.01, "date": "2016-07-01", "artificialMcdM2": want_art}]}))
    os.utime(mfile, None)
    c = lp.estimate(0.0, -160.0, year=2026)["correction"]
    g = 1.096 ** (2026.5 - (2016 + 6.5 / 12))
    assert math.isclose(c["factorOnTotal"], (want_art * g + lpm.NATURAL_MCD_M2) / (am + lpm.NATURAL_MCD_M2), rel_tol=1e-3), c
    # a dark-site reading equal to the natural sky leaves a dark atlas pixel dark
    mfile.write_text(json.dumps({"measurements": [{"lat": 0.01, "lon": -160.01, "date": "2026-03-01", "sqm": 22.5}]}))
    r = lp.estimate(0.0, -160.0, year=2026)
    assert r["correction"]["factorOnTotal"] < 1 and r["artificialMcdM2"] < a0
    # inside an area, beyond the radius: used for that area
    mfile.write_text(json.dumps({"measurements": [{"lat": 0.3, "lon": -160.0, "date": "2026-03-01", "sqm": 18.0}]}))
    area = {"id": "t", "bbox": [-0.1, -160.1, 0.4, -159.9],
            "polygons": [[[-160.1, -0.1, -159.9, -0.1, -159.9, 0.4, -160.1, 0.4]]]}
    assert lp.estimate(0.0, -160.0, year=2026)["correction"]["method"] == "trend"
    ra = lp.estimate(0.0, -160.0, year=2026, area=area, area_key=("t", 1))
    assert ra["correction"]["method"] == "measured" and ra["stats"]["pixels"] > 100
    assert ra["stats"]["min"] <= ra["stats"]["median"] <= ra["stats"]["max"]
    print("correction ok")


def check_server(tmp: Path):
    jerusalem = {"id": "test:jlm", "name": "Test quarter", "kind": "neighborhood", "level": 30, "source": "test",
                 "places": [], "bbox": [31.70, 35.10, 31.86, 35.30],
                 "radiusKm": round(areas.half_diagonal_km([31.70, 35.10, 31.86, 35.30]), 2),
                 "polygons": [[[35.10, 31.70, 35.30, 31.72, 35.25, 31.86, 35.12, 31.80]]]}
    big = {**jerusalem, "id": "test:big", "name": "Big", "level": 10, "bbox": [29.0, 33.0, 34.0, 37.0],
           "radiusKm": round(areas.half_diagonal_km([29.0, 33.0, 34.0, 37.0]), 2),
           "polygons": [[[33.0, 29.0, 37.0, 29.0, 37.0, 34.0, 33.0, 34.0]]]}
    (tmp / "areas.json").write_text(json.dumps({"built": "test", "areas": [jerusalem, big]}))
    cfg_path = tmp / "srv.json"
    cfg_path.write_text(json.dumps({"data_dir": str(tmp / "data"), "areas": {"file": str(tmp / "areas.json")},
                                    "regions": [],
                                    "light_pollution": {"dir": str(tmp / "orig"), "file": "lp_{row}_{col}.tif",
                                                        "url": ""}}))
    cfg = load_config(str(cfg_path))
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = None
    serve.Handler.lightpol = lpm.LightPollution(cfg)
    serve.Handler.areas = areas.AreaIndex(tmp / "areas.json")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(u, code=200):
        try:
            r = urllib.request.urlopen(urllib.request.Request(base + u, headers={"Accept-Encoding": "gzip"}))
        except urllib.error.HTTPError as e:
            assert e.code == code, (u, e.code, e.read())
            return None
        assert code == 200, (u, r.status)
        body = r.read()
        assert "max-age" in r.headers["Cache-Control"] or u == "/v1/status"
        return json.loads(gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body)

    p = get("/v1/light-pollution?lat=31.7767&lon=35.2345")
    rc = lpm.pixel_of(31.7767, 35.2345)
    assert p["pixel"]["row"] == rc[0] and "point" not in p and "stats" not in p, p
    assert decode(p["atlasMcdM2"]) == (rc[0] - 4096, rc[1] - 24576)
    # Blp = the sky's average = 2.25 x the zenith (not pi); the atlas carried to this year by the trend
    assert math.isclose(p["blpCdM2"], 2.25 * p["artificialCdM2"]) and p["skyAverageFactor"] == 2.25
    assert "Falchi" in p["source"]
    c = p["correction"]
    assert c["method"] == "trend" and c["region"] == "world" and c["ratePerYear"] == 0.096
    assert math.isclose(p["artificialMcdM2"], p["atlasMcdM2"] * 1.096 ** (c["year"] - 2014), rel_tol=1e-5)
    p14 = get("/v1/light-pollution?lat=31.7767&lon=35.2345&year=2014")
    assert p14["correction"]["factor"] == 1.0 and p14["artificialMcdM2"] == p14["atlasMcdM2"]
    get("/v1/light-pollution?lat=31.7767&lon=35.2345&year=1800", 400)
    a = get("/v1/light-pollution?lat=31.78&lon=35.20&area=auto")
    assert a["area"]["id"] == "test:jlm" and "polygons" not in a["area"], a
    assert a["stats"]["percentile"] == 90 and a["point"]["pixel"] is not None
    assert math.isclose(a["artificialMcdM2"], round(a["stats"]["p90"], 4), rel_tol=1e-6), a
    a50 = get("/v1/light-pollution?lat=31.78&lon=35.20&area=auto&percentile=50")
    assert math.isclose(a50["artificialMcdM2"], a50["stats"]["median"], rel_tol=1e-6)
    n = get("/v1/light-pollution?lat=30.0&lon=34.0&area=auto")          # only in "Big", over the cap
    assert n["area"] is None and "max_radius_km" in n["areaNote"] and n["pixel"], n
    n = get("/v1/light-pollution?lat=40.0&lon=40.0&area=auto")          # no area
    assert n["area"] is None and "no official area" in n["areaNote"], n
    b = get("/v1/light-pollution?bbox=31.75,35.20,31.77,35.23")
    assert b["lat"] == 31.76 and b["stats"]["pixels"] > 0 and b["bbox"] == [31.75, 35.20, 31.77, 35.23], b
    get("/v1/light-pollution?bbox=31.0,35.0,32.0,36.0", 400)             # too large
    get("/v1/light-pollution?bbox=31.8,35.2,31.7,35.3", 400)             # south > north
    get("/v1/light-pollution?lat=91&lon=0", 400)
    get("/v1/light-pollution", 400)
    get("/v1/light-pollution?lat=0&lon=0", 503)                          # tile not available
    s = get("/v1/status")
    assert s["lightPollution"]["tiles"] == 1 and s["lightPollution"]["downloads"] is False, s
    srv.shutdown()
    print("server ok")


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for sub in ("orig", "lerc", "remote"):
            (tmp / sub).mkdir()
        write_tile(tmp / "orig" / "lp_4096_24576.tif", *TILES[0], "ZSTD")
        write_tile(tmp / "lerc" / "lp_4096_24576_lerc.tif", *TILES[0], "LERC_ZSTD")
        write_tile(tmp / "remote" / "lp_4096_28672.tif", *TILES[1], "ZSTD")
        write_tile(tmp / "remote" / "lp_8192_0.tif", 2, 0, "ZSTD", realistic=True)
        (tmp / "orig" / "lp_4096_28672.tif").symlink_to(tmp / "remote" / "lp_4096_28672.tif")
        check_pixels(tmp)
        check_area(tmp)
        (tmp / "orig" / "lp_4096_28672.tif").unlink()
        check_files(tmp)
        check_correction(tmp)
        check_server(tmp)
    print("all light-pollution tests passed")


if __name__ == "__main__":
    main()
