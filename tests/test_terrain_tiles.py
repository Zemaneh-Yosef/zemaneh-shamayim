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
    """The pixels of an unfiltered 8-bit RGB PNG (what png_rgb writes)."""
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    i, idat, w, h = 8, b"", 0, 0
    while i < len(body):
        n = int.from_bytes(body[i:i + 4], "big")
        kind, data = body[i + 4:i + 8], body[i + 8:i + 8 + n]
        if kind == b"IHDR":
            w, h = int.from_bytes(data[:4], "big"), int.from_bytes(data[4:8], "big")
        elif kind == b"IDAT":
            idat += data
        i += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + 3 * w)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:].reshape(h, w, 3)


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
    check(hz.map_tiles.path(z, x, y).exists(), "kept on disk")
    check(hz.map_tiles.get(z, x, y) == body, "served again from disk")
    try:
        hz.map_tiles.get(9, *tile_xy(9, 31.5, 35.99))      # spans N31E035 and N31E036, low zoom: no downloads
        check(False, "should not be made")
    except LookupError:
        check(True, "a low-zoom tile whose data is not all on the server: LookupError (404)")
    for bad in ((7, 0, 0), (15, 0, 0), (12, 4096, 0)):
        try:
            hz.map_tiles.get(*bad)
            check(False, f"{bad} should be refused")
        except ValueError:
            pass
    check(True, "zooms outside 8..14 and coordinates outside the zoom refused")

    print("3. /v1/terrain-tiles")
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = hz
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with urllib.request.urlopen(f"{base}/v1/terrain-tiles/{z}/{x}/{y}.png") as resp:
        check(resp.headers["Content-Type"] == "image/png" and resp.read() == body, "PNG served")
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
