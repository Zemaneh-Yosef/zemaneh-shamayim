#!/usr/bin/env python3
"""HTTP API: temperature profiles along the sunrise / sunset direction.

    GET /v1/path-profiles?lat=40.71&lon=-74.01[&from=2026-10-05][&days=8]
    GET /v1/horizon?lat=31.7486&lon=35.2374[&radius_km=0.8 | &bbox=S,W,N,E][&grid_m=100][&eye=1.7][&height=..][&min_km=1][&moon=1]
    GET /v1/horizon?lat=40.609283058016736&lon=-73.96828881865329&area=auto[&radius_km=..]   (the official area containing the point)
    GET /v1/area?lat=40.609283058016736&lon=-73.96828881865329
    GET /v1/light-pollution?lat=31.7767&lon=35.2345[&area=auto | &bbox=S,W,N,E][&percentile=90][&year=2026]
    GET /v1/status

Each event comes from the best source available for it (its "source" field):
    "forecast"          the latest GFS run (about 7 days ahead)
    "climatology-gfs"   your own climatology, built from past GFS runs (once a month has enough data)
    "climatology-ncep"  NOAA's 1991-2020 reanalysis climatology (the fallback until then)
Events no source covers are left out; the app then uses its next provider.

/v1/horizon returns terrain horizon profiles for visible sunrise / sunset (terrain.py), computed from
elevation tiles (downloaded on first use; bare-earth layers from dem_layers.py when terrain.layers is
set) and cached on disk: with radius_km 0 the exact point, otherwise the
vantage points within that radius that see lowest in some direction. With area=auto the server looks the
point up in areas.json (build_areas.py) and searches the whole official area it is in - a neighbourhood,
village or city - so everyone in it gets the same visible sunrise; the response's "area" says which.
With moon=1 it also returns moonrise / moonset horizons (terrain.py): over the Moon's wider range of
directions, and in an area from the spots at terrain.moon_percentile (default 90) of horizon height,
so the moonrise printed for the area is one nearly all of it can see. A
point in no known area (or in one over terrain.max_area_radius_km) gets the request as given instead.

/v1/light-pollution returns the artificial zenith sky brightness for the star-visibility nightfall model
(light_pollution.py): the Falchi et al. 2016 world atlas (2014 satellite data) brought to `year` (default
this year) by sky-meter readings nearby, else by the measured growth of skyglow since then; at the point,
or with area=auto / bbox the `percentile` (default light_pollution.area_percentile, 90) over every atlas
pixel in the area.

Run behind nginx (or any reverse proxy) for TLS. Standard library only, plus numpy (and rasterio for
/v1/light-pollution).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

import areas
import climatology
import dem_layers
import light_pollution
import terrain
from climatology import ClimGrid, build_profile, clim_dir
from common import Cycle, destination, gfs_dir, load_config, solar_date, sun_event

log = logging.getLogger("serve")


class Store:
    """The current cycle, reloaded when fetch_gfs.py publishes a new one."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cycle_id = None
        self.regions: dict = {}
        self.hours: list[int] = []
        self.arrays: dict = {}
        self.clim: dict = {}            # (source, region) -> ClimGrid

    def refresh(self) -> None:
        self.refresh_climatology()
        cur = gfs_dir(self.cfg) / "current.json"
        if not cur.exists():
            return
        current = json.loads(cur.read_text())
        cid, stamp = current["cycle"], current.get("published")
        if (cid, stamp) == (self.cycle_id, getattr(self, "stamp", None)):
            return
        with self.lock:
            cdir = gfs_dir(self.cfg) / cid
            info = json.loads((cdir / "cycle.json").read_text())
            regions = {}
            for name in info["regions"]:
                meta = json.loads((cdir / name / "meta.json").read_text())
                meta["lats"] = np.asarray(meta["lats"])
                meta["lons"] = np.asarray(meta["lons"])
                meta["dir"] = cdir / name
                regions[name] = meta
            self.cycle_id, self.regions, self.hours, self.arrays = cid, regions, info["hours"], {}
            self.stamp = stamp
            log.info("serving cycle %s", cid)

    def refresh_climatology(self) -> None:
        for src in ("gfs", "ncep"):
            for r in self.cfg["regions"]:
                path = clim_dir(self.cfg, src, r["name"])
                key = (src, r["name"])
                have = self.clim.get(key)
                ver = ClimGrid.version(path)
                if ver is None:
                    self.clim.pop(key, None)
                elif have is None or have.stamp != ver:
                    try:
                        g = ClimGrid(path)
                        if g.meta.get("region") == r:
                            self.clim[key] = g
                            log.info("loaded %s climatology for %s", src, r["name"])
                        else:
                            self.clim.pop(key, None)        # built for an older box
                    except Exception:
                        log.exception("could not load %s climatology for %s", src, r["name"])

    def array(self, region: str, hour: int) -> np.ndarray:
        key = (region, hour)
        a = self.arrays.get(key)
        if a is None:
            a = np.load(self.regions[region]["dir"] / f"f{hour:03d}.npy", mmap_mode="r")
            self.arrays[key] = a
        return a

    def region_for(self, lat: float, lon: float):
        for r in self.cfg["regions"]:
            if r["south"] <= lat <= r["north"] and r["west"] <= lon <= r["east"]:
                return r["name"]
        return None


def bilinear(store: Store, region: str, lat: float, lon: float):
    """Indices and weights for bilinear interpolation, or None outside the grid."""
    m = store.regions[region]
    la, lo = m["lats"], m["lons"]
    if not (la[0] <= lat <= la[-1]):
        return None
    lo_sorted = lo[0] < lo[-1]
    if not lo_sorted or not (lo[0] <= lon <= lo[-1]):
        return None
    i = min(int(np.searchsorted(la, lat, side="right") - 1), len(la) - 2)
    j = min(int(np.searchsorted(lo, lon, side="right") - 1), len(lo) - 2)
    fy = (lat - la[i]) / (la[i + 1] - la[i])
    fx = (lon - lo[j]) / (lo[j + 1] - lo[j])
    return i, j, fy, fx


def sample(store: Store, region: str, hour: int, ij) -> np.ndarray:
    i, j, fy, fx = ij
    a = store.array(region, hour)
    blk = np.asarray(a[:, i:i + 2, j:j + 2], dtype=np.float64)
    return (blk[:, 0, 0] * (1 - fy) * (1 - fx) + blk[:, 0, 1] * (1 - fy) * fx
            + blk[:, 1, 0] * fy * (1 - fx) + blk[:, 1, 1] * fy * fx)


def profile_at(store: Store, region: str, t: datetime, lat: float, lon: float, dist_km: float):
    """Forecast profile, or None outside the grid / forecast window."""
    if store.cycle_id is None or region not in store.regions:
        return None
    ij = bilinear(store, region, lat, lon)
    if ij is None:
        return None
    c0 = Cycle.parse(store.cycle_id).time
    tau = (t - c0).total_seconds() / 3600
    hours = store.hours
    if tau < hours[0] or tau > hours[-1]:
        return None
    k = max(0, min(int(np.searchsorted(hours, tau, side="right") - 1), len(hours) - 2))
    h0, h1 = hours[k], hours[k + 1]
    w = (tau - h0) / (h1 - h0)
    v = sample(store, region, h0, ij) * (1 - w) + sample(store, region, h1, ij) * w
    f = {n: v[i] for i, n in enumerate(store.regions[region]["fields"])}
    return build_profile(f, store.regions[region]["levels_mb"], dist_km, lat, lon)


def clim_profile_at(store: Store, source: str, region: str, t: datetime, lat: float, lon: float, dist_km: float):
    g = store.clim.get((source, region))
    if g is None:
        return None
    min_samples = 1
    if source == "gfs":
        min_samples = int(store.cfg["climatology"]["min_days"]) * climatology.GFS_SLOT_HOURS
    f = g.fields_at(t, lat, lon, min_samples)
    if f is None:
        return None
    return build_profile(f, g.levels_mb, dist_km, lat, lon, g.near_surface_agl)


SOURCES = ("forecast", "climatology-gfs", "climatology-ncep")


def path_profiles(store: Store, lat: float, lon: float, start, days: int) -> dict:
    cfg = store.cfg
    region = store.region_for(lat, lon)
    if region is None:
        raise LookupError("location is outside the configured regions")
    getters = {
        "forecast": lambda t, a, b, d: profile_at(store, region, t, a, b, d),
        "climatology-gfs": lambda t, a, b, d: clim_profile_at(store, "gfs", region, t, a, b, d),
        "climatology-ncep": lambda t, a, b, d: clim_profile_at(store, "ncep", region, t, a, b, d),
    }
    events = []
    for k in range(days):
        d = start + timedelta(days=k)
        for ev in ("sunrise", "sunset"):
            se = sun_event(d, lat, lon, ev)
            if se is None:
                continue
            t, az = se
            points = [(dist, *destination(lat, lon, az, dist)) for dist in cfg["api"]["distances_km"]]
            for src in SOURCES:
                profs = []
                for dist, plat, plon in points:
                    p = getters[src](t, plat, plon, dist)
                    if p is None:
                        break                  # left the grid, the forecast window, or data too thin
                    profs.append(p)
                if profs:
                    events.append({"date": d.isoformat(), "event": ev, "source": src,
                                   "timeUtc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "azimuthDeg": round(az, 2), "profiles": profs})
                    break
    used = {e["source"] for e in events}
    about = {"forecast": "NOAA GFS 0.25 deg via NOMADS"}
    for src in ("gfs", "ncep"):
        g = store.clim.get((src, region))
        if g is not None:
            about[f"climatology-{src}"] = g.meta.get("source", src)
    return {"cycle": store.cycle_id, "location": {"lat": lat, "lon": lon},
            "sources": {k: v for k, v in about.items() if k in used}, "events": events}


class Horizons:
    """Terrain horizons, computed on demand and cached as gzip JSON files."""

    def __init__(self, cfg: dict):
        t = cfg["terrain"]
        self.cfg = t
        data = Path(cfg["data_dir"])
        folder, url = Path(t["tiles_dir"] or data / "tiles"), t["tiles_url"] or None
        ua = f"zmanim-sky-server/0.1 ({cfg.get('contact') or 'no contact'})"
        # Merged bare-earth tiles (dem_layers.py), when terrain.layers lists sources
        self.dem = dem_layers.signature(t.get("layers"))
        self.dem_sources = [s["name"] for s in dem_layers.enabled_sources(t.get("layers"))]
        layered_dir, builder = dem_layers.layers_dir(cfg), None
        if self.dem and t["layers"].get("auto_build"):
            try:
                builder = dem_layers.make_builder(cfg, terrain.Tiles(folder, url, user_agent=ua))
            except ImportError:
                log.warning("terrain.layers.auto_build is on but rasterio is not installed: "
                            "only prebuilt merged tiles will be used")
        if self.dem:
            log.info("bare-earth layers %s (%s): %s", self.dem, layered_dir, ", ".join(self.dem_sources))
        self.tiles = terrain.Tiles(folder, url, user_agent=ua, layered_dir=layered_dir, builder=builder)
        self.dir = data / "horizons"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.slots = threading.BoundedSemaphore(int(t["max_parallel"]))

    def params(self, q: dict, max_bbox_km: float | None = None) -> dict:
        t = self.cfg
        get = lambda k, d: float(q[k][0]) if k in q and q[k][0] != "" else d
        bbox = None
        if "bbox" in q:
            bbox = [round(float(v), 5) for v in q["bbox"][0].split(",")]
            if len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
                raise ValueError("bbox must be south,west,north,east")
            ky = 111.19                                  # km per degree
            kx = ky * math.cos(math.radians((bbox[0] + bbox[2]) / 2))
            half_diag = math.hypot((bbox[2] - bbox[0]) * ky, (bbox[3] - bbox[1]) * kx) / 2
            cap = float(max_bbox_km if max_bbox_km is not None else t["max_radius_km"])
            if half_diag > cap:
                raise ValueError(f"bbox too large: {half_diag:.1f} km from centre to corner, "
                                 f"max {cap:g} (terrain.max_radius_km)")
        lat, lon = get("lat", None), get("lon", None)
        if bbox and lat is None and lon is None:
            lat, lon = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if lat is None or lon is None or not (-60 <= lat <= 60 and -180 <= lon <= 180):
            raise ValueError("lat/lon (or bbox) missing or outside SRTM coverage (60S..60N)")
        p = {"lat": round(lat, 5), "lon": round(lon, 5), "bbox": bbox,
             "radius_km": round(min(max(get("radius_km", 0.0), 0.0), float(t["max_radius_km"])), 3),
             "grid_m": round(max(get("grid_m", 100.0), float(t["min_grid_m"])), 1),
             "eye": round(min(max(get("eye", float(t["eye_m"])), 0.0), 500.0), 2),
             "height": None if get("height", None) is None else round(get("height", None), 1),
             "min_km": round(min(max(get("min_km", float(t["min_km"])), 0.03), 20.0), 3),
             "max_km": float(t["max_km"])}
        if p["radius_km"] > 0 or bbox:
            p["height"] = None                 # each vantage point stands on its own ground
        else:
            p["grid_m"] = None
        if q.get("moon", [""])[0] in ("1", "true"):
            # added only when asked, so sun-only requests keep their cache keys (and cached files)
            p["moon"] = True
            p["moon_percentile"] = round(min(max(float(t.get("moon_percentile", 90.0)), 0.0), 100.0), 1)
        return p

    def get(self, q: dict, extra: dict | None = None, max_bbox_km: float | None = None) -> bytes:
        """gzip-compressed JSON; extra (e.g. the area) is added to the result, not to the cache key"""
        body = self.compute(q, max_bbox_km)
        if extra:
            res = json.loads(gzip.decompress(body))
            body = gzip.compress(json.dumps({**res, **extra}, separators=(",", ":")).encode())
        return body

    def compute(self, q: dict, max_bbox_km: float | None = None) -> bytes:
        p = self.params(q, max_bbox_km)
        # the terrain source list and the horizon algorithm's version are part of the key, so changing either
        # recomputes (neither changes the key while unset / at version 1, so old caches stay valid)
        key_src = {**p, "dem": self.dem} if self.dem else dict(p)
        if terrain.ALGO_VERSION != 1:
            key_src["algo"] = terrain.ALGO_VERSION
        key = hashlib.sha1(json.dumps(key_src, sort_keys=True).encode()).hexdigest()[:20]
        f = self.dir / f"{key}.json.gz"
        if f.exists():
            return f.read_bytes()
        with self.slots:
            if f.exists():
                return f.read_bytes()
            res = terrain.horizon_set(self.tiles, p["lat"], p["lon"], radius_km=p["radius_km"], bbox=p["bbox"],
                                      grid_m=p["grid_m"] or 100.0, eye=p["eye"], height=p["height"],
                                      min_km=p["min_km"], max_km=p["max_km"],
                                      max_candidates=int(self.cfg["max_candidates"]),
                                      moon=p.get("moon", False), moon_percentile=p.get("moon_percentile", 90.0))
            if self.dem:
                res["terrain"] = {"layers": self.dem, "sources": self.dem_sources}
            body = gzip.compress(json.dumps(res, separators=(",", ":")).encode())
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(f)
            return body


def areas_path(cfg: dict) -> Path:
    return Path(cfg.get("areas", {}).get("file") or Path(cfg["data_dir"]) / "areas.json")


def resolve_area(index: areas.AreaIndex, cfg: dict, q: dict):
    """For area=auto: (query to run, extra fields for the response, bbox cap)."""
    lat, lon = float(q["lat"][0]), float(q["lon"][0])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    a = index.find(lat, lon)
    cap = float(cfg["terrain"]["max_area_radius_km"])
    rest = {k: v for k, v in q.items() if k != "area"}
    if a is None:
        return rest, {"area": None, "areaNote": "no official area here; used the request as given"}, None
    if a["radiusKm"] > cap:
        return rest, {"area": None, "areaNote": f"{a['name']} is {a['radiusKm']:g} km from centre to corner, "
                                                f"over terrain.max_area_radius_km ({cap:g}); used the request "
                                                f"as given"}, None
    run = {k: v for k, v in q.items() if k in ("grid_m", "eye", "min_km", "moon")}
    run["bbox"] = [",".join(f"{v:.5f}" for v in a["bbox"])]
    return run, {"area": areas.public(a)}, cap


def light_pollution_query(lp: light_pollution.LightPollution, index: areas.AreaIndex, q: dict) -> tuple[dict, int]:
    """/v1/light-pollution: (response, cache seconds)."""
    get = lambda k: q[k][0] if k in q and q[k][0] != "" else None
    pct = None if get("percentile") is None else float(get("percentile"))
    bbox = None
    if get("bbox") is not None:
        bbox = [float(v) for v in get("bbox").split(",")]
        if len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            raise ValueError("bbox must be south,west,north,east")
        if areas.half_diagonal_km(bbox) > lp.max_radius_km:
            raise ValueError(f"bbox too large: {areas.half_diagonal_km(bbox):.1f} km from centre to corner, "
                             f"max {lp.max_radius_km:g} (light_pollution.max_radius_km)")
    lat = None if get("lat") is None else float(get("lat"))
    lon = None if get("lon") is None else float(get("lon"))
    if bbox and lat is None and lon is None:
        lat, lon = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon (or bbox) missing or out of range")
    year = None if get("year") is None else float(get("year"))
    if year is not None and not 1990 <= year <= 2100:
        raise ValueError("year must be between 1990 and 2100")
    area, extra = None, {}
    want_area = get("area") in ("auto", "1", "true")
    if want_area:
        a = index.find(lat, lon)
        if a is None:
            extra = {"area": None, "areaNote": "no official area here; the value at the point"}
        elif a["radiusKm"] > lp.max_radius_km:
            extra = {"area": None, "areaNote": f"{a['name']} is {a['radiusKm']:g} km from centre to corner, over "
                                               f"light_pollution.max_radius_km ({lp.max_radius_km:g}); the value "
                                               f"at the point"}
        else:
            area, extra = a, {"area": areas.public(a)}
    res = lp.estimate(lat, lon, area=area, bbox=None if want_area else bbox, percentile=pct, year=year,
                      area_key=(area["id"], index.stamp) if area else None)
    res.update(extra)
    # the default year (and sky-meter readings) change; the atlas does not
    cache_s = 86400
    return res, cache_s


class Handler(BaseHTTPRequestHandler):
    store: Store = None  # set in main()
    horizons: Horizons = None
    lightpol: light_pollution.LightPollution = None
    areas: areas.AreaIndex = None
    server_version = "zmanim-sky-server/0.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, status: int, obj, cache_s: int = 0) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", self.store.cfg["api"]["allow_origin"])
        self.send_header("Cache-Control", f"public, max-age={cache_s}" if cache_s else "no-store")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_gzip_json(self, gz_body: bytes, cache_s: int) -> None:
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body, enc = gz_body, True
        else:
            body, enc = gzip.decompress(gz_body), False
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", self.store.cfg["api"]["allow_origin"])
        self.send_header("Cache-Control", f"public, max-age={cache_s}")
        if enc:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def area_index(self) -> areas.AreaIndex:
        if Handler.areas is None:
            Handler.areas = areas.AreaIndex(areas_path(self.store.cfg))
        return Handler.areas

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", self.store.cfg["api"]["allow_origin"])
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/v1/horizon":
                if q.get("area", [""])[0] in ("auto", "1", "true"):
                    run, extra, cap = resolve_area(self.area_index(), self.store.cfg, q)
                    return self.send_gzip_json(self.horizons.get(run, extra, cap), cache_s=7 * 86400)
                return self.send_gzip_json(self.horizons.get(q), cache_s=30 * 86400)
            if url.path == "/v1/light-pollution":
                body, cache_s = light_pollution_query(self.lightpol, self.area_index(), q)
                return self.send_json(200, body, cache_s=cache_s)
            if url.path == "/v1/area":
                lat, lon = float(q["lat"][0]), float(q["lon"][0])
                a = self.area_index().find(lat, lon)
                if a is None:
                    return self.send_json(404, {"area": None, "error": "no official area contains this point"})
                return self.send_json(200, {"area": areas.public(a)}, cache_s=86400)
            self.store.refresh()
            if url.path == "/v1/status":
                clim = {}
                for (src, region), g in self.store.clim.items():
                    info = {"source": g.meta.get("source")}
                    if g.counts is not None:
                        need = int(self.store.cfg["climatology"]["min_days"]) * climatology.GFS_SLOT_HOURS
                        info["days_per_month"] = [round(float(c.mean()) / climatology.GFS_SLOT_HOURS, 1)
                                                  for c in g.counts]
                        info["months_ready"] = int((g.counts >= need).all(axis=1).sum())
                    clim.setdefault(region, {})[src] = info
                return self.send_json(200, {"cycle": self.store.cycle_id, "hours": self.store.hours,
                                            "regions": {r["name"]: r for r in self.store.cfg["regions"]},
                                            "climatology": clim,
                                            "areas": {"count": len(self.area_index()),
                                                      "built": self.area_index().meta.get("built")},
                                            "lightPollution": self.lightpol and {
                                                "dir": str(self.lightpol.dir),
                                                "tiles": len(list(self.lightpol.dir.glob("*.tif"))),
                                                "downloads": bool(self.lightpol.url)}})
            if url.path != "/v1/path-profiles":
                return self.send_json(404, {"error": "not found"})
            if self.store.cycle_id is None and not self.store.clim:
                return self.send_json(503, {"error": "no forecast or climatology data yet"})
            lat, lon = float(q["lat"][0]), float(q["lon"][0])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("lat/lon out of range")
            if "from" in q:
                start = date.fromisoformat(q["from"][0])
                days = int(q.get("days", ["31"])[0])
            else:                                   # default: yesterday .. one week ahead
                start = solar_date(datetime.now(timezone.utc), lon) - timedelta(days=1)
                days = int(q.get("days", ["8"])[0]) + 1
            days = max(1, min(days, int(self.store.cfg["api"]["max_days"])))
            body = path_profiles(self.store, round(lat, 4), round(lon, 4), start, days)
            only_clim = body["events"] and all(e["source"] != "forecast" for e in body["events"])
            return self.send_json(200, body, cache_s=86400 if only_clim else 1800)
        except (KeyError, ValueError) as e:
            return self.send_json(400, {"error": f"bad request: {e}"})
        except FileNotFoundError as e:
            return self.send_json(503, {"error": str(e)})
        except LookupError as e:
            return self.send_json(404, {"error": str(e)})
        except RuntimeError as e:
            log.warning("%s", e)
            return self.send_json(503, {"error": str(e)})
        except Exception:
            log.exception("request failed")
            return self.send_json(500, {"error": "internal error"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    Handler.store = Store(cfg)
    Handler.store.refresh()
    Handler.horizons = Horizons(cfg)
    Handler.lightpol = light_pollution.LightPollution(cfg)
    Handler.areas = areas.AreaIndex(areas_path(cfg))
    log.info("%d official areas from %s", len(Handler.areas), Handler.areas.path)
    srv = ThreadingHTTPServer((cfg["api"]["host"], int(cfg["api"]["port"])), Handler)
    log.info("listening on %s:%s", cfg["api"]["host"], cfg["api"]["port"])
    srv.serve_forever()


if __name__ == "__main__":
    main()
