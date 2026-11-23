#!/usr/bin/env python3
"""Prepare the server's terrain and horizons ahead of requests, so a visitor doesn't wait for them.

    venv/bin/python prebuild.py                      # after a deploy: every horizon visitors have asked for, again,
                                                     # the listed places' areas, and the regions' base surfaces
    venv/bin/python prebuild.py --region-areas       # and every official area in the regions (Israel: all of it)
    venv/bin/python prebuild.py --fine --tiles       # the listed places' fine terrain and 1" tiles
    venv/bin/python prebuild.py --3d --only "Los Angeles"   # the 3D view's tiles for one listed place
    venv/bin/python prebuild.py --dry-run            # only say what it would do

(deploy/prebuild.sh runs it as the service user; deploy/prebuild-fine.sh is the old name, still understood.)

Where, three sources:
  demand   every horizon in the server's cache (<data_dir>/horizons): the places visitors actually asked for,
           anywhere in the world. Files from before a deploy that changed the cache key are still there under
           their old keys, so they are exactly the list to compute again; remove them only after this has run.
  places   deploy/prebuild-places.txt: the main communities (name|lat|lon|km: the community's own radius plus
           terrain.fine.max_km, 10), for a first visitor there and for the jobs that need a list (fine, 3D).
  regions  deploy/prebuild-regions.txt: whole regions (name|south,west,north,east), at first Israel.

What, in this order (each skips what is already done, so a run can be stopped and started again):
  --tiles         the 1" merged tiles a listed place's horizons cross (dem_layers.py build --horizon)
  --fine          a listed place's fine near-range terrain (dem_layers.py build-fine): only where terrain.fine's
                  sources have data (the US, Canada, France); elsewhere its chunks are tried once and marked empty
  --base          a listed place's base-level surfaces (terrain_base.py build); --regions does the regions'
  --regions       the regions' base-level surfaces (Israel: ~260, ~30-40 min, ~20 MB) [default]
  --horizons      ask the running API for every horizon of the demand and of the listed places' official areas
                  [default]. The API builds what each needs (tiles, fine chunks, base surfaces) as a request
                  would, terrain.max_parallel at a time, and the answer is then in its cache. For a place
                  visitors use, this alone does all of the above.
  --region-areas  with --horizons: also every official area in the regions (Israel: a few thousand, hours of
                  CPU), so a first visit anywhere there is answered at once
  --3d            the 3D view's terrain tiles for the listed places (cesium_terrain.py build): 15-80 min a city,
                  so only on request, and only for the list

The fine terrain matters only for the visible sunrise (the near range and the observer's own ground); the base
level reads the 1" tiles alone, so it does not depend on --fine or on the list's fine coverage.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import logging
import math
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLACES_FILE = HERE / "deploy" / "prebuild-places.txt"
REGIONS_FILE = HERE / "deploy" / "prebuild-regions.txt"
FINE_MAX_KM = 10.0     # the list's km is the community's radius plus this (terrain.fine.max_km)

log = logging.getLogger("prebuild")


# ─── Where ─────────────────────────────────────────────────────────────────────

def read_list(path: Path, fields: int) -> list[list[str]]:
    """Lines of name|... (blank lines and # comments skipped)."""
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != fields:
            raise SystemExit(f"{path.name}: expected {fields} fields separated by |: {line}")
        out.append(parts)
    return out


def places(only: str | None = None) -> list[dict]:
    """deploy/prebuild-places.txt: [{name, lat, lon, radius_km (the community's own), km (with the fine range)}]"""
    out = []
    for name, lat, lon, km in read_list(PLACES_FILE, 4):
        if only and only not in name:
            continue
        out.append({"name": name, "lat": float(lat), "lon": float(lon), "km": float(km),
                    "radius_km": max(float(km) - FINE_MAX_KM, 0.0)})
    return out


def regions(only: str | None = None) -> list[dict]:
    """deploy/prebuild-regions.txt: [{name, bbox: [s, w, n, e]}]"""
    out = []
    for name, box in read_list(REGIONS_FILE, 2):
        if only and only not in name:
            continue
        bbox = [float(v) for v in box.split(",")]
        if len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            raise SystemExit(f"{REGIONS_FILE.name}: {name}: the box must be south,west,north,east")
        out.append({"name": name, "bbox": bbox})
    return out


def stored_params(stored: dict) -> dict:
    """A cached horizon's "params" (terrain.horizon_set's: lat, lon, radiusKm, bbox, gridM, eyeM, heightM, minKm,
    maxKm, moon) as Horizons.params gives them, the request's (radius_km, grid_m, eye, height, min_km, moon): what
    the query is made from. grid_m only counts with a radius or a box (horizon_set is given 100 otherwise)."""
    p = {"lat": stored["lat"], "lon": stored["lon"], "bbox": stored.get("bbox"),
         "radius_km": stored.get("radiusKm") or 0.0, "grid_m": stored.get("gridM"), "eye": stored["eyeM"],
         "height": stored.get("heightM"), "min_km": stored["minKm"]}
    if stored.get("moon"):
        p["moon"] = True
    return p


def query_from_params(p: dict) -> dict[str, str]:
    """The /v1/horizon query that gives a cached horizon's params again (Horizons.params, from stored_params), so
    the same cache key. An area's horizon is stored under its box (serve.resolve_area runs it as bbox=...);
    requery asks for a box too large to be asked for directly as its area."""
    q: dict[str, str] = {}
    if p.get("bbox"):
        q["bbox"] = ",".join(f"{v:.5f}" for v in p["bbox"])
        q["grid_m"] = f"{p['grid_m']:g}"
    else:
        q["lat"], q["lon"] = f"{p['lat']:.5f}", f"{p['lon']:.5f}"
        if p.get("radius_km"):
            q["radius_km"] = f"{p['radius_km']:g}"
            q["grid_m"] = f"{p['grid_m']:g}"
        elif p.get("height") is not None:
            q["height"] = f"{p['height']:g}"
    q["eye"] = f"{p['eye']:g}"
    q["min_km"] = f"{p['min_km']:g}"
    if p.get("moon"):
        q["moon"] = "1"
    return q


def area_query_for_box(index, cfg: dict, bbox: list[float]) -> dict[str, str] | None:
    """An official area's horizon is stored under the area's box, but a box that large can only be asked for as
    the area (area=auto or finest at a point inside it): the area with that box in areas.sqlite, and the rule that
    picks it there. None when no area has that box any more (areas.sqlite rebuilt since)."""
    import areas                                          # noqa: PLC0415
    import serve                                          # noqa: PLC0415
    s, w, n, e = bbox
    tol = 1e-3                                            # the R-tree keeps 32-bit floats
    con = index.connect()
    try:
        rows = con.execute("SELECT id FROM areas_box WHERE w BETWEEN ? AND ? AND e BETWEEN ? AND ? "
                           "AND s BETWEEN ? AND ? AND n BETWEEN ? AND ?",
                           (w - tol, w + tol, e - tol, e + tol, s - tol, s + tol, n - tol, n + tol)).fetchall()
        found = [index.area(con, rowid) for (rowid,) in rows]
    finally:
        con.close()
    for a in found:
        if a is None or [round(v, 5) for v in a["bbox"]] != [round(v, 5) for v in bbox]:
            continue
        pt = inside_point(a)
        if not pt:
            continue
        for rule, name in (("city", "auto"), ("finest", "finest")):
            chosen = serve.find_area(index, cfg, pt[0], pt[1], rule)
            if chosen is not None and chosen["id"] == a["id"]:
                return {"lat": f"{pt[0]:.5f}", "lon": f"{pt[1]:.5f}", "area": name}
    return None


def requery(p: dict, cfg: dict, index) -> dict[str, str] | None:
    """The query that asks for a cached horizon again: its params as given, or, for an official area's (a box
    larger than terrain.max_radius_km), the area at a point inside it with the same options."""
    import areas                                          # noqa: PLC0415
    q = query_from_params(p)
    if not p.get("bbox") or areas.half_diagonal_km(p["bbox"]) <= float(cfg["terrain"]["max_radius_km"]):
        return q
    aq = index is not None and area_query_for_box(index, cfg, p["bbox"])
    if not aq:
        return None
    return {**aq, **{k: q[k] for k in ("grid_m", "eye", "min_km", "moon") if k in q}}


def demand(horizons_dir: Path, cfg: dict) -> list[dict[str, str]]:
    """Every horizon in the server's cache, as the query that asks for it again (one per distinct query: the same
    place under an old and a new key is asked for once)."""
    import areas                                          # noqa: PLC0415
    import serve                                          # noqa: PLC0415
    index = areas.AreaIndex(serve.areas_path(cfg))
    index.refresh()
    if index.stamp is None:
        index = None
    seen, out, bad, gone = set(), [], 0, 0
    for f in sorted(horizons_dir.glob("*.json.gz")):
        try:
            p = stored_params(json.loads(gzip.decompress(f.read_bytes()))["params"])
            q = requery(p, cfg, index)
        except (OSError, ValueError, KeyError, TypeError) as e:
            bad += 1
            if bad <= 3:                                  # the reason, for the first few
                log.info("couldn't read %s: %s: %s", f.name, type(e).__name__, e)
            continue
        if q is None:
            gone += 1
            continue
        key = json.dumps(q, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(q)
    if bad:
        log.info("%d cached files couldn't be read (skipped)", bad)
    if gone:
        log.info("%d cached areas' horizons whose area isn't in areas.sqlite any more (skipped)", gone)
    return out


def inside_point(area: dict) -> tuple[float, float] | None:
    """A point inside an official area (its box's centre, else the first of a 9x9 grid over its box that is)."""
    import areas                                          # noqa: PLC0415
    s, w, n, e = area["bbox"]
    for i, j in [(4, 4)] + [(i, j) for i in range(9) for j in range(9)]:
        lat, lon = s + (n - s) * (i + 0.5) / 9, w + (e - w) * (j + 0.5) / 9
        if areas.area_contains(area, lat, lon):
            return lat, lon
    return None


def region_area_queries(cfg: dict, bbox: list[float]) -> list[dict[str, str]]:
    """area=auto at a point inside every official area in a box, one per area the server would pick (the city
    rule puts a city's neighbourhoods under the city, so most of them are the same horizon)."""
    import sqlite3                                        # noqa: PLC0415

    import areas                                          # noqa: PLC0415
    import serve                                          # noqa: PLC0415
    index = areas.AreaIndex(serve.areas_path(cfg))
    index.refresh()
    if index.stamp is None:
        raise SystemExit(f"no areas file at {index.path} (build_areas.py)")
    s, w, n, e = bbox
    con = index.connect()
    try:
        rows = con.execute("SELECT id FROM areas_box WHERE w <= ? AND e >= ? AND s <= ? AND n >= ?",
                           (e, w, n, s)).fetchall()
        found = [index.area(con, rowid) for (rowid,) in rows]
    except sqlite3.Error as err:
        raise SystemExit(f"couldn't read {index.path}: {err}")
    finally:
        con.close()
    picked, out = set(), []
    for a in found:
        pt = a and inside_point(a)
        if not pt or not (s <= pt[0] <= n and w <= pt[1] <= e):
            continue
        chosen = serve.find_area(index, cfg, pt[0], pt[1], "city")
        if chosen is None or chosen["id"] in picked:
            continue
        picked.add(chosen["id"])
        out.append({"lat": f"{pt[0]:.5f}", "lon": f"{pt[1]:.5f}", "area": "auto"})
    return out


# ─── What ──────────────────────────────────────────────────────────────────────

def run(cmd: list[str], dry: bool) -> bool:
    """One of the build tools, from the repository folder; False if it failed."""
    log.info("$ %s", " ".join(cmd))
    if dry:
        return True
    return subprocess.run([sys.executable, *cmd], cwd=HERE).returncode == 0


def area_radius_km(cfg: dict, place: dict) -> float:
    """A listed place's radius: its own, or its official area's (centre to corner) where that is larger, so the
    vantage points of the area a visitor there is given are all covered."""
    try:
        import areas                                      # noqa: PLC0415
        import serve                                      # noqa: PLC0415
        a = serve.find_area(areas.AreaIndex(serve.areas_path(cfg)), cfg, place["lat"], place["lon"], "city")
    except Exception:                                     # noqa: BLE001 (no areas file: the listed radius)
        a = None
    if a is None:
        return place["radius_km"]
    ky = 111.19
    kx = ky * math.cos(math.radians(place["lat"]))
    s, w, n, e = a["bbox"]
    far = max(math.hypot((y - place["lat"]) * ky, (x - place["lon"]) * kx) for y in (s, n) for x in (w, e))
    return max(place["radius_km"], round(far, 1))


class Warmer:
    """Asks the running API for horizons, `parallel` at a time, until each is in its cache: a 503 "computing"
    is asked again when it says (retryAfterS), as the website does."""

    def __init__(self, api: str, parallel: int, timeout_s: float):
        self.api, self.parallel, self.timeout_s = api.rstrip("/"), parallel, timeout_s
        self.lock = threading.Lock()
        self.done = self.failed = 0

    def one(self, q: dict[str, str]) -> bool:
        url = f"{self.api}/v1/horizon?{urllib.parse.urlencode(q)}"
        for _ in range(200):                              # a first horizon is minutes at most; give up after that
            try:
                # gzip: the API keeps horizons gzipped, so it sends them as they are
                req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    r.read()
                return True
            except urllib.error.HTTPError as e:
                body = e.read()
                if e.code == 503:
                    try:
                        wait = float(json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)
                                     .get("retryAfterS", 15))
                    except ValueError:
                        wait = 15.0
                    time.sleep(min(max(wait, 2.0), 120.0))
                    continue
                log.warning("%s: HTTP %d %s", url, e.code, body[:200].decode("utf-8", "replace"))
                return False
            except urllib.error.URLError as e:
                raise SystemExit(f"can't reach the API at {self.api} ({e.reason}): is it running? (--api)")
        log.warning("%s: still computing after many tries; skipped", url)
        return False

    def all(self, queries: list[dict[str, str]], label: str) -> int:
        if not queries:
            return 0
        t0, n = time.monotonic(), len(queries)
        log.info("%s: %d horizons, %d at a time", label, n, self.parallel)
        with concurrent.futures.ThreadPoolExecutor(self.parallel) as pool:
            futures = [pool.submit(self.one, q) for q in queries]
            for i, f in enumerate(concurrent.futures.as_completed(futures), 1):
                ok = f.result()
                with self.lock:
                    self.done += ok
                    self.failed += not ok
                if i % 25 == 0 or i == n:
                    el = time.monotonic() - t0
                    log.info("  %d/%d; about %.0f min left", i, n, el / i * (n - i) / 60)
        return sum(1 for f in futures if not f.result())


# ─── Main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="config.json (default: next to this script)")
    ap.add_argument("--horizons", action="store_true", help="the demand's and the listed places' horizons")
    ap.add_argument("--regions", action="store_true", help="the regions' base-level surfaces")
    ap.add_argument("--region-areas", action="store_true", help="with --horizons: every official area in the regions")
    ap.add_argument("--tiles", action="store_true", help="the listed places' 1\" tiles")
    ap.add_argument("--fine", action="store_true", help="the listed places' fine terrain")
    ap.add_argument("--base", action="store_true", help="the listed places' base-level surfaces")
    ap.add_argument("--3d", dest="cesium", action="store_true", help="the listed places' 3D-view tiles")
    ap.add_argument("--only", help="only the listed places and regions whose name contains this (no demand)")
    ap.add_argument("--example", action="store_true", help="--fine with config.example.json's fine sources")
    ap.add_argument("--api", help="the running API (default http://127.0.0.1:<api.port>)")
    ap.add_argument("--parallel", type=int, help="horizons asked for at once (default terrain.max_parallel)")
    ap.add_argument("--dry-run", action="store_true", help="only say what would be done")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not (a.horizons or a.regions or a.region_areas or a.tiles or a.fine or a.base or a.cesium):
        a.horizons = a.regions = True                     # what a deploy needs
    if a.region_areas:
        a.horizons = True

    from common import load_config                        # noqa: PLC0415
    cfg = load_config(a.config)
    t = cfg["terrain"]
    listed, regs = places(a.only), regions(a.only)
    conf = ["--config", a.config] if a.config else []
    failed: list[str] = []

    for p in listed:
        if not (a.tiles or a.fine or a.base or a.cesium):
            break
        r = area_radius_km(cfg, p)
        log.info("=== %s (%.2f, %.2f; %.1f km)", p["name"], p["lat"], p["lon"], r)
        around = ["--around", str(p["lat"]), str(p["lon"])]
        if a.tiles and not run(["dem_layers.py", *conf, "build", *around, "--horizon", "--radius-km", str(r)], a.dry_run):
            failed.append(f"{p['name']} (1\" tiles)")
        if a.fine and not run(["dem_layers.py", *conf, "build-fine", *around, "--km", str(r + FINE_MAX_KM)]
                              + (["--example"] if a.example else []), a.dry_run):
            failed.append(f"{p['name']} (fine terrain)")
        if a.base and not run(["terrain_base.py", *conf, "build", *around, "--radius-km", str(r)], a.dry_run):
            failed.append(f"{p['name']} (base level)")
        if a.cesium and not run(["cesium_terrain.py", *conf, "build", *around, "--radius-km", str(r)], a.dry_run):
            failed.append(f"{p['name']} (3D view)")

    if a.regions:
        for g in regs:
            log.info("=== %s (base level)", g["name"])
            if not run(["terrain_base.py", *conf, "build", "--bbox", ",".join(f"{v:g}" for v in g["bbox"])], a.dry_run):
                failed.append(f"{g['name']} (base level)")

    if a.horizons:
        groups: list[tuple[str, list[dict[str, str]]]] = []
        if not a.only:
            groups.append(("asked for before (the server's cache)", demand(Path(cfg["data_dir"]) / "horizons", cfg)))
        groups.append(("the listed places' areas", [{"lat": f"{p['lat']:.5f}", "lon": f"{p['lon']:.5f}", "area": "auto"}
                                                    for p in listed]))
        if a.region_areas:
            for g in regs:
                groups.append((f"{g['name']}'s official areas", region_area_queries(cfg, g["bbox"])))
        api = a.api or f"http://127.0.0.1:{cfg['api']['port']}"
        w = Warmer(api, a.parallel or int(t["max_parallel"]), float(t.get("answer_within_s", 45.0)) + 30)
        for label, qs in groups:
            if a.dry_run:
                log.info("%s: %d horizons (from %s)", label, len(qs), api)
                continue
            if w.all(qs, label):
                failed.append(f"{label}: some horizons")

    if failed:
        log.info("=== some work failed: %s - run this again to retry it", "; ".join(failed))
        return 1
    log.info("=== done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
