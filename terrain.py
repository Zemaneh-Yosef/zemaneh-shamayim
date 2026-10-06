"""Terrain horizons for visible sunrise / sunset, from SRTM-format elevation tiles.

Tiles come from the AWS Terrain Tiles "skadi" set (1 arc-second, free, no key; US data from USGS 3DEP,
elsewhere mainly SRTM), are downloaded on first use and kept in a cache folder. Over open sea there is
no tile; such places are sea level (0 m). With terrain.layers configured, merged bare-earth tiles from
dem_layers.py (3DEP, MRDEM, FABDEM, ...) are used instead wherever they exist.

horizon_for_point():  the horizon (the highest-angle terrain point, every 0.1 deg of azimuth across
                      the year's sunrise and sunset directions) seen from one observer.
vantage_search():     all grid points within a radius, keeping the few that - in some direction - have
                      the lowest horizon, i.e. give the earliest sunrise / latest sunset there.

moon_horizon():       with moon=True, a "moon" entry: ONE composite horizon over the Moon's wider range of
                      directions. At each azimuth it is the horizon of the spot at the `moon_percentile`
                      (default 90th) percentile of horizon height among all the area's spots - a HIGH
                      horizon, i.e. a late moonrise / early moonset. Birkat halevana needs the Moon actually
                      seen, so the time printed for a neighbourhood is when nearly all of it (not its best
                      spot) can see the Moon; the percentile keeps a few spots at the foot of a slope from
                      setting the time for everyone. The sun's output is unchanged.

Why a composite rather than vantage points: the 90th-percentile spot changes every few degrees on rolling
terrain (a 3 km box needed 100+ spots to represent it), while the Moon's position barely depends on which
spot in a few km it is seen from. So each azimuth carries its own spot's terrain point and eye height, and
`tiltDeg`: how much higher the Moon stands from that spot than from the reference point (the spot is
s metres from it towards bearing B: s/R * cos(azimuth - B)), so the client computes the Moon once.

Terrain closer than min_km to an observer is ignored ("near obstructions removed"): buildings, trees
and the observer's own surroundings, which the elevation data represents badly.
The highest point is chosen with a standard refraction coefficient (k = 0.13); the calculator then
traces the real refraction to it.
"""
from __future__ import annotations

import gzip
import io
import logging
import math
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

import numpy as np

log = logging.getLogger("terrain")

R_EARTH = 6371008.8
K_SELECT = 0.13
REFF = R_EARTH / (1 - K_SELECT)
SKADI = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz"


def tile_name(lat_i: int, lon_i: int) -> str:
    return f"{'N' if lat_i >= 0 else 'S'}{abs(lat_i):02d}{'E' if lon_i >= 0 else 'W'}{abs(lon_i):03d}"


class Tiles:
    """Elevation tiles: local folder first, then download (if allowed); absent tile = open sea (0 m).

    With layered_dir, merged bare-earth tiles (dem_layers.py, float32 .npy on the same grid) are used
    wherever they exist; `builder` (optional) is called to make a missing one. Without them, or if
    building fails, the skadi tiles are used as before.
    """

    def __init__(self, folder: Path, url_template: str | None = SKADI, max_in_memory: int = 12,
                 user_agent: str = "zmanim-sky-server/0.1", layered_dir: Path | None = None,
                 builder=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.url = url_template
        self.ua = user_agent
        self.cache: OrderedDict = OrderedDict()
        self.max = max_in_memory
        self.lock = threading.Lock()
        self.sea_tiles: set[str] = set()
        self.layered_dir = Path(layered_dir) if layered_dir else None
        self.builder = builder
        self.layered_used = 0                              # tiles served from merged data (for logs / tests)

    def _load_layered(self, lat_i: int, lon_i: int):
        """Merged tile (memory-mapped) or None to fall back to skadi."""
        if self.layered_dir is None:
            return None
        f = self.layered_dir / f"{tile_name(lat_i, lon_i)}.npy"
        if f.exists():
            return np.load(f, mmap_mode="r")
        if f.with_suffix(".sea").exists():                 # built, and nothing but sea
            return None
        if self.builder is not None:
            try:
                return self.builder.build(lat_i, lon_i)
            except Exception as e:                         # never fail a request over the better data
                log.warning("could not build merged tile %s (%s); using skadi", tile_name(lat_i, lon_i), e)
        return None

    def _load(self, name: str):
        for p in (self.folder / f"{name}.hgt", self.folder / f"{name}.hgt.gz"):
            if p.exists():
                raw = gzip.open(p).read() if p.suffix == ".gz" else p.read_bytes()
                a = np.frombuffer(raw, dtype=">i2")
                n = int(round(math.sqrt(a.size)))
                a = a.reshape(n, n).astype(np.int16)
                return np.where(a == -32768, 0, a).astype(np.int16)
        if (self.folder / f"{name}.sea").exists():
            return None
        if not self.url:
            raise FileNotFoundError(f"elevation tile {name} is not in {self.folder} (downloads disabled)")
        url = self.url.format(ns=name[:3], name=name)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            body = e.read(2000) or b""
            # S3 answers a missing object (no tile = open sea) with 403/404 and an XML <Error>; anything
            # else (a proxy, a firewall) is a real failure and must not be taken for sea
            if e.code in (403, 404) and b"<Error>" in body:
                (self.folder / f"{name}.sea").write_text("")
                return None
            raise RuntimeError(f"could not download elevation tile {name} from {url}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not download elevation tile {name} from {url}: {e.reason}") from e
        (self.folder / f"{name}.hgt.gz").write_bytes(data)
        log.info("downloaded elevation tile %s (%.1f MB)", name, len(data) / 1e6)
        return self._load(name)

    def tile(self, lat_i: int, lon_i: int):
        name = tile_name(lat_i, lon_i)
        with self.lock:
            if name in self.cache:
                self.cache.move_to_end(name)
                return self.cache[name]
            a = self._load_layered(lat_i, lon_i)
            if a is not None:
                self.layered_used += 1
            else:
                a = self._load(name)
            if a is None:
                self.sea_tiles.add(name)
            self.cache[name] = a
            if len(self.cache) > self.max:
                self.cache.popitem(last=False)
            return a

    def heights(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Bilinear heights (m) for arrays of coordinates; 0 over open sea."""
        lat = np.asarray(lat, dtype=np.float64)
        lon = (np.asarray(lon, dtype=np.float64) + 180) % 360 - 180
        out = np.zeros(lat.shape, dtype=np.float64)
        li, lj = np.floor(lat).astype(int), np.floor(lon).astype(int)
        keys = li * 1000 + lj
        for key in np.unique(keys):
            m = keys == key
            ti, tj = int(li[m].flat[0]), int(lj[m].flat[0])
            a = self.tile(ti, tj)
            if a is None:
                continue
            n = a.shape[0]
            r = (ti + 1 - lat[m]) * (n - 1)
            c = (lon[m] - tj) * (n - 1)
            i = np.clip(r.astype(int), 0, n - 2)
            j = np.clip(c.astype(int), 0, n - 2)
            fy, fx = r - i, c - j
            out[m] = (a[i, j] * (1 - fy) * (1 - fx) + a[i, j + 1] * (1 - fy) * fx
                      + a[i + 1, j] * fy * (1 - fx) + a[i + 1, j + 1] * fy * fx)
        return out

    def height(self, lat: float, lon: float) -> float:
        return float(self.heights(np.array([lat]), np.array([lon]))[0])


def destinations(lat: float, lon: float, az_deg: np.ndarray, d_m: np.ndarray):
    """Great-circle destinations for every (azimuth, distance) pair: arrays shaped (len(az), len(d))."""
    p1, l1 = math.radians(lat), math.radians(lon)
    a = np.radians(az_deg)[:, None]
    d = (d_m / R_EARTH)[None, :]
    sp2 = math.sin(p1) * np.cos(d) + math.cos(p1) * np.sin(d) * np.cos(a)
    p2 = np.arcsin(np.clip(sp2, -1, 1))
    l2 = l1 + np.arctan2(np.sin(a) * np.sin(d) * math.cos(p1), np.cos(d) - math.sin(p1) * sp2)
    return np.degrees(p2), np.degrees(l2)


def azimuth_ranges(lat: float, margin: float = 3.0):
    """{'sunrise': (from, to), 'sunset': (from, to)} azimuths over the year at this latitude (+margin)."""
    c = math.sin(math.radians(23.44)) / math.cos(math.radians(lat))
    spread = 90.0 if c >= 1 else math.degrees(math.asin(c))
    return {"sunrise": (90 - spread - margin, 90 + spread + margin),
            "sunset": (270 - spread - margin, 270 + spread + margin)}


# The Moon's declination reaches the ecliptic's 23.44 deg plus the orbit's 5.145 deg inclination, plus up to
# ~0.15 deg of perturbations (major lunar standstill: about 28.7 deg).
MOON_MAX_DEC_DEG = 28.75
# Over a high horizon the Moon is first seen well above it, and by then it has moved along its slanted path,
# outwards (away from due east / west) at the southern (northern) extreme. The range covers horizons up to
# this high; beyond it the client takes the edge's horizon as continuing.
MOON_MAX_HORIZON_DEG = 10.0


def _moon_drift(lat: float, max_alt: float) -> float:
    """Largest outward azimuth change (deg) of a rising body at the extreme declinations between the
    horizon and max_alt degrees of altitude (exact spherical astronomy, sampled)."""
    phi = math.radians(lat)
    worst = 0.0
    for dec_deg in (MOON_MAX_DEC_DEG, -MOON_MAX_DEC_DEG):
        dec = math.radians(dec_deg)
        def az_alt(H):                                     # hour angle (rad, negative = rising) -> az, alt
            sin_alt = math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(H)
            alt = math.asin(max(-1.0, min(1.0, sin_alt)))
            az = math.atan2(-math.cos(dec) * math.sin(H),
                            math.sin(dec) * math.cos(phi) - math.cos(dec) * math.sin(phi) * math.cos(H))
            return math.degrees(az) % 360, math.degrees(alt)
        cos_h0 = -math.tan(phi) * math.tan(dec)
        if not -1 < cos_h0 < 1:
            continue                                       # never rises / never sets here
        H, step = -math.acos(cos_h0), math.radians(0.05)
        az0, _ = az_alt(H)
        for _ in range(4000):                              # climb until max_alt (or culmination)
            H += step
            if H >= 0:
                break
            az, alt = az_alt(H)
            out = (az - az0) if az0 > 90 else (az0 - az)   # outward = away from due east
            worst = max(worst, out)
            if alt >= max_alt:
                break
    return worst


def moon_azimuth_ranges(lat: float, max_horizon_deg: float = MOON_MAX_HORIZON_DEG):
    """{'moonrise': (from, to), 'moonset': (from, to)}: every rise / set direction over the 18.6-year cycle,
    widened so a Moon first seen over a horizon up to max_horizon_deg high is still inside (the drift grows
    with latitude, as the Moon rises at a shallower angle), plus 1 deg and the lit limb's reach."""
    c = math.sin(math.radians(MOON_MAX_DEC_DEG)) / math.cos(math.radians(lat))
    spread = 90.0 if c >= 1 else math.degrees(math.asin(c))
    margin = min(_moon_drift(lat, max_horizon_deg) + 1.3, 179.0 - spread)
    return {"moonrise": (90 - spread - margin, 90 + spread + margin),
            "moonset": (270 - spread - margin, 270 + spread + margin)}


def distance_steps(min_km: float, max_km: float, fine: bool = True) -> np.ndarray:
    """~One elevation cell apart nearby, growing with range (coarser for the vantage pre-screen)."""
    out, d = [], max(30.0, min_km * 1000)
    grow, floor = (0.004, 15.0) if fine else (0.015, 40.0)
    while d <= max_km * 1000:
        out.append(d)
        d += max(floor, d * grow)
    return np.array(out)


# The AWS "skadi" tiles carry ocean depths (bathymetry) where SRTM has no land: off New York they go to
# -85 m within the tile, -3200 m further out. The line of sight meets the water surface, not the sea
# floor, so heights are raised to sea level. Places really below sea level (Dead Sea, Jordan valley,
# Death Valley, Caspian) keep their heights when the observer stands in one (ground below
# DEPRESSION_M); there the floor is the observer's own ground instead.
DEPRESSION_M = -20.0


def surface_floor(raw_ground: float) -> float:
    """Lowest height the visible surface can have around an observer whose raw tile height is raw_ground."""
    return raw_ground if raw_ground < DEPRESSION_M else 0.0


def _angles(tiles: Tiles, lat, lon, h_obs, az, ds, chunk: int = 128, floor: float = 0.0):
    """For each azimuth: (best angle proxy [rad], its distance [m], its terrain height [m])."""
    best_a, best_d, best_h = np.empty(len(az)), np.empty(len(az)), np.empty(len(az))
    for s in range(0, len(az), chunk):                   # in chunks, to keep memory small
        a = az[s:s + chunk]
        plat, plon = destinations(lat, lon, a, ds)
        H = np.maximum(tiles.heights(plat, plon), floor)
        ang = (H - h_obs) / ds[None, :] - ds[None, :] / (2 * REFF)
        k = np.argmax(ang, axis=1)
        rows = np.arange(len(a))
        best_a[s:s + chunk], best_d[s:s + chunk], best_h[s:s + chunk] = ang[rows, k], ds[k], H[rows, k]
    return best_a, best_d, best_h


def horizon_for_point(tiles: Tiles, lat: float, lon: float, *, eye: float = 1.7, height: float | None = None,
                      min_km: float = 1.0, max_km: float = 150.0, az_step: float = 0.1,
                      sides=("sunrise", "sunset"), az_ranges: dict | None = None,
                      floor: float | None = None) -> dict:
    raw = tiles.height(lat, lon)
    floor = surface_floor(raw) if floor is None else floor
    ground = max(raw, floor)
    h_obs = height if height is not None else ground + eye
    ds = distance_steps(min_km, max_km, fine=True)
    out = {"lat": round(lat, 6), "lon": round(lon, 6), "ground": round(ground, 1), "height": round(h_obs, 1)}
    ranges = az_ranges or azimuth_ranges(lat)
    for side in sides:
        lo, hi = ranges[side]
        az = np.arange(lo, hi + 1e-9, az_step)
        _, dist, hgt = _angles(tiles, lat, lon, h_obs, az, ds, floor=floor)
        out[side] = [{"azimuthDeg": round(float(a) % 360, 2), "distanceKm": round(float(d) / 1000, 3),
                      "heightM": round(float(h), 1)} for a, d, h in zip(az, dist, hgt)]
    return out


M_PER_DEG = 111194.9


def candidate_grid(lat: float, lon: float, *, radius_km: float = 0.0, bbox=None, grid_m: float = 100.0):
    """Grid of candidate spots: within radius_km of (lat, lon), or inside bbox (south, west, north, east).
    Local flat approximation; fine at neighbourhood scale."""
    kx = M_PER_DEG * math.cos(math.radians(lat))
    if bbox is not None:
        s, w, n, e = bbox
        ny, nx = int((n - s) * M_PER_DEG // grid_m), int((e - w) * kx // grid_m)
        # centre the grid in the box so its edges are covered evenly
        oy = ((n - s) * M_PER_DEG - ny * grid_m) / 2
        ox = ((e - w) * kx - nx * grid_m) / 2
        return [(s + (oy + iy * grid_m) / M_PER_DEG, w + (ox + ix * grid_m) / kx)
                for iy in range(ny + 1) for ix in range(nx + 1)]
    n = int(radius_km * 1000 // grid_m)
    return [(lat + iy * grid_m / M_PER_DEG, lon + ix * grid_m / kx)
            for iy in range(-n, n + 1) for ix in range(-n, n + 1)
            if math.hypot(ix * grid_m, iy * grid_m) <= radius_km * 1000 + 1e-6]


def _select(prof: np.ndarray, tol_deg: float, max_points: int):
    """Fewest candidate spots so that every azimuth (column of prof: angle proxies [rad]) has one with the
    lowest horizon, within tol_deg. Returns (chosen candidate indices, boolean matrix: good enough)."""
    env = prof.min(axis=0)
    good = prof <= env[None, :] + math.radians(tol_deg)      # within tolerance of the lowest horizon
    # greedy set cover: fewest points so that every azimuth has a (near-)best point
    uncovered = np.ones(prof.shape[1], dtype=bool)
    chosen = []
    while uncovered.any() and len(chosen) < max_points:
        gain = (good & uncovered[None, :]).sum(axis=1)
        p = int(np.argmax(gain))
        if gain[p] == 0:
            break
        chosen.append(p)
        uncovered &= ~good[p]
    if uncovered.any():                                       # cap reached: exact best for the rest
        for k in np.where(uncovered)[0]:
            p = int(np.argmin(prof[:, k]))
            if p not in chosen:
                chosen.append(p)
    return chosen, good


def vantage_search(tiles: Tiles, lat: float, lon: float, radius_km: float = 0.0, *, bbox=None,
                   grid_m: float = 100.0, eye: float = 1.7, min_km: float = 1.0, max_km: float = 150.0,
                   max_points: int = 12, tol_deg: float = 0.003, max_candidates: int | None = None,
                   moon: bool = False, moon_percentile: float = 90.0) -> dict:
    """Spots within radius_km of (lat, lon), or inside bbox (south, west, north, east), whose horizon is
    the lowest somewhere in the sunrise / sunset directions, each with its full-resolution horizon and
    the azimuths where it wins. With max_candidates, grid_m is widened until the grid fits.
    With moon, also the composite moon horizon at moon_percentile (see moon_horizon)."""
    cands = candidate_grid(lat, lon, radius_km=radius_km, bbox=bbox, grid_m=grid_m)
    while max_candidates and len(cands) > max_candidates:
        grid_m *= math.sqrt(len(cands) / max_candidates) * 1.02
        cands = candidate_grid(lat, lon, radius_km=radius_km, bbox=bbox, grid_m=grid_m)
    floor = surface_floor(tiles.height(lat, lon))
    raw = tiles.heights(np.array([c[0] for c in cands]), np.array([c[1] for c in cands])) if cands else np.array([])
    # spots in the sea / a bay (below sea level where the area isn't a depression) can't be stood on
    dry = raw >= floor - 3.0
    if dry.any():
        cands, raw = [c for c, k in zip(cands, dry) if k], raw[dry]
    if not cands:
        cands, raw = [(lat, lon)], np.array([tiles.height(lat, lon)])
    grounds = np.maximum(raw, floor)
    ranges = azimuth_ranges(lat)
    ds_coarse = distance_steps(min_km, max_km, fine=False)
    winners: dict[int, dict] = {}
    for side, (lo, hi) in ranges.items():
        az = np.arange(lo, hi + 1e-9, 0.5)
        prof = np.empty((len(cands), len(az)))
        for p, (la, lo_) in enumerate(cands):
            prof[p] = _angles(tiles, la, lo_, grounds[p] + eye, az, ds_coarse, floor=floor)[0]
        chosen, good = _select(prof, tol_deg, max_points)
        for p in chosen:
            azs = [float(az[k]) for k in np.where(good[p])[0]]
            if azs:
                winners.setdefault(p, {"wins": {}})["wins"][side] = _ranges(azs, 0.5)
    points = []
    for p, w in winners.items():
        la, lo_ = cands[p]
        hz = horizon_for_point(tiles, la, lo_, eye=eye, min_km=min_km, max_km=max_km,
                               sides=tuple(w["wins"]), az_ranges=ranges, floor=floor)
        hz["wins"] = w["wins"]
        for side, rr in w["wins"].items():                   # keep only the directions this point serves
            keep = lambda a: any(_in_range(a, lo - 3, hi + 3) for lo, hi in rr)   # noqa: E731
            hz[side] = [q for q in hz[side] if keep(q["azimuthDeg"])]
        points.append(hz)
    points.sort(key=lambda q: -sum(b - a for r in q["wins"].values() for a, b in r))
    out = {"candidates": len(cands), "gridM": round(grid_m, 1), "points": points}
    if moon:
        out["moon"] = moon_horizon(tiles, lat, lon, cands, grounds + eye, floor=floor, min_km=min_km,
                                   max_km=max_km, percentile=moon_percentile)
    return out


def _offsets(lat0: float, lon0: float, spots):
    """Distance (m) and bearing (deg) of each spot from (lat0, lon0); local flat approximation."""
    kx = M_PER_DEG * math.cos(math.radians(lat0))
    dx = np.array([(lo - lon0) * kx for _, lo in spots])
    dy = np.array([(la - lat0) * M_PER_DEG for la, _ in spots])
    return np.hypot(dx, dy), np.degrees(np.arctan2(dx, dy))


def moon_horizon(tiles: Tiles, lat0: float, lon0: float, spots, heights, *, floor: float = 0.0,
                 min_km: float = 1.0, max_km: float = 150.0, percentile: float = 90.0,
                 az_step: float = 0.1, coarse_step: float = 0.5) -> dict:
    """Composite moonrise / moonset horizon of an area, seen from the reference point (lat0, lon0).

    spots: [(lat, lon)], heights: eye heights above sea level. At each coarse azimuth the spots are ranked
    by horizon height in the reference frame (their own horizon minus the tilt of their zenith towards
    that azimuth), and the one at `percentile` ("higher": a real spot, at or above it) is taken; each fine
    azimuth then uses the spot taken for the nearest coarse azimuth, traced at full resolution.

    Entries: {azimuthDeg, distanceKm, heightM (terrain point), observerM (that spot's eye height), tiltDeg}.
    The client's elevation for an entry is geometricElevation(observerM, heightM, distance) - tiltDeg."""
    heights = np.asarray(heights, dtype=float)
    dist, bearing = _offsets(lat0, lon0, spots)
    ranges = moon_azimuth_ranges(lat0)
    ds_coarse = distance_steps(min_km, max_km, fine=False)
    ds_fine = distance_steps(min_km, max_km, fine=True)
    out = {"lat": round(lat0, 6), "lon": round(lon0, 6), "percentile": percentile, "spots": len(spots)}
    used = set()
    for side, (lo, hi) in ranges.items():
        fine = np.arange(lo, hi + 1e-9, az_step)
        if len(spots) == 1:
            pick = np.zeros(len(fine), dtype=int)
        else:
            coarse = np.arange(lo, hi + 1e-9, coarse_step)
            prof = np.empty((len(spots), len(coarse)))
            for k, ((la, lo_), h) in enumerate(zip(spots, heights)):
                prof[k] = _angles(tiles, la, lo_, h, coarse, ds_coarse, floor=floor)[0]
            # in the reference frame: a spot's zenith leans towards its bearing, so the Moon stands higher
            # (its horizon is effectively lower) in that direction
            tilt = (dist[:, None] / R_EARTH) * np.cos(np.radians(coarse[None, :] - bearing[:, None]))
            eff = prof - tilt
            target = np.percentile(eff, percentile, axis=0, method="higher")
            chosen = np.argmin(np.abs(eff - target[None, :]), axis=0)     # the spot at the percentile
            nearest = np.clip(np.rint((fine - lo) / coarse_step).astype(int), 0, len(coarse) - 1)
            pick = chosen[nearest]
        entries = [None] * len(fine)
        for k in np.unique(pick):
            idx = np.where(pick == k)[0]
            la, lo_ = spots[k]
            _, d, hgt = _angles(tiles, la, lo_, heights[k], fine[idx], ds_fine, floor=floor)
            t = math.degrees(dist[k] / R_EARTH) * np.cos(np.radians(fine[idx] - bearing[k]))
            for j, a, dd, hh, tt in zip(idx, fine[idx], d, hgt, t):
                entries[j] = {"azimuthDeg": round(float(a) % 360, 2), "distanceKm": round(float(dd) / 1000, 3),
                              "heightM": round(float(hh), 1), "observerM": round(float(heights[k]), 1),
                              "tiltDeg": round(float(tt), 5)}
            used.add(int(k))
        out[side] = entries
    out["spotsUsed"] = len(used)
    return out


def _in_range(a: float, lo: float, hi: float) -> bool:
    return ((a - lo) % 360) <= ((hi - lo) % 360)


def _ranges(azs: list[float], step: float):
    """Sorted azimuths -> [[from, to], ...] merged ranges, widened by one step on each side."""
    azs = sorted(azs)
    out = [[azs[0], azs[0]]]
    for a in azs[1:]:
        if a - out[-1][1] <= step * 1.5:
            out[-1][1] = a
        else:
            out.append([a, a])
    return [[round((a - step) % 360, 2), round((b + step) % 360, 2)] for a, b in out]


def horizon_set(tiles: Tiles, lat: float | None = None, lon: float | None = None, *, radius_km: float = 0.0,
                bbox=None, grid_m: float = 100.0, eye: float = 1.7, height: float | None = None,
                min_km: float = 1.0, max_km: float = 150.0, max_candidates: int | None = None,
                moon: bool = False, moon_percentile: float = 90.0) -> dict:
    """What the API returns: the exact point, or the vantage winners within radius_km of it or inside
    bbox (south, west, north, east; lat / lon then default to the box centre). With moon, also the
    moonrise / moonset sides (vantage mode: the spots at moon_percentile of horizon height)."""
    if bbox is not None:
        s_, w, n, e = bbox
        if not (s_ < n and w < e):
            raise ValueError("bbox must be south,west,north,east with south < north and west < east")
        lat = (s_ + n) / 2 if lat is None else lat
        lon = (w + e) / 2 if lon is None else lon
    params = {"lat": lat, "lon": lon, "radiusKm": radius_km, "bbox": list(bbox) if bbox else None,
              "gridM": grid_m, "eyeM": eye, "heightM": height, "minKm": min_km, "maxKm": max_km}
    if moon:                                     # only when asked, so the sun-only output is unchanged
        params["moon"] = True
        params["moonPercentile"] = moon_percentile
    if radius_km <= 0 and bbox is None:
        hz = horizon_for_point(tiles, lat, lon, eye=eye, height=height, min_km=min_km, max_km=max_km)
        r = azimuth_ranges(lat)
        hz["wins"] = {s: [[round(a % 360, 2), round(b % 360, 2)]] for s, (a, b) in r.items()}
        res = {"mode": "point", "points": [hz]}
        if moon:
            fl = surface_floor(tiles.height(lat, lon))
            res["moon"] = moon_horizon(tiles, lat, lon, [(lat, lon)], [hz["height"]], floor=fl,
                                       min_km=min_km, max_km=max_km, percentile=moon_percentile)
    else:
        res = {"mode": "vantage", **vantage_search(tiles, lat, lon, radius_km, bbox=bbox, grid_m=grid_m, eye=eye,
                                                   min_km=min_km, max_km=max_km, max_candidates=max_candidates,
                                                   moon=moon, moon_percentile=moon_percentile)}
    res["params"] = params
    res["seaTiles"] = sorted(tiles.sea_tiles)
    return res
