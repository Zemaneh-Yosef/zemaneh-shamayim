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
    stand_in = cesium.path(level, x, y).with_name(f"{y}.aws.terrain")
    check(not final1 and np.allclose(t1["h"], 456, atol=0.5) and not cesium.path(level, x, y).exists()
          and stand_in.exists(), "a detailed tile without the server's data: AWS's for now, kept apart")
    check(cesium.get(level, x, y) == (body1, False), "asked again: the one kept apart")
    (tiles / "N31E036.hgt").write_bytes(np.full((n, n), 777, ">i2").tobytes())        # the server's data arrives
    body1b, final1b = cesium.get(level, x, y)
    check(final1b and np.allclose(decode(gzip.decompress(body1b))["h"], 777, atol=0.5) and not stand_in.exists(),
          "once the server has the data: its heights, kept, and AWS's dropped")
    (tiles / "N31E036.hgt").unlink()
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

    print("4. ahead of time (cesium_terrain.py build)")
    tiles4 = ct.needed_tiles(31.5, 35.5, 0, 5, curve_km=0)        # kept small here: 5 km, nothing past it
    levels = {lv for lv, _, _ in tiles4}
    check(levels == set(range(14)), f"{len(tiles4)} tiles, one level 0 to 13 each at least")
    full = ct.needed_tiles(31.5, 35.5, 0, 150)
    far = [t for t in full if t[0] == 13]
    check(far and all(ct.km_between(31.5, 35.5, (ct.tile_rect(*t)[1] + ct.tile_rect(*t)[3]) / 2,
                                    (ct.tile_rect(*t)[0] + ct.tile_rect(*t)[2]) / 2) < 15 + 2 for t in far),
          "level 13 only within its reach (15 km)")
    nine = [ct.km_between(31.5, 35.5, (ct.tile_rect(*t)[1] + ct.tile_rect(*t)[3]) / 2, (ct.tile_rect(*t)[0] + ct.tile_rect(*t)[2]) / 2)
            for t in full if t[0] == 9]
    check(250 < max(nine) < ct.CURVE_KM + 40, f"level 9 out past the horizons' 150 km, to the earth's curve ({max(nine):.0f} km)")
    have = set(full)
    check({(0, 0, 0), (0, 1, 0)} <= have and all((lv - 1, x // 2, y // 2) in have for lv, x, y in full if lv),
          "both level-0 tiles, and every tile's parent")
    def inside(t):                                   # wholly on N31E035, the server's only data
        w_, s_, e_, n_ = ct.tile_rect(*t)
        return t[0] >= ct.OWN_DATA_LEVEL and 35 <= w_ and e_ <= 36 and 31 <= s_ and n_ <= 32
    for lv, x, y in tiles4:                          # AWS's tiles for the rest, as section 2's
        if not inside((lv, x, y)):
            w4, s4, e4, n4 = ct.tile_rect(lv, x, y)
            zm, nm = lv + 1, 2 ** (lv + 1)
            for tx in range(max(0, mx_of(w4, nm) - 1), min(nm - 1, mx_of(e4, nm) + 1) + 1):
                for ty in range(max(0, my_of(n4, nm) - 1), min(nm - 1, my_of(s4, nm) + 1) + 1):
                    if not (aws / str(zm) / str(tx) / f"{ty}.png").exists():
                        write_aws(aws, zm, tx, ty, 123.0)
    not_final = ct.build(cesium, 31.5, 35.5, 0, 5, curve_km=0)
    partly_off = [t for t in tiles4 if t[0] >= ct.OWN_DATA_LEVEL and not inside(t)]
    check(not_final == len(partly_off) and all(cesium.path(*t).exists() for t in tiles4 if inside(t) or t[0] < 8)
          and all(cesium.path(*t).with_name(f"{t[2]}.aws.terrain").exists() for t in partly_off),
          f"made and kept; the {len(partly_off)} detailed ones partly off the server's data kept apart (AWS's for now)")
    t13 = next(t for t in tiles4 if t[0] == 13 and inside(t))
    t4 = decode(gzip.decompress(cesium.path(*t13).read_bytes()))
    w4, s4, e4, n4 = ct.tile_rect(*t13)
    err4 = np.abs(t4["h"] - truth(s4 + t4["v"] / 32767 * (n4 - s4), w4 + t4["u"] / 32767 * (e4 - w4))).max()
    check(err4 < 1.0, f"a level-13 one has the server's heights (max error {err4:.2f} m)")
    cesium.aws.url = "http://127.0.0.1:9/{z}/{x}/{y}.png"       # (unchanged: AWS is never asked again)
    check(ct.build(cesium, 31.5, 35.5, 0, 5, curve_km=0) == len(partly_off), "run again: those served as kept, nothing remade")
    print("OK")


def mx_of(lon, n):
    return int((lon + 180) / 360 * n)


def my_of(lat, n):
    lat = max(-85.05, min(85.05, lat))
    return int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
