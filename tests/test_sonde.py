"""Radiosonde path profiles (sonde_profiles.py) from a synthetic IGRA v2 file. No network needed.

    python3 tests/test_sonde.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import sonde_profiles as sp  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def level(l1: str, l2: str, p_pa: int, z: int, t10: int) -> str:
    """One IGRA v2 data line (columns as in igra2-data-format.txt)."""
    return f"{l1}{l2} -9999 {p_pa:>6} {z:>5}B{t10:>5}B  682 -9999   280    50 "


def sounding(y, m, d, h, rel, levels) -> list[str]:
    return [f"#ISM00040179 {y} {m:02d} {d:02d} {h:02d} {rel} {len(levels):>4} ncar-ccd 320000 348167"] + levels


class FlatTiles:
    def __init__(self, h):
        self.h = h

    def heights(self, lat, lon):
        return np.full(np.shape(lat), self.h, dtype=float)


def main():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "igra.txt"
        winter = [level("2", "1", 101700, 30, 92),            # surface 9.2 C at 30 m
                  level("1", "0", 100000, -9999, 109),         # 1000 mb, height to be filled in
                  level("2", "0", 99700, -9999, 112),          # inversion top
                  level("1", "0", 92500, 811, 75), level("1", "0", 85000, 1497, 33),
                  level("1", "0", 82000, 1790, -8888),         # temperature removed by IGRA's quality control
                  level("1", "0", 80000, -8888, 0),            # height removed: filled in like a missing one
                  level("1", "0", 70000, 3034, -60)]
        lines = sounding(1993, 1, 3, 5, "0500", winter) + sounding(1993, 1, 3, 11, "1100", winter)
        lines += sounding(1993, 1, 4, 23, "2300", winter[:2])   # too few levels: left out
        f.write_text("\n".join(lines) + "\n")

        print("1. reading IGRA")
        s = sp.read_igra(f, {1993})
        check(len(s) == 2, f"{len(s)} usable soundings (the 2-level one left out)")
        lv = s[0]["levels"]
        check(lv[0]["surface"] and lv[0]["z"] == 30.0 and abs(lv[1]["z"] - 170) < 5,
              f"missing heights filled hydrostatically: 1000 mb at {lv[1]['z']:.0f} m")
        check([l["z"] for l in lv] == sorted(l["z"] for l in lv), "bottom up")
        check(all(l["t"] > -100 for l in lv) and 820 not in [l["p"] for l in lv]
              and abs(next(l["z"] for l in lv if l["p"] == 800) - 1955) < 30,
              "values IGRA's quality control removed (-8888): not read as -888.8 C or -8888 m")

        print("2. the three versions of a column")
        raw = sp.column(s[0], 708.0, "raw", 0, 31.7, 35.2)["levels"]
        kht = sp.column(s[0], 708.0, "kh-t", 0, 31.7, 35.2)["levels"]
        khtp = sp.column(s[0], 708.0, "kh-tp", 0, 31.7, 35.2)["levels"]
        check(raw[0]["h"] == 30.0 and kht[0]["h"] == 738.0 and khtp[0]["h"] == 738.0, "shifted by the ground offset")
        check([x["t"] for x in raw] == [x["t"] for x in kht] == [x["t"] for x in khtp], "temperatures kept")
        check(khtp[0]["p"] == 1017.0 and abs(kht[0]["p"] - 1017 * np.exp(-9.80665 * 708 / (287.05 * 282.35))) < 0.05,
              f"kh-tp keeps the pressure, kh-t recomputes it for the real height ({kht[0]['p']} mb)")

        print("3. the sounding nearest sunrise, within 6 h before to 1.5 h after")
        files = sp.make(s, [date(1993, 1, 3), date(1993, 7, 3)], 31.748553, 35.237436, FlatTiles(738.0))
        ev = files["kh-t"]["events"]
        check(len(ev) == 1 and ev[0]["sondeTime"].startswith("1993-01-03T05:00"),
              "January 3: the 05Z launch; July 3: none in the window")
        check(ev[0]["profiles"][0]["levels"][0]["h"] == 738.0 and ev[0]["profiles"][-1]["levels"][0]["h"] == 738.0,
              "flat ground: every column on 738 m")
    print("OK")


if __name__ == "__main__":
    main()
