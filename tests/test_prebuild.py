"""prebuild.py without network or terrain.

    python3 tests/test_prebuild.py

1. real horizons computed by the API (on a synthetic slope) and kept in its cache: a point, a point at a height
   with its own eye, a radius, a small box, the moon's, an official area's as the website asks for it
2. read back from the cache files as the API wrote them (terrain.horizon_set's params), and asked for again:
   every one the same cache entry, nothing new computed (an official area's box, too large to ask for directly,
   asked for as area=auto inside it); an unreadable file skipped
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


def api_part(tmp: Path):
    print("1-2. real horizons in the API's cache, read back and asked for again")
    import urllib.request

    import numpy as np
    tiles = tmp / "tiles"
    tiles.mkdir()
    # a gentle slope on one tile (the rest is sea): enough for real horizons, quick to compute
    z = (np.add.outer(np.arange(3601), np.arange(3601)) // 40).astype(">i2")
    (tiles / "N31E035.hgt").write_bytes(z.tobytes())
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({"data_dir": str(tmp / "data"), "terrain": {
        "tiles_dir": str(tiles), "tiles_url": "", "worker_processes": False, "answer_within_s": 600,
        "max_km": 20, "base": {"enabled": False}}}))
    cfg = load_config(str(cfgp))
    city = {"id": "city", "name": "city", "kind": "x", "level": 2, "source": "test", "country": "ISR",
            "bbox": [31.60, 35.40, 31.66, 35.46], "radiusKm": 4.4, "polygons": square(31.60, 35.40, 31.66, 35.46)}
    (tmp / "data").mkdir()
    areas.write_areas(tmp / "data" / "areas.sqlite", [city], {"built": "test"})
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = serve.Horizons(cfg)
    serve.Handler.areas = areas.AreaIndex(serve.areas_path(cfg))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{srv.server_address[1]}"
    asked = {
        "a point": "lat=31.5&lon=35.5",
        "a point at a height, its own eye": "lat=31.52&lon=35.52&height=812.5&eye=2",
        "a radius": "lat=31.55&lon=35.55&radius_km=0.4&grid_m=400",
        "a small box": "bbox=31.40,35.60,31.41,35.61&grid_m=500&min_km=0.5",
        "the moon's": "lat=31.45&lon=35.45&moon=1",
        "an official area (as the website asks)": "lat=31.63&lon=35.43&area=auto&grid_m=1500",
    }
    try:
        for name, q in asked.items():
            with urllib.request.urlopen(f"{api}/v1/horizon?{q}", timeout=600) as r:
                r.read()
        hdir = tmp / "data" / "horizons"
        before = {f.name: f.stat().st_mtime_ns for f in hdir.glob("*.json.gz")}
        check(len(before) == len(asked), f"{len(asked)} horizons in the cache")
        (hdir / "broken.json.gz").write_bytes(b"not gzip")
        qs = prebuild.demand(hdir, cfg)
        check(len(qs) == len(asked), "every one read back (the broken file skipped)")
        aq = [q for q in qs if q.get("area")]
        check(len(aq) == 1 and aq[0]["area"] == "auto" and aq[0]["grid_m"] == "1500",
              "the area's horizon asked for as the area, with its own grid")
        failed = prebuild.Warmer(api, 2, 600).all(qs, "again")
        after = {f.name: f.stat().st_mtime_ns for f in hdir.glob("*.json.gz") if f.name != "broken.json.gz"}
        check(failed == 0 and after == before, "asked for again, each is the same cache entry (nothing new computed)")
    finally:
        srv.shutdown()


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
    with tempfile.TemporaryDirectory() as d:
        api_part(Path(d))
    warmer_part()
    with tempfile.TemporaryDirectory() as d:
        areas_part(cfg, Path(d))
    with tempfile.TemporaryDirectory() as d:
        dry_run_part(Path(d))
    print("all ok")


if __name__ == "__main__":
    main()
