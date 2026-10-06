"""Official areas (municipalities, neighbourhoods) and which one a point is in.

areas.json is written by build_areas.py. Each area has an official boundary (simplified to ~25 m), the
bounding box of the full boundary, and a level: when areas nest (a neighbourhood inside a city, a village
inside a town), the point gets the one with the highest level, then the smallest box.

    idx = AreaIndex(Path('/var/lib/zmanim-sky/areas.json'))
    a = idx.find(40.609283058016736, -73.96828881865329)     # {'id': 'nyc-nta:BK....', 'name': '<neighbourhood>, Brooklyn', 'bbox': [...], ...}

Polygons are stored like neighborhoods.js: a list of polygons, each a list of rings (outer ring first,
then holes), each ring a flat [lon, lat, lon, lat, ...] list. The test is even-odd over all rings.
Standard library only.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

LEVEL_NEIGHBOURHOOD = 30
LEVEL_LOCALITY = 20
LEVEL_COARSE = 10

KM_PER_DEG = 111.19
CELL_DEG = 0.25                      # grid index cell size


def half_diagonal_km(bbox) -> float:
    """Centre-to-corner distance of a [south, west, north, east] box, as serve.py measures it."""
    s, w, n, e = bbox
    kx = KM_PER_DEG * math.cos(math.radians((s + n) / 2))
    return math.hypot((n - s) * KM_PER_DEG, (e - w) * kx) / 2


def ring_contains(x: float, y: float, r) -> bool:
    """Even-odd test against one flat ring [lon, lat, lon, lat, ...]."""
    c = False
    j = len(r) - 2
    for i in range(0, len(r), 2):
        xi, yi, xj, yj = r[i], r[i + 1], r[j], r[j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def polygon_contains(x: float, y: float, rings) -> bool:
    """Even-odd over an outer ring and its holes."""
    c = False
    for r in rings:
        if ring_contains(x, y, r):
            c = not c
    return c


def area_contains(area: dict, lat: float, lon: float) -> bool:
    s, w, n, e = area["bbox"]
    if not (s <= lat <= n and w <= lon <= e):
        return False
    return any(polygon_contains(lon, lat, p) for p in area["polygons"])


def public(area: dict) -> dict:
    """The area without its polygons, as the API returns it."""
    return {k: v for k, v in area.items() if k != "polygons"}


class AreaIndex:
    """areas.json in memory, reloaded when the file changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.stamp = None
        self.areas: list[dict] = []
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.meta: dict = {}

    def refresh(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.stamp, self.areas, self.grid, self.meta = None, [], {}, {}
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self.stamp:
            return
        with self.lock:
            if stamp == self.stamp:
                return
            data = json.loads(self.path.read_text())
            areas = data["areas"]
            grid: dict[tuple[int, int], list[int]] = {}
            for i, a in enumerate(areas):
                s, w, n, e = a["bbox"]
                for gy in range(math.floor(s / CELL_DEG), math.floor(n / CELL_DEG) + 1):
                    for gx in range(math.floor(w / CELL_DEG), math.floor(e / CELL_DEG) + 1):
                        grid.setdefault((gy, gx), []).append(i)
            self.areas, self.grid, self.stamp = areas, grid, stamp
            self.meta = {k: v for k, v in data.items() if k not in ("areas", "unresolved")}

    def find(self, lat: float, lon: float) -> dict | None:
        """The finest area containing the point, or None."""
        self.refresh()
        best = None
        for i in self.grid.get((math.floor(lat / CELL_DEG), math.floor(lon / CELL_DEG)), ()):
            a = self.areas[i]
            if not area_contains(a, lat, lon):
                continue
            s, w, n, e = a["bbox"]
            rank = (a["level"], -(n - s) * (e - w))
            if best is None or rank > best[0]:
                best = (rank, a)
        return best[1] if best else None

    def __len__(self) -> int:
        self.refresh()
        return len(self.areas)
