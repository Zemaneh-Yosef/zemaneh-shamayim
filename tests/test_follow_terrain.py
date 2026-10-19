"""Columns stood on the real ground (api.follow_terrain): climatology.follow_terrain / real_ground, and
serve.Ground over synthetic elevation tiles. No network needed.

    python3 tests/test_follow_terrain.py
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import serve  # noqa: E402
import terrain  # noqa: E402
from climatology import G0, R_DRY, build_profile, follow_terrain, real_ground  # noqa: E402

FOLLOW = {"enabled": True, "max_km": 150.0, "full_m": 500.0, "fade_m": 2000.0, "ground_radius_m": 1000.0}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def fields(orog: float, t2m: float = 280.0, inversion: float = 4.0) -> dict:
    """GFS-like fields over ground at `orog`: a 2 m air `inversion` K colder than the 80 m air, standard
    lapse above."""
    psfc = 101325.0 * (1 - 0.0065 * orog / 288.15) ** 5.2559
    f = {"t2m": t2m, "tsfc": t2m - 1, "psfc": psfc, "orog": orog, "land": 1.0,
         "t80m": t2m + inversion, "t100m": t2m + inversion - 0.1}
    for p in (1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500):
        z = 44330.8 * (1 - (p * 100 / 101325.0) ** 0.190263)
        f[f"z{p}"], f[f"t{p}"] = z, 288.15 - 0.0065 * z
    return f


def hydrostatic_ok(levels) -> bool:
    for lo, hi in zip(levels, levels[1:]):
        want = lo["p"] * math.exp(-G0 * (hi["h"] - lo["h"]) / (R_DRY * ((lo["t"] + hi["t"]) / 2 + 273.15)))
        if abs(hi["p"] - want) > 0.05 * max(1.0, (hi["h"] - lo["h"]) / 100):
            return False
    return True


def main():
    levels_mb = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500]

    print("1. Jerusalem-like: GFS ground 583 m, real ground 780 m")
    f = fields(583.0)
    plain = build_profile(f, levels_mb, 0, 31.77, 35.23)
    moved = build_profile(f, levels_mb, 0, 31.77, 35.23, ground_samples=[780.0] * 5, follow=FOLLOW)
    lp, lm = plain["levels"], moved["levels"]
    check(moved["groundM"] == 780.0 and moved["modelGroundM"] == 583.0, "ground recorded")
    check([round(b["h"] - a["h"], 1) for a, b in zip(lp[:3], lm[:3])] == [197.0] * 3,
          f"2 m / 80 m / 100 m air moved up with the ground: {[lv['h'] for lv in lm[:3]]}")
    check([a["t"] for a in lp] == [b["t"] for b in lm], "temperatures kept")
    check(all(a["h"] < b["h"] for a, b in zip(lm, lm[1:])), "heights still increasing")
    top = [lv for lv in lp if lv["h"] - 583 >= 2000]
    check(top and all(lv in lm for lv in top), f"{len(top)} levels 2 km above the model ground untouched")
    check(abs(lm[0]["p"] - lp[0]["p"] * math.exp(-G0 * 197 / (R_DRY * 280))) < 1.5,
          f"surface pressure on the real ground {lm[0]['p']} mb (model ground {lp[0]['p']})")
    check(hydrostatic_ok(lm[:-1]), "moved levels hydrostatic")
    check(lm[1]["t"] - lm[0]["t"] == 4.0, "the inversion travels with the ground")

    print("2. Dead Sea rift: GFS ground -306 m, real -420 m; offshore depths don't count")
    f = fields(-306.0)
    m = build_profile(f, levels_mb, 25, 31.6, 35.45, ground_samples=[-420.0] * 5, follow=FOLLOW)
    check(m["groundM"] == -420.0 and m["levels"][0]["h"] == -418.0, f"moved down to {m['levels'][0]['h']} m")
    check(real_ground([-85.0, 10.0, -20.0, 30.0], 12.0) == 10.0, "off the coast the sea floor counts as sea level")
    check(real_ground([-400.0, -420.0], -306.0) == -410.0, "in a depression the model has, it counts")
    check(real_ground([-400.0, -420.0], 33.0) == -410.0, "and in one a smoother model (ERA5) has lost")

    print("3. no change over water, or when the grounds agree")
    f = fields(0.0); f["land"] = 0.0
    w = build_profile(f, levels_mb, 50, 31.0, 34.0, ground_samples=[40.0] * 5, follow=FOLLOW)
    check("groundM" not in w and w["levels"][0]["h"] == 2.0, "water column untouched")
    lv = build_profile(fields(583.0), levels_mb, 0, 0, 0)["levels"]
    check(follow_terrain(lv, 583.0, 583.4, 500, 2000) is lv, "under a metre: as is")

    print("4. a big step keeps the levels in order (the fade widens)")
    lv = build_profile(fields(100.0), levels_mb, 0, 0, 0)["levels"]
    big = follow_terrain(lv, 100.0, 1500.0, 500, 2000)
    check(all(a["h"] < b["h"] for a, b in zip(big, big[1:])), f"1400 m step: heights increasing")

    print("5. serve.Ground reads the elevation tiles")
    with tempfile.TemporaryDirectory() as d:
        n = 361
        a = np.full((n, n), 780, dtype=">i2")
        (Path(d) / "N31E035.hgt").write_bytes(a.tobytes())
        tiles = terrain.Tiles(Path(d), url_template=None)
        g = serve.Ground(tiles, FOLLOW)
        h = g([(31.77, 35.23), (31.5, 35.5)])
        check(len(h) == 2 and len(h[0]) == len(g.dy) == 13 and abs(np.mean(h[0]) - 780) < 1e-6,
              f"{len(g.dy)} samples within 1 km, mean {np.mean(h[0]):.0f} m")
    print("OK")


if __name__ == "__main__":
    main()
