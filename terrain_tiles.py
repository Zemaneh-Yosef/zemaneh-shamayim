"""Terrain as web-map tiles (/v1/terrain-tiles/{z}/{x}/{y}.png), for the website's 3D horizon view.

The tiles hold the same heights /v1/horizon is computed from (terrain.Tiles.ground_heights: the fine near-range
data where it has heights, else the merged bare-earth layers, else the 1" skadi tiles), so what the 3D view shows
is the ground the horizon was found on. Encoding: Terrarium (the format of the open AWS Terrain Tiles, which
MapLibre's raster-dem source reads): height = R * 256 + G + B / 256 - 32768, in metres; 256 x 256 pixels,
web-Mercator (EPSG:3857) tiles in the usual z/x/y scheme, each pixel the height at its centre.

Only zoom levels MIN_ZOOM..MAX_ZOOM are served. A tile below DOWNLOAD_ZOOM covers several 1-degree tiles, so it is
made only from data already on the server (horizons computed nearby have brought it); where some is missing the
answer is 404 and the website falls back to the AWS tiles there. Each tile is made once and kept in
<data_dir>/terrain-tiles/<terrain signature>/.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

import numpy as np

import terrain

MIN_ZOOM, MAX_ZOOM = 8, 14      # z14: ~10 m pixels at 30 deg latitude, the fine data's resolution
DOWNLOAD_ZOOM = 11              # from here a tile spans at most 2 x 2 one-degree tiles: may download / build them
SIZE = 256
FORMAT_VERSION = 1


def pixel_coords(z: int, x: int, y: int, size: int = SIZE):
    """(lat, lon) arrays (size x size) of the pixel centres of tile z/x/y, rows north to south."""
    n = 2 ** z
    f = (np.arange(size) + 0.5) / size
    lon = (x + f) / n * 360.0 - 180.0
    merc = math.pi * (1 - 2 * (y + f) / n)
    lat = np.degrees(np.arctan(np.sinh(merc)))
    return np.repeat(lat[:, None], size, axis=1), np.repeat(lon[None, :], size, axis=0)


def terrarium(h: np.ndarray) -> np.ndarray:
    """Heights (m) as Terrarium RGB bytes (rows x cols x 3)."""
    v = np.clip(np.nan_to_num(h, nan=0.0) + 32768.0, 0.0, 65535.996)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - np.floor(v)) * 256.0)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def png_rgb(rgb: np.ndarray) -> bytes:
    """An 8-bit RGB PNG (no filtering; zlib does the work)."""
    hgt, wid = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[i].tobytes() for i in range(hgt))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", wid, hgt, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class TerrainTiles:
    """Makes and caches the tiles from a horizons' terrain.Tiles (`tiles`); `local` is the same data without
    downloads or builds (for the low zooms)."""

    def __init__(self, tiles: terrain.Tiles, local: terrain.Tiles, folder: Path, signature: str):
        self.tiles, self.local = tiles, local
        self.dir = Path(folder) / f"v{FORMAT_VERSION}-{signature}"

    def path(self, z: int, x: int, y: int) -> Path:
        return self.dir / str(z) / str(x) / f"{y}.png"

    def get(self, z: int, x: int, y: int) -> bytes:
        """The PNG. ValueError: not a tile served here; LookupError: not all its data is on the server."""
        if not (MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError(f"terrain tiles are served for zoom {MIN_ZOOM}..{MAX_ZOOM}")
        p = self.path(z, x, y)
        if p.exists():
            return p.read_bytes()
        lat, lon = pixel_coords(z, x, y)
        if lat.max() > 60.0 or lat.min() < -60.0:
            raise LookupError("terrain data covers 60S..60N")
        src = self.tiles if z >= DOWNLOAD_ZOOM else self.local
        try:
            h = src.ground_heights(lat, lon)
        except FileNotFoundError as e:
            raise LookupError(f"not on the server yet ({e})") from None
        body = png_rgb(terrarium(h))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(body)
        tmp.replace(p)
        return body
