"""Columns from build_profile always rise with falling pressure, even where a climatology's separately averaged
fields disagree by a few metres. No network.

    python3 tests/test_profile_levels.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from climatology import NEAR_LEVEL_GAP_M, build_profile  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def rising(levels):
    return all(b["h"] > a["h"] and b["p"] < a["p"] for a, b in zip(levels, levels[1:]))


K = 273.15
# The served climatology for Jerusalem's sunrise, 6 Feb 2027, 50 km east (over Jordan): the 950 hPa level's averaged
# height (582.3 m) lands 2 m under the 100 m air (584.3 m), whose pressure, worked out from the averaged surface
# pressure, is 950.23 hPa. Before the fix the column read 950 hPa at 582.3 m, then 950.23 hPa at 584.3 m.
JERUSALEM_50KM = {"orog": 484.3, "t2m": 9.63 + K, "psfc": 96176.0, "land": 1.0,
                  "t80m": 10.64 + K, "t100m": 10.59 + K,
                  "z1000": 151.6, "t1000": 14.0 + K, "z975": 364.6, "t975": 12.61 + K,
                  "z950": 582.3, "t950": 10.23 + K, "z925": 803.7, "t925": 9.34 + K, "z900": 1030.7, "t900": 7.78 + K}
LEVELS_MB = [1000, 975, 950, 925, 900]


def main():
    print("climatology column next to a fixed-height level:")
    prof = build_profile(JERUSALEM_50KM, LEVELS_MB, 50.0, 31.6432, 35.719, 2.0)
    lv = prof["levels"]
    check(rising(lv), "heights rise and pressures fall: " + ", ".join(f"{x['h']} m {x['p']} hPa" for x in lv))
    check(not any(x["p"] == 950.0 for x in lv), "the 950 hPa level 2 m from the 100 m air is left out")
    check(any(abs(x["h"] - 584.3) < 0.05 for x in lv), "the 100 m air is kept")
    check(any(x["p"] == 925.0 for x in lv) and any(x["p"] == 900.0 for x in lv), "the levels above are kept")
    check(not any(x["p"] in (1000.0, 975.0) for x in lv), "levels under the ground stay out")

    print("a level well clear of the fixed heights is kept:")
    f = {**JERUSALEM_50KM, "z950": 584.3 + NEAR_LEVEL_GAP_M + 25, "t950": 10.3 + K}
    lv = build_profile(f, LEVELS_MB, 50.0, 31.6432, 35.719, 2.0)["levels"]
    check(any(x["p"] == 950.0 for x in lv) and rising(lv), "950 hPa 40 m above the 100 m air stays, column in order")

    print("the last guard drops what still conflicts:")
    # a 925 hPa height below the 950 one cannot happen; the guard keeps the column in order
    f = {**JERUSALEM_50KM, "z950": 700.0, "z925": 690.0}
    lv = build_profile(f, LEVELS_MB, 50.0, 31.6432, 35.719, 2.0)["levels"]
    check(rising(lv), "column still rising with falling pressure: " + ", ".join(f"{x['h']} m {x['p']} hPa" for x in lv))

    print("a column without fixed-height levels (forecast without t80m / t100m):")
    f = {k: v for k, v in JERUSALEM_50KM.items() if k not in ("t80m", "t100m")}
    lv = build_profile(f, LEVELS_MB, 50.0, 31.6432, 35.719, 2.0)["levels"]
    check(rising(lv) and lv[0]["h"] == 486.3 and any(x["p"] == 950.0 for x in lv), "2 m air, then 950 / 925 / 900 hPa")
    print("all profile-level checks passed")


if __name__ == "__main__":
    main()
