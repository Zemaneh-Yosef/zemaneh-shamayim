#!/usr/bin/env python3
"""Build <data_dir>/areas.json: the official areas covering every place in the Chai Tables list.

    venv/bin/python build_areas.py [--config config.json] [--only USA --only France] [--chai FILE|URL]

Each place in chaiTable.json (a metro-area box, or a single point) becomes the official units it
covers - never a box drawn by hand:
  * a box:   every official unit with at least a quarter of its area inside the box, or whose part inside
             covers 3% of the box (or, failing both, the unit around the box centre); units completely
             covered by finer ones (a city by its neighbourhoods) are dropped
  * a point: the finest official unit containing it
  * nothing: listed as unresolved until areas_overrides.json gives it names, a point or a box

Official units, finest first (serve.py gives a point the finest area containing it):
  USA      NYC Planning Neighborhood Tabulation Areas, LA Times "Mapping L.A." neighbourhoods,
           Census TIGER places (cities, villages, CDPs), then county subdivisions (towns) as fallback
  Israel   the Central Bureau of Statistics' locality outlines (statistical areas 2022), then Ministry of
           Interior municipal jurisdictions and other locality outlines (OpenStreetMap, which
           carries the Ministry's boundaries; one download of Geofabrik's daily Israel and Palestine
           extract, read with pyosmium). For the places listed under "Eretz Yisrael (Neighborhoods)":
           the Central Bureau of Statistics' sub-quarters (statistical areas 2022; a city without
           sub-quarters gets its statistical areas). Regional councils are never used as an area.
  others   the municipal level of geoBoundaries (national statistics / mapping agencies; see PLANS)
A source can be replaced with a local file (e.g. the CBS localities layer) via --sources.

Downloads are cached in <data_dir>/area-sources (delete a file there to fetch it again).
Needs pyshp (Census shapefiles) and osmium (pyosmium, the OpenStreetMap extract).
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import logging
import math
import random
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from areas import LEVEL_COARSE, LEVEL_LOCALITY, LEVEL_NEIGHBOURHOOD, half_diagonal_km
from common import HERE, load_config

log = logging.getLogger("build_areas")

CHAI_URL = "https://raw.githubusercontent.com/Zemaneh-Yosef/royzmanimwebsite/main/_data/chaiTable.json"
GB_URL = ("https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/main/releaseData/gbOpen/"
          "{iso}/ADM{adm}/geoBoundaries-{iso}-ADM{adm}_simplified.geojson")
TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER{year}/{layer_uc}/tl_{year}_{fips}_{layer}.zip"
NYC_NTA_URL = "https://data.cityofnewyork.us/resource/9nt8-h7nd.geojson?$limit=5000"
LA_TIMES_URL = ("https://raw.githubusercontent.com/codeforgermany/click_that_hood/main/"
                "public/data/los-angeles.geojson")
CBS_STAT_URL = ("https://services8.arcgis.com/JcXY3lLZni6BK4El/arcgis/rest/services/"
                "statistical_areas_2022/FeatureServer/0/query")     # Israel CBS statistical areas 2022
CBS_PAGE = 2000                                   # the service's maxRecordCount
# the "Eretz Yisrael (Neighborhoods)" cities: Jerusalem, Haifa, Beit Shemesh, Tiberias, Safed (CBS codes);
# where a city has no sub-quarters (only cities of 40,000+ do), its statistical areas are used instead
CBS_HOOD_CITIES = [3000, 4000, 2610, 6700, 8000]
GEOFABRIK_URL = "https://download.geofabrik.de/asia/israel-and-palestine-latest.osm.pbf"   # daily, ~150 MB

MIN_SHARE = 0.25            # a unit belongs to a place's box if this share of it lies inside the box,
MIN_BOX_SHARE = 0.03        # or if its part inside covers this share of the box (big units at the edge)
POINT_KM = 0.3              # boxes smaller than this (centre to corner) are points
BIG_BOX_KM = 60.0           # chaiTable boxes bigger than this are flagged in the report


def gb(iso, adm, kind):
    return {"type": "geoboundaries", "iso": iso, "adm": adm, "kind": kind}


# Official units per chaiTable country, finest first. "level" defaults per type.
PLANS: dict[str, list[dict]] = {
    "Argentina": [gb("ARG", 2, "department")],          # in Buenos Aires city: the 15 comunas
    "Australia": [gb("AUS", 2, "local government area")],
    "Austria": [gb("AUT", 3, "municipality")],
    "Belgium": [gb("BEL", 4, "municipality")],
    "Brazil": [gb("BRA", 2, "municipality")],
    "Bulgaria": [gb("BGR", 2, "municipality")],
    "Canada": [gb("CAN", 3, "census subdivision")],
    "Chile": [gb("CHL", 3, "commune")],
    "China": [],                                         # Hong Kong: no district layer available
    "Colombia": [gb("COL", 2, "municipality")],
    "Czech Republic": [gb("CZE", 3, "municipality")],
    "Denmark": [gb("DNK", 2, "municipality")],
    "France": [gb("FRA", 5, "commune")],                 # Paris, Lyon, Marseille: arrondissements
    "Germany": [gb("DEU", 3, "district / independent city")],
    "Greece": [gb("GRC", 3, "municipality")],
    "Hungary": [gb("HUN", 3, "municipality")],           # Budapest: districts
    "Italy": [gb("ITA", 4, "comune")],
    "Mexico": [gb("MEX", 2, "municipality")],
    "Netherlands": [gb("NLD", 2, "municipality")],
    "Panama": [gb("PAN", 3, "corregimiento")],
    "Poland": [gb("POL", 3, "gmina")],
    "Romania": [gb("ROU", 2, "municipality")],
    "Russia": [gb("RUS", 2, "district")],
    "South-Africa": [gb("ZAF", 4, "ward")],              # local municipalities are metro-sized
    "Spain": [gb("ESP", 3, "municipality")],
    "Switzerland": [gb("CHE", 3, "municipality")],
    "Turkey": [gb("TUR", 2, "district")],
    "UK and Ireland": [gb("GBR", 3, "local authority district"), gb("IRL", 2, "local electoral area")],
    "Ukraine": [gb("UKR", 3, "hromada / council")],
    "Uruguay": [gb("URY", 2, "municipality")],
    "Venezuela": [gb("VEN", 2, "municipality")],
    "USA": [{"type": "nyc-nta"}, {"type": "la-times"},
            {"type": "tiger", "layer": "place"}, {"type": "tiger", "layer": "cousub"}],
    "Eretz Yisrael (Cities)": [{"type": "cbs-localities"}, {"type": "israel-osm"}],
    "Eretz Yisrael (Neighborhoods)": [{"type": "cbs-subquarters"}, {"type": "cbs-localities"}, {"type": "israel-osm"}],
}


# ---------------------------------------------------------------- geometry
# A polygon is a list of rings (outer first, then holes); a ring a list of (lon, lat) tuples.

@dataclass
class Unit:
    id: str
    name: str
    kind: str
    level: int
    source: str
    polys: list
    names: list = field(default_factory=list)          # alternative names for matching
    _bbox: tuple | None = None

    @property
    def bbox(self):
        if self._bbox is None:
            xs = [x for p in self.polys for x, _ in p[0]]
            ys = [y for p in self.polys for _, y in p[0]]
            self._bbox = (min(ys), min(xs), max(ys), max(xs))
        return self._bbox


def geojson_polys(g) -> list:
    if not g:
        return []
    if g["type"] == "Polygon":
        parts = [g["coordinates"]]
    elif g["type"] == "MultiPolygon":
        parts = g["coordinates"]
    elif g["type"] == "GeometryCollection":
        return [p for sub in g["geometries"] for p in geojson_polys(sub)]
    else:
        return []
    out = []
    for p in parts:
        rings = [[(float(c[0]), float(c[1])) for c in r] for r in p if len(r) >= 4]
        if rings:
            out.append(rings)
    return out


def ring_contains(x, y, r) -> bool:
    c = False
    j = len(r) - 1
    for i in range(len(r)):
        xi, yi = r[i]
        xj, yj = r[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def unit_contains(u: Unit, lat, lon) -> bool:
    s, w, n, e = u.bbox
    if not (s <= lat <= n and w <= lon <= e):
        return False
    for p in u.polys:
        c = False
        for r in p:
            if ring_contains(lon, lat, r):
                c = not c
        if c:
            return True
    return False


def ring_area(r) -> float:
    """Planar area in square degrees (signed)."""
    a = 0.0
    for i in range(len(r) - 1):
        a += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1]
    return a / 2


def unit_area(u: Unit) -> float:
    return sum(abs(ring_area(p[0])) - sum(abs(ring_area(h)) for h in p[1:]) for p in u.polys)


def clip_ring(r, box):
    """Sutherland-Hodgman clip of a ring to (south, west, north, east)."""
    s, w, n, e = box
    edges = [(lambda p: p[0] >= w, lambda a, b: (w, a[1] + (b[1] - a[1]) * (w - a[0]) / (b[0] - a[0]))),
             (lambda p: p[0] <= e, lambda a, b: (e, a[1] + (b[1] - a[1]) * (e - a[0]) / (b[0] - a[0]))),
             (lambda p: p[1] >= s, lambda a, b: (a[0] + (b[0] - a[0]) * (s - a[1]) / (b[1] - a[1]), s)),
             (lambda p: p[1] <= n, lambda a, b: (a[0] + (b[0] - a[0]) * (n - a[1]) / (b[1] - a[1]), n))]
    pts = list(r[:-1]) if r and r[0] == r[-1] else list(r)
    for inside, cut in edges:
        if not pts:
            break
        out = []
        prev = pts[-1]
        for cur in pts:
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
            prev = cur
        pts = out
    return pts + pts[:1] if pts else []


def share_inside(u: Unit, box) -> float:
    total = unit_area(u)
    if total <= 0:
        return 0.0
    s, w, n, e = box
    us, uw, un, ue = u.bbox
    if us >= s and un <= n and uw >= w and ue <= e:
        return 1.0
    inside = 0.0
    for p in u.polys:
        inside += abs(ring_area(clip_ring(p[0], box)))
        inside -= sum(abs(ring_area(clip_ring(h, box))) for h in p[1:])
    return max(0.0, inside / total)


def sample_points(u: Unit, k: int = 24, seed: int = 0):
    """Up to k points inside the unit (for the coverage test)."""
    rnd = random.Random(f"{u.id}/{seed}")
    s, w, n, e = u.bbox
    pts = []
    for _ in range(k * 40):
        lat, lon = rnd.uniform(s, n), rnd.uniform(w, e)
        if unit_contains(u, lat, lon):
            pts.append((lat, lon))
            if len(pts) == k:
                break
    return pts


def simplify_ring(r, tol_m: float):
    """Douglas-Peucker in local metres; keeps the ring closed. None if it collapses."""
    if len(r) <= 4 or tol_m <= 0:
        return r
    lat0 = sum(p[1] for p in r) / len(r)
    kx = 111190.0 * math.cos(math.radians(lat0))
    ky = 111190.0
    pts = [(x * kx, y * ky) for x, y in r]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = (abs(dy * (px - ax) - dx * (py - ay)) / seg) if seg > 0 else math.hypot(px - ax, py - ay)
            if d > best:
                best, bi = d, i
        if best > tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    out = [r[i] for i in range(len(r)) if keep[i]]
    if len(out) < 4:
        # a closed ring whose first and last points coincide: keep the farthest point too
        far = max(range(1, len(r) - 1), key=lambda i: math.hypot(pts[i][0] - pts[0][0], pts[i][1] - pts[0][1]))
        out = sorted({0, far, len(r) - 1} | {i for i in range(len(r)) if keep[i]})
        out = [r[i] for i in out]
        if len(out) < 4:
            return None
    return out


def flat(r):
    return [round(v, 5) for x, y in r for v in (x, y)]


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(village|city|town|borough|cdp|township|municipality|comune|commune)\b", "", s)
    for a, b in (("kiriat", "kiryat"), ("tz", "z"), ("kh", "h"), ("ch", "h"), ("ph", "f"), ("ee", "i"),
                 ("ei", "e"), ("ey", "e"), ("ah ", "a "), ("eh ", "e ")):
        s = s.replace(a, b)
    s = re.sub(r"ah$|eh$", "a", s.strip())
    s = re.sub(r"[^a-z0-9]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


# ---------------------------------------------------------------- downloads

def bar(done: int, total: int, width: int = 20) -> str:
    n = round(width * done / max(total, 1))
    return f"[{'#' * n}{'.' * (width - n)}] {done}/{total}"


def read_with_progress(r, label: str) -> bytes:
    """The response body; big downloads log a progress bar every 10%."""
    total = int(r.headers.get("Content-Length") or 0)
    if total < 20_000_000:
        return r.read()
    chunks, got, shown = [], 0, -1
    while True:
        c = r.read(1 << 20)
        if not c:
            break
        chunks.append(c)
        got += len(c)
        tenth = got * 10 // total
        if tenth != shown:
            shown = tenth
            log.info("   [%s%s] %.0f of %.0f MB", "#" * (2 * tenth), "." * (20 - 2 * tenth), got / 1e6, total / 1e6)
    return b"".join(chunks)


class Fetcher:
    def __init__(self, cache: Path, contact: str):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ua = f"zmanim-sky-server/0.1 build_areas ({contact or 'no contact'})"

    def path(self, url_or_path: str, post: str | None = None, name: str | None = None, check=None) -> Path:
        """Local file for a URL (downloaded once, then cached) or a path. check(body) may raise to
        reject a bad answer: it is retried and never cached."""
        if not re.match(r"^https?://", url_or_path):
            p = Path(url_or_path).expanduser()
            if not p.exists():
                raise FileNotFoundError(p)
            return p
        key = name or re.sub(r"[^A-Za-z0-9._-]+", "_", url_or_path.split("://", 1)[1])[-150:]
        f = self.cache / key
        if f.exists() and f.stat().st_size > 0:
            return f
        log.info("downloading %s", url_or_path)
        data = post.encode() if post is not None else None
        req = urllib.request.Request(url_or_path, data=data, headers={"User-Agent": self.ua})
        t0 = time.time()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    body = read_with_progress(r, url_or_path)
                if check:
                    check(body)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                busy = getattr(e, "code", None) == 429          # too many requests: give it longer
                wait = (60 if busy else 20) * (attempt + 1)
                log.warning("download failed (%s); retry %d/2 in %d s", e, attempt + 1, wait)
                time.sleep(wait)
        log.info("  got %.1f MB in %.0f s", len(body) / 1e6, time.time() - t0)
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(f)
        return f


# ---------------------------------------------------------------- sources

def prop(props: dict, *keys, default=""):
    low = {k.lower(): v for k, v in props.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v)
    return default


def read_geojson(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def read_shapefile(path: Path) -> list[dict]:
    """Features (GeoJSON-like) from a zipped or plain shapefile, in its own lon/lat coordinates."""
    import shapefile                                            # pyshp
    if path.suffix == ".zip":
        z = zipfile.ZipFile(path)
        shp = next(n for n in z.namelist() if n.lower().endswith(".shp"))
        base = shp[:-4]
        names = {n.lower(): n for n in z.namelist()}
        rd = shapefile.Reader(shp=io.BytesIO(z.read(shp)), dbf=io.BytesIO(z.read(names[(base + ".dbf").lower()])),
                              encoding="utf-8", encodingErrors="replace")
    else:
        rd = shapefile.Reader(str(path), encoding="utf-8", encodingErrors="replace")
    fields = [f[0] for f in rd.fields[1:]]
    out = []
    for sr in rd.iterShapeRecords():
        out.append({"properties": dict(zip(fields, sr.record)), "geometry": sr.shape.__geo_interface__})
    return out


def read_features(path: Path) -> list[dict]:
    if path.suffix in (".zip", ".shp"):
        return read_shapefile(path)
    return read_geojson(path)


def fix_text(s: str) -> str:
    """Undo UTF-8 read as Latin-1 ("MontrÃ©al"), which some source files carry."""
    if "Ã" in s or "Å" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def units_from_features(feats, *, key, id_keys, name_keys, kind, level, source, name_suffix="", alt_keys=()):
    out = []
    for i, f in enumerate(feats):
        polys = geojson_polys(f.get("geometry"))
        if not polys:
            continue
        p = f.get("properties") or {}
        nid = prop(p, *id_keys, default=str(i))
        name = fix_text(prop(p, *name_keys, default=nid))
        alts = [prop(p, k) for k in alt_keys if prop(p, k)]
        out.append(Unit(f"{key}:{nid}", name + name_suffix, kind, level, source, polys, [name] + alts))
    return out


class Sources:
    def __init__(self, fetch: Fetcher, cfg: dict):
        self.fetch = fetch
        self.cfg = cfg
        self.cache: dict[str, list[Unit]] = {}

    def units(self, spec: dict, boxes: list) -> list[Unit]:
        """All units of one source; boxes = the places' boxes (used to pick US states)."""
        t = spec["type"]
        key = json.dumps(spec, sort_keys=True)
        if t == "tiger":
            fips = self.us_states(boxes)
            return [u for f in fips for u in self.tiger(spec["layer"], f)]
        if key not in self.cache:
            fn = getattr(self, t.replace("-", "_"))
            self.cache[key] = fn(spec, boxes) if t == "israel-osm" else fn(spec)
            log.info("%s: %d units", spec.get("label") or t, len(self.cache[key]))
        return self.cache[key]

    # geoBoundaries (national statistics / mapping agencies, via the geoBoundaries project)
    def geoboundaries(self, spec):
        iso, adm = spec["iso"], spec["adm"]
        path = self.fetch.path(spec.get("path") or GB_URL.format(iso=iso, adm=adm), name=f"gb-{iso}-ADM{adm}.geojson")
        meta_src = spec.get("source") or f"geoBoundaries {iso} ADM{adm}"
        return units_from_features(read_features(path), key=f"gb-{iso}{adm}", id_keys=("shapeID",),
                                   name_keys=("shapeName",), kind=spec["kind"], level=spec.get("level", LEVEL_LOCALITY),
                                   source=meta_src)

    def nyc_nta(self, spec):
        path = self.fetch.path(spec.get("path") or NYC_NTA_URL, name="nyc-nta2020.geojson")
        feats = read_features(path)
        out = []
        for u in units_from_features(feats, key="nyc-nta", id_keys=("nta2020", "ntacode"),
                                     name_keys=("ntaname",), kind="neighborhood tabulation area",
                                     level=spec.get("level", LEVEL_NEIGHBOURHOOD),
                                     source="NYC Department of City Planning, 2020 Neighborhood Tabulation Areas"):
            out.append(u)
        boro = {f"nyc-nta:{prop(f['properties'], 'nta2020', 'ntacode')}": prop(f["properties"], "boroname")
                for f in feats}
        for u in out:
            if boro.get(u.id):
                u.name = f"{u.name}, {boro[u.id]}"
        return out

    def la_times(self, spec):
        path = self.fetch.path(spec.get("path") or LA_TIMES_URL, name="la-times-neighborhoods.geojson")
        feats = read_features(path)
        for f in feats:
            nm = prop(f["properties"], "name")
            f["properties"]["_slug"] = re.sub(r"[^a-z0-9]+", "-", nm.lower()).strip("-")
        return units_from_features(feats, key="la", id_keys=("_slug",), name_keys=("name",),
                                   kind="neighborhood", level=spec.get("level", LEVEL_NEIGHBOURHOOD),
                                   source='Los Angeles Times "Mapping L.A." neighborhood boundary',
                                   name_suffix=", Los Angeles")

    def file(self, spec):
        """Any official GeoJSON / shapefile: {"type": "file", "path" or "url", "id_field", "name_field", ...}"""
        path = self.fetch.path(spec.get("path") or spec["url"])
        return units_from_features(read_features(path), key=spec.get("key", path.stem),
                                   id_keys=(spec.get("id_field", "id"),), name_keys=(spec.get("name_field", "name"),),
                                   alt_keys=tuple(spec.get("alt_fields", ())), kind=spec.get("kind", "area"),
                                   level=spec.get("level", LEVEL_LOCALITY), source=spec.get("source", str(path.name)))

    # US Census TIGER/Line
    def us_states(self, boxes) -> list[str]:
        year = self.cfg.get("tiger_year", 2024)
        if "us-states" not in self.cache:
            path = self.fetch.path(TIGER_URL.format(year=year, layer_uc="STATE", fips="us", layer="state"))
            self.cache["us-states"] = units_from_features(read_shapefile(path), key="us-state", id_keys=("STATEFP",),
                                                          name_keys=("NAME",), kind="state", level=0, source="")
        fips = []
        for st in self.cache["us-states"]:
            s, w, n, e = st.bbox
            for b in boxes:
                if b[0] <= n and b[2] >= s and b[1] <= e and b[3] >= w:
                    fips.append(st.id.split(":")[1])
                    break
        return sorted(fips)

    def tiger(self, layer, fips):
        key = f"tiger-{layer}-{fips}"
        if key not in self.cache:
            year = self.cfg.get("tiger_year", 2024)
            path = self.fetch.path(TIGER_URL.format(year=year, layer_uc=layer.upper(), fips=fips, layer=layer))
            feats = read_shapefile(path)
            if layer == "place":
                units = []
                for f in feats:
                    p = f["properties"]
                    cdp = str(p.get("CLASSFP", "")).startswith("U")
                    units += units_from_features(
                        [f], key="us-place", id_keys=("GEOID",), name_keys=("NAMELSAD",), alt_keys=("NAME",),
                        kind="census-designated place" if cdp else "incorporated place", level=LEVEL_LOCALITY,
                        source=f"US Census TIGER/Line {year} places")
            else:
                feats = [f for f in feats if str(f["properties"].get("COUSUBFP")) != "00000"]
                units = units_from_features(feats, key="us-cousub", id_keys=("GEOID",), name_keys=("NAMELSAD",),
                                            alt_keys=("NAME",), kind="county subdivision", level=LEVEL_COARSE,
                                            source=f"US Census TIGER/Line {year} county subdivisions")
            st = next((s.name for s in self.cache["us-states"] if s.id == f"us-state:{fips}"), fips)
            for u in units:
                u.name = f"{u.name}, {st}"
            self.cache[key] = units
            log.info("%s: %d units", key, len(units))
        return self.cache[key]

    # Israel: OpenStreetMap, whose admin_level=8 boundaries are the Ministry of Interior's
    # Israel: OpenStreetMap, whose admin_level=8 boundaries are the Ministry of Interior's
    def israel_osm(self, spec, boxes=()):
        if "israel-osm-units" not in self.cache:
            path = self.fetch.path(spec.get("path") or spec.get("url", GEOFABRIK_URL),
                                   name=GEOFABRIK_URL.rsplit("/", 1)[1])
            self.cache["israel-osm-units"] = read_osm_areas(path)
        units = self.cache["israel-osm-units"]
        if not spec.get("neighbourhoods"):             # official CBS sub-quarters are used instead
            units = [u for u in units if u.level < LEVEL_NEIGHBOURHOOD]
        return units

    # Israel: CBS statistical areas 2022 - every locality's official outline, and sub-quarters (tat-rova)
    def cbs_features(self, spec) -> list[dict]:
        if "cbs-features" not in self.cache:
            feats = []
            if spec.get("path"):
                feats = read_features(self.fetch.path(spec["path"]))
            else:
                offset = 0
                while True:
                    q = urllib.parse.urlencode({
                        "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson",
                        "geometryPrecision": 6, "maxAllowableOffset": 0.00002,      # ~2 m
                        "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": CBS_PAGE})
                    path = self.fetch.path(f"{spec.get('url', CBS_STAT_URL)}?{q}",
                                           name=f"cbs-statistical-areas-2022-all-{offset}.geojson",
                                           check=lambda b: json.loads(b)["features"])
                    page = read_geojson(path)
                    feats += page
                    log.info("CBS statistical areas: %d so far", len(feats))
                    if len(page) < CBS_PAGE:
                        break
                    offset += CBS_PAGE
            self.cache["cbs-features"] = feats
        return self.cache["cbs-features"]

    def cbs_subquarters(self, spec):
        return cbs_units(self.cbs_features(spec), spec.get("stat_areas_for", CBS_HOOD_CITIES))

    def cbs_localities(self, spec):
        return cbs_locality_units(self.cbs_features(spec))


def cbs_units(feats: list[dict], stat_areas_for) -> list[Unit]:
    """Sub-quarters (all statistical areas sharing SEMEL_YISHUV and TAT_ROVA, kept as one multipolygon);
    for the listed cities without sub-quarters, each statistical area."""
    src = "Israel Central Bureau of Statistics, statistical areas 2022"
    groups: dict[tuple, dict] = {}
    for f in feats:
        p = f.get("properties") or {}
        polys = geojson_polys(f.get("geometry"))
        if not polys:
            continue
        code = int(p.get("SEMEL_YISHUV") or 0)
        town = fix_text(str(p.get("SHEM_YISHUV_ENGLISH") or "")).strip().title() or str(code)
        town_he = str(p.get("SHEM_YISHUV") or "").strip()
        sub = p.get("TAT_ROVA")
        if sub:
            key = ("q", code, int(sub))
            name = f"{town}, sub-quarter {int(sub)}"
            kind = f"sub-quarter (quarter {p.get('ROVA')})"
        elif code in stat_areas_for and p.get("STAT_2022"):
            key = ("s", code, int(p["STAT_2022"]))
            name = f"{town}, statistical area {int(p['STAT_2022'])}"
            kind = "statistical area"
        else:
            continue
        g = groups.setdefault(key, {"name": name, "kind": kind, "polys": [], "names": [town, town_he]})
        g["polys"] += polys
    per_town: dict[str, list[int]] = {}
    for (t, code, n), g in groups.items():
        c = per_town.setdefault(g["names"][0], [0, 0])
        c[0 if t == "q" else 1] += 1
    for town, (q, st) in sorted(per_town.items()):
        log.info("CBS %s: %s", town, f"{q} sub-quarters" if q else f"no sub-quarters; {st} statistical areas")
    out = []
    for (t, code, n), g in sorted(groups.items()):
        uid = f"cbs-{'subq' if t == 'q' else 'stat'}:{code}-{n}"
        out.append(Unit(uid, g["name"], g["kind"], LEVEL_NEIGHBOURHOOD, src, g["polys"], g["names"]))
    return out


def cbs_is_locality(code: int, name: str) -> bool:
    """CBS codes 5500-5599 are regional councils' land outside their localities, 9900+ and "No Name"
    unnamed open areas: not places anyone lives in."""
    return code > 0 and not 5500 <= code <= 5599 and code < 9900 and name.lower() not in ("no name", "") \
        and not name.isdigit()


def cbs_locality_units(feats: list[dict]) -> list[Unit]:
    """Every locality's official outline: its statistical areas together (a village is one). A town's
    industrial and open-space areas (function codes 2 and 4) are left out when it has others, so a
    remote industrial zone does not stretch the town's box."""
    src = "Israel Central Bureau of Statistics, localities (statistical areas 2022)"
    groups: dict[int, dict] = {}
    for f in feats:
        p = f.get("properties") or {}
        polys = geojson_polys(f.get("geometry"))
        code = int(p.get("SEMEL_YISHUV") or 0)
        town = fix_text(str(p.get("SHEM_YISHUV_ENGLISH") or "")).strip().title()
        if not polys or not cbs_is_locality(code, town):
            continue
        g = groups.setdefault(code, {"name": town, "lived": [], "other": [],
                                     "names": [town, str(p.get("SHEM_YISHUV") or "").strip()]})
        g["other" if p.get("COD_TIFKUD") in (2, 4) else "lived"] += polys
    log.info("CBS: %d localities", len(groups))
    return [Unit(f"cbs-loc:{code}", g["name"], "locality", LEVEL_LOCALITY + 1, src, g["lived"] or g["other"],
                 [n for n in g["names"] if n]) for code, g in sorted(groups.items())]


PLACES = {"city", "town", "village", "hamlet", "isolated_dwelling", "suburb", "neighbourhood", "quarter"}
REGIONAL = re.compile(r"מועצה אזורית|regional council", re.I)


def read_osm_areas(path: Path) -> list[Unit]:
    """Municipal boundaries, locality outlines and neighbourhoods from an OpenStreetMap extract (.osm.pbf)."""
    import osmium                                                  # pyosmium
    log.info("reading %s (a few minutes)", path.name)
    fp = (osmium.FileProcessor(str(path))
          .with_areas(osmium.filter.KeyFilter("boundary", "place"))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)))
    out, seen, t0 = [], 0, time.time()
    for a in fp:
        seen += 1
        if seen % 5000 == 0:
            log.info("   %d boundary / place areas read, %d kept, %.0f s", seen, len(out), time.time() - t0)
        tags = {t.k: t.v for t in a.tags}
        if not (tags.get("place") in PLACES or (tags.get("boundary") == "administrative"
                                                  and tags.get("admin_level") in ("8", "9", "10"))):
            continue
        polys = []
        for outer in a.outer_rings():
            ring = [(n.lon, n.lat) for n in outer]
            if len(ring) < 4:
                continue
            polys.append([ring] + [[(n.lon, n.lat) for n in inner] for inner in a.inner_rings(outer)])
        if not polys:
            continue
        u = osm_unit(f"osm:{'w' if a.from_way() else 'r'}{a.orig_id()}", tags, polys)
        if u:
            out.append(u)
    log.info("israel-osm: %d areas kept of %d read in %.0f s", len(out), seen, time.time() - t0)
    return out


def osm_unit(ref: str, t: dict, polys: list) -> Unit | None:
    name_en = t.get("name:en") or t.get("int_name") or t.get("name", "")
    names = [t[k] for k in ("name:en", "name", "name:he", "int_name", "alt_name:en", "old_name:en",
                            "official_name:en", "name:he-Latn") if t.get(k)]
    adm, place = t.get("admin_level"), t.get("place", "")
    if not name_en:
        return None                               # unnamed fragments (enclave pieces, slivers)
    if t.get("boundary") == "administrative" and adm == "8":
        if any(REGIONAL.search(t.get(k, "")) for k in ("name", "name:en", "official_name", "official_name:en")):
            return None                           # regional council: too big to be anyone's area
        src = "Israel Ministry of Interior municipal jurisdiction (via OpenStreetMap)" \
            if "interior" in (t.get("source", "") + t.get("source:boundary", "")).lower() \
            else "OpenStreetMap municipal boundary (admin_level 8)"
        return Unit(ref, name_en, "municipality", LEVEL_LOCALITY, src, polys, names)
    if place in ("city", "town", "village", "hamlet", "isolated_dwelling"):
        return Unit(ref, name_en, f"locality ({place})", LEVEL_LOCALITY - 1,
                    "OpenStreetMap locality outline", polys, names)
    if (t.get("boundary") == "administrative" and adm in ("9", "10")) or place in ("suburb", "neighbourhood", "quarter"):
        src = f"OpenStreetMap neighbourhood boundary (admin_level {adm})" if adm in ("9", "10") else \
            f"OpenStreetMap neighbourhood outline (place={place})"
        return Unit(ref, name_en, "neighbourhood", LEVEL_NEIGHBOURHOOD, src, polys, names)
    return None


# ---------------------------------------------------------------- selection


def place_box(b: dict):
    return (b["s"], b["w"], b["n"], b["e"])


def is_empty(box) -> bool:
    return all(abs(v) < 1e-9 for v in box)


def select(units: list[Unit], box, neighbourhoods: bool) -> list[Unit]:
    """The official units for one place."""
    s, w, n, e = box
    clat, clon = (s + n) / 2, (w + e) / 2
    if half_diagonal_km(box) < POINT_KM:
        hits = [u for u in units if unit_contains(u, clat, clon)]
        if not hits:
            return []
        return [max(hits, key=lambda u: (u.level, -unit_area(u)))]
    cands, around = [], []
    box_area = (n - s) * (e - w)
    for u in units:
        us, uw, un, ue = u.bbox
        if us > n or un < s or uw > e or ue < w:
            continue
        share = share_inside(u, box)
        if share >= MIN_SHARE or share * unit_area(u) >= MIN_BOX_SHARE * box_area:
            cands.append(u)
        elif unit_contains(u, clat, clon):
            around.append(u)
    if not cands:                 # the box lies inside one bigger unit (a town inside its department)
        cands = around[:1] if len(around) <= 1 else [max(around, key=lambda u: (u.level, -unit_area(u)))]
    return prune(cands)


def prune(cands: list[Unit]) -> list[Unit]:
    """Drop units that finer units cover completely (they would never be returned)."""
    keep = []
    for u in cands:
        finer = [v for v in cands if v.level > u.level and v is not u
                 and v.bbox[0] <= u.bbox[2] and v.bbox[2] >= u.bbox[0]
                 and v.bbox[1] <= u.bbox[3] and v.bbox[3] >= u.bbox[1]]
        if finer:
            pts = sample_points(u)
            if pts and all(any(unit_contains(v, la, lo) for v in finer) for la, lo in pts):
                continue
        keep.append(u)
    return keep


def place_name(name: str) -> str:
    return re.sub(r"_area_.*$|_", " ", name).strip()


def check_point_name(units, u: Unit, name: str, box, key: str, report: list) -> list[Unit]:
    """A chaiTable point inside a unit whose name has nothing to do with the place's (Beit El inside Tel
    Aviv) is usually a city's default coordinates: if a unit elsewhere carries the place's name, use it."""
    want = norm_name(place_name(name))
    here = max((difflib.SequenceMatcher(None, want, norm_name(n)).ratio() for n in u.names or [u.name]), default=0)
    if here >= 0.75:
        return [u]
    (_, other, sc), = match_names(units, [place_name(name)], min_score=0.9)
    if other is None or other is u:
        return [u]
    s, w, n, e = box
    la, lo = (s + n) / 2, (w + e) / 2
    os_, ow, on, oe = other.bbox
    km = math.hypot((la - (os_ + on) / 2) * 111.19, (lo - (ow + oe) / 2) * 111.19 * math.cos(math.radians(la)))
    report.append(f"{key}: the chaiTable point is in {u.name}, but the name matches {other.name} ({sc}), "
                  f"{km:.0f} km away - used {other.name}; check it")
    return [other]


def match_names(units: list[Unit], wanted: list[str], min_score: float = 0.82):
    """Best unit per wanted name (municipal / locality level), or None."""
    pool = [u for u in units if LEVEL_COARSE < u.level < LEVEL_NEIGHBOURHOOD]
    out = []
    for nm in wanted:
        key = norm_name(nm)
        best, score = None, 0.0
        for u in pool:
            for alt in u.names or [u.name]:
                r = difflib.SequenceMatcher(None, key, norm_name(alt)).ratio()
                if r > score or (r == score and best is not None and u.level > best.level):
                    best, score = u, r
        out.append((nm, best if score >= min_score else None, round(score, 2)))
    return out


# ---------------------------------------------------------------- main

def load_json(src: str, fetch: Fetcher):
    return json.loads(fetch.path(src, name="chaiTable.json").read_text(encoding="utf-8"))


def build(args, cfg) -> dict:
    data_dir = Path(cfg["data_dir"])
    fetch = Fetcher(Path(args.cache or data_dir / "area-sources"), cfg.get("contact", ""))
    chai = load_json(args.chai, fetch)
    plans = json.loads(json.dumps(PLANS))
    if args.sources:
        plans.update(json.loads(Path(args.sources).read_text()))
    overrides = json.loads(Path(args.overrides).read_text()) if args.overrides and Path(args.overrides).exists() else {}
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
    sources = Sources(fetch, cfg.get("areas", {}))

    areas: dict[str, dict] = {}
    collapsed: set[str] = set()
    unresolved, report = [], []
    todo = [c for c in chai if not args.only or c["info"]["title"] in args.only]
    t_start = time.time()
    for ci, country in enumerate(todo, 1):
        title = country["info"]["title"]
        log.info("== %s %s (%d places), %.0f min so far", bar(ci - 1, len(todo)), title,
                 len(country["metroAreas"]), (time.time() - t_start) / 60)
        plan = plans.get(title)
        if plan is None:
            log.warning("no plan for %s; skipped", title)
            continue
        neighbourhoods = "Neighborhoods" in title
        metros = []
        for m in country["metroAreas"]:
            key = f"{title}/{m['name']}"
            ov = overrides.get(key, {})
            box = place_box(ov["bounds"]) if "bounds" in ov else place_box(m["bounds"])
            if "point" in ov:
                la, lo = ov["point"]
                box = (la, lo, la, lo)
            metros.append((key, m["name"], box, ov))
        boxes = [b for _, _, b, _ in metros if not is_empty(b)]
        units: list[Unit] = []
        failed = []
        for spec in plan:
            try:
                units += sources.units(spec, boxes)
            except Exception as e:
                log.error("%s: source %s failed: %s", title, spec, e)
                failed.append(f"{spec.get('type')} ({e})")
        for mi, (key, name, box, ov) in enumerate(metros, 1):
            if mi % 50 == 0:
                log.info("   %s places matched", bar(mi, len(metros)))
            if ov.get("skip"):
                report.append(f"{key}: skipped by override")
                continue
            if ov.get("names"):
                chosen, notes = [], []
                for nm, u, sc in match_names(units, ov["names"]):
                    if u:
                        chosen.append(u)
                        notes.append(f"{nm} -> {u.name} ({sc})")
                    else:
                        notes.append(f"{nm} -> NO MATCH (best {sc})")
                if not all("NO MATCH" not in x for x in notes):
                    unresolved.append({"place": key, "reason": "override names not matched: " + "; ".join(notes)})
                report.append(f"{key}: by name: " + "; ".join(notes))
            elif is_empty(box):
                (_, u, sc), = match_names(units, [place_name(name)], min_score=0.9)
                if not u:
                    unresolved.append({"place": key, "reason": "no location in chaiTable.json; add names, a "
                                       "point or bounds in areas_overrides.json"})
                    continue
                chosen = [u]
                report.append(f"{key}: no location in chaiTable.json; matched by name -> {u.name} ({sc}) - check it")
            else:
                chosen = select(units, box, neighbourhoods)
                if len(chosen) == 1 and half_diagonal_km(box) < POINT_KM:
                    chosen = check_point_name(units, chosen[0], name, box, key, report)
            if not chosen and not ov.get("names") and not is_empty(box):
                # the chaiTable point may simply be wrong: try the place's own name
                (_, u, sc), = match_names(units, [place_name(name)], min_score=0.9)
                if u:
                    s_, w_, n_, e_ = box
                    la, lo = (s_ + n_) / 2, (w_ + e_) / 2
                    us, uw, un, ue = u.bbox
                    km = math.hypot((la - (us + un) / 2) * 111.19,
                                    (lo - (uw + ue) / 2) * 111.19 * math.cos(math.radians(la)))
                    chosen = [u]
                    report.append(f"{key}: the chaiTable point is in no official unit; matched by name -> "
                                  f"{u.name} ({sc}), {km:.0f} km from the point - check it")
            if not chosen:
                why = "no official unit found" + (f" (failed sources: {', '.join(failed)})" if failed else "")
                if not ov.get("names"):
                    unresolved.append({"place": key, "reason": why})
                continue
            big = max(half_diagonal_km(u.bbox) for u in chosen)
            if not ov.get("names"):
                hd = half_diagonal_km(box)
                flag = "  <- chaiTable box looks too big" if hd > BIG_BOX_KM else ""
                report.append(f"{key}: box {hd:.1f} km -> {len(chosen)} unit(s), largest {big:.1f} km{flag}")
            else:
                report[-1] += f" -> {len(chosen)} unit(s), largest {big:.1f} km"
            if neighbourhoods:
                hoods = [u for u in chosen if u.level >= LEVEL_NEIGHBOURHOOD]
                for city in (u for u in chosen if u.level < LEVEL_NEIGHBOURHOOD
                             and half_diagonal_km(u.bbox) >= 2.0):
                    pts = sample_points(city, 200)
                    cov = sum(any(unit_contains(h, la, lo) for h in hoods) for la, lo in pts) / max(len(pts), 1)
                    report.append(f"    {city.name}: neighbourhoods cover ~{cov:.0%}; the rest gets the whole "
                                  f"{city.kind} ({half_diagonal_km(city.bbox):.1f} km)")
            for u in chosen:
                if u.id in collapsed:
                    continue
                a = areas.get(u.id)
                if a is None:
                    a = to_area(u, args.simplify_m)
                    if a is None:                      # a sliver thinner than the simplification
                        collapsed.add(u.id)
                        report.append(f"    dropped {u.name} ({u.id}): thinner than {args.simplify_m:g} m")
                        continue
                    areas[u.id] = a
                if key not in a["places"]:
                    a["places"].append(key)
    out = {"version": 1, "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "chaiTable": args.chai, "levels": {"neighbourhood": LEVEL_NEIGHBOURHOOD, "locality": LEVEL_LOCALITY,
                                              "coarse": LEVEL_COARSE},
           "areas": sorted(areas.values(), key=lambda a: a["id"]), "unresolved": unresolved}
    return out, report


def to_area(u: Unit, tol_m: float) -> dict | None:
    polys = []
    for p in u.polys:
        outer = simplify_ring(p[0], tol_m)
        if outer is None:
            continue
        holes = [h2 for h2 in (simplify_ring(h, tol_m) for h in p[1:]) if h2 is not None]
        polys.append([flat(outer)] + [flat(h) for h in holes])
    if not polys:
        return None
    s, w, n, e = u.bbox
    bbox = [round(s, 5), round(w, 5), round(n, 5), round(e, 5)]
    return {"id": u.id, "name": u.name, "kind": u.kind, "level": u.level, "source": u.source,
            "places": [], "bbox": bbox, "radiusKm": round(half_diagonal_km(bbox), 2), "polygons": polys}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--chai", default=CHAI_URL, help="chaiTable.json path or URL")
    ap.add_argument("--out", help="default <data_dir>/areas.json")
    ap.add_argument("--cache", help="download cache, default <data_dir>/area-sources")
    ap.add_argument("--sources", help="JSON {country title: [source, ...]} replacing PLANS entries")
    ap.add_argument("--overrides", default=str(HERE / "areas_overrides.json"))
    ap.add_argument("--only", action="append", help="chaiTable country title (repeatable)")
    ap.add_argument("--simplify-m", type=float, default=25.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    out, report = build(args, cfg)
    dest = Path(args.out or Path(cfg["data_dir"]) / "areas.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(dest)
    rep = dest.with_name("areas_report.txt")
    big = [a for a in out["areas"] if a["radiusKm"] > float(cfg["terrain"].get("max_area_radius_km", 30))]
    lines = report + ["", f"{len(out['areas'])} areas, {len(out['unresolved'])} unresolved places", ""]
    lines += [f"UNRESOLVED {u['place']}: {u['reason']}" for u in out["unresolved"]]
    lines += [f"OVER CAP {a['name']} ({a['id']}): {a['radiusKm']} km > terrain.max_area_radius_km" for a in big]
    rep.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote %s (%d areas, %d unresolved, %d over the cap); report %s", dest, len(out["areas"]),
             len(out["unresolved"]), len(big), rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
