"""Terrain as web-map tiles (/v1/terrain-tiles/{z}/{x}/{y}.png), for the website's 3D horizon view and the app's
sunrise countdown.

The tiles hold the same heights /v1/horizon is computed from (terrain.Tiles.ground_heights: the fine near-range
data where it has heights, else the merged bare-earth layers, else the 1" skadi tiles), so what the 3D view shows
is the ground the horizon was found on. Encoding: Terrarium (the format of the open AWS Terrain Tiles, which
MapLibre's raster-dem source reads): height = R * 256 + G + B / 256 - 32768, in metres; 256 x 256 pixels,
web-Mercator (EPSG:3857) tiles in the usual z/x/y scheme, each pixel the height at its centre.

Heights are rounded to QUANTUM_M (a quarter metre, far finer than the data's own accuracy, and what the app keeps
them in) and every row is Paeth-filtered: the Terrarium blue byte then takes only four values and the rows
predict each other, and a tile is about a third of the unfiltered, full-precision size (~140 KB -> ~45 KB; flat
z14 tiles ~15 KB). Any PNG decoder reads it as before.

Only zoom levels MIN_ZOOM..MAX_ZOOM are served. A tile below DOWNLOAD_ZOOM covers several 1-degree tiles, so it is
made only from data already on the server (horizons computed nearby have brought it); where some is missing the
answer is 404 with "pending" (NotYet: it will come) and the clients fall back to the AWS tiles there for now.

Each tile is made once and kept in <data_dir>/terrain-tiles/v<FORMAT_VERSION>-<terrain signature>/, under a name
that carries the revisions of the merged 1-degree tiles under it (dem_layers.tile_rev), so a tile rebuilt with
different heights (dem_layers.py build --force) gets new map tiles, as it gets new horizons. A tile made while
a fine chunk under it could not be built (the horizons there are then "-partial" too) is not kept, and is sent
with a short cache, so the map catches up when the chunk does. Tiles of the previous format are re-encoded on
first use rather than computed again.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
import zlib
from pathlib import Path

import numpy as np

import terrain

MIN_ZOOM, MAX_ZOOM = 8, 14      # z14: ~10 m pixels at 30 deg latitude, the fine data's resolution
DOWNLOAD_ZOOM = 11              # from here a tile spans at most 2 x 2 one-degree tiles: may download / build them
SIZE = 256
FORMAT_VERSION = 2              # 1: full precision, unfiltered
QUANTUM_M = 0.25
FINE_RETRY_S = 3600             # dem_layers.FineTiles retries a failed chunk after an hour


class NotYet(LookupError):
    """The tile's data is not all on the server yet: it will be once horizons nearby bring it."""


class NoData(LookupError):
    """The server has no terrain there at all (outside 60S..60N)."""


def pixel_coords(z: int, x: int, y: int, size: int = SIZE):
    """(lat, lon) arrays (size x size) of the pixel centres of tile z/x/y, rows north to south."""
    n = 2 ** z
    f = (np.arange(size) + 0.5) / size
    lon = (x + f) / n * 360.0 - 180.0
    merc = math.pi * (1 - 2 * (y + f) / n)
    lat = np.degrees(np.arctan(np.sinh(merc)))
    return np.repeat(lat[:, None], size, axis=1), np.repeat(lon[None, :], size, axis=0)


def bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """south, west, north, east of tile z/x/y (degrees)."""
    n = 2 ** z
    lat = lambda t: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))  # noqa: E731
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def terrarium(h: np.ndarray, quantum: float | None = None) -> np.ndarray:
    """Heights (m) as Terrarium RGB bytes (rows x cols x 3), rounded to `quantum` metres first if given."""
    v = np.nan_to_num(h, nan=0.0)
    if quantum:
        v = np.round(v / quantum) * quantum
    v = np.clip(v + 32768.0, 0.0, 65535.996)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - np.floor(v)) * 256.0)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def paeth_rows(rgb: np.ndarray) -> bytes:
    """The PNG scanlines of an RGB image, each Paeth-filtered (filter type 4)."""
    hgt, wid = rgb.shape[:2]
    x = rgb.reshape(hgt, wid * 3).astype(np.int16)
    a = np.zeros_like(x)
    a[:, 3:] = x[:, :-3]                       # left
    b = np.zeros_like(x)
    b[1:] = x[:-1]                             # up
    c = np.zeros_like(x)
    c[1:, 3:] = x[:-1, :-3]                    # up-left
    p = a + b - c
    pa, pb, pc = np.abs(p - a), np.abs(p - b), np.abs(p - c)
    pred = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
    out = np.empty((hgt, 1 + wid * 3), dtype=np.uint8)
    out[:, 0] = 4
    out[:, 1:] = ((x - pred) % 256).astype(np.uint8)
    return out.tobytes()


def png_rgb(rgb: np.ndarray, filtered: bool = True, level: int = 9) -> bytes:
    """An 8-bit RGB PNG, its rows Paeth-filtered (or unfiltered: filtered=False)."""
    hgt, wid = rgb.shape[:2]
    raw = paeth_rows(rgb) if filtered else b"".join(b"\x00" + rgb[i].tobytes() for i in range(hgt))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", wid, hgt, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))


def tile_png(h: np.ndarray) -> bytes:
    """A served tile: heights rounded to QUANTUM_M, Terrarium, Paeth-filtered."""
    return png_rgb(terrarium(h, QUANTUM_M))


class TerrainTiles:
    """Makes and caches the tiles from a horizons' terrain.Tiles (`tiles`); `local` is the same data without
    downloads or builds (for the low zooms). `layers_out`: the merged tiles' folder (their revisions)."""

    def __init__(self, tiles: terrain.Tiles, local: terrain.Tiles, folder: Path, signature: str,
                 layers_out: Path | None = None):
        self.tiles, self.local = tiles, local
        self.dir = Path(folder) / f"v{FORMAT_VERSION}-{signature}"
        self.old_dir = Path(folder) / f"v{FORMAT_VERSION - 1}-{signature}"
        self.layers_out = Path(layers_out) if layers_out else None

    def revisions(self, z: int, x: int, y: int) -> str:
        """'' while no merged tile under z/x/y was rebuilt, else a short hash of their revisions."""
        if self.layers_out is None:
            return ""
        import dem_layers
        s, w, n, e = bounds(z, x, y)
        revs = {f"{i},{j}": r for i, j in dem_layers.tiles_in(s, w, n, e)
                if (r := dem_layers.tile_rev(self.layers_out, i, j))}
        return hashlib.sha1(json.dumps(revs, sort_keys=True).encode()).hexdigest()[:8] if revs else ""

    def path(self, z: int, x: int, y: int, rev: str | None = None) -> Path:
        rev = self.revisions(z, x, y) if rev is None else rev
        return self.dir / str(z) / str(x) / (f"{y}.r{rev}.png" if rev else f"{y}.png")

    def fine_unsettled(self, z: int, x: int, y: int) -> bool:
        """Whether a fine chunk under the tile failed to build within the hour (it is then retried)."""
        fine = getattr(self.tiles, "fine", None)
        if fine is None:
            return False
        import dem_layers
        s, w, n, e = bounds(z, x, y)
        f = lambda v: math.floor(v / dem_layers.FINE_CHUNK_DEG + 1e-9)  # noqa: E731
        now = time.time()
        return any(now - fine.failed_at((i, j)) < FINE_RETRY_S
                   for i in range(f(s), f(n) + 1) for j in range(f(w), f(e) + 1))

    def get(self, z: int, x: int, y: int) -> bytes:
        return self.get_ex(z, x, y)[0]

    def get_ex(self, z: int, x: int, y: int) -> tuple[bytes, bool]:
        """The PNG, and whether it is final (False: made while some of its fine data was missing; not kept).
        ValueError: not a tile served here; NoData: no terrain there; NotYet: not all its data is here yet."""
        if not (MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError(f"terrain tiles are served for zoom {MIN_ZOOM}..{MAX_ZOOM}")
        rev = self.revisions(z, x, y)
        p = self.path(z, x, y, rev)
        if p.exists():
            return p.read_bytes(), True
        lat, lon = pixel_coords(z, x, y)
        if lat.max() > 60.0 or lat.min() < -60.0:
            raise NoData("terrain data covers 60S..60N")
        old = self.old_dir / str(z) / str(x) / f"{y}.png"
        if not rev and old.exists():
            # the previous format's tile (same heights; a rebuilt merged tile would have given a revision)
            from cesium_terrain import decode_png_rgb
            try:
                body = tile_png(decode_terrarium(decode_png_rgb(old.read_bytes())[..., :3]))
            except (OSError, ValueError, zlib.error):
                body = None
            if body is not None:
                self._keep(p, body)
                return body, True
        src = self.tiles if z >= DOWNLOAD_ZOOM else self.local
        unsettled = self.fine_unsettled(z, x, y)
        try:
            h = src.ground_heights(lat, lon)
        except FileNotFoundError as e:
            raise NotYet(f"not on the server yet ({e})") from None
        body = tile_png(h)
        # a chunk may have failed while this tile was being made, too
        final = not (unsettled or self.fine_unsettled(z, x, y))
        if final:
            self._keep(p, body)
        return body, final

    @staticmethod
    def _keep(p: Path, body: bytes) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{time.monotonic_ns()}.part")
        tmp.write_bytes(body)
        tmp.replace(p)
