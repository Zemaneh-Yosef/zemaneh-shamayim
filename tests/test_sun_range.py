#!/usr/bin/env python3
"""Sun azimuth ranges past high horizons (terrain.sun_azimuth_ranges), no network.

The winter Sun is first seen over a high horizon well south of its sea-level rise direction (north in the
southern hemisphere). The horizon's range is widened there, only as far as the terrain still hides it, and
nowhere else.
"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import terrain  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def sun_alt_at(lat, dec, az):
    """Altitude (deg) at which the rising Sun of this declination passes azimuth az, by brute force."""
    path = terrain._rise_path(lat, dec, 60)
    best = min(path, key=lambda p: abs(p[0] - az))
    return best[1] if abs(best[0] - az) < 0.1 else None


def flat(_az):
    return np.zeros(len(_az))


def wall(deg, lo=0.0, hi=360.0):
    """A horizon `deg` high at azimuths in [lo, hi], level elsewhere."""
    return lambda az: np.where((np.asarray(az) >= lo) & (np.asarray(az) <= hi), deg, 0.0)


for lat in (31.78, 40.76, 47.37, -37.87, 10.0):
    base = terrain.azimuth_ranges(lat)
    check(terrain.sun_azimuth_ranges(lat, flat) == base, f"lat {lat}: a level horizon leaves the ranges as they were")

# Salt Lake City: a 4 deg horizon everywhere; the solstice Sun clears it past the old edge
lat = 40.76
base = terrain.azimuth_ranges(lat)
r = terrain.sun_azimuth_ranges(lat, wall(4.0))
check(r["sunrise"][1] > base["sunrise"][1] + 2, f"4 deg horizon: sunrise range widened to the south "
      f"({base['sunrise'][1]:.1f} -> {r['sunrise'][1]:.1f})")
check(r["sunset"][0] < base["sunset"][0] - 2, f"4 deg horizon: sunset range widened to the south "
      f"({base['sunset'][0]:.1f} -> {r['sunset'][0]:.1f})")
check(r["sunrise"][0] == base["sunrise"][0] and r["sunset"][1] == base["sunset"][1],
      "4 deg horizon: the northern (summer) edges, where the Sun moves inwards, are unchanged")
# the new edge: past where the solstice Sun stands SUN_CLEAR_DEG above the terrain, by no more than step + pad
edge = r["sunrise"][1] - terrain.SUN_EDGE_PAD_DEG
alt = sun_alt_at(lat, -terrain.SUN_MAX_DEC_DEG, edge)
check(alt is not None and alt <= 4.0 + terrain.SUN_CLEAR_DEG + 0.2,
      f"the last hiding direction ({edge:.1f}) is where the Sun is still within reach of the terrain ({alt:.2f} deg)")
nxt = edge + terrain.SUN_EXTEND_STEP_DEG
alt2 = sun_alt_at(lat, -terrain.SUN_MAX_DEC_DEG, nxt)
check(alt2 is not None and alt2 >= 4.0 + terrain.SUN_CLEAR_DEG - 0.01,
      f"one step further ({nxt:.1f}) the Sun clears the terrain ({alt2:.2f} deg)")

# higher terrain, wider range; mirrored in the southern hemisphere
r10 = terrain.sun_azimuth_ranges(lat, wall(10.0))
check(r10["sunrise"][1] > r["sunrise"][1] + 3, "a 10 deg horizon widens further than a 4 deg one")
rs = terrain.sun_azimuth_ranges(-lat, wall(4.0))
bs = terrain.azimuth_ranges(-lat)
check(abs((bs["sunrise"][0] - rs["sunrise"][0]) - (r["sunrise"][1] - base["sunrise"][1])) < 0.6,
      "southern hemisphere: widened to the north by the same amount")

# only where the terrain is high: a ridge north of the old edge widens nothing
check(terrain.sun_azimuth_ranges(lat, wall(8.0, 0, 100)) == base,
      "a high ridge in the summer directions (moving inwards) widens nothing")

# a ridge just past the old edge, then level: the edge stops once the terrain is clear
ridge = wall(6.0, 0, base["sunrise"][1] + 2)
rr = terrain.sun_azimuth_ranges(lat, ridge)
check(base["sunrise"][1] < rr["sunrise"][1] <= base["sunrise"][1] + 2 + terrain.SUN_EXTEND_STEP_DEG
      + terrain.SUN_EDGE_PAD_DEG + 1e-9, f"a ridge ending 2 deg past the edge: widened only past it "
      f"({rr['sunrise'][1]:.1f})")

# the tropics: the summer Sun moves inwards first and comes back past the northern edge 40+ deg up, so even a
# 15 deg horizon leaves that edge alone; the winter edge widens as elsewhere
lt = 10.0
bt = terrain.azimuth_ranges(lt)
rt = terrain.sun_azimuth_ranges(lt, wall(15.0))
check(rt["sunrise"][0] == bt["sunrise"][0] and rt["sunrise"][1] > bt["sunrise"][1],
      "10 N, 15 deg horizon: only the winter (southern) sunrise edge widens")

# horizon_set: a point beside a high ridge to the south-east gets a wider range and its wins say so
class Ridge:
    fine = None
    sea_tiles: set = set()

    def heights(self, lat, lon):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        y = (lat - 40.0) * 111195.0
        x = (lon + 111.0) * 111195.0 * math.cos(math.radians(40.0))
        d = np.hypot(x, y)
        bearing = np.degrees(np.arctan2(x, y)) % 360
        return np.where((d > 3000) & (d < 6000) & (bearing > 100) & (bearing < 160), 1800.0, 1300.0)

    def ground(self, lat, lon):
        return float(self.heights(np.array([lat]), np.array([lon]))[0])

    def ground_heights(self, lat, lon):
        return self.heights(lat, lon)


res = terrain.horizon_set(Ridge(), 40.0, -111.0, max_km=10)
pt = res["points"][0]
azs = [e["azimuthDeg"] for e in pt["sunrise"]]
b40 = terrain.azimuth_ranges(40.0)
check(max(azs) > b40["sunrise"][1] + 1, f"point beside a ridge: sunrise entries reach {max(azs):.1f} "
      f"(old edge {b40['sunrise'][1]:.1f})")
check(abs(pt["wins"]["sunrise"][0][1] - round(max(azs) % 360, 2)) < 0.11, "its wins cover the widened range")
check(min(e["azimuthDeg"] for e in pt["sunset"]) == round(b40["sunset"][0] % 360, 2),
      "no ridge to the west: the sunset range is as before")
check(terrain.ALGO_VERSION == 4, "algorithm version 4 (horizons recomputed, clients re-fetch)")


# Ridge crests between the distance steps: a sharp hill 600 m across at its base and 300 m high at 60 km (the steps
# there are 240 m apart), its top midway between two steps. (A spike narrower than a step that leaves no trace on
# the samples either side can't be found this way; on 1" data a crest stands on slopes several cells wide.)
class Crest:
    fine = None
    sea_tiles: set = set()
    D, WIDTH, TOP = None, 600.0, 300.0

    def heights(self, lat, lon):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        y = (lat - 40.0) * 111195.0
        x = (lon + 111.0) * 111195.0 * math.cos(math.radians(40.0))
        d = np.hypot(x, y)
        ramp = np.clip((d - 20000) / 60000, 0, 1) * 500.0          # rises 500 m from 20 to 80 km
        spike = np.clip(1 - np.abs(d - self.D) / (self.WIDTH / 2), 0, 1) * self.TOP
        return ramp + spike

    def ground(self, lat, lon):
        return float(self.heights(np.array([lat]), np.array([lon]))[0])

    def ground_heights(self, lat, lon):
        return self.heights(lat, lon)


ds = terrain.distance_steps(1.0, 150.0, fine=True)
k = int(np.searchsorted(ds, 60000))
Crest.D = (ds[k] + ds[k + 1]) / 2                                  # midway between two steps
c = Crest()
az = np.array([90.0, 100.0])
coarse = terrain._angles(c, 40.0, -111.0, 1.7, az, ds)
fine_ = terrain._angles(c, 40.0, -111.0, 1.7, az, ds, refine=True)
dense = terrain._angles(c, 40.0, -111.0, 1.7, az, np.arange(1000.0, 150000.0, 2.0), chunk=1)
miss = np.degrees(dense[0] - coarse[0]) * 60
err = np.degrees(dense[0] - fine_[0]) * 60
check(miss.min() > 3, f"between the steps, the old search misses the crest ({miss.min():.1f}' low)")
check(np.abs(err).max() < 0.2, f"refined: within {np.abs(err).max():.3f}' of 2 m sampling")
# (the hill is placed by a flat-earth distance, which drifts from the great-circle one away from due east: compare
# with the dense search's own crest)
check(np.all(np.abs(fine_[1] - dense[1]) < 20), f"refined: the crest's distance ({fine_[1][0] / 1000:.3f} km, "
      f"2 m sampling {dense[1][0] / 1000:.3f})")
hz = terrain.horizon_for_point(c, 40.0, -111.0, max_km=150, sides=("sunrise",))
check(all(abs(e["distanceKm"] * 1000 - Crest.D) < 100 for e in hz["sunrise"]), "horizon_for_point reports the crest")

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
