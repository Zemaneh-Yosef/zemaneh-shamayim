"""/v1/cesium-terrain without network: quantized-mesh tiles of a synthetic 1" tile, decoded here independently
(the format: https://github.com/CesiumGS/quantized-mesh) and checked against the known heights.

    python3 tests/test_cesium_terrain.py

Tile N31E035 slopes from 0 m (west edge) to 900 m (east edge), plus 0.3 m per arc-second north. The "AWS" tiles are
local PNGs (no network): a constant 123 m, and -50 m (sea bed) for one, for the coarse levels and as the stand-in.
"""
from __future__ import annotations

import gzip
import json
import math
import struct
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cesium_terrain as ct  # noqa: E402
import serve  # noqa: E402
import terrain_tiles as tt  # noqa: E402
from common import load_config  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def truth(lat, lon):
    return (lon - 35.0) * 900.0 + (lat - 31.0) * 3600 * 0.3


def unzigzag_delta(buf: bytes) -> np.ndarray:
    z = np.frombuffer(buf, dtype="<u2").astype(np.int64)
    return np.cumsum((z >> 1) ^ -(z & 1))


def decode(tile: bytes) -> dict:
    """A quantized-mesh tile, read as Cesium reads it."""
    o = 0
    center = struct.unpack_from("<3d", tile, o); o += 24
    hmin, hmax = struct.unpack_from("<2f", tile, o); o += 8
    o += 32                                         # bounding sphere
    occ = struct.unpack_from("<3d", tile, o); o += 24
    (nv,) = struct.unpack_from("<I", tile, o); o += 4
    u = unzigzag_delta(tile[o:o + 2 * nv]); o += 2 * nv
    v = unzigzag_delta(tile[o:o + 2 * nv]); o += 2 * nv
    h = unzigzag_delta(tile[o:o + 2 * nv]); o += 2 * nv
    if o % 2:
        o += 1
    (nt,) = struct.unpack_from("<I", tile, o); o += 4
    codes = np.frombuffer(tile, dtype="<u2", count=3 * nt, offset=o).astype(np.int64); o += 6 * nt
    idx, highest = np.empty(3 * nt, np.int64), 0
    for k, c in enumerate(codes.tolist()):
        idx[k] = highest - c
        if c == 0:
            highest += 1
    edges = []
    for _ in range(4):
        (n,) = struct.unpack_from("<I", tile, o); o += 4
        edges.append(np.frombuffer(tile, dtype="<u2", count=n, offset=o)); o += 2 * n
    ext = {}
    while o < len(tile):
        eid, length = struct.unpack_from("<BI", tile, o); o += 5
        ext[eid] = tile[o:o + length]; o += length
    return {"center": center, "hmin": hmin, "hmax": hmax, "occ": occ, "u": u, "v": v,
            "h": hmin + h / 32767 * (hmax - hmin), "tri": idx.reshape(-1, 3), "edges": edges, "ext": ext}


def oct_decode(b: bytes) -> np.ndarray:
    e = np.frombuffer(b, dtype=np.uint8).reshape(-1, 2).astype(np.float64) / 255 * 2 - 1
    x, y = e[:, 0], e[:, 1]
    z = 1 - np.abs(x) - np.abs(y)
    neg = z < 0
    x2 = np.where(neg, (1 - np.abs(y)) * np.sign(x + 1e-12), x)
    y2 = np.where(neg, (1 - np.abs(x)) * np.sign(y + 1e-12), y)
    n = np.stack([x2, y2, z], axis=1)
    return n / np.linalg.norm(n, axis=1, keepdims=True)


def write_aws(folder: Path, z: int, x: int, y: int, h: float):
    f = folder / str(z) / str(x) / f"{y}.png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(tt.png_rgb(tt.terrarium(np.full((256, 256), h))))


def run(tmp: Path):
    n = 3601
    tiles = tmp / "tiles"
    tiles.mkdir()
    r, c = np.arange(n)[:, None], np.arange(n)[None, :]
    (tiles / "N31E035.hgt").write_bytes(np.round(truth(32.0 - r / (n - 1), 35.0 + c / (n - 1))).astype(">i2").tobytes())
    data = tmp / "data"
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({"data_dir": str(data), "terrain": {"tiles_dir": str(tiles), "tiles_url": ""}}))
    cfg = load_config(str(cfgp))
    hz = serve.Horizons(cfg)
    cesium = hz.cesium
    aws = data / "aws-terrarium"
    cesium.aws.url = "http://127.0.0.1:9/{z}/{x}/{y}.png"       # never reached: every AWS tile used is on disk

    print("1. a tile from the server's own data (level 12, inside N31E035)")
    level = 12
    size = 180 / 2 ** level
    x, y = int((35.5 + 180) // size), int((31.5 + 90) // size)
    body, final = cesium.get(level, x, y)
    t = decode(gzip.decompress(body))
    west, south, east, north = ct.tile_rect(level, x, y)
    lon = west + t["u"] / 32767 * (east - west)
    lat = south + t["v"] / 32767 * (north - south)
    err = np.abs(t["h"] - truth(lat, lon)).max()
    check(final and len(t["u"]) == 65 * 65 and len(t["tri"]) == 2 * 64 * 64, f"final, 65 x 65 vertices, {len(t['tri'])} triangles")
    check(err < 1.0, f"vertex heights are the 1\" data's (max error {err:.2f} m: its whole metres)")
    check(t["u"].min() == 0 and t["u"].max() == 32767 and t["v"].min() == 0 and t["v"].max() == 32767, "u, v span 0..32767")
    w_e, s_e, e_e, n_e = t["edges"]
    check(np.all(t["u"][w_e] == 0) and np.all(t["v"][s_e] == 0) and np.all(t["u"][e_e] == 32767)
          and np.all(t["v"][n_e] == 32767) and all(len(e) == 65 for e in t["edges"]), "edge vertices: west, south, east, north")
    # triangles counter-clockwise seen from above, and the oct-encoded normals pointing up and uphill-tilted
    pts = ct.ecef(lat, lon, t["h"])
    a, b, cc = pts[t["tri"][:, 0]], pts[t["tri"][:, 1]], pts[t["tri"][:, 2]]
    up = pts / np.linalg.norm(pts, axis=1, keepdims=True)
    check(np.all(np.einsum("ij,ij->i", np.cross(b - a, cc - a), up[t["tri"][:, 0]]) > 0), "every triangle counter-clockwise from above")
    nrm = oct_decode(t["ext"][1])
    lat0, lon0 = math.radians(31.5), math.radians(35.5)
    east_v = np.array([-math.sin(lon0), math.cos(lon0), 0])
    tilt = math.degrees(math.atan(900 / (111320 * math.cos(lat0))))      # the slope eastward (west to east: 900 m a degree)
    k = len(nrm) // 2
    lean = math.degrees(math.asin(-nrm[k] @ east_v))
    check(np.all(np.einsum("ij,ij->i", nrm, up) > 0.99) and abs(lean - tilt) < 0.2,
          f"normals: up, leaning {lean:.2f} deg west on a slope rising {tilt:.2f} deg eastward")
    check(cesium.path(level, x, y).exists() and cesium.get(level, x, y)[0] == body, "kept on disk, served again from there")
    occ = np.array(t["occ"])
    check(np.all(np.isfinite(occ)) and np.linalg.norm(occ) > 1, "horizon occlusion point above the ellipsoid (scaled space)")

    print("2. coarse levels and missing data: AWS's heights")
    for mx_ in (0, 1):                               # level 0 reads Mercator zoom 1 (its east edge, 180 deg, the west tile's)
        write_aws(aws, 1, mx_, 0, 123.0)
        write_aws(aws, 1, mx_, 1, -50.0)             # the southern half: sea bed
    body0, final0 = cesium.get(0, 1, 0)
    t0 = decode(gzip.decompress(body0))
    lat0s = -90 + t0["v"] / 32767 * 180
    check(final0 and np.allclose(t0["h"][lat0s > 1], 123, atol=0.5) and np.allclose(t0["h"][lat0s < -1], 0, atol=0.5),
          "level 0 from AWS: 123 m, and its sea bed at sea level")
    level = 10
    size = 180 / 2 ** level
    x, y = int((36.5 + 180) // size), int((31.5 + 90) // size)   # in N31E036: not on the server, downloads off
    zm = level + 1
    nm = 2 ** zm
    mx = lambda lon: int((lon + 180) / 360 * nm)                     # noqa: E731
    my = lambda lat: int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * nm)   # noqa: E731
    w2, s2, e2, n2 = ct.tile_rect(level, x, y)
    for tx in range(mx(w2) - 1, mx(e2) + 2):          # and the ring around: bilinear reads one pixel over the edge
        for ty in range(my(n2) - 1, my(s2) + 2):
            write_aws(aws, zm, tx, ty, 456.0)
    body1, final1 = cesium.get(level, x, y)
    t1 = decode(gzip.decompress(body1))
    check(not final1 and np.allclose(t1["h"], 456, atol=0.5) and not cesium.path(level, x, y).exists(),
          "a detailed tile without the server's data: AWS's for now, not kept")
    for bad in ((14, 0, 0), (3, 16, 0), (3, 0, 8)):
        try:
            cesium.get(*bad)
            check(False, f"{bad} should be refused")
        except ValueError:
            pass
    check(True, "levels beyond 13 and coordinates outside the level refused")

    print("3. /v1/cesium-terrain")
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = hz
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1/cesium-terrain"
    layer = json.loads(urllib.request.urlopen(base + "/layer.json").read())
    check(layer["format"] == "quantized-mesh-1.0" and layer["scheme"] == "tms" and layer["maxzoom"] == 13
          and "octvertexnormals" in layer["extensions"] and layer["tiles"][0].startswith("{z}/{x}/{y}.terrain?v=")
          and layer["available"][2] == [{"startX": 0, "startY": 0, "endX": 7, "endY": 3}], "layer.json")
    level, size = 12, 180 / 2 ** 12
    x, y = int((35.5 + 180) // size), int((31.5 + 90) // size)
    with urllib.request.urlopen(f"{base}/{level}/{x}/{y}.terrain") as resp:
        check(resp.headers["Content-Type"] == "application/vnd.quantized-mesh" and resp.headers["Content-Encoding"] == "gzip"
              and "max-age=2592000" in resp.headers["Cache-Control"] and resp.read() == body, "a tile, gzipped, kept 30 days")
    try:
        urllib.request.urlopen(f"{base}/20/0/0.terrain")
        check(False, "should be 404")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "no such level: 404")
    srv.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
