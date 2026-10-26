"""Terrain for the website's 3D horizon view, in Cesium's own format (/v1/cesium-terrain).

Cesium draws terrain from quantized-mesh tiles (https://github.com/CesiumGS/quantized-mesh): triangle meshes on its
geographic tiling (level 0: two tiles, west and east, 180 deg each; each level halves them; rows counted from the south),
with each vertex's surface normal (the "octvertexnormals" extension), which lets Cesium light the ground itself.

Each tile here is a regular 65 x 65 grid of heights over its rectangle, sampled straight from the heights the horizons
are computed from (terrain.Tiles.ground_heights: the fine near-range data where it is on disk, else the merged
bare-earth layers, else the 1" tiles), so what the 3D view shows is the ground the horizon was found on. Where the
server has no data for a tile (open sea it knows as such aside), and for the coarse levels (a level-7 tile spans
1.4 deg), heights come from the AWS Terrain Tiles (Terrarium PNGs, web Mercator), downloaded once and kept; their sea
bed is set to sea level. A detailed tile drawn from AWS's data starts the download of the server's own 1" tile
under it in the background (from DOWNLOAD_LEVEL) and is kept apart (<y>.aws.terrain) only until the server has the
data: each request first tries the server's heights, so the next one after they arrive gets those.

Tiles are kept gzipped in <data_dir>/cesium-terrain/<terrain signature>/; layer.json says which levels exist.

Made when first asked for (a first visit to a place waits ~10 s or more for its tiles), or ahead of time for a place:

    venv/bin/python cesium_terrain.py build --around 34.09 -118.40 --radius-km 20

builds the tiles a view from anywhere within --radius-km of the point asks for (see needed_tiles), fetching the 1"
tiles under the detailed ones as a visit would. Run it as the server's user; the server may be running.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import math
import os
import struct
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import numpy as np

import terrain

log = logging.getLogger("cesium_terrain")

MAX_LEVEL = 13          # ~38 m between heights (65 per tile): about the 1" data's own spacing
OWN_DATA_LEVEL = 8      # from here a tile (0.7 deg) is made from the server's data where it has it
DOWNLOAD_LEVEL = 10     # from here (0.18 deg) a missing 1" tile is fetched in the background
N = 65
FORMAT_VERSION = 1
AWS_TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

# WGS84, as Cesium's Ellipsoid.WGS84
A, B = 6378137.0, 6356752.3142451793
E2 = 1 - (B / A) ** 2


def tile_rect(level: int, x: int, y: int) -> tuple[float, float, float, float]:
    """west, south, east, north (deg) of a tile; y counted from the south (TMS)."""
    size = 180.0 / 2 ** level
    return -180 + x * size, -90 + y * size, -180 + (x + 1) * size, -90 + (y + 1) * size


def ecef(lat: np.ndarray, lon: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Earth-centred coordinates (m) of geodetic points (deg, m above the ellipsoid), shape (..., 3)."""
    p, l = np.radians(lat), np.radians(lon)
    n = A / np.sqrt(1 - E2 * np.sin(p) ** 2)
    return np.stack([(n + h) * np.cos(p) * np.cos(l), (n + h) * np.cos(p) * np.sin(l),
                     (n * (1 - E2) + h) * np.sin(p)], axis=-1)


def zigzag_delta(values: np.ndarray) -> bytes:
    d = np.diff(values.astype(np.int32), prepend=0)
    return ((d << 1) ^ (d >> 31)).astype("<u2").tobytes()


def high_water_mark(indices: np.ndarray) -> np.ndarray:
    out, highest = np.empty_like(indices), 0
    for k, i in enumerate(indices.tolist()):
        out[k] = highest - i
        if i == highest:
            highest += 1
    return out


def oct_encode(normals: np.ndarray) -> bytes:
    """Unit vectors as Cesium's oct-encoding: 2 bytes each (AttributeCompression.octEncode, range 255)."""
    v = normals / np.abs(normals).sum(axis=1, keepdims=True)
    x, y = v[:, 0].copy(), v[:, 1].copy()
    neg = v[:, 2] < 0
    x[neg], y[neg] = ((1 - np.abs(v[neg, 1])) * np.where(v[neg, 0] >= 0, 1, -1),
                      (1 - np.abs(v[neg, 0])) * np.where(v[neg, 1] >= 0, 1, -1))
    q = lambda c: np.round((np.clip(c, -1, 1) * 0.5 + 0.5) * 255).astype(np.uint8)   # noqa: E731
    return np.stack([q(x), q(y)], axis=1).tobytes()


def occlusion_point(points: np.ndarray, center: np.ndarray) -> np.ndarray:
    """The horizon occlusion point in ellipsoid-scaled space (Cesium's EllipsoidalOccluder
    computeHorizonCullingPoint): the tile is behind the horizon whenever this point is."""
    scale = np.array([1 / A, 1 / A, 1 / B])
    sp, sc = points * scale, center * scale
    direction = sc / np.linalg.norm(sc)
    mag = np.linalg.norm(sp, axis=1)
    mag2 = np.maximum(mag * mag, 1.0)
    mag = np.sqrt(mag2)
    pd = sp / mag[:, None]
    cos_a = pd @ direction
    sin_a = np.linalg.norm(np.cross(pd, direction), axis=1)
    cos_b = 1 / mag
    sin_b = np.sqrt(mag2 - 1) * cos_b
    den = cos_a * cos_b - sin_a * sin_b
    m = 1e6 if (den <= 0).any() else float((1 / den).max())   # a tile reaching round the globe: never occluded
    return direction * m


def encode(west: float, south: float, east: float, north: float, h: np.ndarray) -> bytes:
    """A quantized-mesh tile (with vertex normals) of a 65 x 65 grid of heights, rows north to south."""
    lat = np.linspace(north, south, N)[:, None] * np.ones((1, N))
    lon = np.linspace(west, east, N)[None, :] * np.ones((N, 1))
    pts = ecef(lat, lon, h)
    flat = pts.reshape(-1, 3)
    hmin, hmax = float(h.min()), float(h.max())
    center = ecef(np.array((north + south) / 2), np.array((west + east) / 2), np.array((hmin + hmax) / 2))
    bs_center = flat.mean(axis=0)
    radius = float(np.linalg.norm(flat - bs_center, axis=1).max())
    occ = occlusion_point(flat, flat.mean(axis=0))

    # vertices, row by row from the north: u west to east, v south to north, height between hmin and hmax
    jj, ii = np.mgrid[0:N, 0:N]
    u = np.round(ii / (N - 1) * 32767).astype(np.int32).ravel()
    v = np.round((N - 1 - jj) / (N - 1) * 32767).astype(np.int32).ravel()
    span = hmax - hmin
    hq = (np.zeros(N * N, np.int32) if span <= 0
          else np.round((h.ravel() - hmin) / span * 32767).astype(np.int32))

    # two triangles a cell, counter-clockwise seen from above
    k = (jj[:-1, :-1] * N + ii[:-1, :-1]).ravel()
    tri = np.stack([k + N, k + N + 1, k + 1, k + N, k + 1, k], axis=1).reshape(-1)

    # normals: the sum of the adjacent faces' (area-weighted) normals
    a, b, c = flat[tri[0::3]], flat[tri[1::3]], flat[tri[2::3]]
    fn = np.cross(b - a, c - a)
    nrm = np.zeros_like(flat)
    for corner in (0, 1, 2):
        np.add.at(nrm, tri[corner::3], fn)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)

    # vertices renumbered in the order the triangles first use them (high-water-mark coding needs that)
    order = np.unique(tri, return_index=True)[1]
    first = np.unique(tri)[np.argsort(order)]          # old vertex numbers by first use
    new = np.empty(N * N, dtype=np.int64)
    new[first] = np.arange(N * N)
    tri, u, v, hq, nrm = new[tri], u[first], v[first], hq[first], nrm[first]

    out = bytearray()
    out += struct.pack("<3d", *center)
    out += struct.pack("<2f", hmin, hmax)
    out += struct.pack("<4d", *bs_center, radius)
    out += struct.pack("<3d", *occ)
    out += struct.pack("<I", N * N)
    out += zigzag_delta(u) + zigzag_delta(v) + zigzag_delta(hq)
    if len(out) % 2:
        out += b"\0"
    out += struct.pack("<I", len(tri) // 3)
    out += high_water_mark(tri).astype("<u2").tobytes()
    grid = new.reshape(N, N)
    for edge in (grid[:, 0], grid[-1, :], grid[:, -1], grid[0, :]):        # west, south, east, north
        out += struct.pack("<I", len(edge)) + edge.astype("<u2").tobytes()
    normals = oct_encode(nrm)
    out += struct.pack("<BI", 1, len(normals)) + normals                   # extension 1: oct-encoded normals
    return bytes(out)


def layer_json(tiles_url: str, extra_query: str = "") -> dict:
    """What Cesium's CesiumTerrainProvider reads first: the format, the levels, where every tile exists."""
    return {
        "tilejson": "2.1.0", "name": "zmanim-sky-server terrain", "format": "quantized-mesh-1.0",
        "version": "1.0.0", "scheme": "tms", "projection": "EPSG:4326", "bounds": [-180, -90, 180, 90],
        "tiles": [tiles_url + ("?" + extra_query if extra_query else "")], "minzoom": 0, "maxzoom": MAX_LEVEL,
        "extensions": ["octvertexnormals"],
        "available": [[{"startX": 0, "startY": 0, "endX": 2 ** (z + 1) - 1, "endY": 2 ** z - 1}]
                      for z in range(MAX_LEVEL + 1)],
        "attribution": "Terrain: zmanim-sky-server (SRTM, FABDEM, national surveys); elsewhere AWS Terrain Tiles",
    }


def decode_png_rgb(data: bytes) -> np.ndarray:
    """An 8-bit RGB / RGBA PNG's pixels (rows x cols x channels), all five filter types."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, idat, w = 8, b"", 0
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        kind, chunk = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        if kind == b"IHDR":
            w, hgt, depth, ctype = struct.unpack(">IIBB", chunk[:10])
            if depth != 8 or ctype not in (2, 6):
                raise ValueError(f"PNG type {ctype} depth {depth} not supported")
            ch = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += chunk
        i += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(hgt, 1 + w * ch)
    out = np.zeros((hgt, w * ch), dtype=np.int32)
    prev = np.zeros(w * ch, dtype=np.int32)
    for r in range(hgt):
        f, line = raw[r, 0], raw[r, 1:].astype(np.int32)
        if f == 0:
            cur = line
        elif f == 2:
            cur = (line + prev) & 255
        else:                                       # 1, 3, 4 depend on the pixel to the left: per pixel
            cur = np.zeros_like(line)
            for x in range(w * ch):
                left = cur[x - ch] if x >= ch else 0
                up, ul = prev[x], prev[x - ch] if x >= ch else 0
                if f == 1:
                    pred = left
                elif f == 3:
                    pred = (left + up) >> 1
                else:
                    p = left + up - ul
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                    pred = left if pa <= pb and pa <= pc else up if pb <= pc else ul
                cur[x] = (line[x] + pred) & 255
        out[r], prev = cur, cur
    return out.reshape(hgt, w, ch)


class AwsTerrarium:
    """The AWS Terrain Tiles' heights, from PNGs downloaded once into a folder."""

    def __init__(self, folder: Path, user_agent: str, url: str = AWS_TERRARIUM):
        self.folder, self.ua, self.url = Path(folder), user_agent, url
        self.cache: dict[str, np.ndarray] = {}
        self.lock = threading.Lock()

    def tile(self, z: int, x: int, y: int) -> np.ndarray:
        key = f"{z}/{x}/{y}"
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        f = self.folder / str(z) / str(x) / f"{y}.png"
        if not f.exists():
            req = urllib.request.Request(self.url.format(z=z, x=x, y=y), headers={"User-Agent": self.ua})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
            except urllib.error.URLError as e:
                raise RuntimeError(f"AWS terrain tile {key}: {e}") from e
            f.parent.mkdir(parents=True, exist_ok=True)
            f.with_suffix(".part").write_bytes(data)
            f.with_suffix(".part").replace(f)
        px = decode_png_rgb(f.read_bytes()).astype(np.float64)
        h = px[..., 0] * 256 + px[..., 1] + px[..., 2] / 256 - 32768
        h = np.maximum(h, 0.0)                      # the sea bed (bathymetry) drawn as the sea's surface
        with self.lock:
            self.cache[key] = h
            if len(self.cache) > 64:
                self.cache.pop(next(iter(self.cache)))
        return h

    def heights(self, lat: np.ndarray, lon: np.ndarray, level: int) -> np.ndarray:
        """Bilinear heights at the zoom whose pixels are about half a step of a 65-wide grid at this level."""
        z = min(14, level + 1)
        n = 2 ** z
        gx = (lon + 180) / 360 * n * 256 - 0.5
        la = np.radians(np.clip(lat, -85.05, 85.05))
        gy = (1 - np.arcsinh(np.tan(la)) / math.pi) / 2 * n * 256 - 0.5
        x0, y0 = np.floor(gx).astype(int), np.floor(gy).astype(int)
        fx, fy = gx - x0, gy - y0
        out = np.zeros(lat.shape)
        tiles: dict[tuple[int, int], np.ndarray] = {}

        def at(px, py):
            tx, ty = px // 256 % n, np.clip(py // 256, 0, n - 1)
            vals = np.empty(px.shape)
            for key in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
                if key not in tiles:
                    tiles[key] = self.tile(z, *key)
                m = (tx == key[0]) & (ty == key[1])
                vals[m] = tiles[key][np.clip(py[m] - key[1] * 256, 0, 255), (px[m] % 256)]
            return vals

        out = (at(x0, y0) * (1 - fx) * (1 - fy) + at(x0 + 1, y0) * fx * (1 - fy)
               + at(x0, y0 + 1) * (1 - fx) * fy + at(x0 + 1, y0 + 1) * fx * fy)
        return out


class CesiumTerrain:
    """Makes, keeps and serves the tiles (see the module doc).

    tiles: the horizons' terrain.Tiles (downloads allowed); local: the same without downloads or builds."""

    def __init__(self, tiles: terrain.Tiles, local: terrain.Tiles, folder: Path, aws_folder: Path,
                 signature: str, user_agent: str):
        self.tiles, self.local = tiles, local
        self.signature = f"v{FORMAT_VERSION}-{signature}"
        self.dir = Path(folder) / self.signature
        self.aws = AwsTerrarium(aws_folder, user_agent)
        self.fetching: set[tuple[int, int]] = set()
        self.fetch_lock = threading.Lock()

    def path(self, level: int, x: int, y: int) -> Path:
        return self.dir / str(level) / str(x) / f"{y}.terrain"

    def _own_heights(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray | None:
        """The server's heights where it has all the 1" data on disk (no downloads, no builds), else None."""
        try:
            h = self.local.heights(lat, lon)
        except FileNotFoundError:
            return None
        fine = self.tiles.fine
        if fine is not None:
            s, w, n, e = float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())
            if not fine.missing(s, w, n, e):
                f = fine.heights(lat, lon)
                h = np.where(np.isfinite(f), f, h)
        return h

    def _fetch_later(self, lat: np.ndarray, lon: np.ndarray) -> None:
        """Downloads (or builds) the 1" tiles under these points in the background, once."""
        for key in {(int(math.floor(a)), int(math.floor(o))) for a, o in zip(lat.ravel()[::400], lon.ravel()[::400])}:
            with self.fetch_lock:
                if key in self.fetching:
                    continue
                self.fetching.add(key)

            def run(k=key):
                try:
                    self.tiles.tile(*k)
                except Exception as e:                  # noqa: BLE001 (next request retries)
                    log.warning("1\" tile %s for the 3D view: %s", terrain.tile_name(*k), e)
                finally:
                    with self.fetch_lock:
                        self.fetching.discard(k)
            threading.Thread(target=run, daemon=True).start()

    def get(self, level: int, x: int, y: int) -> tuple[bytes, bool]:
        """The tile, gzipped, and whether it is final (kept; else made from AWS's data for now)."""
        if not (0 <= level <= MAX_LEVEL and 0 <= x < 2 ** (level + 1) and 0 <= y < 2 ** level):
            raise ValueError(f"no tile {level}/{x}/{y} (levels 0..{MAX_LEVEL})")
        p = self.path(level, x, y)
        if p.exists():
            return p.read_bytes(), True
        west, south, east, north = tile_rect(level, x, y)
        lat = np.linspace(north, south, N)[:, None] * np.ones((1, N))
        lon = np.linspace(west, east, N)[None, :] * np.ones((N, 1))
        h, final = None, True
        stand_in = p.with_name(f"{y}.aws.terrain")
        if level >= OWN_DATA_LEVEL and abs(north) <= 61 and abs(south) <= 61:
            h = self._own_heights(lat, lon)
            if h is None:
                final = False
                if level >= DOWNLOAD_LEVEL:
                    self._fetch_later(lat, lon)
                if stand_in.exists():           # made from AWS's data before: kept until the server's data is here
                    return stand_in.read_bytes(), False
        if h is None:
            h = self.aws.heights(lat, lon, level)
        body = gzip.compress(encode(west, south, east, north, h), 6)
        out = p if final else stand_in
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(f"{out.name}.{os.getpid()}-{threading.get_ident()}.part")   # the server and a build may race
        part.write_bytes(body)
        part.replace(out)
        if final:
            stand_in.unlink(missing_ok=True)
        return body, final


# How far from the camera Cesium asks for each level. It splits a tile into its four children while the tile's
# geometric error, seen from the camera, exceeds maximumScreenSpaceError (2 px, as the page sets it): the error of a
# level-0 65 x 65 heightmap tile is ~77 km (Cesium's estimate for this tiling), halving each level, and at the
# page's lens (53 deg) on a screen 1600 px high, a level-L tile is split within 77 km * 800 / 2^L of the camera. So
# level-L tiles are wanted within twice that of the previous level: 123,000 km / 2^L (level 13: 15 km; 10: 120 km).
LEVEL_REACH_KM = 77_067 * 800 / 1000 * 2
# Past the horizons' own reach Cesium still asks for what rises above the earth's curve: ~3.57 km * (sqrt(eye m) +
# sqrt(peak m)), ~300 km for 4,000 m peaks seen from 100 m (a Los Angeles view asked for level-8 tiles 284 km out).
CURVE_KM = 300.0


def needed_tiles(lat: float, lon: float, radius_km: float, max_km: float,
                 curve_km: float = CURVE_KM) -> list[tuple[int, int, int]]:
    """(level, x, y) of the tiles a view from anywhere within radius_km of the point asks for: those within each
    level's reach (LEVEL_REACH_KM) of the area, the detailed ones (from OWN_DATA_LEVEL) no further than max_km (what
    the horizons read) or CURVE_KM, the coarse ones no further than 3,000 km, and every one's parents. A narrower lens or a bigger screen asks for finer tiles a
    little further out; those are made when first asked for."""
    out = {(0, 0, 0), (0, 1, 0)}                    # both level-0 tiles: Cesium always loads them
    for level in range(MAX_LEVEL + 1):
        reach = LEVEL_REACH_KM / 2 ** level
        r_km = radius_km + (min(reach, max(max_km, curve_km)) if level >= OWN_DATA_LEVEL
                             else min(reach, max(max_km, 3000.0 if curve_km else 0.0)))
        size = 180 / 2 ** level
        dlat = r_km / 111.32
        dlon = r_km / (111.32 * max(math.cos(math.radians(min(abs(lat) + dlat, 89.9))), 1e-3))
        x0, x1 = int((lon - dlon + 180) // size), int((lon + dlon + 180) // size)
        y0, y1 = max(0, int((lat - dlat + 90) // size)), min(2 ** level - 1, int((lat + dlat + 90) // size))
        for y in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                x = xx % 2 ** (level + 1)
                west, south, east, north = tile_rect(level, x, y)
                # the tile's nearest point to the place (longitudes taken around the place)
                near_lat = min(max(lat, south), north)
                w_, e_ = west + 360 * round((lon - west - size / 2) / 360), east + 360 * round((lon - west - size / 2) / 360)
                near_lon = min(max(lon, w_), e_)
                if km_between(lat, lon, near_lat, near_lon) <= r_km:
                    out.add((level, x, y))
    for level, x, y in list(out):                   # Cesium loads a tile's parents first
        while level > 0:
            level, x, y = level - 1, x // 2, y // 2
            out.add((level, x, y))
    return sorted(out)


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def build(cesium: CesiumTerrain, lat: float, lon: float, radius_km: float, max_km: float,
          curve_km: float = CURVE_KM) -> int:
    """Makes the tiles needed_tiles lists (those not made yet), first fetching the 1" tiles under the detailed ones
    (from DOWNLOAD_LEVEL, as a visit would). Returns how many are not final (the server lacks their data)."""
    todo = [t for t in needed_tiles(lat, lon, radius_km, max_km, curve_km) if not cesium.path(*t).exists()]
    log.info("%d tiles to make around %.4f, %.4f (radius %g km) -> %s", len(todo), lat, lon, radius_km, cesium.dir)

    def one_degree(t):
        w, s_, e, n = tile_rect(*t)
        return int(math.floor((s_ + n) / 2)), int(math.floor((w + e) / 2))
    todo.sort(key=lambda t: (one_degree(t), t))   # tile by tile of the 1" data (the server keeps a few in memory)
    fetched, not_final = set(), 0
    for i, (level, x, y) in enumerate(todo, 1):
        w, s_, e, n = tile_rect(level, x, y)
        if level >= DOWNLOAD_LEVEL and abs(s_) <= 61 and abs(n) <= 61:
            for k in {(int(math.floor(a)), int(math.floor(o))) for a in (s_, n - 1e-9) for o in (w, e - 1e-9)} - fetched:
                fetched.add(k)
                try:
                    cesium.tiles.tile(*k)
                except Exception as err:            # noqa: BLE001 (the tile is made from AWS's data meanwhile)
                    log.warning("1\" tile %s: %s", terrain.tile_name(*k), err)
        if not cesium.get(level, x, y)[1]:
            not_final += 1
        if i % 200 == 0 or i == len(todo):
            log.info("%d / %d", i, len(todo))
    if not_final:
        log.warning("%d tiles made from AWS's data for now (no data of the server's there yet)", not_final)
    return not_final


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="config.json (default: next to this script)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="make the 3D view's tiles around a place ahead of time")
    b.add_argument("--around", nargs=2, type=float, metavar=("LAT", "LON"), required=True)
    b.add_argument("--radius-km", type=float, default=0.0, help="the area's own radius around the point")
    b.add_argument("--km", type=float, default=None, help="how far past the area (default terrain.max_km)")
    b.add_argument("--count", action="store_true", help="only say how many tiles that is")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("rasterio").setLevel(logging.WARNING)
    import serve                                    # noqa: PLC0415 (it imports this module)
    from common import load_config                  # noqa: PLC0415
    cfg = load_config(a.config)
    max_km = a.km if a.km is not None else float(cfg["terrain"]["max_km"])
    if a.count:
        tiles = needed_tiles(*a.around, a.radius_km, max_km)
        print(len(tiles), "tiles:", ", ".join(f"level {lv}: {sum(t[0] == lv for t in tiles)}" for lv in range(MAX_LEVEL + 1)))
        return
    build(serve.Horizons(cfg).cesium, *a.around, a.radius_km, max_km)


if __name__ == "__main__":
    main()
