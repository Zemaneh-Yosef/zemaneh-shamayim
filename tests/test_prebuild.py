"""prebuild.py without network or terrain.

    python3 tests/test_prebuild.py

1. a cached horizon's params give back a query with the same params (so the same cache key): a small box, a
   point, a radius, a point at a given height, the moon's
2. the demand: every cached horizon once, the same place under two keys once, an unreadable file skipped; an
   official area's (its box, too large to ask for directly) asked for as area=auto inside it, giving the same
   params again, or skipped when areas.sqlite no longer has it
3. the warmer: a 503 "computing" asked again after its retryAfterS (plain or gzipped), a 400 given up, and an
   API that isn't running reported
4. the regions' official areas: one horizon per area the city rule picks (a small city's neighbourhoods are the
   city), areas outside the region left out
5. a dry run with the default jobs: the regions' base surfaces and the horizons, nothing built or asked
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import areas  # noqa: E402
import prebuild  # noqa: E402
import serve  # noqa: E402
from common import load_config  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def params_part(cfg):
    print("1. a cached horizon's params give back the same params")
    stub = SimpleNamespace(cfg=cfg["terrain"])
    queries = {
        "a small box": {"bbox": ["31.77000,35.21000,31.79000,35.23000"], "grid_m": ["100"]},
        "a point": {"lat": ["40.6093"], "lon": ["-73.9683"]},
        "a radius": {"lat": ["31.7486"], "lon": ["35.2374"], "radius_km": ["0.8"], "grid_m": ["150"]},
        "a point at a height": {"lat": ["31.7486"], "lon": ["35.2374"], "height": ["812.5"], "eye": ["2"]},
        "the moon's": {"bbox": ["40.60,-73.99,40.62,-73.97"], "moon": ["1"], "min_km": ["0.5"]},
    }
    for name, q in queries.items():
        p = serve.Horizons.params(stub, q)
        again = serve.Horizons.params(stub, {k: [v] for k, v in prebuild.query_from_params(p).items()})
        check(again == p, f"{name}: the same params")


def demand_part(cfg, tmp: Path):
    print("2. the demand")
    cfg = {**cfg, "data_dir": str(tmp)}
    stub = SimpleNamespace(cfg=cfg["terrain"])
    cap = float(cfg["terrain"]["max_area_radius_km"])
    city = {"id": "city", "name": "city", "kind": "x", "level": 2, "source": "test", "country": "ISR",
            "bbox": [31.74, 35.17, 31.82, 35.25], "radiusKm": 6.0, "polygons": square(31.74, 35.17, 31.82, 35.25)}
    areas.write_areas(tmp / "areas.sqlite", [city], {"built": "test"})
    d = tmp / "horizons"
    d.mkdir()
    # an area's horizon as resolve_area runs it: its box, with the request's own options
    p1 = serve.Horizons.params(stub, {"bbox": ["31.74000,35.17000,31.82000,35.25000"], "eye": ["2"]}, cap)
    p2 = serve.Horizons.params(stub, {"lat": ["40.6093"], "lon": ["-73.9683"]})
    p3 = serve.Horizons.params(stub, {"bbox": ["32.00000,35.00000,32.09000,35.09000"]}, cap)   # no such area now
    for name, p in (("old-key", p1), ("new-key", p1), ("point", p2), ("gone", p3)):
        (d / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps({"points": [], "params": p}).encode()))
    (d / "broken.json.gz").write_bytes(b"not gzip")
    qs = prebuild.demand(d, cfg)
    check(len(qs) == 2, "two places: the same one under two keys once, the broken file and the gone area skipped")
    aq = next(q for q in qs if q.get("area"))
    check(aq["area"] == "auto" and aq["eye"] == "2", "the area asked for as area=auto inside it, with its own eye")
    index = areas.AreaIndex(tmp / "areas.sqlite")
    run, _, rcap = serve.resolve_area(index, cfg, {k: [v] for k, v in aq.items()})
    check(serve.Horizons.params(stub, run, rcap) == p1, "which the server runs with the same params (same cache key)")
    check(any(q.get("lat") == "40.60930" and "area" not in q for q in qs), "the point by its point")


class FakeAPI(BaseHTTPRequestHandler):
    seen: dict[str, int] = {}

    def do_GET(self):  # noqa: N802
        n = FakeAPI.seen[self.path] = FakeAPI.seen.get(self.path, 0) + 1
        if "bad" in self.path:
            return self.reply(400, json.dumps({"error": "bbox too large"}).encode())
        if n == 1:   # computing: asked again after retryAfterS
            body = json.dumps({"error": "computing", "computing": True, "retryAfterS": 0.1}).encode()
            return self.reply(503, gzip.compress(body) if "gz" in self.path else body)
        return self.reply(200, gzip.compress(b'{"points": []}'))

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def warmer_part():
    print("3. the warmer")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        w = prebuild.Warmer(f"http://127.0.0.1:{srv.server_port}", 2, 10)
        failed = w.all([{"lat": "1", "lon": "2"}, {"lat": "3", "lon": "4", "gz": "1"}, {"bad": "1"}], "test")
        check(w.done == 2 and failed == 1, "two horizons in, the 400 given up")
        check(sorted(FakeAPI.seen.values()) == [1, 2, 2], "each 503 asked again once, the 400 not")
    finally:
        srv.shutdown()
    try:
        prebuild.Warmer("http://127.0.0.1:9", 1, 2).one({"lat": "1", "lon": "2"})
        check(False, "an API that isn't running is reported")
    except SystemExit as e:
        check("is it running" in str(e), "an API that isn't running is reported")


def square(s, w, n, e):
    return [[[w, s, e, s, e, n, w, n, w, s]]]


def areas_part(cfg, tmp: Path):
    print("4. the regions' official areas")
    hood = areas.LEVEL_NEIGHBOURHOOD
    town = lambda aid, box, level, rad: {"id": aid, "name": aid, "kind": "x", "level": level, "source": "test",  # noqa: E731
                                         "country": "ISR", "bbox": box, "radiusKm": rad, "polygons": square(*box)}
    areas.write_areas(tmp / "areas.sqlite", [
        town("small-city", [31.70, 35.10, 31.80, 35.20], 2, 6.0),
        town("hood-a", [31.70, 35.10, 31.75, 35.20], hood, 3.0),     # under the small city: the city's horizon
        town("hood-b", [31.75, 35.10, 31.80, 35.20], hood, 3.0),
        town("village", [32.00, 35.00, 32.02, 35.02], 2, 1.5),
        town("abroad", [40.00, -74.00, 40.10, -73.90], 2, 5.0),     # outside the region
    ], {"built": "test"})
    qs = prebuild.region_area_queries({**cfg, "data_dir": str(tmp)}, [29.4, 34.2, 33.4, 35.95])
    check(len(qs) == 2, "the small city once (not its neighbourhoods) and the village; nothing abroad")
    check(all(q["area"] == "auto" for q in qs), "asked for as the website asks (area=auto)")


def dry_run_part(tmp: Path):
    print("5. a dry run")
    conf = tmp / "config.json"
    conf.write_text(json.dumps({"data_dir": str(tmp / "data")}))
    (tmp / "data" / "horizons").mkdir(parents=True)
    import io
    import logging
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    logging.getLogger("prebuild").addHandler(h)
    try:
        rc = prebuild.main(["--config", str(conf), "--dry-run"])
    finally:
        logging.getLogger("prebuild").removeHandler(h)
    out = buf.getvalue()
    check(rc == 0, "it succeeds")
    check("terrain_base.py" in out and "--bbox 29.4,34.2,33.4,35.95" in out, "the regions' base surfaces (Israel)")
    check("the listed places' areas:" in out and "asked for before" in out, "the horizons: the cache's and the list's")
    check("build-fine" not in out and "cesium_terrain.py" not in out, "no fine terrain or 3D unless asked for")
    check(not (tmp / "data" / "base-surfaces").exists(), "nothing built")


def main():
    cfg = load_config(str(HERE / "does-not-exist.json"))
    params_part(cfg)
    with tempfile.TemporaryDirectory() as d:
        demand_part(cfg, Path(d))
    warmer_part()
    with tempfile.TemporaryDirectory() as d:
        areas_part(cfg, Path(d))
    with tempfile.TemporaryDirectory() as d:
        dry_run_part(Path(d))
    print("all ok")


if __name__ == "__main__":
    main()
