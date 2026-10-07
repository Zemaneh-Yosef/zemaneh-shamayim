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
  Canada   the cities' own neighbourhood layers (Toronto neighbourhoods, Montreal arrondissements, Ottawa
           ONS Gen 3, Hamilton planning units, Halifax communities, Calgary community districts, Edmonton
           and Winnipeg neighbourhoods), then Statistics Canada 2021 census subdivisions (municipalities)
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

# Canada. Statistics Canada 2021 census subdivisions (the municipalities): the cartographic boundary file
# (shoreline-clipped), one shapefile for the whole country in Statistics Canada Lambert (EPSG:3347),
# reprojected here. Open Government Licence - Canada. (Its ArcGIS query service is not reliable enough.)
STATCAN_CSD_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/"
                   "files-fichiers/lcsd000b21a_e.zip")
STATCAN_MARGIN_DEG = (0.1, 0.15)    # CSDs within ~10 km of a place's box are kept (lat, lon)
CA_PROVINCES = {"10": "NL", "11": "PE", "12": "NS", "13": "NB", "24": "QC", "35": "ON", "46": "MB",
                "47": "SK", "48": "AB", "59": "BC", "60": "YT", "61": "NT", "62": "NU"}
CSD_TYPES = {"CY": "city", "C": "city", "T": "town", "TV": "town", "VL": "village", "V": "ville",
             "VC": "village cri", "VK": "village naskapi", "MU": "municipality", "M": "municipality",
             "DM": "district municipality", "RGM": "regional municipality", "RM": "rural municipality",
             "TP": "township", "CT": "canton", "CU": "cantons unis", "PE": "paroisse", "SV": "summer village",
             "ID": "improvement district", "SC": "subdivision of county municipality",
             "SNO": "subdivision of unorganized", "NO": "unorganized", "IRI": "Indian reserve",
             "S-É": "Indian settlement", "TL": "teslin land", "NH": "northern hamlet", "HAM": "hamlet",
             "LOT": "township and royalty", "CC": "chartered community", "COM": "community",
             "CN": "crown colony", "IGD": "Indian government district", "NL": "Nisga'a land",
             "NVL": "northern village", "RV": "resort village", "SÉ": "settlement", "SET": "settlement",
             "TC": "terres réservées aux Cris", "TI": "terre inuite", "TK": "terres réservées aux Naskapis"}
# The big cities' own neighbourhood layers: an amalgamated Canadian city is one census subdivision of up
# to ~200 km (Halifax), far over terrain.max_area_radius_km. All in lon/lat GeoJSON (ArcGIS: outSR=4326).
SOCRATA = "https://{host}/resource/{id}.geojson?$limit=50000"
ARCGIS_HRM = "https://services2.arcgis.com/11XBiaBYA9Ep0yNJ/arcgis/rest/services/GSA/FeatureServer/0/query"
ARCGIS_OTTAWA = "https://services.arcgis.com/G6F8XLCl5KtAlZ2G/arcgis/rest/services/GEN3_OTT_1_3_3/FeatureServer/0/query"
ARCGIS_HAMILTON = "https://services.arcgis.com/rYz782eMbySr2srL/ArcGIS/rest/services/Neighborhoods/FeatureServer/8/query"
TORONTO_URL = ("https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/fc443770-ef0a-4025-9c2c-2cb558bfab00/"
               "resource/0719053b-28b7-48ea-b863-068823a93aaa/download/neighbourhoods-4326.geojson")
MONTREAL_URL = ("https://donnees.montreal.ca/dataset/9797a946-9da8-41ec-8815-f6b276dec7e9/resource/"
                "e18bfd07-edc8-4ce8-8a5a-3b617662a794/download/limites-administratives-agglomeration.geojson")

MIN_SHARE = 0.25            # a unit belongs to a place's box if this share of it lies inside the box,
MIN_BOX_SHARE = 0.03        # or if its part inside covers this share of the box (big units at the edge)
POINT_KM = 0.3              # boxes smaller than this (centre to corner) are points
BIG_BOX_KM = 60.0           # chaiTable boxes bigger than this are flagged in the report


# Big US cities (Census places over, near or well into terrain.max_area_radius_km) and their own layers
US_CITIES = [
    {"type": "city", "key": "us-chi", "url": SOCRATA.format(host="data.cityofchicago.org", id="igwz-8jzy"),
     "id_fields": ["area_numbe", "area_num_1"], "name_fields": ["community"], "title_case": True,
     "name_suffix": ", Chicago", "kind": "community area", "source": "City of Chicago, Community Areas (77)"},
    {"type": "city", "key": "us-hou",
     "arcgis": "https://geohwp.houstontx.gov/arcgis/rest/services/02_BaseData_Boundaries/COH/MapServer/1/query",
     "id_fields": ["OBJECTID"], "name_fields": ["Name"], "title_case": True, "name_suffix": ", Houston",
     "kind": "super neighborhood", "source": "City of Houston, Super Neighborhoods"},
    {"type": "city", "key": "us-dal",
     "arcgis": "https://services2.arcgis.com/rwnOSbfKSwyTBcwN/ArcGIS/rest/services/CouncilAreas/FeatureServer/0/query",
     "id_fields": ["DISTRICT", "OBJECTID"], "name_fields": ["DISTRICT"], "name_prefix": "Council District ",
     "name_suffix": ", Dallas", "kind": "council district",      # Dallas has no citywide neighbourhood layer
     "source": "City of Dallas, City Council districts (2023)"},
    {"type": "city", "key": "us-phx",
     "arcgis": "https://maps.phoenix.gov/pub/rest/services/Public/Villages/MapServer/0/query",
     "id_fields": ["ANID", "OBJECTID"], "name_fields": ["NAME"], "title_case": True, "name_suffix": ", Phoenix",
     "kind": "urban village", "source": "City of Phoenix, Urban Villages"},
    {"type": "city", "key": "us-sd",
     "arcgis": "https://geo.sandag.org/server/rest/services/Hosted/Community_Plan_SD/FeatureServer/0/query",
     "id_fields": ["CPCODE", "OBJECTID"], "name_fields": ["CPNAME"], "title_case": True, "name_suffix": ", San Diego",
     "kind": "community planning area", "source": "City of San Diego, Community Plan areas (published by SANDAG)"},
    {"type": "city", "key": "us-aus", "url": SOCRATA.format(host="data.austintexas.gov", id="inrm-c3ee"),
     "id_fields": ["objectid", "gis_id"], "name_fields": ["planning_area_name"], "title_case": True,
     "name_suffix": ", Austin", "kind": "neighborhood planning area",
     "source": "City of Austin, Neighborhood Planning Areas"},
    {"type": "city", "key": "us-sea",
     "arcgis": "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services/nma_nhoods_sub/FeatureServer/0/query",
     "id_fields": ["OBJECTID"], "name_fields": ["S_HOOD"], "alt_fields": ["S_HOOD_ALT_NAMES", "L_HOOD"],
     "name_suffix": ", Seattle", "kind": "neighborhood",
     "source": "City of Seattle, Neighborhood Map Atlas neighborhoods (City Clerk)"},
    {"type": "city", "key": "us-bal",
     "arcgis": "https://geodata.baltimorecity.gov/egis/rest/services/Planning/Neighborhoods/MapServer/0/query",
     "id_fields": ["OBJECTID"], "name_fields": ["Name"], "name_suffix": ", Baltimore",
     "kind": "neighborhood statistical area", "source": "Baltimore City Department of Planning, Neighborhoods"},
]


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
    "Canada": [                                          # city neighbourhoods, then StatCan municipalities
        {"type": "city", "key": "ca-tor", "url": TORONTO_URL, "id_fields": ["AREA_SHORT_CODE", "AREA_ID"],
         "name_fields": ["AREA_NAME"], "name_suffix": ", Toronto", "kind": "neighbourhood",
         "source": "City of Toronto, Neighbourhoods (158) (Open Government Licence - Toronto)"},
        {"type": "city", "key": "ca-mtl", "url": MONTREAL_URL, "id_fields": ["CODEID", "CODE_3C", "NUM"],
         "name_fields": ["NOM", "NOM_OFFICIEL"], "name_suffix": ", Montréal", "kind": "arrondissement",
         "only": {"TYPE": ["Arrondissement"]},                    # the villes liées are census subdivisions
         "source": "Ville de Montréal, limites administratives de l'agglomération (CC BY 4.0)"},
        {"type": "city", "key": "ca-ott", "arcgis": ARCGIS_OTTAWA, "id_fields": ["ONS_ID", "OBJECTID"],
         "name_fields": ["ONS_Name", "Name_EN", "Name", "NAME_EN"], "name_suffix": ", Ottawa",
         "kind": "neighbourhood (ONS Gen 3)",
         "source": "City of Ottawa, Ottawa Neighbourhood Study Gen 3 (Open Data Licence v2.0)"},
        {"type": "city", "key": "ca-ham", "arcgis": ARCGIS_HAMILTON, "id_fields": ["PLANNING_UNIT", "OBJECTID"],
         "name_fields": ["NEIGHBOURHOOD"], "suffix_field": "COMMUNITY", "title_case": True,
         "kind": "neighbourhood (planning unit)", "source": "City of Hamilton, Neighbourhoods (planning units)"},
        {"type": "city", "key": "ca-hfx", "arcgis": ARCGIS_HRM, "id_fields": ["GSA_KEY", "OBJECTID"],
         "name_fields": ["GSA_NAME"], "name_suffix": ", Halifax Regional Municipality", "title_case": True,
         "kind": "community", "source": "Halifax Regional Municipality, Community Boundaries (Open Data Licence)"},
        {"type": "city", "key": "ca-cgy", "url": SOCRATA.format(host="data.calgary.ca", id="surr-xmvs"),
         "id_fields": ["comm_code"], "name_fields": ["name"], "name_suffix": ", Calgary", "title_case": True,
         "kind": "community district",
         "source": "City of Calgary, Community District Boundaries (Open Government Licence - City of Calgary)"},
        {"type": "city", "key": "ca-edm", "url": SOCRATA.format(host="data.edmonton.ca", id="65fr-66s6"),
         "id_fields": ["neighbourhood_number"], "name_fields": ["descriptive_name", "name"],
         "name_suffix": ", Edmonton", "kind": "neighbourhood",
         "source": "City of Edmonton, Neighbourhoods (Open Government Licence - City of Edmonton)"},
        {"type": "city", "key": "ca-wpg", "url": SOCRATA.format(host="data.winnipeg.ca", id="8k6x-xxsy"),
         "id_fields": ["id"], "name_fields": ["name"], "name_suffix": ", Winnipeg", "kind": "neighbourhood",
         "source": "City of Winnipeg, Neighbourhoods (OpenData Licence - City of Winnipeg)"},
        {"type": "statcan-csd"},
    ],
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
    "USA": [{"type": "nyc-nta"}, {"type": "la-times"}, *US_CITIES,
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
        cpg = names.get((base + ".cpg").lower())
        dbf = z.read(names[(base + ".dbf").lower()])
        enc = shapefile_encoding(z.read(cpg).decode("ascii", "replace")) if cpg else dbf_encoding(dbf)
        rd = shapefile.Reader(shp=io.BytesIO(z.read(shp)), dbf=io.BytesIO(dbf), encoding=enc, encodingErrors="replace")
    else:
        rd = shapefile.Reader(str(path), encoding=shapefile_text_encoding(path.with_suffix("")),
                              encodingErrors="replace")
    fields = [f[0] for f in rd.fields[1:]]
    out = []
    for sr in rd.iterShapeRecords():
        out.append({"properties": dict(zip(fields, sr.record)), "geometry": sr.shape.__geo_interface__})
    return out


def shapefile_encoding(cpg: str) -> str:
    """The text encoding a .cpg names (e.g. Statistics Canada's), UTF-8 when there is none."""
    import codecs
    c = cpg.strip().lower()
    c = {"ansi 1252": "cp1252", "1252": "cp1252"}.get(c, c)
    try:
        return codecs.lookup(c).name if c else "utf-8"
    except LookupError:
        return "utf-8"


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
            self.cache[key] = fn(spec, boxes) if t in ("israel-osm", "statcan-csd") else fn(spec)
            log.info("%s: %d units", spec.get("label") or spec.get("source") or t, len(self.cache[key]))
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

    def arcgis(self, url: str, name: str, page: int, extra: dict | None = None, offset_deg: float = 0.00002,
               label: str = "") -> list[dict]:
        """Every feature of an ArcGIS feature / map layer query endpoint, as lon/lat GeoJSON, a page at a time
        (each page cached as <name>-<offset>.geojson)."""
        feats, offset = [], 0
        while True:
            q = {"where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson", "geometryPrecision": 6,
                 "maxAllowableOffset": offset_deg, "orderByFields": "OBJECTID", "resultOffset": offset,
                 "resultRecordCount": page}
            q.update(extra or {})
            path = self.fetch.path(f"{url}?{urllib.parse.urlencode(q)}", name=f"{name}-{offset}.geojson",
                                   check=arcgis_check)
            got = read_geojson(path)
            feats += got
            if label:
                log.info("%s: %d so far", label, len(feats))
            if len(got) < page:
                return feats
            offset += page

    # a city's own neighbourhood layer: {"type": "city", "key", "url" (GeoJSON) or "arcgis" (a layer's
    # /query), "id_fields", "name_fields", "kind", "source", optional "name_suffix" / "suffix_field",
    # "title_case", "only": {field: [values]}, "level" (default neighbourhood), "path" (a local file)}
    def city(self, spec):
        if spec.get("path"):
            feats = read_features(self.fetch.path(spec["path"]))
        elif spec.get("arcgis"):
            feats = self.arcgis(spec["arcgis"], spec["key"], spec.get("page", 1000),
                                extra={"orderByFields": spec.get("order", "")})    # these layers fit one page
        else:
            feats = read_features(self.fetch.path(spec["url"], name=f"{spec['key']}.geojson",
                                                  check=lambda b: json.loads(b)["features"]))
        return city_units(feats, spec)

    # Canada: Statistics Canada 2021 census subdivisions near the places (one download for the country).
    # The file is big (a 315 MB .shp): it is unpacked to disk once and read one shape at a time, and only
    # the shapes whose box lies near a place are converted and reprojected.
    def statcan_csd(self, spec, boxes=()):
        import shapefile                                        # pyshp
        path = self.fetch.path(spec.get("path") or spec.get("url", STATCAN_CSD_URL), name="lcsd000b21a_e.zip")
        base = unpack_shapefile(path, self.fetch.cache)
        prj_f = base.with_suffix(".prj")
        prj = prj_f.read_text(encoding="utf-8", errors="replace") if prj_f.exists() else ""
        rd = shapefile.Reader(str(base), encoding=shapefile_text_encoding(base), encodingErrors="replace")
        fields = [f[0] for f in rd.fields[1:]]
        dl, dw = STATCAN_MARGIN_DEG
        near = [project_box((s - dl, w - dw, n + dl, e + dw), prj) for s, w, n, e in boxes]
        kept, total = [], 0
        try:
            for sr in rd.iterShapeRecords():
                total += 1
                x0, y0, x1, y1 = sr.shape.bbox if sr.shape.points else (0, 0, -1, -1)
                if x1 < x0 or (near and not any(w <= x1 and e >= x0 and s <= y1 and n >= y0
                                                for s, w, n, e in near)):
                    continue
                kept.append({"properties": dict(zip(fields, sr.record)),
                             "geometry": to_lonlat(sr.shape.__geo_interface__, prj)})
        finally:
            rd.close()
        log.info("Statistics Canada: %d of %d census subdivisions near the places", len(kept), total)
        return statcan_units(kept)

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


def unpack_shapefile(path: Path, cache: Path) -> Path:
    """A shapefile's path without extension, ready for pyshp to read from disk; a .zip is unpacked once into
    <cache>/<name>-unzipped (again only when a member's size changed, e.g. after a fresh download)."""
    if path.suffix.lower() != ".zip":
        return path.with_suffix("")
    dest = cache / f"{path.stem}-unzipped"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        shp = next(i for i in z.infolist() if i.filename.lower().endswith(".shp"))
        stem = shp.filename[:-4]
        for i in z.infolist():
            ext = i.filename[len(stem):].lower()
            if not i.filename.startswith(stem) or ext not in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                continue
            out = dest / ("x" + ext)                         # one fixed name: no dots or paths from the zip
            if out.exists() and out.stat().st_size == i.file_size:
                continue
            tmp = out.with_suffix(ext + ".tmp")
            with z.open(i) as src, open(tmp, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            tmp.replace(out)
        for ext in (".cpg", ".prj"):                          # left over from an older file
            if not any(i.filename.lower() == (stem + ext).lower() for i in z.infolist()):
                (dest / ("x" + ext)).unlink(missing_ok=True)
    return dest / "x"


def shapefile_text_encoding(base: Path) -> str:
    """The .dbf's text encoding: what its .cpg says, else UTF-8 if every byte decodes as UTF-8, else
    Windows-1252 (older Canadian / European files without a .cpg)."""
    cpg = base.with_suffix(".cpg")
    if cpg.exists():
        return shapefile_encoding(cpg.read_text(errors="replace"))
    return dbf_encoding(base.with_suffix(".dbf").read_bytes())


def dbf_encoding(dbf: bytes) -> str:
    hdr = int.from_bytes(dbf[8:10], "little") if len(dbf) >= 10 else 0
    try:
        dbf[hdr:].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def project_box(box, prj: str):
    """A lon/lat (south, west, north, east) box in a projected system's coordinates, as the extent of a grid
    of points over it (enough for boxes of a few dozen km); the box itself if prj is geographic."""
    s, w, n, e = box
    if not prj or prj.lstrip().upper().startswith("GEOGCS"):
        return box
    from rasterio.crs import CRS
    from rasterio.warp import transform
    k = 6
    lons = [w + (e - w) * i / k for i in range(k + 1) for _ in range(k + 1)]
    lats = [s + (n - s) * j / k for _ in range(k + 1) for j in range(k + 1)]
    xs, ys = transform("EPSG:4326", CRS.from_wkt(prj), lons, lats)
    return (min(ys), min(xs), max(ys), max(xs))


def to_lonlat(geom: dict | None, prj: str) -> dict | None:
    """A GeoJSON geometry in a projected coordinate system (its .prj WKT) brought to lon/lat (WGS 84)."""
    if not geom or not prj or prj.lstrip().upper().startswith("GEOGCS"):
        return geom
    from rasterio.crs import CRS                                # bundles PROJ; in requirements.txt
    from rasterio.warp import transform
    src = CRS.from_wkt(prj)

    def ring(r):
        xs, ys = transform(src, "EPSG:4326", [p[0] for p in r], [p[1] for p in r])
        return [(x, y) for x, y in zip(xs, ys)]

    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [ring(r) for r in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[ring(r) for r in p] for p in geom["coordinates"]]}
    return None


def arcgis_check(body: bytes):
    """An ArcGIS query answers errors with HTTP 200 and {"error": ...}: reject it (retried, never cached)."""
    d = json.loads(body)
    if "error" in d or "features" not in d:
        raise ValueError(f"ArcGIS error: {str(d.get('error', d))[:300]}")


def first_field(feats: list[dict], fields, what: str, src: str) -> str:
    """The first of the candidate fields the features carry (case-insensitive); a clear error otherwise,
    so a renamed column fails the source loudly instead of naming every area by its id."""
    have = {k.lower(): k for f in feats[:50] for k in (f.get("properties") or {})}
    for c in fields:
        if c.lower() in have:
            return have[c.lower()]
    raise ValueError(f"{src}: none of the {what} fields {list(fields)} found; the data has {sorted(have.values())}")


def city_units(feats: list[dict], spec: dict) -> list[Unit]:
    src = spec.get("source", spec["key"])
    if not feats:
        raise ValueError(f"{src}: no features")
    name_f = first_field(feats, spec["name_fields"], "name", src)
    id_f = first_field(feats, spec["id_fields"], "id", src)
    # "only": keep features whose field has one of the values (a field the data lacks filters nothing)
    only = {k: {v.lower() for v in vals} for k, vals in (spec.get("only") or {}).items()}
    out = []
    for i, f in enumerate(feats):
        p = f.get("properties") or {}
        if any(prop(p, k) and prop(p, k).strip().lower() not in vals for k, vals in only.items()):
            continue
        polys = geojson_polys(f.get("geometry"))
        name = fix_text(prop(p, name_f)).strip()
        if not polys or not name:
            continue
        if spec.get("title_case"):
            name = title_case(name)
        name = spec.get("name_prefix", "") + name
        suffix = spec.get("name_suffix", "")
        if spec.get("suffix_field") and prop(p, spec["suffix_field"]):
            sfx = fix_text(prop(p, spec["suffix_field"])).strip()
            suffix = ", " + (title_case(sfx) if spec.get("title_case") else sfx)
        nid = re.sub(r"\.0$", "", prop(p, id_f, default=str(i)))
        alts = [fix_text(prop(p, k)).strip() for k in spec.get("alt_fields", ()) if prop(p, k)]
        out.append(Unit(f"{spec['key']}:{nid}", name + suffix, spec.get("kind", "neighbourhood"),
                        spec.get("level", LEVEL_NEIGHBOURHOOD), src, polys, [name] + alts))
    # one id may come in several pieces (multi-part features split by the service): merge them
    merged: dict[str, Unit] = {}
    for u in out:
        if u.id in merged:
            merged[u.id].polys += u.polys
        else:
            merged[u.id] = u
    return list(merged.values())


def title_case(s: str) -> str:
    """CALGARY's / HALIFAX's upper-case names: "BRIDLEWOOD" -> "Bridlewood", "ST. ANDREWS" -> "St. Andrews"."""
    if s != s.upper():
        return s
    return re.sub(r"[A-Za-zÀ-ÿ']+", lambda m: m.group(0).capitalize(), s.lower())


def statcan_units(feats: list[dict]) -> list[Unit]:
    src = "Statistics Canada, 2021 Census cartographic boundary files, census subdivisions (Open Government Licence - Canada)"
    out = []
    for f in feats:
        p = f.get("properties") or {}
        polys = geojson_polys(f.get("geometry"))
        uid, name = prop(p, "CSDUID"), fix_text(prop(p, "CSDNAME")).strip()
        if not polys or not uid or not name:
            continue
        typ = prop(p, "CSDTYPE")
        prov = CA_PROVINCES.get(prop(p, "PRUID") or uid[:2], "")
        out.append(Unit(f"statcan-csd:{uid}", f"{name}, {prov}" if prov else name,
                        CSD_TYPES.get(typ, "census subdivision") + (f" ({typ})" if typ else ""),
                        LEVEL_LOCALITY, src, polys, [name]))
    return out


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


def match_names(units: list[Unit], wanted: list, min_score: float = 0.82):
    """Best unit per wanted name (municipal / locality level), or None. A wanted entry may be a list of
    alternative spellings of one place (["Halamish", "Neve Tsuf"]): the best-scoring spelling wins."""
    pool = [u for u in units if LEVEL_COARSE < u.level < LEVEL_NEIGHBOURHOOD]
    out = []
    for nm in wanted:
        best, score = None, 0.0
        for spelling in ([nm] if isinstance(nm, str) else nm):
            key = norm_name(spelling)
            for u in pool:
                for alt in u.names or [u.name]:
                    r = difflib.SequenceMatcher(None, key, norm_name(alt)).ratio()
                    if r > score or (r == score and best is not None and u.level > best.level):
                        best, score = u, r
        label = nm if isinstance(nm, str) else " / ".join(nm)
        out.append((label, best if score >= min_score else None, round(score, 2)))
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
    extra = overrides.get("_extra", {})            # places chaiTable lacks: {country title: [{name, point|bounds}]}
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
        for e in extra.get(title, []):
            key = f"{title}/{e['name']}"
            if any(k == key for k, *_ in metros):
                raise ValueError(f"_extra place {key} is already in chaiTable.json; use a normal override")
            if "point" in e:
                la, lo = e["point"]
                box = (la, lo, la, lo)
            else:
                box = place_box(e["bounds"])
            metros.append((key, e["name"], box, {}))
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


def merge_previous(out: dict, old: dict, rebuilt: set[str]) -> int:
    """A build with --only covers some countries: add the previous file's areas and unresolved places of every
    other country (an area shared across countries keeps the other countries' places). Returns how many
    areas came from the old file."""
    country = lambda place: place.split("/", 1)[0]
    by_id = {a["id"]: a for a in out["areas"]}
    kept = 0
    for a in old.get("areas", []):
        places = [p for p in a.get("places", []) if country(p) not in rebuilt]
        if not places:
            continue
        if a["id"] in by_id:
            by_id[a["id"]]["places"] += [p for p in places if p not in by_id[a["id"]]["places"]]
        else:
            by_id[a["id"]] = dict(a, places=places)
            kept += 1
    out["areas"] = sorted(by_id.values(), key=lambda a: a["id"])
    out["unresolved"] = [u for u in old.get("unresolved", []) if country(u["place"]) not in rebuilt] + out["unresolved"]
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--chai", default=CHAI_URL, help="chaiTable.json path or URL")
    ap.add_argument("--out", help="default <data_dir>/areas.json")
    ap.add_argument("--cache", help="download cache, default <data_dir>/area-sources")
    ap.add_argument("--sources", help="JSON {country title: [source, ...]} replacing PLANS entries")
    ap.add_argument("--overrides", default=str(HERE / "areas_overrides.json"))
    ap.add_argument("--only", action="append", help="chaiTable country title (repeatable); the other countries' "
                    "areas already in the output file are kept")
    ap.add_argument("--replace", action="store_true", help="with --only: write only those countries, dropping "
                    "every other area in the output file")
    ap.add_argument("--simplify-m", type=float, default=25.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    out, report = build(args, cfg)
    dest = Path(args.out or Path(cfg["data_dir"]) / "areas.json")
    if args.only and not args.replace and dest.exists():
        kept = merge_previous(out, json.loads(dest.read_text(encoding="utf-8")), set(args.only))
        log.info("kept %d areas of the other countries from %s (--replace to drop them)", kept, dest)
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
