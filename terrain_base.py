"""The land the horizon lies on, without the mountains: candidate points for the elevated sunrise's base level.

The website measures an observer's height for elevated sunrise and sunset not from the sea but from the land the
horizon lies on (the base level, KosherJava's horizonElevationAtSunrise / AtSunset). This module supplies that
land; the site's calculator does the optics (which point is the horizon in the day's air, its dip, the base).

Definition C of the report "Elevation-adjusted sunrise from the terrain":
  1. Sample the bare-earth heights on a square grid around the area: GRID_KM cells out to HALF_KM each way
     (~150 km of view plus half the mountain width and a margin). Open sea is level at 0 m; land below sea
     level keeps its height (the Dead Sea, the Jordan valley).
  2. Fill dips narrower than fill_km (a grey closing with a disk that wide): a river like the Hudson by Monsey
     doesn't count, the sea, wide bays and broad lowlands do (Decision 2).
  3. Remove hills narrower than width_km (a grey opening with a disk that wide): the ridge under Jerusalem
     goes, plateaus like Moab stay as land (Decision 1).
  4. For every azimuth of an observer's horizon, sample that surface from 1 to 150 km and keep the points that
     could be the horizon in some air: those that maximise (z - eye)/d - d(1 - k)/2R for a refraction
     coefficient k from -0.5 (hot afternoons) to 0.9 (strong inversions), plus any local maximum within
     NEAR_DEG of winning, so a real (curved, not constant-k) ray can't pick one that was dropped. At k >= 1
     the ray bends as fast as the Earth and no point wins on height alone; the site then uses its
     standard-air base.

The surface depends only on the terrain and the settings, so it is built once per area (an in-process cache
here; the horizons that carry its candidates are stored on disk by serve.py). Where the land it is built from
isn't bare earth (a fallback surface model, which raises the land under cities by the buildings), no
candidates are sent, and the site keeps its last good base (Decision 3).

The flag (Decision 1): where the times still move FLAG_S seconds or more between width_km and check_km, the
width decides the answer, and the place should be decided by hand (an override in terrain.base.overrides: a
width, or no base). The seconds are estimated from the horizon angle under standard refraction and the Sun's
vertical speed at the horizon, 15 deg/h * cos(lat) * |sin(azimuth)|.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict

import numpy as np

import terrain

VERSION = 1                        # bump when the surface or the candidates change for the same settings
GRID_KM = 2.0
HALF_KM = 220.0
D_KM = np.arange(1.0, 150.0 + 1e-9, 0.5)
K_RANGE = np.round(np.arange(-0.5, 0.9 + 1e-9, 0.1), 2)
NEAR_DEG = 0.02
MAX_NEAR = 4
AZ_STEP = 0.5                      # candidates on every entry at a multiple of this azimuth (the surface is smooth)
K_STANDARD = terrain.K_SELECT
DEPRESSION_M = terrain.DEPRESSION_M

DEFAULTS = {"enabled": True, "width_km": 80.0, "fill_km": 5.0, "check_km": 120.0, "flag_s": 30.0,
            "max_non_bare": 0.01, "overrides": []}


def settings(cfg_base: dict | None) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg_base or {})
    return s


def signature(s: dict) -> str:
    """What the candidates depend on besides the terrain: part of serve.py's cache key and /v1/terrain-version."""
    key = {"v": VERSION, "grid": GRID_KM, "half": HALF_KM, "width": s["width_km"], "fill": s["fill_km"],
           "check": s["check_km"], "flag": s["flag_s"], "bare": s["max_non_bare"], "over": s["overrides"]}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:10]


def override_for(s: dict, lat: float, lon: float) -> dict | None:
    """The first override whose circle holds (lat, lon): {"lat", "lon", "radius_km", "width_km" | "none": true}."""
    for o in s.get("overrides") or []:
        dy = (lat - o["lat"]) * 111.19
        dx = (lon - o["lon"]) * 111.19 * math.cos(math.radians(lat))
        if math.hypot(dx, dy) <= float(o.get("radius_km", 5.0)):
            return o
    return None


def _disk(r: int) -> list[tuple[int, int]]:
    return [(i, j) for i in range(-r, r + 1) for j in range(-r, r + 1) if i * i + j * j <= r * r]


def _rank(a: np.ndarray, r: int, op) -> np.ndarray:
    """Grey dilation (op=np.maximum) or erosion (np.minimum) with a disk of radius r cells, edges extended."""
    if r <= 0:
        return a.copy()
    p = np.pad(a, r, mode="edge")
    n, m = a.shape
    out = None
    for i, j in _disk(r):
        win = p[r + i:r + i + n, r + j:r + j + m]
        out = win.copy() if out is None else op(out, win, out=out)
    return out


def closing(a: np.ndarray, r: int) -> np.ndarray:
    return _rank(_rank(a, r, np.maximum), r, np.minimum)


def opening(a: np.ndarray, r: int) -> np.ndarray:
    return _rank(_rank(a, r, np.minimum), r, np.maximum)


def sea_level(H: np.ndarray) -> np.ndarray:
    """Open sea level at 0 m: heights below 0 connected to the grid's edge through heights below 0 are the sea
    (and the skadi tiles' bathymetry under it); closed basins below sea level (the Dead Sea, the Jordan valley,
    Death Valley) keep their heights. The grid reaches 220 km out, so any sea in view touches its edge."""
    below = H < 0
    sea = np.zeros_like(below)
    sea[0, :], sea[-1, :], sea[:, 0], sea[:, -1] = below[0, :], below[-1, :], below[:, 0], below[:, -1]
    while True:                                            # flood fill through the 4-neighbours below 0
        grow = sea.copy()
        grow[1:, :] |= sea[:-1, :]
        grow[:-1, :] |= sea[1:, :]
        grow[:, 1:] |= sea[:, :-1]
        grow[:, :-1] |= sea[:, 1:]
        grow &= below
        if np.array_equal(grow, sea):
            break
        sea = grow
    return np.where(sea, 0.0, H)


def cells(km: float) -> int:
    """A disk km wide, in grid cells of radius (at least one)."""
    return max(1, int(round(km / 2 / GRID_KM)))


class Surface:
    """The mountain-free land around (lat0, lon0), on a local grid of GRID_KM cells (north-south rows from the
    south, east-west columns from the west), at width_km and, for the flag, at check_km."""

    def __init__(self, tiles, lat0: float, lon0: float, s: dict, width_km: float | None = None, non_bare=None):
        self.lat0, self.lon0, self.s = lat0, lon0, s
        self.width_km = float(width_km or s["width_km"])
        n = int(round(2 * HALF_KM / GRID_KM)) + 1
        self.k = (np.arange(n) - n // 2) * GRID_KM
        self.kx = 111.19 * math.cos(math.radians(lat0))
        la = lat0 + self.k[:, None] / 111.19 + 0 * self.k[None, :]
        lo = lon0 + 0 * self.k[:, None] + self.k[None, :] / self.kx
        H = np.asarray(tiles.heights(la, lo), dtype=float)
        H = sea_level(H)
        self.status, self.reason = "ok", None
        if non_bare is not None:
            bad = {}
            for i, j in sorted({(int(math.floor(a)), int(math.floor(b))) for a, b in
                                zip(la[::5, ::5].ravel(), lo[::5, ::5].ravel())}):
                f = non_bare(i, j)
                if f is not None and f > s["max_non_bare"]:
                    bad[terrain.tile_name(i, j)] = round(f, 3)
            if bad:
                self.status = "no-bare-earth"
                self.reason = {"tiles": bad, "maxNonBare": s["max_non_bare"]}
        filled = closing(H, cells(s["fill_km"]))
        self.z = opening(filled, cells(self.width_km))
        self.z_check = opening(filled, cells(s["check_km"])) if s.get("check_km") else None

    def profile(self, lat: float, lon: float, az: np.ndarray, which: str = "z") -> np.ndarray:
        """Heights of the surface along each azimuth at D_KM: shape (len(az), len(D_KM))."""
        grid = self.z if which == "z" else self.z_check
        pla, plo = terrain.destinations(lat, lon, np.asarray(az, float), D_KM * 1000)
        fy = (pla - self.lat0) * 111.19 / GRID_KM + len(self.k) // 2
        fx = (plo - self.lon0) * self.kx / GRID_KM + len(self.k) // 2
        n = len(self.k)
        fy, fx = np.clip(fy, 0, n - 1.000001), np.clip(fx, 0, n - 1.000001)
        i, j = fy.astype(int), fx.astype(int)
        ty, tx = fy - i, fx - j
        return (grid[i, j] * (1 - ty) * (1 - tx) + grid[i, j + 1] * (1 - ty) * tx
                + grid[i + 1, j] * ty * (1 - tx) + grid[i + 1, j + 1] * ty * tx)


def _score(z: np.ndarray, eye: float, k: float) -> np.ndarray:
    d = D_KM * 1000
    return (z - eye) / d - d * (1 - k) / (2 * terrain.R_EARTH)


def candidates(z: np.ndarray, eye: float) -> list[list[float]]:
    """[[distance km, height m], ...]: the points of one profile that can be the horizon for some k in K_RANGE,
    with any local maximum within NEAR_DEG of the winner."""
    keep: set[int] = set()
    near: dict[int, float] = {}                           # local maximum -> how close it came to winning (rad)
    tol = math.radians(NEAR_DEG)
    for k in K_RANGE:
        v = _score(z, eye, float(k))
        best = float(v.max())
        keep.add(int(np.argmax(v)))
        peak = np.r_[v[0] >= v[1], (v[1:-1] >= v[:-2]) & (v[1:-1] >= v[2:]), v[-1] >= v[-2]]
        for i in np.where(peak & (v >= best - tol))[0]:
            near[int(i)] = min(near.get(int(i), tol), best - float(v[i]))
    # a near winner counts when it is another crest, not a step along one a k already won (2 km: one grid
    # cell), the MAX_NEAR closest to winning
    won = np.array(sorted(keep))
    other = sorted((gap, i) for i, gap in near.items() if np.min(np.abs(D_KM[won] - D_KM[i])) > GRID_KM)
    keep.update(i for _, i in other[:MAX_NEAR])
    return [[round(float(D_KM[i]), 1), round(float(z[i]), 1)] for i in sorted(keep)]


def flag_seconds(surface: Surface, lat: float, lon: float, eye: float, az: np.ndarray) -> float:
    """Largest estimated shift of the time between width_km and check_km over these azimuths, seconds."""
    if surface.z_check is None or not len(az):
        return 0.0
    a = np.asarray(az, float)
    ang = []
    for which in ("z", "check"):
        prof = surface.profile(lat, lon, a, which)
        ang.append(np.degrees(np.arctan(_score(prof, eye, K_STANDARD).max(axis=1))))
    rate = 15.0 / 3600.0 * math.cos(math.radians(lat)) * np.maximum(np.abs(np.sin(np.radians(a))), 0.2)
    return float(np.max(np.abs(ang[0] - ang[1]) / rate))


_CACHE: OrderedDict = OrderedDict()
_LOCK = threading.Lock()


def surface_for(tiles, lat0: float, lon0: float, s: dict, width_km: float | None = None, non_bare=None,
                key_extra: str = "") -> Surface:
    """The area's surface, from a small in-process cache (an area's spots share it; lat / lon to 0.01 deg)."""
    key = (round(lat0, 2), round(lon0, 2), signature(s), width_km, key_extra)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    surf = Surface(tiles, round(lat0, 2), round(lon0, 2), s, width_km, non_bare)
    with _LOCK:
        _CACHE[key] = surf
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return surf


def annotate(hz: dict, surface: Surface | None, s: dict, *, none_reason: str | None = None) -> None:
    """Add the base fields to one horizon_for_point result: the sunrise / sunset entries at every AZ_STEP of
    azimuth get baseCandidates (the site uses the nearest such entry), and the point a "base" summary ({version, widthKm, fillKm, status, reason?, flagSeconds?, flagged?})."""
    base = {"version": VERSION, "fillKm": s["fill_km"]}
    if surface is None or surface.status != "ok":
        base.update(status="none" if surface is None else surface.status,
                    reason=none_reason if surface is None else surface.reason)
        hz["base"] = base
        return
    base.update(widthKm=surface.width_km, status="ok")
    eye = float(hz["height"])
    flag = 0.0
    for side in ("sunrise", "sunset"):
        entries = hz.get(side) or []
        if not entries:
            continue
        near: dict[int, dict] = {}                         # the entry nearest each multiple of AZ_STEP
        for e in entries:
            m = round(e["azimuthDeg"] / AZ_STEP)
            if m not in near or abs(e["azimuthDeg"] - m * AZ_STEP) < abs(near[m]["azimuthDeg"] - m * AZ_STEP):
                near[m] = e
        on = list(near.values())
        az = np.array([e["azimuthDeg"] for e in on], float)
        prof = surface.profile(hz["lat"], hz["lon"], az)
        for e, z in zip(on, prof):
            e["baseCandidates"] = candidates(z, eye)
        flag = max(flag, flag_seconds(surface, hz["lat"], hz["lon"], eye, az))
    if surface.z_check is not None:
        base["checkKm"] = s["check_km"]
        base["flagSeconds"] = round(flag, 1)
        base["flagged"] = flag >= s["flag_s"]
    hz["base"] = base
