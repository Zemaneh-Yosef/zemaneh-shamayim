"""Official areas (municipalities, neighbourhoods) and which one a point is in.

areas.sqlite is written by build_areas.py: every official unit worldwide - each country's municipalities
(towns, villages, communes...) and, where there are any, the neighbourhoods or districts inside them - each
with its official boundary (simplified to ~25 m), the bounding box of the full boundary and a level
(LEVEL_NEIGHBOURHOOD for sub-city units, lower for settlements and the coarse units around them). Two rules
pick a point's area:

    city    the settlement containing the point (the finest unit below LEVEL_NEIGHBOURHOOD) as one area when
            it is no bigger than Jerusalem (city_radius_km from centre to corner); a bigger one by the
            neighbourhood or district containing the point, or whole where it has none. This is the Chai
            Tables' rule: one visible sunrise for a city of Jerusalem's size ("ein osim agudos agudos"),
            sub-city areas only beyond it.
    finest  the finest unit containing the point: a neighbourhood over its city, a village over the town
            around it (the highest level, then the smallest box)

    idx = AreaIndex(Path('/var/lib/zmanim-sky/areas.sqlite'))
    a = idx.find(40.609283058016736, -73.96828881865329, "city")    # {'id': 'nyc-nta:BK....', 'name': '<neighbourhood>, Brooklyn', 'bbox': [...], ...}

Polygons are stored like neighborhoods.js: a list of polygons, each a list of rings (outer ring first,
then holes), each ring a flat [lon, lat, lon, lat, ...] list. The test is even-odd over all rings.
Standard library only.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import zlib
from collections import OrderedDict
from pathlib import Path

LEVEL_NEIGHBOURHOOD = 30
LEVEL_LOCALITY = 20
LEVEL_COARSE = 10

# Jerusalem, one city for its visible sunrise, is ~16 km north-south and ~15 km east-west (the Chai Tables'
# introduction): 11.2 km from the centre of its box to a corner by the CBS locality outline, 12.8 km by its
# municipal boundary (OpenStreetMap)
CITY_RADIUS_KM = 13.0
RULES = ("city", "finest")

KM_PER_DEG = 111.19
DECODED_KEPT = 4096                  # areas kept decoded in memory (areas.sqlite)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE areas (id TEXT NOT NULL UNIQUE, country TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                    level INTEGER NOT NULL, source TEXT NOT NULL, s REAL NOT NULL, w REAL NOT NULL,
                    n REAL NOT NULL, e REAL NOT NULL, radius_km REAL NOT NULL, polygons BLOB NOT NULL);
CREATE INDEX areas_country ON areas (country);
CREATE VIRTUAL TABLE areas_box USING rtree (id, w, e, s, n);
"""


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


def public(area: dict, outline: bool = False) -> dict:
    """The area as the API returns it: without its polygons, or (outline) with them as stored, so a client can
    tell which points share this area's answers by the same even-odd test (area_contains)."""
    if outline:
        return dict(area)
    return {k: v for k, v in area.items() if k != "polygons"}


def pick(candidates: list[dict], rule: str = "finest", city_radius_km: float = CITY_RADIUS_KM) -> dict | None:
    """The area for a point among the areas containing it, by rule ("city" or "finest", see the top)."""
    if rule not in RULES:
        raise ValueError(f"area rule must be one of {RULES}")
    if not candidates:
        return None

    def rank(a):
        s, w, n, e = a["bbox"]
        return a["level"], -(n - s) * (e - w)

    if rule == "finest":
        return max(candidates, key=rank)
    towns = [a for a in candidates if a["level"] < LEVEL_NEIGHBOURHOOD]
    if towns:
        town = max(towns, key=rank)
        if town["radiusKm"] <= city_radius_km:
            return town
    hoods = [a for a in candidates if a["level"] >= LEVEL_NEIGHBOURHOOD]
    return max(hoods, key=rank) if hoods else max(candidates, key=rank)


def encode_polygons(polys) -> bytes:
    return zlib.compress(json.dumps(polys, separators=(",", ":")).encode(), 6)


def insert_area(con: sqlite3.Connection, a: dict, country: str) -> bool:
    """Add an area (as AreaIndex returns them) to an areas.sqlite; False if its id is already there."""
    s, w, n, e = a["bbox"]
    cur = con.execute("INSERT OR IGNORE INTO areas (id, country, name, kind, level, source, s, w, n, e, radius_km, "
                      "polygons) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (a["id"], country, a["name"], a["kind"], a["level"], a["source"], s, w, n, e, a["radiusKm"],
                       encode_polygons(a["polygons"])))
    if not cur.rowcount:
        return False
    con.execute("INSERT INTO areas_box (id, w, e, s, n) VALUES (?, ?, ?, ?, ?)", (cur.lastrowid, w, e, s, n))
    return True


def write_areas(path: Path, area_list: list[dict], meta: dict | None = None) -> None:
    """A new areas.sqlite holding these areas (each with its "country"): for tests and hand-made files."""
    Path(path).unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        for a in area_list:
            insert_area(con, a, a.get("country", ""))
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                        [(k, json.dumps(v)) for k, v in (meta or {}).items()])
        con.commit()
    finally:
        con.close()


class AreaIndex:
    """areas.sqlite, reread when the file changes (or appears). It stays on disk and is read through its R-tree
    (a connection per lookup, so a rebuilt file can be swapped in at any time), the areas a lookup needs decoded
    once and kept."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.stamp = None
        self.meta: dict = {}
        self.count = 0
        self.decoded: OrderedDict[int, dict] = OrderedDict()

    def refresh(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            with self.lock:
                self.stamp, self.meta, self.count = None, {}, 0
                self.decoded.clear()
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self.stamp:
            return
        with self.lock:
            if stamp == self.stamp:
                return
            con = self.connect()
            try:
                meta = {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM meta")}
                count = con.execute("SELECT count(*) FROM areas").fetchone()[0]
            finally:
                con.close()
            self.meta, self.count, self.stamp = meta, count, stamp
            self.decoded.clear()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)

    def candidates(self, lat: float, lon: float) -> list[dict]:
        """Every area containing the point."""
        self.refresh()
        if self.stamp is None:
            return []
        con = self.connect()
        try:
            rows = con.execute("SELECT id FROM areas_box WHERE w <= ? AND e >= ? AND s <= ? AND n >= ?",
                               (lon, lon, lat, lat)).fetchall()
            found = [self.area(con, rowid) for (rowid,) in rows]
        finally:
            con.close()
        return [a for a in found if a is not None and area_contains(a, lat, lon)]

    def area(self, con: sqlite3.Connection, rowid: int) -> dict | None:
        """One row of areas.sqlite as a dict (decoded once, then kept while the file stays the same)."""
        with self.lock:
            a = self.decoded.get(rowid)
            if a is not None:
                self.decoded.move_to_end(rowid)
                return a
        row = con.execute("SELECT id, name, kind, level, source, country, s, w, n, e, radius_km, polygons "
                          "FROM areas WHERE rowid = ?", (rowid,)).fetchone()
        if row is None:
            return None
        aid, name, kind, level, source, country, s, w, n, e, rad, polys = row
        a = {"id": aid, "name": name, "kind": kind, "level": level, "source": source, "country": country,
             "bbox": [s, w, n, e], "radiusKm": rad, "polygons": json.loads(zlib.decompress(polys))}
        with self.lock:
            self.decoded[rowid] = a
            while len(self.decoded) > DECODED_KEPT:
                self.decoded.popitem(last=False)
        return a

    def find(self, lat: float, lon: float, rule: str = "finest",
             city_radius_km: float = CITY_RADIUS_KM) -> dict | None:
        """The point's area by rule ("city" or "finest", see the top), or None."""
        return pick(self.candidates(lat, lon), rule, city_radius_km)

    def __len__(self) -> int:
        self.refresh()
        return self.count
