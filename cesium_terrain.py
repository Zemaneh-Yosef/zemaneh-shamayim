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
under it in the background and is not kept, so the next request gets the server's heights.

Tiles are kept gzipped in <data_dir>/cesium-terrain/<terrain signature>/; layer.json says which levels exist.
"""
from __future__ import annotations

import gzip
import logging
import math
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
        if level >= OWN_DATA_LEVEL and abs(north) <= 61 and abs(south) <= 61:
            h = self._own_heights(lat, lon)
            if h is None:
                final = False
                if level >= DOWNLOAD_LEVEL:
                    self._fetch_later(lat, lon)
        if h is None:
            h = self.aws.heights(lat, lon, level)
        body = gzip.compress(encode(west, south, east, north, h), 6)
        if final:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.with_suffix(".part").write_bytes(body)
            p.with_suffix(".part").replace(p)
        return body, final
