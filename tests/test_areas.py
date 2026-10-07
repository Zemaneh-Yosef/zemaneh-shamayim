"""build_areas.py + areas.py + serve.py area=auto, without network: every source is a local fixture
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
    make_canada_fixtures(cache)
    # the big US cities' layers, each in its own field names (away from the test places)
    far = lambda k: [sq(30.0 + k, -100.0, 30.01 + k, -99.99)]
    us = {"us-chi.geojson": {"area_numbe": "50", "community": "WEST RIDGE"},
          "us-hou-0.geojson": {"OBJECTID": 7, "Name": "MEYERLAND AREA"},
          "us-dal-0.geojson": {"OBJECTID": 3, "DISTRICT": "13"},
          "us-phx-0.geojson": {"OBJECTID": 1, "ANID": 9, "NAME": "NORTH MOUNTAIN"},
          "us-sd-0.geojson": {"OBJECTID": 2, "CPCODE": 40, "CPNAME": "TORREY PINES"},
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


CA_BOXES = {"Toronto_area_ON": (43.62, -79.58, 43.78, -79.22), "Halifax_area_NS": (44.60, -63.70, 44.70, -63.55),
            "Montreal_area_QC": (45.455, -73.69, 45.59, -73.51)}


CHAI = [
    {"info": {"title": "USA"}, "metroAreas": [
        {"name": "Queens_area_NY", "bounds": {"n": 40.745, "s": 40.695, "e": -73.79, "w": -74.01}},
        {"name": "Great_Neck_area_NY", "bounds": {"n": 40.82, "s": 40.77, "e": -73.70, "w": -73.75}},
        {"name": "Encino_point_CA", "bounds": {"n": 34.15, "s": 34.15, "e": -118.50, "w": -118.50}}]},
    {"info": {"title": "Eretz Yisrael (Neighborhoods)"}, "metroAreas": [
        {"name": "Jerusalem", "bounds": {"n": 31.86, "s": 31.69, "e": 35.26, "w": 35.09}},
        {"name": "Safed", "bounds": {"n": 32.99, "s": 32.95, "e": 35.52, "w": 35.48}}]},
    {"info": {"title": "Eretz Yisrael (Cities)"}, "metroAreas": [
        {"name": "Alon Shvut", "bounds": {"n": 31.6551, "s": 31.6550, "e": 35.1251, "w": 35.1250}},
        {"name": "Kfar Etzion", "bounds": {"n": 31.6001, "s": 31.6000, "e": 35.1001, "w": 35.1000}},
        {"name": "Kiriat-yam-mozkin-bialik", "bounds": {"n": 0, "s": 0, "e": 0, "w": 0}},
        {"name": "Smalltown", "bounds": {"n": 30.0001, "s": 30.0, "e": 35.0001, "w": 35.0}},   # wrong point
        {"name": "Zefat", "bounds": {"n": 0, "s": 0, "e": 0, "w": 0}},                        # no location
        {"name": "Smalltown point", "bounds": {"n": 32.9601, "s": 32.96, "e": 35.5101, "w": 35.51}},
        {"name": "Beit El", "bounds": {"n": 32.9601, "s": 32.96, "e": 35.4901, "w": 35.49}},     # inside Safed
        {"name": "Dimonah", "bounds": {"n": 31.0701, "s": 31.07, "e": 35.0301, "w": 35.03}},
        {"name": "Tzuba", "bounds": {"n": 31.5001, "s": 31.50, "e": 34.9001, "w": 34.90}},       # council land
        {"name": "Nowhere", "bounds": {"n": 0, "s": 0, "e": 0, "w": 0}},
        {"name": "Smalltown Old", "bounds": {"n": 31.7701, "s": 31.77, "e": 35.2101, "w": 35.21}}]},   # wrong point
    {"info": {"title": "France"}, "metroAreas": [
        {"name": "Paris", "bounds": {"n": 48.87, "s": 48.84, "e": 2.38, "w": 2.34}},
        {"name": "Nantes", "bounds": {"n": 0, "s": 0, "e": 0, "w": 0}}]},
    {"info": {"title": "Canada"}, "metroAreas": [
        {"name": k, "bounds": {"s": b[0], "w": b[1], "n": b[2], "e": b[3]}} for k, b in CA_BOXES.items()]},
]

OVERRIDES = {"_extra": {"USA": [{"name": "Gardens_area_NY", "point": [40.805, -73.725]}]},   # not in chaiTable
             "Eretz Yisrael (Cities)/Smalltown Old": {"names": [["Nonexistent Spelling", "Smalltown"]]},
             "Eretz Yisrael (Cities)/Kiriat-yam-mozkin-bialik": {"names": ["Kiryat Yam", "Kiryat Motzkin", "Kiryat Bialik"]},
             "France/Nantes": {"names": ["Nantes"]}}


def check_build(tmp: Path, la: Path | None) -> Path:
    cache = tmp / "cache"
    make_fixtures(cache, la)
    (tmp / "chai.json").write_text(json.dumps(CHAI))
    (tmp / "ov.json").write_text(json.dumps(OVERRIDES))
    (tmp / "config.json").write_text(json.dumps({"data_dir": str(tmp / "data")}))
    assert build_areas.main(["--config", str(tmp / "config.json"), "--chai", str(tmp / "chai.json"),
                             "--cache", str(cache), "--overrides", str(tmp / "ov.json")]) == 0
    out = tmp / "data" / "areas.json"
    data = json.loads(out.read_text())
    ids = {a["id"] for a in data["areas"]}
    report = (tmp / "data" / "areas_report.txt").read_text()
    print(report)

    # NYC: the NTAs replace "New York city" (fully covered); Great Neck: village + CDP + the town around them
    assert {"nyc-nta:QN0801", "nyc-nta:QN0802"} <= ids, ids
    assert "us-place:3651000" not in ids, "New York city should be pruned: its NTAs cover it"
    assert {"us-place:3630367", "us-place:3630356", "us-cousub:3605951000"} <= ids, ids
    assert "la:encino" in ids, ids
    # Israel: CBS sub-quarters for the Neighborhoods list (Safed: statistical areas, a small town: none);
    # OpenStreetMap neighbourhoods are not used; never the regional council
    assert {"cbs-subq:3000-11", "cbs-subq:3000-12", "cbs-stat:8000-1", "cbs-stat:8000-2"} <= ids, ids
    assert not any(i.startswith("cbs-stat:1234") for i in ids), ids
    # CBS locality outlines: Smalltown by its point, by name (a wrong point), Zefat by name (no location)
    assert "cbs-loc:1234" in ids and "cbs-loc:8000" in ids, ids
    assert "Smalltown: the chaiTable point is in no official unit; matched by name -> Smalltown" in report
    assert "Zefat: no location in chaiTable.json; matched by name -> Zefat" in report
    assert "cbs-loc:3000" not in ids, "Jerusalem's outline is covered by its sub-quarters"
    assert not {"cbs-loc:5526", "cbs-loc:9920"} & ids, "council land and unnamed areas are not localities"
    assert any(u["place"] == "Eretz Yisrael (Cities)/Tzuba" for u in data["unresolved"]), data["unresolved"]
    assert "cbs-loc:3574" in ids and "the chaiTable point is in Zefat, but the name matches Bet El" in report
    dim = next(a for a in data["areas"] if a["id"] == "cbs-loc:2200")
    assert dim["bbox"] == [31.06, 35.02, 31.08, 35.04], dim["bbox"]
    assert {"osm:w5", "osm:r6", "osm:r7"} <= ids and not {"osm:r2", "osm:w3"} & ids, ids
    assert "osm:r1" not in ids, "Jerusalem is covered by its sub-quarters, so the city itself is dropped"
    assert "osm:r4" not in ids, "regional councils must not become areas"
    unresolved = {u["place"]: u["reason"] for u in data["unresolved"]}
    assert "Eretz Yisrael (Cities)/Nowhere" in unresolved, unresolved
    assert "Eretz Yisrael (Cities)/Kfar Etzion" in unresolved, "only a regional council there: unresolved"
    assert "USA/Gardens_area_NY" in next(a for a in data["areas"] if a["id"] == "us-place:3630356")["places"], \
        "an _extra place gets its official unit like a chaiTable place"
    assert "Nonexistent Spelling / Smalltown -> Smalltown (1.0)" in report, "best of the alternative spellings"
    assert "Eretz Yisrael (Cities)/Smalltown Old" in next(a for a in data["areas"] if a["id"] == "cbs-loc:1234")["places"]
    assert "Kiryat Yam -> NO MATCH" in unresolved["Eretz Yisrael (Cities)/Kiriat-yam-mozkin-bialik"]
    assert "gb-FRA5:FRA-2" in ids and "gb-FRA5:FRA-1" in ids, ids
    assert "gb-FRA5:FRA-3" not in ids and "dropped Sliver" in report, "slivers are dropped and reported"
    q11 = next(a for a in data["areas"] if a["id"] == "cbs-subq:3000-11")
    assert len(q11["polygons"]) == 2 and q11["name"] == "Jerusalem, sub-quarter 11", q11
    assert q11["places"] == ["Eretz Yisrael (Neighborhoods)/Jerusalem"]

    # Canada: Toronto and Halifax replaced by their neighbourhoods / communities; Vaughan stays a CSD;
    # Montreal by its arrondissements, its villes liées come from StatCan (not twice)
    by_id = {a["id"]: a for a in data["areas"]}
    assert {"ca-tor:34", "ca-tor:173", "ca-hfx:101", "ca-hfx:102", "statcan-csd:3519028"} <= ids, ids
    assert "statcan-csd:3520005" not in ids and "statcan-csd:1209034" not in ids, "covered by finer units"
    assert by_id["ca-tor:34"]["name"] == "Bathurst Manor, Toronto" and by_id["ca-tor:34"]["level"] == 30
    assert by_id["ca-hfx:101"]["name"] == "Halifax, Halifax Regional Municipality", by_id["ca-hfx:101"]
    assert by_id["statcan-csd:3519028"]["name"] == "Vaughan, ON"
    assert by_id["statcan-csd:3519028"]["kind"] == "city (CY)", by_id["statcan-csd:3519028"]
    assert "ca-mtl:6" in ids and "ca-mtl:52" not in ids and "statcan-csd:2466058" in ids, ids
    assert by_id["statcan-csd:2466058"]["name"] == "Côte-Saint-Luc, QC", by_id["statcan-csd:2466058"]["name"]
    assert by_id["statcan-csd:2466023"]["name"] == "Montréal, QC", by_id["statcan-csd:2466023"]["name"]
    assert build_areas.dbf_encoding(b"\0" * 8 + (32).to_bytes(2, "little") + b"\0" * 22 + "Lévis".encode()) == "utf-8"
    assert (tmp / "cache" / "lcsd000b21a_e-unzipped" / "x.shp").exists(), "unpacked once, read from disk"
    assert "statcan-csd:5915022" not in ids, "census subdivisions far from every place are left out"
    s, w, n, e = by_id["statcan-csd:3519028"]["bbox"]                    # reprojected back to lon/lat
    assert abs(s - 43.75) < 1e-4 and abs(w + 79.60) < 1e-3 and abs(n - 43.90) < 1e-4 and abs(e + 79.40) < 1e-3, (s, w, n, e)
    assert "statcan-csd:2466023" in ids, "Montréal city: its arrondissements cover only part of it here"
    # the other cities' layers loaded with their own field names (no source failed)
    assert "failed" not in report and "UNRESOLVED Canada" not in report, report
    feats = json.loads((tmp / "cache" / "ca-ham-0.geojson").read_text())["features"]
    ham = build_areas.city_units(feats, next(s for s in build_areas.PLANS["Canada"] if s.get("key") == "ca-ham"))
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
    assert "failed" not in (tmp / "data" / "areas_report.txt").read_text()

    # --only rebuilds some countries and keeps the rest of the file; --replace writes only those countries
    common = ["--config", str(tmp / "config.json"), "--chai", str(tmp / "chai.json"), "--cache", str(cache),
              "--overrides", str(tmp / "ov.json"), "--only", "Canada"]
    assert build_areas.main(common) == 0
    again = json.loads(out.read_text())
    assert {a["id"] for a in again["areas"]} == ids, "--only Canada must not drop the other countries"
    assert again["unresolved"] == [u for u in data["unresolved"] if not u["place"].startswith("Canada/")] + \
        [u for u in data["unresolved"] if u["place"].startswith("Canada/")]
    assert next(a for a in again["areas"] if a["id"] == "nyc-nta:QN0801")["places"] == \
        next(a for a in data["areas"] if a["id"] == "nyc-nta:QN0801")["places"]
    only = tmp / "data" / "only-canada.json"
    (only).write_text(out.read_text())
    assert build_areas.main(common + ["--replace", "--out", str(only)]) == 0
    canada = {a["id"] for a in json.loads(only.read_text())["areas"]}
    assert canada and all(i.startswith(("ca-", "statcan-")) for i in canada), canada
    return out


def check_lookup(path: Path):
    idx = areas.AreaIndex(path)
    f = lambda la, lo: (idx.find(la, lo) or {}).get("id")
    assert f(40.72, -73.85) == "nyc-nta:QN0801"
    assert f(40.72, -73.95) == "nyc-nta:QN0802"
    assert f(40.785, -73.725) == "us-place:3630367"           # village beats the town around it
    assert f(40.77, -73.75) == "us-cousub:3605951000"         # outside any village / CDP: the town
    assert f(34.15, -118.50) == "la:encino"
    assert f(31.81, 35.22) == "cbs-subq:3000-12"              # CBS sub-quarter
    assert f(31.75, 35.20) == "cbs-subq:3000-11"              # the second statistical area of sub-quarter 11
    assert f(32.96, 35.49) == "cbs-stat:8000-1"               # Safed: statistical area
    assert f(31.655, 35.125) == "osm:w5"                      # village
    assert f(31.60, 35.10) is None                            # regional council only
    assert f(0, 0) is None
    print(f"lookup ok ({len(idx)} areas)")


def check_server(tmp: Path, path: Path):
    cfg_path = tmp / "srv.json"
    cfg_path.write_text(json.dumps({"data_dir": str(tmp / "data"), "areas": {"file": str(path)},
                                    "terrain": {"tiles_dir": str(tmp / "tiles"), "max_area_radius_km": 30}}))
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

    def get(u):
        r = urllib.request.urlopen(urllib.request.Request(base + u, headers={"Accept-Encoding": "gzip"}))
        body = r.read()
        return json.loads(gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body)

    a = get("/v1/area?lat=40.72&lon=-73.85")["area"]
    assert a["id"] == "nyc-nta:QN0801" and "polygons" not in a, a
    try:
        urllib.request.urlopen(base + "/v1/area?lat=0&lon=0")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    # two different points in the same NTA -> the same computation (cached once), over the NTA's box
    h1 = get("/v1/horizon?lat=40.72&lon=-73.85&area=auto")
    h2 = get("/v1/horizon?lat=40.73&lon=-73.82&area=auto&radius_km=0.8")
    assert h1["area"]["id"] == h2["area"]["id"] == "nyc-nta:QN0801"
    assert len(calls) == 1, calls
    assert calls[0][2] == [40.7, -73.9, 40.74, -73.8], calls
    # sub-quarter 12 (9 km from centre to corner) is over the user-bbox cap (3 km) but within the area cap
    h3 = get("/v1/horizon?lat=31.80&lon=35.15&area=auto")
    assert h3["area"]["id"] == "cbs-subq:3000-12" and calls[-1][2] == [31.775, 35.1, 31.85, 35.25], calls[-1]
    # no area: the request as given (here a 0.8 km radius around the point)
    h4 = get("/v1/horizon?lat=31.60&lon=35.10&area=auto&radius_km=0.8")
    assert h4["area"] is None and "no official area" in h4["areaNote"], h4
    assert calls[-1][:2] == (31.6, 35.1) and calls[-1][3] == 0.8, calls[-1]
    # a client's own bbox still has the 3 km cap
    try:
        urllib.request.urlopen(base + "/v1/horizon?bbox=31.7,35.1,31.85,35.25")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    # over the area cap: falls back
    serve.Handler.store.cfg["terrain"]["max_area_radius_km"] = 5
    h5 = get("/v1/horizon?lat=31.80&lon=35.15&area=auto")
    assert h5["area"] is None and "over terrain.max_area_radius_km" in h5["areaNote"], h5
    st = get("/v1/status")
    assert st["areas"]["count"] == len(serve.Handler.areas), st
    srv.shutdown()
    print("server ok:", len(calls), "horizon computations")


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
        check_download_progress()
    print("all area tests passed")


if __name__ == "__main__":
    main()
