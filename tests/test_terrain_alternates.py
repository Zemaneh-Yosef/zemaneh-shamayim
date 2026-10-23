"""Alternate horizon points (terrain.K_ALTERNATES), without network or tiles: a stub terrain with a near ridge
that is the highest under standard refraction and a farther, higher one that overtakes it in strong refraction.

    python3 tests/test_terrain_alternates.py

Also floorM: the lowest surface the line of sight crosses (0 m here; a lake's own surface over a lake).

Eye 800 m over flat 0 m ground; to the east a 1000 m ridge 30 km out and a 3000 m one 149-149.9 km out. Under
k = 0.13 the near ridge is highest (0.265 deg against 0.263 deg); from k ~ 0.15 up the far one is.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import terrain  # noqa: E402

OBS = (31.0, 35.0)
KX = 111195.0 * math.cos(math.radians(OBS[0]))


class StubTiles:
    fine = None

    def heights(self, lat, lon):
        x = (np.asarray(lon) - OBS[1]) * KX                   # metres east (the ridges run north-south)
        h = np.zeros(np.shape(x))
        h[np.abs(x - 30000) < 300] = 1000.0
        h[(x > 149000) & (x < 149900)] = 3000.0
        return h

    def ground_heights(self, lat, lon):
        return np.zeros(np.shape(lat))

    def ground(self, lat, lon):
        return 0.0


def main():
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok &= bool(cond)

    hz = terrain.horizon_for_point(StubTiles(), *OBS, height=800.0, sides=("sunrise", "sunset"), max_km=150)
    e = next(q for q in hz["sunrise"] if abs(q["azimuthDeg"] - 90) < 0.05)
    check(abs(e["distanceKm"] - 30) < 0.5 and e["heightM"] == 1000.0,
          f"main point: the near ridge ({e['distanceKm']} km, {e['heightM']} m)")
    alts = e.get("alternates", [])
    check(any(abs(d - 149.5) < 0.6 and h == 3000.0 for d, h in alts),
          f"alternates carry the far ridge ({alts})")
    check(all(not (abs(d - e["distanceKm"]) < 1e-6 and h == e["heightM"]) for d, h in alts),
          "the main point is not repeated among the alternates")
    west = [q for q in hz["sunset"] if "alternates" in q]
    check(not west, "no alternates over flat ground (sunset side)")
    check(e["floorM"] == 0.0, f"floorM: the flat ground the line of sight crosses ({e['floorM']})")

    class LakeTiles(StubTiles):
        """A lake at 176 m from 1 to 100 km east, then a 230 m shore; the observer on the shore at 176 m."""
        def heights(self, lat, lon):
            x = (np.asarray(lon) - OBS[1]) * KX
            return np.where(x < 100000, 176.0, 230.0)

        def ground(self, lat, lon):
            return 176.0

    lake = terrain.horizon_for_point(LakeTiles(), *OBS, eye=10.0, sides=("sunrise",), max_km=150)
    e = next(q for q in lake["sunrise"] if abs(q["azimuthDeg"] - 90) < 0.05)
    check(e["floorM"] == 176.0, f"floorM over a lake is its surface, not sea level ({e['floorM']})")
    three = terrain._angles(StubTiles(), *OBS, 800.0, np.array([90.0]), terrain.steps_for(StubTiles(), 1, 150))
    check(len(three) == 3, "without alternates=True, _angles still returns three items")
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
