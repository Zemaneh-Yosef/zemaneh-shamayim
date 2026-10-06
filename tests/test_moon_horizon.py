"""Moon horizons (&moon=1), without network: analytic terrain instead of elevation tiles.

    python3 tests/test_moon_horizon.py

Checks that
  - the sun's output does not change when the moon is asked for too (same points, same arrays),
  - the moon's azimuth ranges hold every moonrise / moonset direction of the 18.6-year cycle,
  - in an area, the moon's composite horizon is at the moon_percentile of the spots' horizon heights,
  - the API only changes the cache key when moon=1 is given, and area=auto passes moon=1 on.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import serve  # noqa: E402
import terrain  # noqa: E402

LAT, LON = 31.778, 35.235


class FakeTiles(terrain.Tiles):
    """Rolling local relief, a ridge to the east, hills to the west, a valley through the middle."""

    def __init__(self):
        self.sea_tiles = set()

    def heights(self, lat, lon):
        lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        x = (lon - LON) * 111.2 * math.cos(math.radians(LAT))
        y = (lat - LAT) * 111.2
        h = 750 + 120 * np.sin(x / 1.3) * np.cos(y / 0.9)
        h += 400 * np.exp(-((x - 30) / 6) ** 2)
        h += 250 * np.exp(-((x + 20) / 8) ** 2) * (1 + 0.5 * np.sin(y / 5))
        h -= 150 * np.exp(-(x / 0.6) ** 2)
        return h

    def height(self, lat, lon):
        return float(self.heights(np.array([lat]), np.array([lon]))[0])


def sun_only(res: dict) -> dict:
    """The result without its moon entry, for comparing with a sun-only request."""
    return {k: v for k, v in res.items() if k != "moon" and k != "params"}


def check_sun_unchanged(tiles):
    for kw in ({}, {"radius_km": 0.6, "grid_m": 150}, {"bbox": [31.772, 35.228, 31.784, 35.242], "grid_m": 200}):
        lat, lon = (None, None) if "bbox" in kw else (LAT, LON)
        a = terrain.horizon_set(tiles, lat, lon, max_km=60, **kw)
        b = terrain.horizon_set(tiles, lat, lon, max_km=60, moon=True, **kw)
        assert sun_only(a) == sun_only(b), f"sun output changed with moon=True ({kw})"
        assert "moon" not in a and "moon" not in a["params"], "moon data without moon=True"
        assert b["params"]["moon"] is True and b["moon"]["moonrise"] and b["moon"]["moonset"]
    print("sun output unchanged with moon=True")


def rise_azimuth(lat: float, dec: float) -> float:
    """Azimuth (deg east of north) of a body with this declination on the horizon, rising."""
    return math.degrees(math.acos(math.sin(math.radians(dec)) / math.cos(math.radians(lat))))


def altaz(lat: float, dec: float, hour_angle: float):
    """(altitude, azimuth east of north) in degrees, from vectors (independent of terrain.py's formulas)."""
    p, d, h = math.radians(lat), math.radians(dec), math.radians(hour_angle)
    v = np.array([math.cos(d) * math.cos(h), math.cos(d) * math.sin(h), math.sin(d)])   # equatorial, x to meridian
    up = np.array([math.cos(p), 0, math.sin(p)])
    north = np.array([-math.sin(p), 0, math.cos(p)])
    east = np.cross(up, north)                       # = -y: the hour-angle frame's y axis points west
    alt = math.degrees(math.asin(v @ up))
    az = math.degrees(math.atan2(v @ east, v @ north)) % 360
    return alt, az


def inside(a: float, lo: float, hi: float) -> bool:
    return (a - lo) % 360 <= (hi - lo) % 360 or hi - lo >= 360


def check_ranges(tiles):
    for lat in (0.0, 20.0, 31.78, 40.7, 52.0, 59.9):
        r = terrain.moon_azimuth_ranges(lat)
        sun = terrain.azimuth_ranges(lat)
        for dec in (-28.72, 28.72):
            az = rise_azimuth(lat, dec)
            assert r["moonrise"][0] < az < r["moonrise"][1], (lat, dec, az, r)
            assert r["moonset"][0] < 360 - az < r["moonset"][1], (lat, dec, az, r)
            # still inside while climbing to 10 deg (a Moon first seen over a 10-deg horizon)
            for ha in np.arange(-180, 0, 0.05):
                alt, a = altaz(lat, dec, ha)
                if 0 <= alt <= terrain.MOON_MAX_HORIZON_DEG:
                    assert inside(a, *r["moonrise"]), (lat, dec, alt, a, r["moonrise"])
                    assert inside(360 - a, *r["moonset"]), (lat, dec, alt, 360 - a, r["moonset"])
        assert r["moonrise"][0] < sun["sunrise"][0] and r["moonrise"][1] > sun["sunrise"][1]
        assert r["moonset"][0] < sun["sunset"][0] and r["moonset"][1] > sun["sunset"][1]
    # the composite covers its range every 0.1 deg, with no gaps
    m = terrain.horizon_set(tiles, LAT, LON, max_km=60, moon=True)["moon"]
    for side in ("moonrise", "moonset"):
        az = [e["azimuthDeg"] for e in m[side]]
        steps = np.diff(az)
        assert len(az) > 600 and np.all(np.abs(steps - 0.1) < 0.011), side
        assert all(e["tiltDeg"] == 0 for e in m[side]), "point mode: no tilt"
    r = terrain.moon_azimuth_ranges(LAT)
    print(f"moon ranges ok (Jerusalem: rise {r['moonrise'][0]:.1f}..{r['moonrise'][1]:.1f}, "
          f"set {r['moonset'][0]:.1f}..{r['moonset'][1]:.1f})")


def check_percentile(tiles):
    """At sample azimuths, the composite's horizon is the 90th percentile of the area's spots: about 90%
    of them have a horizon no higher (in the reference frame), and it is well above the lowest."""
    bbox = [31.772, 35.228, 31.784, 35.242]
    res = terrain.horizon_set(tiles, None, None, bbox=bbox, grid_m=200, max_km=60, moon=True, moon_percentile=90.0)
    m = res["moon"]
    lat0, lon0 = m["lat"], m["lon"]
    spots = terrain.candidate_grid(lat0, lon0, bbox=bbox, grid_m=200)
    floor = terrain.surface_floor(tiles.height(lat0, lon0))
    eyes = np.maximum(tiles.heights(np.array([c[0] for c in spots]), np.array([c[1] for c in spots])), floor) + 1.7
    dist, bearing = terrain._offsets(lat0, lon0, spots)
    ds = terrain.distance_steps(1.0, 60, fine=True)
    R = terrain.R_EARTH

    def elev(observer, height, d):                          # geometric elevation, radians (client formula)
        return math.atan2((R + height) * math.cos(d / R) - (R + observer), (R + height) * math.sin(d / R))

    checked, fractions = 0, []
    for side in ("moonrise", "moonset"):
        for e in m[side][::25]:
            a = np.array([e["azimuthDeg"]])
            effs = []
            for k, (la, lo) in enumerate(spots):
                _, d, h = terrain._angles(tiles, la, lo, eyes[k], a, ds, floor=floor)
                tilt = (dist[k] / R) * math.cos(math.radians(a[0] - bearing[k]))
                effs.append(elev(eyes[k], h[0], d[0]) - tilt)
            effs = np.array(effs)
            mine = elev(e["observerM"], e["heightM"], e["distanceKm"] * 1000) - math.radians(e["tiltDeg"])
            frac = float(np.mean(effs <= mine + 1e-9))
            fractions.append(frac)
            assert mine > effs.min() + 1e-9 or effs.min() == effs.max(), "picked the lowest horizon"
            checked += 1
    # coarse selection, fine output: allow a little slack around 90%
    assert checked > 40 and np.median(fractions) >= 0.88 and min(fractions) >= 0.75, (checked, fractions)
    print(f"composite at the 90th percentile ok ({checked} azimuths; median share of spots with a horizon "
          f"no higher: {np.median(fractions):.2f}, lowest {min(fractions):.2f}; {m['spotsUsed']} spots used)")


def check_api_params():
    cfg = serve.load_config("/nonexistent/config.json")      # the defaults
    h = serve.Horizons.__new__(serve.Horizons)                # params() needs only the terrain config
    h.cfg = cfg["terrain"]
    plain = h.params({"lat": [str(LAT)], "lon": [str(LON)]})
    moon = h.params({"lat": [str(LAT)], "lon": [str(LON)], "moon": ["1"]})
    assert "moon" not in plain and "moon_percentile" not in plain, "sun-only cache key must not change"
    assert moon["moon"] is True and moon["moon_percentile"] == 90.0
    assert json.dumps(plain, sort_keys=True) != json.dumps(moon, sort_keys=True)

    class Index:
        def find(self, lat, lon):
            return {"name": "Test", "radiusKm": 1.0, "bbox": [31.77, 35.22, 31.79, 35.25], "id": "t"}
    run, _, _ = serve.resolve_area(Index(), cfg, {"lat": [str(LAT)], "lon": [str(LON)], "area": ["auto"],
                                                 "moon": ["1"]})
    assert run.get("moon") == ["1"], run
    print("API parameters ok")


def main():
    tiles = FakeTiles()
    check_ranges(tiles)
    check_sun_unchanged(tiles)
    check_percentile(tiles)
    check_api_params()
    print("all moon horizon tests passed")


if __name__ == "__main__":
    main()
