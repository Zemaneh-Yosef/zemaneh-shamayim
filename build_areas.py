#!/usr/bin/env python3
"""Build <data_dir>/areas.sqlite: the official areas of the whole world, for area=auto (areas.py).

    venv/bin/python build_areas.py [--config config.json] [--only USA --only FRA] [--replace]

Every country gets its official units, and nothing else decides which: no list of places. Per country
(ISO 3166 alpha-3 code), finest first:
  USA      NYC Planning Neighborhood Tabulation Areas, LA Times "Mapping L.A." neighbourhoods, the big cities'
           own neighbourhood layers (US_CITIES), Census TIGER places (cities, villages, CDPs), then county
           subdivisions (towns) - every state and territory (Puerto Rico, Guam, ... too)
  ISR      the Central Bureau of Statistics' sub-quarters (statistical areas 2022; cities of 40,000+; a few
           cities without sub-quarters get their statistical areas), its locality outlines, then Ministry of
           Interior municipal jurisdictions and other locality outlines (OpenStreetMap, which carries the
           Ministry's boundaries; one download of Geofabrik's daily Israel and Palestine extract, read with
           pyosmium). Regional councils are never an area.
  CAN      the cities' own neighbourhood layers (Toronto neighbourhoods, Montreal arrondissements, Ottawa
           ONS Gen 3, Hamilton planning units, Halifax communities, Calgary community districts, Edmonton
           and Winnipeg neighbourhoods), then Statistics Canada 2021 census subdivisions (municipalities)
  others   geoBoundaries (national statistics / mapping agencies): the municipal level, and where there is one
           a finer level inside it (wards, sub-municipalities, cadastral areas...) as neighbourhoods
           (GB_LEVELS; countries it does not list follow default_levels)
A country's plan can be replaced with local files via --sources.

Settlements get a level below LEVEL_NEIGHBOURHOOD, sub-city units LEVEL_NEIGHBOURHOOD: areas.py picks a
point's area from them by rule (a city up to Jerusalem's size whole, or the finest unit).

Downloads are cached in <data_dir>/area-sources (delete a file there to fetch it again). The output is
written beside the old file (<out>.tmp) and swapped in whole; a country whose sources failed keeps the areas
it had. Each country is committed as it is done: a killed build started again with the same countries and
options goes on where it stopped (--restart to start over). The big sources (geoBoundaries, TIGER, Statistics
Canada) are read a feature at a time, so a country never needs all its boundaries in memory at once.
Needs pyshp (Census shapefiles) and osmium (pyosmium, the OpenStreetMap extract).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from areas import LEVEL_COARSE, LEVEL_LOCALITY, LEVEL_NEIGHBOURHOOD, SCHEMA, half_diagonal_km, insert_area
from common import load_config

log = logging.getLogger("build_areas")

GB_META_URL = "https://www.geoboundaries.org/api/current/gbOpen/ALL/ALL/"   # every boundary file's metadata

GB_URL = ("https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/main/releaseData/gbOpen/"
          "{iso}/ADM{adm}/geoBoundaries-{iso}-ADM{adm}_simplified.geojson")
TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER{year}/{layer_uc}/tl_{year}_{fips}_{layer}.zip"
NYC_NTA_URL = "https://data.cityofnewyork.us/resource/9nt8-h7nd.geojson?$limit=5000"
LA_TIMES_URL = ("https://raw.githubusercontent.com/codeforgermany/click_that_hood/main/"
                "public/data/los-angeles.geojson")
CBS_STAT_URL = ("https://services8.arcgis.com/JcXY3lLZni6BK4El/arcgis/rest/services/"
                "statistical_areas_2022/FeatureServer/0/query")     # Israel CBS statistical areas 2022
CBS_PAGE = 2000                                   # the service's maxRecordCount
# cities without sub-quarters whose statistical areas are their neighbourhoods (CBS codes): Jerusalem,
# Haifa, Beit Shemesh, Tiberias, Safed - only cities of 40,000+ have sub-quarters
CBS_HOOD_CITIES = [3000, 4000, 2610, 6700, 8000]
GEOFABRIK_URL = "https://download.geofabrik.de/asia/israel-and-palestine-latest.osm.pbf"   # daily, ~150 MB

# Canada. Statistics Canada 2021 census subdivisions (the municipalities): the cartographic boundary file
# (shoreline-clipped), one shapefile for the whole country in Statistics Canada Lambert (EPSG:3347),
# reprojected here. Open Government Licence - Canada. (Its ArcGIS query service is not reliable enough.)
STATCAN_CSD_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/"
                   "files-fichiers/lcsd000b21a_e.zip")
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


# The countries with plans of their own, finest first ("level" defaults per type)
COUNTRY_PLANS: dict[str, list[dict]] = {
    "USA": [{"type": "nyc-nta"}, {"type": "la-times"}, *US_CITIES,
            {"type": "tiger", "layer": "place"}, {"type": "tiger", "layer": "cousub"}],
    "CAN": [                                          # city neighbourhoods, then StatCan municipalities
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
    "ISR": [{"type": "cbs-subquarters"}, {"type": "cbs-localities"}, {"type": "israel-osm"}],
}
COVERED_BY = {"PRI": "USA", "GUM": "USA", "VIR": "USA", "ASM": "USA", "MNP": "USA", "UMI": "USA"}   # TIGER has them

# geoBoundaries levels: (municipal level, a finer level inside it or None, what the municipal units are).
# Countries not listed follow default_levels. Listed: every country chosen by hand before (the kinds are
# theirs), and where the rule picks wrong - India's villages are its settlements (its blocks are 15 km
# groups of them); Austria's, Czechia's and Slovakia's finest level is cadastral areas inside municipalities;
# Rwanda's sectors are its municipal level.
GB_LEVELS: dict[str, tuple[int, int | None, str | None]] = {
    "ARG": (2, None, "department"),                     # in Buenos Aires city: the 15 comunas
    "AUS": (2, None, "local government area"),
    "AUT": (3, 4, "municipality"),
    "BEL": (4, None, "municipality"),
    "BGR": (2, None, "municipality"),
    "BRA": (2, None, "municipality"),
    "BRN": (2, None, "mukim"),
    "CAF": (3, 5, "municipality"),
    "CHE": (3, None, "municipality"),
    "CHL": (3, None, "commune"),
    "COL": (2, None, "municipality"),
    "CZE": (3, 4, "municipality"),
    "DEU": (3, None, "district / independent city"),
    "DNK": (2, None, "municipality"),
    "ESP": (3, None, "municipality"),
    "FJI": (3, None, "tikina"),                         # its finest level is census enumeration areas
    "FRA": (5, None, "commune"),                        # Paris, Lyon, Marseille: arrondissements
    "GBR": (3, None, "local authority district"),
    "GRC": (3, None, "municipality"),
    "HUN": (3, None, "municipality"),                   # Budapest: districts
    "IND": (5, None, "village / town"),
    "IRL": (2, None, "local electoral area"),
    "ITA": (4, None, "comune"),
    "JAM": (1, 2, "parish"),
    "MEX": (2, None, "municipality"),
    "NLD": (2, None, "municipality"),
    "PAN": (3, None, "corregimiento"),
    "POL": (3, None, "gmina"),
    "ROU": (2, None, "municipality"),
    "RUS": (2, None, "district"),
    "RWA": (3, 4, "sector"),
    "SVK": (3, 4, "municipality"),
    "TUR": (2, None, "district"),
    "UKR": (3, None, "hromada / council"),
    "URY": (2, None, "municipality"),
    "VEN": (2, None, "municipality"),
    "ZAF": (3, 4, "local municipality"),                # wards inside the metros
}
# a finest level named like these is inside the municipalities: the level above it is the municipal one
SUB_MUNICIPAL = re.compile(r"village|ward|cell|colline|fokontany|grama|enumeration|barrio|quartier|^communit|"
                           r"submunic|freguesia|hamlet|mukim|dehestan|union council", re.I)


def gb(iso, adm, kind, level=LEVEL_LOCALITY, source=None, url=None):
    return {"type": "geoboundaries", "iso": iso, "adm": adm, "kind": kind, "level": level,
            **({"source": source} if source else {}), **({"url": url} if url else {})}


def gb_name(rec: dict | None, adm: int) -> str:
    """What a level's units are called (geoBoundaries' boundaryCanonical), or "ADM<n> unit"."""
    c = str((rec or {}).get("boundaryCanonical") or "").strip()
    return c if c and c.lower() not in ("unknown", "gbopen") and not c.upper().startswith("ADM") else f"ADM{adm} unit"


def default_levels(levels: dict[int, dict]) -> tuple[int, int | None]:
    """The finest level as the municipal one - unless it is named like a unit inside municipalities (wards,
    villages, cells...): then the level above it, with the finest as its neighbourhoods."""
    have = sorted(levels) or [0]
    fin = have[-1]
    coarser = [a for a in have if 0 < a < fin]
    if coarser and SUB_MUNICIPAL.search(gb_name(levels.get(fin), fin)):
        return coarser[-1], fin
    return fin, None


def gb_plan(iso: str, levels: dict[int, dict]) -> list[dict]:
    """A country's geoBoundaries sources; levels = {adm: its metadata record} (empty: none known)."""
    muni, hood, kind = GB_LEVELS.get(iso) or (*default_levels(levels), None)

    def spec(adm, kind, level):
        rec = levels.get(adm) or {}
        src = rec.get("boundarySource")
        src = f"{src} (via geoBoundaries; {rec.get('boundaryLicense') or 'see geoBoundaries'})" if src else None
        return gb(iso, adm, kind or gb_name(rec, adm), level, src, rec.get("simplifiedGeometryGeoJSON"))

    out = [spec(muni, kind, LEVEL_LOCALITY)]
    if hood is not None:
        out.append(spec(hood, None, LEVEL_NEIGHBOURHOOD))
    return out


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


def iter_geojson_features(path: Path, chunk: int = 1 << 23):
    """A FeatureCollection's features one at a time, never the whole file in memory: geoBoundaries' biggest
    file (India's 650,000 villages) is ~470 MB, which json.loads would make several GB of objects."""
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8") as f:
        buf, i = "", 0

        def more() -> bool:
            nonlocal buf, i
            got = f.read(chunk)
            buf, i = buf[i:] + got, 0
            return bool(got)

        while (k := buf.find('"features"', i)) < 0 or buf.find("[", k) < 0:
            if not more():
                raise ValueError(f"{path.name}: no features")
        i = buf.index("[", buf.find('"features"', i)) + 1
        while True:
            while i < len(buf) and buf[i] in " \t\r\n,":
                i += 1
            if i >= len(buf):
                if not more():
                    raise ValueError(f"{path.name}: ends inside its features")
                continue
            if buf[i] == "]":
                return
            try:
                obj, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                if not more():                     # a feature cut off by the end of what was read
                    raise
                continue
            yield obj
            i = j
            if i > chunk:
                buf, i = buf[i:], 0


def looks_whole_json(body: bytes) -> None:
    """A cheap check that a download is a whole JSON object (a big one is never parsed all at once)."""
    if not body.lstrip()[:1] == b"{" or not body.rstrip()[-1:] == b"}":
        raise ValueError("not a whole JSON object")


def read_shapefile(path: Path) -> list[dict]:
    """Features (GeoJSON-like) from a zipped or plain shapefile, in its own lon/lat coordinates."""
    return list(iter_shapefile(path))


def iter_shapefile(path: Path, prj: str = ""):
    """read_shapefile's features one at a time (a generator), brought to lon/lat from prj's system if given."""
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
    try:
        for sr in rd.iterShapeRecords():
            if not sr.shape.points:
                continue
            geom = sr.shape.__geo_interface__
            yield {"properties": dict(zip(fields, sr.record)), "geometry": to_lonlat(geom, prj) if prj else geom}
    finally:
        rd.close()


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


def units_from_features(feats, *, key, id_keys, name_keys, kind, level, source, name_suffix="", alt_keys=(),
                        index=0):
    out = []
    for i, f in enumerate(feats, index):
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

    STREAMED = ("geoboundaries", "tiger", "statcan-csd")      # the big ones: never all in memory at once

    def stream(self, spec: dict):
        """One source's units, as an iterable: the big sources (STREAMED) read and made a feature at a time and
        dropped once written, the others whole."""
        if spec["type"] in self.STREAMED:
            return getattr(self, spec["type"].replace("-", "_"))(spec)
        return self.units(spec)

    def units(self, spec: dict) -> list[Unit]:
        """All units of one source."""
        t = spec["type"]
        key = json.dumps(spec, sort_keys=True)
        if t in self.STREAMED:
            return list(self.stream(spec))
        if key not in self.cache:
            self.cache[key] = getattr(self, t.replace("-", "_"))(spec)
            log.info("%s: %d units", spec.get("label") or spec.get("source") or t, len(self.cache[key]))
        return self.cache[key]

    def clear(self) -> None:
        """Forget the units read (each country's are written, then dropped)."""
        self.cache.clear()

    # geoBoundaries (national statistics / mapping agencies, via the geoBoundaries project)
    def geoboundaries(self, spec):
        """A geoBoundaries level's units, read and made one feature at a time (a generator)."""
        iso, adm = spec["iso"], spec["adm"]
        path = self.fetch.path(spec.get("path") or spec.get("url") or GB_URL.format(iso=iso, adm=adm),
                               name=f"gb-{iso}-ADM{adm}.geojson", check=looks_whole_json)
        meta_src = spec.get("source") or f"geoBoundaries {iso} ADM{adm}"
        for i, f in enumerate(iter_geojson_features(path)):
            yield from units_from_features([f], key=f"gb-{iso}{adm}", id_keys=("shapeID",), name_keys=("shapeName",),
                                           kind=spec["kind"], level=spec.get("level", LEVEL_LOCALITY),
                                           source=meta_src, index=i)

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

    # Canada: Statistics Canada 2021 census subdivisions (one download for the country). The file is big
    # (a 315 MB .shp): it is unpacked to disk once and read, reprojected and made one shape at a time.
    def statcan_csd(self, spec):
        path = self.fetch.path(spec.get("path") or spec.get("url", STATCAN_CSD_URL), name="lcsd000b21a_e.zip")
        base = unpack_shapefile(path, self.fetch.cache)
        prj_f = base.with_suffix(".prj")
        prj = prj_f.read_text(encoding="utf-8", errors="replace") if prj_f.exists() else ""
        n = 0
        for f in iter_shapefile(base.with_suffix(".shp"), prj):
            for u in statcan_units([f]):
                n += 1
                yield u
        log.info("Statistics Canada: %d census subdivisions", n)

    # US Census TIGER/Line
    def us_states(self) -> dict[str, str]:
        """{FIPS code: name} of every state and territory in the Census state file."""
        year = self.cfg.get("tiger_year", 2024)
        if "us-states" not in self.cache:
            path = self.fetch.path(TIGER_URL.format(year=year, layer_uc="STATE", fips="us", layer="state"))
            self.cache["us-states"] = {prop(f["properties"], "STATEFP"): prop(f["properties"], "NAME")
                                       for f in iter_shapefile(path)}
        return self.cache["us-states"]

    def tiger(self, spec):
        """A TIGER/Line layer ("place" or "cousub") of every state, a state and a feature at a time."""
        layer, year = spec["layer"], self.cfg.get("tiger_year", 2024)
        for fips, state in sorted(self.us_states().items()):
            path = self.fetch.path(TIGER_URL.format(year=year, layer_uc=layer.upper(), fips=fips, layer=layer))
            n = 0
            for f in iter_shapefile(path):
                p = f["properties"]
                if layer == "place":
                    cdp = str(p.get("CLASSFP", "")).startswith("U")
                    units = units_from_features(
                        [f], key="us-place", id_keys=("GEOID",), name_keys=("NAMELSAD",), alt_keys=("NAME",),
                        kind="census-designated place" if cdp else "incorporated place", level=LEVEL_LOCALITY,
                        source=f"US Census TIGER/Line {year} places")
                elif str(p.get("COUSUBFP")) != "00000":
                    units = units_from_features([f], key="us-cousub", id_keys=("GEOID",), name_keys=("NAMELSAD",),
                                                alt_keys=("NAME",), kind="county subdivision", level=LEVEL_COARSE,
                                                source=f"US Census TIGER/Line {year} county subdivisions")
                else:
                    continue
                for u in units:
                    u.name = f"{u.name}, {state or fips}"
                    n += 1
                    yield u
            log.info("tiger-%s-%s: %d units", layer, fips, n)

    # Israel: OpenStreetMap, whose admin_level=8 boundaries are the Ministry of Interior's
    def israel_osm(self, spec):
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
            kind = f"sub-quarter (quarter {p['ROVA']})" if p.get("ROVA") else "sub-quarter"
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



# ---------------------------------------------------------------- build

def gb_meta(fetch: Fetcher) -> dict[str, dict[int, dict]]:
    """geoBoundaries' metadata: {ISO: {adm level: record}}; empty (logged) if it can't be had - the countries
    in GB_LEVELS are then still built, without source details."""
    try:
        recs = json.loads(fetch.path(GB_META_URL, name="gb-meta.json",
                                     check=lambda b: json.loads(b)[0]["boundaryISO"]).read_text(encoding="utf-8"))
    except Exception as e:                         # noqa: BLE001 - reported, the rest still builds
        log.error("geoBoundaries metadata: %s; only the countries in GB_LEVELS get geoBoundaries areas", e)
        return {}
    out: dict[str, dict[int, dict]] = {}
    for r in recs:
        m = re.fullmatch(r"ADM(\d)", str(r.get("boundaryType", "")))
        if m and r.get("boundaryISO"):
            out.setdefault(r["boundaryISO"], {})[int(m.group(1))] = r
    return out


def country_plans(fetch: Fetcher, args) -> dict[str, list[dict]]:
    """{country: its sources}: the countries with plans of their own, then every other country geoBoundaries
    has; --sources replaces plans, --only keeps those countries."""
    meta = gb_meta(fetch)
    plans = {k: list(v) for k, v in COUNTRY_PLANS.items()}
    for iso in sorted(meta or GB_LEVELS):
        if iso not in plans and iso not in COVERED_BY:
            plans[iso] = gb_plan(iso, meta.get(iso, {}))
    if args.sources:
        plans.update(json.loads(Path(args.sources).read_text(encoding="utf-8")))
    if args.only:
        unknown = [c for c in args.only if c not in plans]
        if unknown:
            raise SystemExit(f"--only: no plan for {unknown} (countries are ISO 3166 alpha-3 codes: USA, FRA, ...)")
        plans = {c: plans[c] for c in args.only}
    return plans


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
            "bbox": bbox, "radiusKm": round(half_diagonal_km(bbox), 2), "polygons": polys}


def drop_country(con: sqlite3.Connection, country: str) -> None:
    con.execute("DELETE FROM areas_box WHERE id IN (SELECT rowid FROM areas WHERE country = ?)", (country,))
    con.execute("DELETE FROM areas WHERE country = ?", (country,))


def write_country(con: sqlite3.Connection, country: str, units, tol_m: float, cap_km: float,
                  report: list[str]) -> dict:
    """Replace the country's areas with these units (any iterable: they are written as they come). Not
    committed: the caller commits, or rolls back to the areas it had. Returns its summary for the metadata."""
    drop_country(con, country)
    kinds: dict[str, int] = {}
    sources: dict[str, int] = {}
    n = over = dropped = dup = 0
    for u in units:
        a = to_area(u, tol_m)
        if a is None:                                  # a sliver thinner than the simplification
            dropped += 1
            if dropped <= 20:
                report.append(f"    dropped {u.name} ({u.id}): thinner than {tol_m:g} m")
            continue
        if not insert_area(con, a, country):
            dup += 1
            continue
        n += 1
        over += a["radiusKm"] > cap_km
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
        sources[a["source"]] = sources.get(a["source"], 0) + 1
    report.append(f"{country}: {n} areas, {over} over the {cap_km:g} km cap"
                  + (f", {dropped} slivers dropped" if dropped else "") + (f", {dup} ids already taken" if dup else ""))
    report += [f"    {c:6d} {k}" for k, c in sorted(kinds.items(), key=lambda kv: -kv[1])[:8]]
    return {"areas": n, "overCap": over, "sources": sorted(sources)}


def read_meta(con: sqlite3.Connection) -> dict:
    return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM meta")}


def save_meta(con: sqlite3.Connection, **kv) -> None:
    """Set these metadata keys (in the open transaction: committed with the country written)."""
    con.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    [(k, json.dumps(v, ensure_ascii=False)) for k, v in kv.items()])


def resumable(tmp: Path, run: dict) -> dict | None:
    """The progress left in tmp by an interrupted build of the same run (same countries and options), or None."""
    if not tmp.exists():
        return None
    try:
        con = sqlite3.connect(tmp)
        try:
            p = read_meta(con).get("progress")
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return None
    return p if p and p.get("run") == run else None


def build(args, cfg: dict, dest: Path) -> tuple[dict, list[str]]:
    """Writes dest.tmp (from a copy of dest unless --replace), then swaps it in. Returns (meta, report).
    Each country is committed to dest.tmp with the run's progress: a build that was killed (out of memory, a
    reboot) and is started again with the same countries and options goes on from the next country."""
    fetch = Fetcher(Path(args.cache or Path(cfg["data_dir"]) / "area-sources"), cfg.get("contact", ""))
    sources = Sources(fetch, cfg.get("areas", {}))
    plans = country_plans(fetch, args)
    cap_km = float(cfg["terrain"].get("max_area_radius_km", 30))
    tmp = dest.with_name(dest.name + ".tmp")
    fresh = bool(args.replace) or not dest.exists()
    run = {"countries": list(plans), "fresh": fresh, "only": bool(args.only), "simplifyM": args.simplify_m,
           "sources": args.sources or None}
    progress = None if args.restart else resumable(tmp, run)
    if progress is None:
        tmp.unlink(missing_ok=True)
        if not fresh:
            shutil.copyfile(dest, tmp)
    con = sqlite3.connect(tmp)
    try:
        if progress is not None:
            old = read_meta(con)
            countries, failed, reports = old.get("countries", {}), old.get("failed", {}), progress["reports"]
            log.info("resuming the build in %s: %d of %d countries done (--restart to start over)",
                     tmp, len(progress["done"]), len(plans))
        else:
            if fresh:
                con.executescript(SCHEMA)
            old = read_meta(con)
            countries = {} if fresh else dict(old.get("countries", {}))
            failed, reports = {}, {}
            if not args.only and not fresh:            # a full build: countries no plan has any more go
                for (c,) in con.execute("SELECT DISTINCT country FROM areas").fetchall():
                    if c not in plans:
                        drop_country(con, c)
                        countries.pop(c, None)
            progress = {"run": run, "done": [], "reports": reports}
            save_meta(con, countries=countries, failed=failed, progress=progress)
            con.commit()
        done = set(progress["done"])
        t0 = time.time()
        for ci, (country, plan) in enumerate(plans.items(), 1):
            if country in done:
                continue
            log.info("== %s %s, %.0f min so far", bar(ci - 1, len(plans)), country, (time.time() - t0) / 60)
            report: list[str] = []
            at = [None]

            def units():
                for spec in plan:
                    at[0] = spec
                    yield from sources.stream(spec)

            try:
                countries[country] = write_country(con, country, units(), args.simplify_m, cap_km, report)
                failed.pop(country, None)
            except Exception as e:                     # noqa: BLE001 - reported; the country keeps its areas
                con.rollback()
                spec = at[0] or {}
                log.error("%s: source %s failed: %s", country, spec.get("type"), e)
                failed[country] = [f"{spec.get('type')} {spec.get('key') or spec.get('adm', '')} ({e})"]
                had = con.execute("SELECT count(*) FROM areas WHERE country = ?", (country,)).fetchone()[0]
                report = [f"{country}: FAILED ({'; '.join(failed[country])}); kept the {had} areas it had"]
            finally:
                sources.clear()
            reports[country] = report
            progress["done"].append(country)
            save_meta(con, countries=countries, failed=failed, progress=progress)
            con.commit()
        meta = {"version": 2, "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "levels": {"neighbourhood": LEVEL_NEIGHBOURHOOD, "locality": LEVEL_LOCALITY, "coarse": LEVEL_COARSE},
                "countries": countries, "failed": failed}
        con.execute("DELETE FROM meta")
        save_meta(con, **meta)
        con.commit()
        if fresh or not args.only:
            con.execute("VACUUM")
    finally:
        con.close()
    tmp.replace(dest)
    old_json = dest.with_name("areas.json")             # what build_areas.py wrote before areas.sqlite
    if old_json.exists():
        old_json.unlink()
        log.info("removed %s (replaced by %s)", old_json, dest.name)
    return meta, [line for c in plans for line in reports.get(c, [])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--out", help="default areas.file, else <data_dir>/areas.sqlite")
    ap.add_argument("--cache", help="download cache, default <data_dir>/area-sources")
    ap.add_argument("--sources", help="JSON {country: [source, ...]} replacing those countries' plans")
    ap.add_argument("--only", action="append", help="a country (ISO 3166 alpha-3, repeatable): rebuild only "
                    "these; the other countries' areas already in the output file are kept")
    ap.add_argument("--replace", action="store_true", help="start a new file, dropping every area in the old one")
    ap.add_argument("--restart", action="store_true", help="start over even if an interrupted build of the same "
                    "countries left <out>.tmp to go on from")
    ap.add_argument("--simplify-m", type=float, default=25.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    dest = Path(args.out or cfg.get("areas", {}).get("file") or Path(cfg["data_dir"]) / "areas.sqlite")
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta, report = build(args, cfg, dest)
    total = sum(c["areas"] for c in meta["countries"].values())
    over = sum(c["overCap"] for c in meta["countries"].values())
    lines = report + ["", f"{total} areas in {len(meta['countries'])} countries, {over} over the cap"]
    lines += [f"FAILED {c}: {'; '.join(f)}" for c, f in meta["failed"].items()]
    rep = dest.with_name("areas_report.txt")
    rep.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote %s (%d areas, %d countries failed); report %s", dest, total, len(meta["failed"]), rep)
    return 1 if meta["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
