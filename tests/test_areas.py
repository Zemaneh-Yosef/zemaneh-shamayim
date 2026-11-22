"""build_areas.py + areas.py + serve.py area=auto / finest, without network: every source is a local fixture
placed in the download cache under the name build_areas.py would give the real download.

    python3 tests/test_areas.py [--la PATH_TO_REAL_la-times.geojson]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import shapefile  # noqa: E402  (pyshp)
import sqlite3  # noqa: E402

import areas  # noqa: E402
import build_areas  # noqa: E402
import serve  # noqa: E402
import terrain  # noqa: E402
from common import load_config  # noqa: E402


def sq(s, w, n, e):
    """A closed rectangle ring as (lon, lat) pairs, counter-clockwise."""
    return [(w, s), (e, s), (e, n), (w, n), (w, s)]


def cache_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[1])[-150:]


def write_shp(path: Path, fields, rows):
    """rows: (record values, list of rings)"""
    base = path.parent / "fixture_shp"             # pyshp would read dots in the cache name as an extension
    w = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    for name, typ, size in fields:
        w.field(name, typ, size=size)
    for rec, rings in rows:
        # shapefile outer rings are clockwise
        w.poly([list(reversed(r)) for r in rings[:1]] + [list(r) for r in rings[1:]])
        w.record(*rec)
    w.close()
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for ext in (".shp", ".shx", ".dbf"):
            z.write(str(base) + ext, base.name + ext)


def feature(props, rings):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in r] for r in rings]}}


def write_osm(path: Path):
    import osmium
    from osmium.osm.mutable import Node, Relation, Way
    nodes, ways, rels = [], [], []
    nid = [0]

    def way(wid, ring, tags=None):
        ids = []
        for lon, lat in ring[:-1] if ring[0] == ring[-1] else ring:
            nid[0] += 1
            nodes.append(Node(id=nid[0], location=(lon, lat), version=1))
            ids.append(nid[0])
        if ring[0] == ring[-1]:
            ids.append(ids[0])
        ways.append(Way(id=wid, nodes=ids, tags=tags or {}, version=1))
        return ids

    def rel(rid, tags, members):
        rels.append(Relation(id=rid, members=members, tags={"type": "boundary", **tags}, version=1))

    outer = sq(31.70, 35.10, 31.85, 35.25)
    a_ids = way(101, outer[:3])                     # two halves of the outer ring, sharing end nodes
    b_ids = []
    for lon, lat in outer[3:-1]:
        nid[0] += 1
        nodes.append(Node(id=nid[0], location=(lon, lat), version=1))
        b_ids.append(nid[0])
    ways.append(Way(id=102, nodes=[a_ids[-1]] + b_ids + [a_ids[0]], version=1))
    way(103, sq(31.80, 35.20, 31.81, 35.21))
    rel(1, {"boundary": "administrative", "admin_level": "8", "name": "ירושלים", "name:en": "Jerusalem",
            "source": "Israel Ministry of Interior"}, [("w", 101, "outer"), ("w", 102, "outer"), ("w", 103, "inner")])
    way(104, sq(31.80, 35.21, 31.82, 35.23))
    rel(2, {"boundary": "administrative", "admin_level": "10", "name:en": "Ramat Shlomo"}, [("w", 104, "outer")])
    way(3, sq(31.77, 35.20, 31.78, 35.21), {"place": "neighbourhood", "name:en": "Rehavia"})
    way(105, sq(31.55, 35.05, 31.69, 35.25))
    rel(4, {"boundary": "administrative", "admin_level": "8", "name": "מועצה אזורית גוש עציון",
            "name:en": "Gush Etzion Regional Council"}, [("w", 105, "outer")])
    way(5, sq(31.650, 35.120, 31.660, 35.130), {"place": "village", "name:en": "Alon Shvut"})
    way(106, sq(32.83, 35.07, 32.85, 35.09))
    rel(6, {"boundary": "administrative", "admin_level": "8", "name:en": "Kiryat Motzkin"}, [("w", 106, "outer")])
    way(107, sq(32.82, 35.08, 32.83, 35.10))
    rel(7, {"boundary": "administrative", "admin_level": "8", "name:en": "Kiryat Bialik"}, [("w", 107, "outer")])
    way(108, sq(32.0, 34.9, 32.01, 34.91), {"building": "yes"})        # not a boundary: ignored
    if path.exists():
        path.unlink()
    w = osmium.SimpleWriter(str(path))
    for o in nodes:
        w.add_node(o)
    for o in sorted(ways, key=lambda x: x.id):
        w.add_way(o)
    for o in sorted(rels, key=lambda x: x.id):
        w.add_relation(o)
    w.close()


def make_fixtures(cache: Path, la_src: Path | None):
    cache.mkdir(parents=True, exist_ok=True)
    y = 2024
    tiger = lambda layer, fips: build_areas.TIGER_URL.format(year=y, layer_uc=layer.upper(), fips=fips, layer=layer)
    # one state: "New York" = a box around NYC and Long Island
    write_shp(cache / cache_name(tiger("state", "us")), [("STATEFP", "C", 2), ("NAME", "C", 40)],
              [(("36", "New York"), [sq(40.4, -74.4, 41.3, -72.0)])])
    # places: New York city (covered completely by NTAs -> pruned), a village, a CDP
    write_shp(cache / cache_name(tiger("place", "36")),
              [("GEOID", "C", 7), ("NAME", "C", 40), ("NAMELSAD", "C", 60), ("CLASSFP", "C", 2)],
              [(("3651000", "New York", "New York city", "C1"), [sq(40.70, -74.00, 40.74, -73.80)]),
               (("3630367", "Great Neck Plaza", "Great Neck Plaza village", "C5"), [sq(40.78, -73.73, 40.79, -73.72)]),
               (("3630356", "Great Neck Gardens", "Great Neck Gardens CDP", "U1"), [sq(40.80, -73.73, 40.81, -73.72)])])
    # county subdivision: the Town of North Hempstead around both
    write_shp(cache / cache_name(tiger("cousub", "36")),
              [("GEOID", "C", 10), ("NAME", "C", 40), ("NAMELSAD", "C", 60), ("COUSUBFP", "C", 5)],
              [(("3605951000", "North Hempstead", "North Hempstead town", "51000"),
                [sq(40.76, -73.76, 40.83, -73.68)])])
    # NYC NTAs: two halves of "New York city"
    nta = {"type": "FeatureCollection", "features": [
        feature({"nta2020": "QN0801", "ntaname": "Kew Gardens Hills", "boroname": "Queens"},
                [sq(40.70, -73.90, 40.74, -73.80)]),
        feature({"nta2020": "QN0802", "ntaname": "Forest Hills", "boroname": "Queens"},
                [sq(40.70, -74.00, 40.74, -73.90)])]}
    (cache / "nyc-nta2020.geojson").write_text(json.dumps(nta))
    if la_src and la_src.exists():
        (cache / "la-times-neighborhoods.geojson").write_bytes(la_src.read_bytes())
    else:
        la = {"type": "FeatureCollection", "features": [
            feature({"name": "Encino"}, [sq(34.126, -118.53, 34.186, -118.468)])]}
        (cache / "la-times-neighborhoods.geojson").write_text(json.dumps(la))
    # Israel: an OpenStreetMap extract (.osm.pbf, like Geofabrik's): Jerusalem as a boundary relation of
    # two ways with a hole, two neighbourhoods, a regional council (must be ignored), a village inside it,
    # and two towns (matched by name)
    write_osm(cache / build_areas.GEOFABRIK_URL.rsplit("/", 1)[1])
    # CBS statistical areas 2022 (one page of the service's GeoJSON): Jerusalem's two sub-quarters
    # (11 made of two statistical areas), and Safed without sub-quarters (its statistical areas are used)
    sa = lambda code, town, stat, rova, sub, ring: feature(
        {"SEMEL_YISHUV": code, "SHEM_YISHUV_ENGLISH": town, "SHEM_YISHUV": "", "STAT_2022": stat,
         "ROVA": rova, "TAT_ROVA": sub, "COD_TIFKUD": 1}, [ring])
    cbs = {"type": "FeatureCollection", "features": [
        sa(3000, "JERUSALEM", 111, 1, 11, sq(31.70, 35.10, 31.775, 35.175)),
        sa(3000, "JERUSALEM", 112, 1, 11, sq(31.70, 35.175, 31.775, 35.25)),
        sa(3000, "JERUSALEM", 121, 1, 12, sq(31.775, 35.10, 31.85, 35.25)),
        sa(8000, "ZEFAT", 1, None, None, sq(32.95, 35.48, 32.97, 35.50)),
        sa(8000, "ZEFAT", 2, None, None, sq(32.97, 35.48, 32.99, 35.50)),
        sa(1234, "SMALLTOWN", 1, None, None, sq(32.95, 35.50, 32.97, 35.52)),
        # a regional council's land outside its localities (5500s) and an unnamed area (9900+): ignored
        sa(5526, "MATTE YEHUDA", 1, None, None, sq(31.40, 34.80, 31.60, 35.05)),
        sa(9920, "9920", 1, None, None, sq(31.40, 35.30, 31.45, 35.35)),
        # Dimona: a residential area and a remote industrial one (function code 2), left out of its outline
        dict(sa(2200, "DIMONA", 1, None, None, sq(31.06, 35.02, 31.08, 35.04)),
             properties={"SEMEL_YISHUV": 2200, "SHEM_YISHUV_ENGLISH": "DIMONA", "STAT_2022": 1, "COD_TIFKUD": 1}),
        feature({"SEMEL_YISHUV": 2200, "SHEM_YISHUV_ENGLISH": "DIMONA", "STAT_2022": 2, "COD_TIFKUD": 2},
                [sq(31.00, 35.14, 31.01, 35.15)]),
        # Beit El, where chaiTable's point is wrong (inside Safed)
        sa(3574, "BET EL", 1, None, None, sq(31.93, 35.21, 31.95, 35.23))]}
    (cache / "cbs-statistical-areas-2022-all-0.geojson").write_text(json.dumps(cbs))
    # a geoBoundaries-style country file
    gbf = {"type": "FeatureCollection", "features": [
        feature({"shapeID": "FRA-1", "shapeName": "Paris 4e Arrondissement"}, [sq(48.85, 2.35, 48.86, 2.37)]),
        feature({"shapeID": "FRA-2", "shapeName": "Nantes"}, [sq(47.18, -1.64, 47.29, -1.47)]),
        # a sliver 1 m wide: collapses when simplified to 25 m, must be dropped (not crash the build)
        feature({"shapeID": "FRA-3", "shapeName": "Sliver"},
                [[(2.350, 48.860), (2.355, 48.860), (2.360, 48.860), (2.360, 48.86001),
                  (2.355, 48.86001), (2.350, 48.86001), (2.350, 48.860)]])]}
    (cache / "gb-FRA-ADM5.geojson").write_text(json.dumps(gbf))
    # a country without a plan of its own, by geoBoundaries' metadata alone: its finest level is named like
    # a unit inside municipalities, so the level above it is the municipal one and the finest its neighbourhoods.
    # A big city (~21 km centre to corner) in two sub-municipalities, and a small one (~4 km) in one
    (cache / "gb-KOR-ADM2.geojson").write_text(json.dumps(fc(
        feature({"shapeID": "KOR-B", "shapeName": "Bigcity"}, [sq(37.40, 126.80, 37.70, 127.20)]),
        feature({"shapeID": "KOR-S", "shapeName": "Smallcity"}, [sq(37.00, 127.00, 37.05, 127.06)]))))
    (cache / "gb-KOR-ADM3.geojson").write_text(json.dumps(fc(
        feature({"shapeID": "KOR-B1", "shapeName": "Bigcity West"}, [sq(37.40, 126.80, 37.70, 127.00)]),
        feature({"shapeID": "KOR-B2", "shapeName": "Bigcity East"}, [sq(37.40, 127.00, 37.70, 127.20)]),
        feature({"shapeID": "KOR-S1", "shapeName": "Smallcity Dong"}, [sq(37.00, 127.00, 37.05, 127.06)]))))
    rec = lambda iso, adm, canon: {"boundaryISO": iso, "boundaryType": f"ADM{adm}", "boundaryCanonical": canon,
                                   "boundarySource": f"{iso} statistics office", "boundaryLicense": "CC BY 4.0",
                                   "simplifiedGeometryGeoJSON": f"https://example.invalid/{iso}{adm}.geojson"}
    (cache / "gb-meta.json").write_text(json.dumps(
        [rec("FRA", a, c) for a, c in ((0, "Country"), (4, "Unknown"), (5, "Arrondissement municipal, Commune"))]
        + [rec("KOR", a, c) for a, c in ((0, "Republic of Korea"), (1, "Province"),
                                          (2, "Si, Kun (districts and cities)"), (3, "submunicipalities"))]
        + [rec("PRI", 2, "Municipality")]))                              # covered by TIGER: no plan of its own
    make_canada_fixtures(cache)
    # the big US cities' layers, each in its own field names (away from the test places)
    far = lambda k: [sq(30.0 + k, -100.0, 30.01 + k, -99.99)]
    us = {"us-chi.geojson": {"area_numbe": "50", "community": "WEST RIDGE"},
          "us-hou-0.geojson": {"OBJECTID": 7, "Name": "MEYERLAND AREA"},
          "us-dal-0.geojson": {"OBJECTID": 3, "DISTRICT": "13"},
          "us-phx-0.geojson": {"OBJECTID": 1, "ANID": 9, "NAME": "NORTH MOUNTAIN"},
          "us-sd-0.geojson": {"objectid": 2, "cpcode": 40, "cpname": "TORREY PINES"},
          "us-aus.geojson": {"objectid": "1", "gis_id": "19.0", "planning_area_name": "HANCOCK"},
          "us-sea-0.geojson": {"OBJECTID": 5, "S_HOOD": "Seward Park", "L_HOOD": "Rainier Valley",
                               "S_HOOD_ALT_NAMES": ""},
          "us-bal-0.geojson": {"OBJECTID": 60, "Name": "Cheswolde"}}
    for k, (fname, props) in enumerate(us.items()):
        (cache / fname).write_text(json.dumps(fc(feature(props, far(k)))))


def fc(*feats):
    return {"type": "FeatureCollection", "features": list(feats)}


def write_statcan_csd(path: Path, rows):
    """A zipped shapefile like Statistics Canada's lcsd000b21a_e.zip: EPSG:3347 (Lambert, metres) with its
    .prj, an .xml, no .cpg, and accented names in Windows-1252 (the encoding is found from the bytes).
    rows: (CSDUID, CSDNAME, CSDTYPE, lon/lat ring)."""
    import zipfile
    from rasterio.crs import CRS
    from rasterio.warp import transform
    base = path.parent / "fixture_csd"
    w = shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="cp1252")
    for name, size in (("CSDUID", 7), ("CSDNAME", 100), ("CSDTYPE", 3), ("PRUID", 2)):
        w.field(name, "C", size=size)
    for uid, name, typ, ring in rows:
        xs, ys = transform("EPSG:4326", "EPSG:3347", [p[0] for p in ring], [p[1] for p in ring])
        w.poly([list(reversed(list(zip(xs, ys))))])              # shapefile outer rings are clockwise
        w.record(uid, name, typ, uid[:2])
    w.close()
    with zipfile.ZipFile(path, "w") as z:
        for ext in (".shp", ".shx", ".dbf"):
            z.write(str(base) + ext, "lcsd000b21a_e" + ext)
        z.writestr("lcsd000b21a_e.prj", CRS.from_epsg(3347).to_wkt(morph_to_esri_dialect=True))
        z.writestr("lcsd000b21a_e.xml", "<metadata/>")


def make_canada_fixtures(cache: Path):
    """Canada: Statistics Canada's census subdivision file (projected, for the whole country) and every
    city layer, each in its own field names. Toronto and Halifax are covered completely by their
    neighbourhoods / communities."""
    write_statcan_csd(cache / "lcsd000b21a_e.zip", [
        ("3520005", "Toronto", "C", sq(43.60, -79.60, 43.80, -79.20)),
        ("3519028", "Vaughan", "CY", sq(43.75, -79.60, 43.90, -79.40)),
        ("1209034", "Halifax", "RGM", sq(44.40, -64.00, 45.20, -62.50)),
        ("2466023", "Montréal", "V", sq(45.45, -73.70, 45.60, -73.50)),
        ("2466058", "Côte-Saint-Luc", "V", sq(45.46, -73.68, 45.47, -73.66)),
        ("5915022", "Vancouver", "CY", sq(49.20, -123.22, 49.31, -123.02))])     # far from every place
    (cache / "ca-tor.geojson").write_text(json.dumps(fc(
        feature({"AREA_SHORT_CODE": 34, "AREA_NAME": "Bathurst Manor"}, [sq(43.59, -79.61, 43.81, -79.40)]),
        feature({"AREA_SHORT_CODE": 173, "AREA_NAME": "North Toronto"}, [sq(43.59, -79.40, 43.81, -79.19)]))))
    (cache / "ca-mtl.geojson").write_text(json.dumps(fc(
        feature({"CODEID": 6, "NOM": "Outremont", "TYPE": "Arrondissement"}, [sq(45.51, -73.62, 45.53, -73.59)]),
        feature({"CODEID": 52, "NOM": "Côte-Saint-Luc", "TYPE": "Ville liée"}, [sq(45.46, -73.68, 45.47, -73.66)]))))
    (cache / "ca-hfx-0.geojson").write_text(json.dumps(fc(
        feature({"OBJECTID": 1, "GSA_KEY": 101, "GSA_NAME": "HALIFAX"}, [sq(44.39, -64.01, 45.21, -63.58)]),
        feature({"OBJECTID": 2, "GSA_KEY": 102, "GSA_NAME": "DARTMOUTH"}, [sq(44.39, -63.58, 45.21, -62.49)]))))
    far = lambda k: [sq(60.0 + k, -100.0, 60.01 + k, -99.99)]      # nowhere near the places
    (cache / "ca-ott-0.geojson").write_text(json.dumps(fc(feature({"ONS_ID": 3001, "ONS_Name": "Glebe"}, far(0)))))
    (cache / "ca-ham-0.geojson").write_text(json.dumps(fc(feature(
        {"PLANNING_UNIT": 7, "NEIGHBOURHOOD": "WESTDALE NORTH", "COMMUNITY": "HAMILTON"}, far(1)))))
    (cache / "ca-cgy.geojson").write_text(json.dumps(fc(feature({"comm_code": "BRI", "name": "BRIDLEWOOD"}, far(2)))))
    (cache / "ca-edm.geojson").write_text(json.dumps(fc(feature(
        {"neighbourhood_number": "2010", "name": "ABBOTTSFIELD", "descriptive_name": "Abbottsfield"}, far(3)))))
    (cache / "ca-wpg.geojson").write_text(json.dumps(fc(feature({"id": "18", "name": "Tuxedo"}, far(4)))))


def rows(path: Path) -> dict[str, dict]:
    """Every area in an areas.sqlite, by id (as areas.py decodes them)."""
    idx = areas.AreaIndex(path)
    idx.refresh()
    con = idx.connect()
    try:
        return {a["id"]: a for a in (idx.area(con, r) for (r,) in con.execute("SELECT rowid FROM areas"))}
    finally:
        con.close()


def check_build(tmp: Path, la: Path | None) -> Path:
    cache = tmp / "cache"
    make_fixtures(cache, la)
    (tmp / "config.json").write_text(json.dumps({"data_dir": str(tmp / "data")}))
    base = ["--config", str(tmp / "config.json"), "--cache", str(cache)]
    (tmp / "data").mkdir()
    (tmp / "data" / "areas.json").write_text("{}")                 # what an older build_areas.py wrote
    assert build_areas.main(base) == 0
    assert not (tmp / "data" / "areas.json").exists(), "the old areas.json is removed once areas.sqlite is in"
    out = tmp / "data" / "areas.sqlite"
    by_id = rows(out)
    ids = set(by_id)
    report = (tmp / "data" / "areas_report.txt").read_text(encoding="utf-8")
    print(report)
    meta = areas.AreaIndex(out)
    meta.refresh()
    assert set(meta.meta["countries"]) == {"USA", "CAN", "ISR", "FRA", "KOR"}, meta.meta["countries"]
    assert "PRI" not in meta.meta["countries"], "Puerto Rico comes with the USA's TIGER files"
    assert {a["country"] for a in by_id.values()} == set(meta.meta["countries"])

    # every official unit, nothing pruned: New York city and its NTAs, the village, the CDP, the town
    assert {"nyc-nta:QN0801", "nyc-nta:QN0802", "us-place:3651000"} <= ids, ids
    assert {"us-place:3630367", "us-place:3630356", "us-cousub:3605951000"} <= ids, ids
    assert "la:encino" in ids and by_id["la:encino"]["level"] == areas.LEVEL_NEIGHBOURHOOD
    # Israel: CBS sub-quarters and Safed's statistical areas, every locality outline, the OSM municipalities
    # and villages; never a regional council, council land or an unnamed area, nor OSM's neighbourhoods
    assert {"cbs-subq:3000-11", "cbs-subq:3000-12", "cbs-stat:8000-1", "cbs-stat:8000-2"} <= ids, ids
    assert not any(i.startswith("cbs-stat:1234") for i in ids), ids
    assert {"cbs-loc:1234", "cbs-loc:8000", "cbs-loc:3000", "cbs-loc:3574"} <= ids, ids
    assert not {"cbs-loc:5526", "cbs-loc:9920"} & ids, "council land and unnamed areas are not localities"
    assert by_id["cbs-loc:2200"]["bbox"] == [31.06, 35.02, 31.08, 35.04], "Dimona without its industrial zone"
    assert {"osm:r1", "osm:w5", "osm:r6", "osm:r7"} <= ids and not {"osm:r2", "osm:w3", "osm:r4"} & ids, ids
    q11 = by_id["cbs-subq:3000-11"]
    assert len(q11["polygons"]) == 2 and q11["name"] == "Jerusalem, sub-quarter 11", q11
    # France: geoBoundaries with the kind chosen by hand and the source from the metadata; slivers dropped
    assert {"gb-FRA5:FRA-1", "gb-FRA5:FRA-2"} <= ids and "gb-FRA5:FRA-3" not in ids, ids
    assert "dropped Sliver" in report, "slivers are dropped and reported"
    assert by_id["gb-FRA5:FRA-2"]["kind"] == "commune", by_id["gb-FRA5:FRA-2"]
    assert by_id["gb-FRA5:FRA-2"]["source"] == "FRA statistics office (via geoBoundaries; CC BY 4.0)"
    # Korea, by the rule: the municipal level and its sub-municipalities as neighbourhoods
    assert by_id["gb-KOR2:KOR-B"]["level"] == areas.LEVEL_LOCALITY and by_id["gb-KOR2:KOR-B"]["kind"].startswith("Si, Kun")
    assert by_id["gb-KOR3:KOR-B1"]["level"] == areas.LEVEL_NEIGHBOURHOOD, by_id["gb-KOR3:KOR-B1"]
    assert build_areas.default_levels({1: {"boundaryCanonical": "Province"}, 2: {"boundaryCanonical": "Unknown"},
                                       3: {"boundaryCanonical": "Ward"}}) == (2, 3)
    assert build_areas.default_levels({1: {"boundaryCanonical": "Province"},
                                       2: {"boundaryCanonical": "Municipalities"}}) == (2, None)
    assert build_areas.default_levels({0: {"boundaryCanonical": "Gibraltar"}}) == (0, None)
    # geoBoundaries files are read a feature at a time: the same features, whatever the chunks they are read in
    whole = json.loads((cache / "gb-KOR-ADM3.geojson").read_text())["features"]
    for chunk in (5, 16, 97, 1 << 20):
        assert list(build_areas.iter_geojson_features(cache / "gb-KOR-ADM3.geojson", chunk)) == whole, chunk
    (tmp / "cut.geojson").write_text(json.dumps(fc(*whole))[:-40])
    try:
        list(build_areas.iter_geojson_features(tmp / "cut.geojson", 16))
        raise AssertionError("a cut-off file must fail")
    except ValueError:
        pass

    # Canada: the whole country (Vancouver too, far from any place), names, kinds and reprojection
    assert {"ca-tor:34", "ca-tor:173", "ca-hfx:101", "ca-hfx:102", "statcan-csd:3519028",
            "statcan-csd:3520005", "statcan-csd:1209034", "statcan-csd:5915022"} <= ids, ids
    assert by_id["ca-tor:34"]["name"] == "Bathurst Manor, Toronto" and by_id["ca-tor:34"]["level"] == 30
    assert by_id["ca-hfx:101"]["name"] == "Halifax, Halifax Regional Municipality", by_id["ca-hfx:101"]
    assert by_id["statcan-csd:3519028"]["name"] == "Vaughan, ON"
    assert by_id["statcan-csd:3519028"]["kind"] == "city (CY)", by_id["statcan-csd:3519028"]
    assert "ca-mtl:6" in ids and "ca-mtl:52" not in ids and "statcan-csd:2466058" in ids, ids
    assert by_id["statcan-csd:2466058"]["name"] == "Côte-Saint-Luc, QC", by_id["statcan-csd:2466058"]["name"]
    assert by_id["statcan-csd:2466023"]["name"] == "Montréal, QC", by_id["statcan-csd:2466023"]["name"]
    assert build_areas.dbf_encoding(b"\0" * 8 + (32).to_bytes(2, "little") + b"\0" * 22 + "Lévis".encode()) == "utf-8"
    assert (tmp / "cache" / "lcsd000b21a_e-unzipped" / "x.shp").exists(), "unpacked once, read from disk"
    s_, w, n, e = by_id["statcan-csd:3519028"]["bbox"]                   # reprojected back to lon/lat
    assert abs(s_ - 43.75) < 1e-4 and abs(w + 79.60) < 1e-3 and abs(n - 43.90) < 1e-4 and abs(e + 79.40) < 1e-3, (s_, w, n, e)
    assert "FAILED" not in report, report
    feats = json.loads((tmp / "cache" / "ca-ham-0.geojson").read_text())["features"]
    ham = build_areas.city_units(feats, next(s for s in build_areas.COUNTRY_PLANS["CAN"] if s.get("key") == "ca-ham"))
    assert [u.name for u in ham] == ["Westdale North, Hamilton"], [u.name for u in ham]
    try:
        build_areas.city_units(feats, {"key": "x", "id_fields": ["nope"], "name_fields": ["NEIGHBOURHOOD"]})
        raise AssertionError("a missing id field must fail the source")
    except ValueError as e:
        assert "PLANNING_UNIT" in str(e), e
    us_names = {}
    for spec in build_areas.US_CITIES:
        f = json.loads((cache / (spec["key"] + ("-0.geojson" if "arcgis" in spec else ".geojson"))).read_text())
        us_names.update({u.id: u.name for u in build_areas.city_units(f["features"], spec)})
    assert us_names == {"us-chi:50": "West Ridge, Chicago", "us-hou:7": "Meyerland Area, Houston",
                        "us-dal:13": "Council District 13, Dallas", "us-phx:9": "North Mountain, Phoenix",
                        "us-sd:40": "Torrey Pines, San Diego", "us-aus:1": "Hancock, Austin",
                        "us-sea:5": "Seward Park, Seattle", "us-bal:60": "Cheswolde, Baltimore"}, us_names

    check_streaming(cache)
    check_resume(tmp, base, ids)

    # --only rebuilds some countries and keeps the rest; --replace writes only those countries
    assert build_areas.main(base + ["--only", "CAN"]) == 0
    assert set(rows(out)) == ids, "--only CAN must not drop the other countries"
    # a country whose source fails keeps the areas it had (and the build says so)
    (tmp / "bad.json").write_text(json.dumps({"FRA": [{"type": "file", "path": str(tmp / "missing.geojson")}]}))
    assert build_areas.main(base + ["--only", "FRA", "--sources", str(tmp / "bad.json")]) == 1
    assert set(rows(out)) == ids, "FRA's areas kept when its source failed"
    rep = (tmp / "data" / "areas_report.txt").read_text(encoding="utf-8")
    assert "FRA: FAILED" in rep and "kept the 2 areas it had" in rep, rep
    only = tmp / "data" / "only-canada.sqlite"
    assert build_areas.main(base + ["--only", "CAN", "--replace", "--out", str(only)]) == 0
    canada = set(rows(only))
    assert canada and all(i.startswith(("ca-", "statcan-")) for i in canada), canada
    try:
        build_areas.main(base + ["--only", "Canada"])
        raise AssertionError("--only takes country codes")
    except SystemExit as e:
        assert "ISO 3166" in str(e), e
    return out


def check_streaming(cache: Path):
    """The big sources are generators that keep nothing: a country's boundaries are never all in memory."""
    import inspect
    src = build_areas.Sources(build_areas.Fetcher(cache, ""), {})
    for spec in ({"type": "tiger", "layer": "place"}, {"type": "tiger", "layer": "cousub"}, {"type": "statcan-csd"},
                 {"type": "geoboundaries", "iso": "KOR", "adm": 3, "kind": "x"}):
        g = src.stream(spec)
        assert inspect.isgenerator(g), spec
        assert next(g).id and not [k for k in src.cache if k != "us-states"], (spec, list(src.cache))
        g.close()
    assert src.us_states() == {"36": "New York"}, "the states' names only, not their boundaries"
    assert len(src.units({"type": "tiger", "layer": "place"})) == 3          # still whole when asked for
    print("streaming ok")


class Killed(BaseException):
    """What the kernel's out-of-memory kill is to a build: nothing after it runs (not an Exception)."""


def check_resume(tmp: Path, base: list, ids: set):
    """A build killed part-way goes on from the next country when started again; --restart starts over."""
    out = tmp / "data" / "resume.sqlite"
    args = base + ["--out", str(out)]
    streamed = []
    real = build_areas.Sources.stream

    def stream(self, spec, kill_at=[None]):
        country = {"nyc-nta": "USA", "statcan-csd": "CAN", "cbs-subquarters": "ISR"}.get(spec["type"]) \
            or spec.get("iso")
        if country:
            streamed.append(country)
        if country and country == kill_at[0]:
            raise Killed()
        return real(self, spec)

    build_areas.Sources.stream = stream
    try:
        stream.__defaults__[0][0] = "FRA"
        try:
            build_areas.main(args)
            raise AssertionError("the build should have been killed")
        except Killed:
            pass
        assert streamed == ["USA", "CAN", "ISR", "FRA"], streamed
        assert not out.exists() and (tmp / "data" / "resume.sqlite.tmp").exists()
        con = sqlite3.connect(tmp / "data" / "resume.sqlite.tmp")
        done = json.loads(con.execute("SELECT value FROM meta WHERE key = 'progress'").fetchone()[0])["done"]
        con.close()
        assert done == ["USA", "CAN", "ISR"], done
        stream.__defaults__[0][0] = None
        streamed.clear()
        assert build_areas.main(args) == 0
        assert list(dict.fromkeys(streamed)) == ["FRA", "KOR"], f"went on from FRA: {streamed}"
        assert set(rows(out)) == ids, "the resumed build has every area"
        assert not (tmp / "data" / "resume.sqlite.tmp").exists()
        report = (tmp / "data" / "areas_report.txt").read_text(encoding="utf-8")
        assert report.index("USA: ") < report.index("ISR: ") < report.index("KOR: "), "the whole run's report"
        meta = areas.AreaIndex(out)
        meta.refresh()
        assert "progress" not in meta.meta and set(meta.meta["countries"]) == {"USA", "CAN", "ISR", "FRA", "KOR"}
        # a leftover from another run (here: other countries) is not resumed; --restart never resumes
        stream.__defaults__[0][0] = "ISR"
        try:
            build_areas.main(args + ["--only", "USA", "--only", "ISR"])
        except Killed:
            pass
        stream.__defaults__[0][0] = None
        streamed.clear()
        assert build_areas.main(args) == 0 and streamed[0] == "USA", streamed
        stream.__defaults__[0][0] = "CAN"
        try:
            build_areas.main(args)
        except Killed:
            pass
        stream.__defaults__[0][0] = None
        streamed.clear()
        assert build_areas.main(args + ["--restart"]) == 0 and streamed[0] == "USA", streamed
    finally:
        build_areas.Sources.stream = real
    print("resume ok")


def check_lookup(path: Path):
    idx = areas.AreaIndex(path)
    fin = lambda la, lo: (idx.find(la, lo, "finest") or {}).get("id")
    city = lambda la, lo: (idx.find(la, lo, "city") or {}).get("id")
    # finest: the finest unit
    assert fin(40.72, -73.85) == "nyc-nta:QN0801"
    assert fin(40.72, -73.95) == "nyc-nta:QN0802"
    assert fin(40.785, -73.725) == "us-place:3630367"         # village beats the town around it
    assert fin(40.77, -73.75) == "us-cousub:3605951000"       # outside any village / CDP: the town
    assert fin(34.15, -118.50) == "la:encino"
    assert fin(31.81, 35.22) == "cbs-subq:3000-12"            # CBS sub-quarter
    assert fin(31.75, 35.20) == "cbs-subq:3000-11"            # the second statistical area of sub-quarter 11
    assert fin(32.96, 35.49) == "cbs-stat:8000-1"             # Safed: statistical area
    assert fin(31.655, 35.125) == "osm:w5"                    # village
    assert fin(37.50, 126.90) == "gb-KOR3:KOR-B1"
    assert fin(37.02, 127.02) == "gb-KOR3:KOR-S1"
    # city: a settlement up to Jerusalem's size whole; a bigger one by its neighbourhoods
    assert city(40.72, -73.85) == city(40.72, -73.95) == "us-place:3651000"     # this "New York" is 8.7 km
    assert city(40.785, -73.725) == "us-place:3630367"
    assert city(40.77, -73.75) == "us-cousub:3605951000"
    assert city(31.81, 35.22) == city(31.75, 35.20) == "cbs-loc:3000"           # all Jerusalem, one area
    assert city(32.96, 35.49) == "cbs-loc:8000"
    assert city(43.70, -79.50) == "ca-tor:34"                 # Toronto (~20 km) by its neighbourhoods
    assert city(43.85, -79.50) == "statcan-csd:3519028"       # Vaughan (~12 km), north of Toronto: whole
    assert city(45.00, -63.80) == "ca-hfx:101"                # Halifax (~70 km) by its communities
    assert city(37.50, 126.90) == "gb-KOR3:KOR-B1" and city(37.50, 127.10) == "gb-KOR3:KOR-B2"
    assert city(37.02, 127.02) == "gb-KOR2:KOR-S"             # the small city whole, not its one dong
    assert city(49.25, -123.10) == "statcan-csd:5915022"      # no neighbourhoods: the city itself
    assert idx.find(43.70, -79.50, "city", 30.0)["id"] == "statcan-csd:3520005", "a bigger city_radius_km"
    for f in (fin, city):
        assert f(31.60, 35.10) is None                        # regional council only
        assert f(0, 0) is None
    try:
        idx.find(0, 0, "nearest")
        raise AssertionError("an unknown rule must fail")
    except ValueError:
        pass
    print(f"lookup ok ({len(idx)} areas)")


def check_server(tmp: Path, path: Path):
    cfg_path = tmp / "srv.json"
    cfg_path.write_text(json.dumps({"data_dir": str(tmp / "data"), "areas": {"file": str(path)},
                                    "terrain": {"tiles_dir": str(tmp / "tiles"), "max_area_radius_km": 30,
                                                "worker_processes": False}}))
    cfg = load_config(str(cfg_path))
    calls = []

    def fake_horizon_set(tiles, lat, lon, **kw):
        calls.append((lat, lon, kw.get("bbox"), kw.get("radius_km")))
        return {"mode": "vantage" if kw.get("bbox") or kw.get("radius_km") else "point", "points": []}

    terrain.horizon_set = fake_horizon_set
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = serve.Horizons(cfg)
    serve.Handler.areas = areas.AreaIndex(path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    stored = rows(path)

    def get(u):
        r = urllib.request.urlopen(urllib.request.Request(base + u, headers={"Accept-Encoding": "gzip"}))
        body = r.read()
        return json.loads(gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body)

    def status(u):
        try:
            return urllib.request.urlopen(base + u).status
        except urllib.error.HTTPError as e:
            return e.code

    # /v1/area: the city rule unless area=finest
    a = get("/v1/area?lat=40.72&lon=-73.85")["area"]
    assert a["id"] == "us-place:3651000" and "polygons" not in a and a["country"] == "USA", a
    assert get("/v1/area?lat=40.72&lon=-73.85&area=finest")["area"]["id"] == "nyc-nta:QN0801"
    assert status("/v1/area?lat=0&lon=0") == 404
    assert status("/v1/area?lat=40.72&lon=-73.85&area=nearest") == 400
    # two different points in the same NTA -> the same computation (cached once), over the NTA's box
    h1 = get("/v1/horizon?lat=40.72&lon=-73.85&area=finest")
    h2 = get("/v1/horizon?lat=40.73&lon=-73.82&area=finest&radius_km=0.8")
    assert h1["area"]["id"] == h2["area"]["id"] == "nyc-nta:QN0801"
    assert len(calls) == 1, calls
    assert calls[0][2] == [40.7, -73.9, 40.74, -73.8], calls
    # area=auto: the whole (small) city, one computation over its box, for both NTAs' points
    ha = get("/v1/horizon?lat=40.72&lon=-73.85&area=auto")
    hb = get("/v1/horizon?lat=40.72&lon=-73.95&area=city")
    assert ha["area"]["id"] == hb["area"]["id"] == "us-place:3651000", ha["area"]
    assert len(calls) == 2 and calls[-1][2] == [40.7, -74.0, 40.74, -73.8], calls
    # outline=1: the same area (and the same cached computation) with its polygons as stored, so a client can
    # test later fixes against the real boundary; without it the answer is as before, polygons and all absent
    nta = stored["nyc-nta:QN0801"]
    h1o = get("/v1/horizon?lat=40.72&lon=-73.85&area=finest&outline=1")
    assert len(calls) == 2, calls
    assert h1o["area"] == nta and h1o["area"]["polygons"] == nta["polygons"], h1o["area"]
    assert {k: v for k, v in h1o["area"].items() if k != "polygons"} == h1["area"] == areas.public(nta)
    assert {k: v for k, v in h1o.items() if k != "area"} == {k: v for k, v in h1.items() if k != "area"}
    assert areas.area_contains(h1o["area"], 40.72, -73.85) and not areas.area_contains(h1o["area"], 40.72, -73.95)
    raw = lambda u: urllib.request.urlopen(base + u).read()
    assert raw("/v1/horizon?lat=40.72&lon=-73.85&area=finest") == raw("/v1/horizon?lat=40.73&lon=-73.82&area=finest")
    assert b"polygons" not in raw("/v1/horizon?lat=40.72&lon=-73.85&area=auto")
    assert get("/v1/horizon?lat=31.60&lon=35.10&area=auto&outline=1")["area"] is None    # no area: nothing to outline
    assert get("/v1/area?lat=40.72&lon=-73.85&area=finest&outline=1")["area"] == nta
    tv = get("/v1/terrain-version?lat=40.72&lon=-73.85&area=finest")
    assert "area" not in tv, tv
    tvo = get("/v1/terrain-version?lat=40.72&lon=-73.85&area=finest&outline=1")
    assert tvo["area"] == nta and {k: v for k, v in tvo.items() if k != "area"} == tv, tvo
    # without area: the plain answer (this once failed with a 500 for every place)
    tvp = get("/v1/terrain-version?lat=40.72&lon=-73.85")
    assert set(tvp) == {"fine", "layers", "algo", "base"}, tvp
    assert status("/v1/horizon?lat=40.72&lon=-73.85&area=nearest") == 400
    # Jerusalem: one area by the city rule; sub-quarter 12 (9 km from centre to corner) by area=finest, over
    # the user-bbox cap (3 km) but within the area cap
    hj = get("/v1/horizon?lat=31.80&lon=35.15&area=auto")
    assert hj["area"]["id"] == "cbs-loc:3000" and calls[-1][2] == [31.7, 35.1, 31.85, 35.25], calls[-1]
    h3 = get("/v1/horizon?lat=31.80&lon=35.15&area=finest")
    assert h3["area"]["id"] == "cbs-subq:3000-12" and calls[-1][2] == [31.775, 35.1, 31.85, 35.25], calls[-1]
    # no area: the request as given (here a 0.8 km radius around the point)
    h4 = get("/v1/horizon?lat=31.60&lon=35.10&area=auto&radius_km=0.8")
    assert h4["area"] is None and "no official area" in h4["areaNote"], h4
    assert calls[-1][:2] == (31.6, 35.1) and calls[-1][3] == 0.8, calls[-1]
    # a client's own bbox still has the 3 km cap
    assert status("/v1/horizon?bbox=31.7,35.1,31.85,35.25") == 400
    # over the area cap: falls back
    serve.Handler.store.cfg["terrain"]["max_area_radius_km"] = 5
    h5 = get("/v1/horizon?lat=31.80&lon=35.15&area=finest")
    assert h5["area"] is None and "over terrain.max_area_radius_km" in h5["areaNote"], h5
    st = get("/v1/status")
    assert st["areas"]["count"] == len(serve.Handler.areas) == len(stored), st
    srv.shutdown()
    print("server ok:", len(calls), "horizon computations")


def check_reload(tmp: Path, path: Path):
    """A rebuilt file is swapped in whole (even on Windows: no connection is left open); the index rereads it."""
    copy = tmp / "reload.sqlite"
    copy.write_bytes(path.read_bytes())
    idx = areas.AreaIndex(copy)
    assert idx.find(40.72, -73.85, "finest")["id"] == "nyc-nta:QN0801"
    con = sqlite3.connect(copy)
    con.execute("DELETE FROM areas_box WHERE id IN (SELECT rowid FROM areas WHERE id LIKE 'nyc-nta:%')")
    con.execute("DELETE FROM areas WHERE id LIKE 'nyc-nta:%'")
    con.commit()
    con.close()
    new = tmp / "reload.new"
    new.write_bytes(copy.read_bytes())
    new.replace(copy)
    assert idx.find(40.72, -73.85, "finest")["id"] == "us-place:3651000", "reopened after the swap"
    print("reload ok")


def check_download_progress():
    """Big downloads log a progress bar and still return every byte."""
    import io

    class R(io.BytesIO):
        headers = {"Content-Length": str(25_000_000)}
    body = bytes(range(256)) * (25_000_000 // 256) + b"x" * (25_000_000 % 256)
    assert build_areas.read_with_progress(R(body), "test") == body
    print("download progress ok")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--la", help="real los-angeles.geojson (LA Times Mapping L.A.) to test with")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        path = check_build(tmp, Path(args.la) if args.la else None)
        check_lookup(path)
        check_server(tmp, path)
        check_reload(tmp, path)
        check_download_progress()
    print("all area tests passed")


if __name__ == "__main__":
    main()
