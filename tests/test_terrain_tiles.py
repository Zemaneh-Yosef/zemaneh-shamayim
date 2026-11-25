"""/v1/terrain-tiles without network: Terrarium tiles made from a synthetic 1" tile.

    python3 tests/test_terrain_tiles.py

Tile N31E035 slopes from 0 m (west edge) to 900 m (east edge), plus 0.3 m per arc-second north, so every
pixel's height is known exactly; N31E036 is not on the server and downloads are off.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import serve  # noqa: E402
import terrain_tiles as tt  # noqa: E402
from cesium_terrain import decode_png_rgb  # noqa: E402
from common import load_config  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def truth(lat, lon):
    return (lon - 35.0) * 900.0 + (lat - 31.0) * 3600 * 0.3


def tile_xy(z, lat, lon):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def read_png(body: bytes) -> np.ndarray:
    """The pixels of an 8-bit RGB PNG (any filter: the served tiles are Paeth-filtered)."""
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    return decode_png_rgb(body)[..., :3]


def run(tmp: Path):
    n = 3601
    tiles = tmp / "tiles"
    tiles.mkdir()
    r = np.arange(n)[:, None]                                # row 0 = north edge
    c = np.arange(n)[None, :]
    lat, lon = 32.0 - r / (n - 1), 35.0 + c / (n - 1)
    (tiles / "N31E035.hgt").write_bytes(np.round(truth(lat, lon)).astype(">i2").tobytes())
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({"data_dir": str(tmp / "data"), "terrain": {"tiles_dir": str(tiles), "tiles_url": ""}}))
    cfg = load_config(str(cfgp))

    print("1. encoding")
    h = np.array([[-430.25, 0.0, 754.9, 8848.5]])
    check(np.abs(tt.decode_terrarium(tt.terrarium(h)) - h).max() < 1 / 256 + 1e-9, "Terrarium round trip within 1/256 m")
    q = tt.decode_terrarium(tt.terrarium(h, tt.QUANTUM_M))
    check(np.abs(q - h).max() <= tt.QUANTUM_M / 2 + 1e-9 and np.all(q * 4 == np.round(q * 4)),
          "rounded to quarter metres: within 1/8 m, and exactly quarter metres")
    rough = np.cumsum(np.random.default_rng(1).normal(0, 3, (256, 256)), axis=1) + 700.123
    rgb = tt.terrarium(rough, tt.QUANTUM_M)
    small, plain = tt.png_rgb(rgb), tt.png_rgb(rgb, filtered=False)
    check((read_png(small) == rgb).all() and (read_png(plain) == rgb).all(), "Paeth-filtered PNG decodes to the same pixels")
    check(len(small) < len(plain), f"and is smaller ({len(small)} vs {len(plain)} bytes)")
    check(tt.bounds(1, 1, 0)[1:3] == (0.0, tt.bounds(1, 1, 0)[2]) and abs(tt.bounds(1, 1, 0)[0]) < 1e-9, "tile bounds")
    plat, plon = tt.pixel_coords(3, 4, 2)
    check(abs(plon[0, 0] - (0 + 0.5 / 256 * 45)) < 1e-9 and plat[0, 0] > plat[-1, 0], "pixel centres, rows north to south")

    print("2. tiles")
    hz = serve.Horizons(cfg)
    z = 13
    x, y = tile_xy(z, 31.5, 35.5)
    body = hz.map_tiles.get(z, x, y)
    got = tt.decode_terrarium(read_png(body))
    plat, plon = tt.pixel_coords(z, x, y)
    err = np.abs(got - truth(plat, plon)).max()
    check(err < 1.0, f"z{z} tile: heights at the pixel centres (max error {err:.2f} m, the tile's whole metres)")
    check(np.all(got * 4 == np.round(got * 4)), "served heights are quarter metres")
    check(hz.map_tiles.path(z, x, y).exists(), "kept on disk")
    check(hz.map_tiles.get(z, x, y) == body, "served again from disk")
    try:
        hz.map_tiles.get(9, *tile_xy(9, 31.5, 35.99))      # spans N31E035 and N31E036, low zoom: no downloads
        check(False, "should not be made")
    except tt.NotYet:
        check(True, "a low-zoom tile whose data is not all on the server: NotYet (404, pending)")
    for bad in ((7, 0, 0), (15, 0, 0), (12, 4096, 0)):
        try:
            hz.map_tiles.get(*bad)
            check(False, f"{bad} should be refused")
        except ValueError:
            pass
    check(True, "zooms outside 8..14 and coordinates outside the zoom refused")

    # the previous format's tile is re-encoded, not computed again
    zo, xo, yo = 12, *tile_xy(12, 31.3, 35.3)
    old = hz.map_tiles.old_dir / str(zo) / str(xo) / f"{yo}.png"
    old.parent.mkdir(parents=True, exist_ok=True)
    marker = np.full((256, 256), 1234.0)                    # not what the synthetic terrain has there
    old.write_bytes(tt.png_rgb(tt.terrarium(marker), filtered=False))
    got_old = tt.decode_terrarium(read_png(hz.map_tiles.get(zo, xo, yo)))
    check(np.all(got_old == 1234.0) and hz.map_tiles.path(zo, xo, yo).exists(), "a v1 tile is re-encoded and kept")

    # a rebuilt merged tile gives its map tiles a new name (so they are made again)
    lo = tmp / "layers"
    lo.mkdir()
    mt = tt.TerrainTiles(hz.map_tiles.tiles, hz.map_tiles.local, tmp / "data" / "tt2", "sig", lo)
    before = mt.path(z, x, y)
    (lo / "N31E035.rev").write_text("2")
    after = mt.path(z, x, y)
    check(before.name == f"{y}.png" and after.name.startswith(f"{y}.r") and after != before,
          f"a merged tile's revision renames its map tiles ({before.name} -> {after.name})")

    # a tile made while a fine chunk under it failed within the hour is not kept
    class Fine:
        def failed_at(self, k):
            return time.time() - 60

        def heights(self, lat, lon):
            return np.full(np.shape(lat), np.nan)         # the failed chunk: no fine heights
    mt.tiles.fine = Fine()
    try:
        b2, final = mt.get_ex(z, x, y)
    finally:
        mt.tiles.fine = None
    check(not final and not mt.path(z, x, y).exists(), "made while fine data failed: not final, not kept")
    b3, final3 = mt.get_ex(z, x, y)
    check(final3 and mt.path(z, x, y).exists(), "made again once settled: final and kept")

    print("3. /v1/terrain-tiles")
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = hz
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with urllib.request.urlopen(f"{base}/v1/terrain-tiles/{z}/{x}/{y}.png") as resp:
        check(resp.headers["Content-Type"] == "image/png" and resp.read() == body, "PNG served")
    with urllib.request.urlopen(f"{base}/v1/terrain-tiles/{z}/{x}/{y}.png?v=abc_def") as resp:
        check(resp.read() == body, "?v= is accepted (and ignored)")
    for path, pending in ((f"/v1/terrain-tiles/9/{tile_xy(9, 31.5, 35.99)[0]}/{tile_xy(9, 31.5, 35.99)[1]}.png", True),
                          ("/v1/terrain-tiles/8/10/10.png", False), ("/v1/terrain-tiles/3/1/1.png", False)):
        try:
            urllib.request.urlopen(base + path)
            check(False, f"{path}: should be 404")
        except urllib.error.HTTPError as e:
            check(e.code == 404 and json.loads(e.read())["pending"] is pending, f"{path}: 404, pending {pending}")
    for path, why in ((f"/v1/terrain-tiles/9/{tile_xy(9, 31.5, 35.99)[0]}/{tile_xy(9, 31.5, 35.99)[1]}.png", "missing data"),
                      ("/v1/terrain-tiles/3/1/1.png", "zoom 3"), ("/v1/terrain-tiles/x.png", "bad path")):
        try:
            urllib.request.urlopen(base + path)
            check(False, f"{why}: should be 404")
        except urllib.error.HTTPError as e:
            check(e.code == 404, f"{why}: 404")
    srv.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
