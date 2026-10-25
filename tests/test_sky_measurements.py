"""build_sky_measurements.py without network: its filters on a few made-up Globe at Night rows, and the Moon.

    python3 tests/test_sky_measurements.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_sky_measurements as b  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


HEAD = "ID,ObsType,Latitude,Longitude,Elevation(m),LocalDate,LocalTime,UTDate,UTTime,LimitingMag,SQMReading,SQMSerial,CloudCover,Constellation,SkyComment,LocationComment,Country\n"
ROWS = [
    "1,GAN,40.1,-74.2,10,2025-10-21,23:00,2025-10-22,03:00,4,18.9,,clear,Orion,,,US",      # dark, new moon: kept
    "2,GAN,40.1,-74.2,10,2025-10-21,23:00,2025-10-22,03:00,4,,,clear,Orion,,,US",          # no reading
    "3,GAN,40.1,-74.2,10,2025-10-21,23:00,2025-10-22,03:00,4,18.9,,1/4 of sky,Orion,,,US",  # not clear
    "4,GAN,40.1,-74.2,10,2025-10-21,19:00,2025-10-21,23:00,4,18.9,,clear,Orion,,,US",      # 7 pm: twilight
    "5,GAN,40.1,-74.2,10,2025-10-06,23:00,2025-10-07,03:00,4,18.9,,clear,Orion,,,US",      # full moon up
    "6,GAN,40.1,-74.2,10,2025-10-21,23:00,2025-10-22,03:00,4,1e90,,clear,Orion,,,US",      # out of range
]


def main():
    print("1. the Moon (Astronomical Almanac low-precision formulae)")
    full = b.moon_altitude(datetime(2025, 10, 6, 21, 0, tzinfo=timezone.utc), 31.78, 35.23)
    new = b.moon_altitude(datetime(2025, 10, 21, 21, 0, tzinfo=timezone.utc), 31.78, 35.23)
    check(full > 40 and new < -40, f"Jerusalem at midnight: full moon {full:.1f} deg up, new moon {new:.1f} deg")
    print("2. the filters")
    kept, why = b.readings(HEAD + "\n".join(ROWS) + "\n")
    check([k["note"].split()[3] for k in kept] == ["1"], f"only the dark, clear, moonless reading kept: {why}")
    check(why == {"no reading": 1, "out of range": 1, "not clear": 1, "twilight": 1, "moon up": 1, "bad row": 0},
          "each test left out its row")
    check(b.yearly_files('<a href="/documents/1190/GaN2025.csv">x</a> <a href="/documents/323/GaN2006.xls">') ==
          {2025: "https://globeatnight.org/documents/1190/GaN2025.csv"}, "yearly CSV links found on the data page")
    print("OK")


if __name__ == "__main__":
    main()
