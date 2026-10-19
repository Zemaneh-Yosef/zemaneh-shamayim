"""ERA5 path profiles for past dates (era5_profiles.py) from synthetic NetCDF files laid out like the CDS's
(the current layout: valid_time / pressure_level, and the older one: time / level). No network needed.

    python3 tests/test_era5.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import era5_profiles  # noqa: E402
from common import load_config  # noqa: E402

LAT, LON = 31.7486, 35.2374
LEVELS = era5_profiles.LEVELS


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def t2m_at(t: datetime) -> float:
    """2 m air, K: 1 K warmer every hour (so time interpolation shows)."""
    return 275.0 + (t - datetime(1995, 1, 1, tzinfo=timezone.utc)).total_seconds() / 3600 % 24


def write_month(folder: Path, month: str, box, hours: list[datetime], new_layout: bool = True):
    import netCDF4
    n, w, s, e = box
    lats = np.arange(n, s - 1e-9, -0.25)                 # north to south, like the CDS
    lons = np.arange(w, e + 1e-9, 0.25)
    base = datetime(1900, 1, 1, tzinfo=timezone.utc)
    tname, lname = ("valid_time", "pressure_level") if new_layout else ("time", "level")
    for kind in ("pl", "sl"):
        with netCDF4.Dataset(folder / f"era5-{kind}-{month}.nc", "w") as ds:
            ds.createDimension(tname, len(hours))
            ds.createDimension("latitude", len(lats))
            ds.createDimension("longitude", len(lons))
            tv = ds.createVariable(tname, "i8", (tname,))
            tv.units = "hours since 1900-01-01 00:00:00"
            tv[:] = [int((h - base).total_seconds() // 3600) for h in hours]
            ds.createVariable("latitude", "f4", ("latitude",))[:] = lats
            ds.createVariable("longitude", "f4", ("longitude",))[:] = lons
            shape2 = (len(hours), len(lats), len(lons))
            land = np.broadcast_to((lons > 34.6)[None, None, :], shape2).astype("f4")     # sea west of 34.6
            orog = np.where(land > 0, 580.0, 0.0)
            psfc = 101325.0 * (1 - 0.0065 * orog / 288.15) ** 5.2559
            if kind == "sl":
                t2 = np.stack([np.full((len(lats), len(lons)), t2m_at(h)) for h in hours])
                for name, val in (("t2m", t2), ("sp", psfc), ("z", orog * 9.80665), ("lsm", land), ("skt", t2 + 1)):
                    ds.createVariable(name, "f4", (tname, "latitude", "longitude"))[:] = val
            else:
                ds.createDimension(lname, len(LEVELS))
                ds.createVariable(lname, "f4", (lname,))[:] = LEVELS
                z = np.array([44330.8 * (1 - (p * 100 / 101325.0) ** 0.190263) for p in LEVELS])
                shape = (len(hours), len(LEVELS), len(lats), len(lons))
                ds.createVariable("t", "f4", (tname, lname, "latitude", "longitude"))[:] = np.broadcast_to(
                    (288.15 - 0.0065 * z)[None, :, None, None], shape)
                ds.createVariable("z", "f4", (tname, lname, "latitude", "longitude"))[:] = np.broadcast_to(
                    (z * 9.80665)[None, :, None, None], shape)


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg = load_config(str(tmp / "none.json"))
        cfg["data_dir"] = str(tmp / "data")
        dates = [date(1995, 1, 10), date(1995, 1, 31), date(1995, 2, 1)]
        evs, box = era5_profiles.plan(cfg, LAT, LON, dates, ["sunrise"])
        need = era5_profiles.hours_needed(evs)
        print("1. what is asked: a box around the 400 km paths, the hours around each sunrise")
        check(box[0] > LAT and box[2] < LAT and box[3] > LON + 4 and box[1] < LON,
              f"box N W S E = {box}: reaches 400 km east of the observer")
        check(list(need) == ["199501", "199502"] and need["199501"]["days"] == [10, 31] and need["199501"]["hours"] == [4, 5],
              f"by month, only the event days and the hours either side: {need}")

        for m, new in (("199501", True), ("199502", False)):     # both CDS layouts
            hours = [datetime(int(m[:4]), int(m[4:]), dd, hh, tzinfo=timezone.utc)
                     for dd in need[m]["days"] for hh in need[m]["hours"]]
            write_month(tmp, m, box, hours, new_layout=new)
        files = lambda month, nd, bx: (tmp / f"era5-pl-{month}.nc", tmp / f"era5-sl-{month}.nc")   # noqa: E731

        print("2. profiles as /v1/path-profiles serves them")
        cfg["api"]["follow_terrain"]["enabled"] = False
        body = era5_profiles.make(cfg, LAT, LON, dates, ["sunrise"], files)
        check(len(body["events"]) == 3 and {e["source"] for e in body["events"]} == {"era5"}, "3 sunrises, source era5")
        e = body["events"][0]
        t = datetime.fromisoformat(e["timeUtc"].replace("Z", "+00:00"))
        p0 = e["profiles"][0]
        check(abs(p0["levels"][0]["t"] - (t2m_at(t) - 273.15)) < 0.02,
              f"2 m air interpolated to the sunrise time {e['timeUtc']}: {p0['levels'][0]['t']} C")
        check(p0["levels"][0]["h"] == 582.0 and all(lv["p"] < p0["levels"][0]["p"] for lv in p0["levels"][1:]),
              "on ERA5's ground (580 m), levels below it left out")
        check(len(e["profiles"]) == len(cfg["api"]["distances_km"]), "every distance out to 400 km")
        check(body["events"][2]["date"] == "1995-02-01", "February from the older file layout")

        print("3. columns stood on the real ground, as the API does")
        cfg["api"]["follow_terrain"]["enabled"] = True
        moved = era5_profiles.make(cfg, LAT, LON, dates, ["sunrise"], files, ground=lambda pts: [[780.0] * 13 for _ in pts])
        m0 = moved["events"][0]["profiles"][0]
        check(m0["groundM"] == 780.0 and m0["levels"][0]["h"] == 782.0, "observer's column on 780 m")
        check(all("groundM" not in p for p in moved["events"][0]["profiles"] if p["distanceKm"] > 150),
              "beyond 150 km: as ERA5 has it")

        print("4. command line, with --dates read from an observation file")
        obs = tmp / "obs.csv"
        obs.write_text("date,time\n1995-01-10,06:39:12\n1995-01-31,06:33:50\n# a comment\n1995-02-01,06:33:10\n")
        check(era5_profiles.read_dates(obs) == dates, "dates from a CSV of observations")
        out = tmp / "era5.json"
        assert era5_profiles.main(["--config", str(tmp / "none.json"), "--lat", str(LAT), "--lon", str(LON),
                                   "--dates", str(obs), "--event", "sunrise", "--nc-dir", str(tmp),
                                   "--no-follow-terrain", "--out", str(out)]) == 0
        check(len(json.loads(out.read_text())["events"]) == 3, "written")
    print("OK")


if __name__ == "__main__":
    main()
