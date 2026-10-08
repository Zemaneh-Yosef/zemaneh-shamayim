"""Climatology tests without network: NOAA reanalysis reduction, own GFS archive, API date ranges.

    python3 tests/test_climatology.py [--json-out FILE]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import build_prior  # noqa: E402
import fetch_gfs  # noqa: E402
import serve  # noqa: E402
from climatology import ClimGrid, clim_dir, month_weights  # noqa: E402
from common import Cycle, forecast_hours, load_config  # noqa: E402
from make_synthetic import write as write_grib  # noqa: E402
from make_synthetic_ncep import make as make_ncep, near_surface  # noqa: E402

REGION = {"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def run(tmp: Path, json_out: Path | None):
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({
        "data_dir": str(tmp / "data"), "regions": [REGION],
        "forecast_hours": {"hourly_until": 11, "step_after": 3, "max": 11},
        "request_delay_s": 0, "climatology": {"min_days": 1, "ncep_years": 0}}))
    cfg = load_config(str(cfgp))

    print("1. region selection on a global 2.5 deg grid, across 0 deg longitude")
    lats, lons = np.arange(90, -90.1, -2.5), np.arange(0, 360, 2.5)
    rows, cols, rl, rlo = build_prior.region_index(lats, lons, {"name": "uk", "north": 59, "south": 50, "west": -8, "east": 2}, 2.5)
    check(list(rl) == sorted(rl) and rl[0] < 50 and rl[-1] > 59, f"latitudes ascending, bracket 50..59: {rl[0]}..{rl[-1]}")
    check(list(rlo) == sorted(rlo) and rlo[0] < -8 and rlo[-1] > 2, f"longitudes monotonic across 0, bracket -8..2: {rlo[0]}..{rlo[-1]}")

    print("2. own GFS archive from two runs (+ automatic NOAA build from local files)")
    ncep_dir = tmp / "ncep-files"
    make_ncep(ncep_dir)
    src = tmp / "grib"
    cycles = [Cycle(datetime(2026, 10, 3, 0, tzinfo=timezone.utc)), Cycle(datetime(2026, 10, 3, 12, tzinfo=timezone.utc))]
    for c in cycles:
        for h in forecast_hours(cfg):
            write_grib(src / REGION["name"] / fetch_gfs.grib_file_name(c, h), REGION,
                       t2m_land=283.15, t2m_sea=285.15, skin_sea=289.15, coast_lon=-74.0)
        args = ["--config", str(cfgp), "--cycle", c.id, "--source-dir", str(src), "--prior-source-dir", str(ncep_dir)]
        assert fetch_gfs.main(args) == 0
        assert fetch_gfs.main(args) == 0          # again: must not be counted twice
    g = ClimGrid(clim_dir(cfg, "gfs", "nyc"))
    check(g.counts[9].tolist() == [3] * 8, f"October slots each hold 3 hours (counts {g.counts[9].tolist()})")
    check(int(g.counts.sum()) == 24, "nothing double counted, other months empty")
    f = g.fields_at(datetime(2026, 10, 15, 11, tzinfo=timezone.utc), 40.7, -73.0, min_samples=3)
    check(abs(f["t2m"] - 285.15) < 0.05, f"October mean over water = {f['t2m'] - 273.15:.2f} C")

    print("3. NOAA reanalysis climatology (built by the fetcher)")
    n = ClimGrid(clim_dir(cfg, "ncep", "nyc"))
    check(n.meta["slot_hours"] == [0.0, 6.0, 12.0, 18.0] and n.meta["near_surface_agl"] == 40.0, "4 times a day, air at ~40 m")
    jan = [d for d in range(31)]
    expect = np.mean([near_surface(d, 0) for d in jan]) - 0.26                   # over water
    ri, ci = int(np.argmin(abs(n.lats - 40.0))), int(np.argmin(abs(n.lons + 70.0)))   # a sea point
    got = float(n.values[0, 0, n.fields.index("t2m"), ri, ci])
    check(abs(got - expect) < 0.01, f"January 00Z near-surface mean {got - 273.15:.2f} C (expected {expect - 273.15:.2f})")
    t1000 = float(n.values[0, 0, n.fields.index("t1000"), ri, ci])
    check(270 < t1000 < 300, f"pressure-level temperatures converted from degC to K ({t1000:.2f} K)")
    sunrise_like = n.fields_at(datetime(2026, 1, 15, 11, tzinfo=timezone.utc), 40.7, -73.0)
    afternoon = n.fields_at(datetime(2026, 1, 15, 20, tzinfo=timezone.utc), 40.7, -73.0)
    check(afternoon["t2m"] - sunrise_like["t2m"] > 5, f"time of day kept: 11Z {sunrise_like['t2m'] - 273.15:.1f} C "
          f"vs 20Z {afternoon['t2m'] - 273.15:.1f} C")
    check(build_prior.regions_missing(cfg) == [], "no region left without a NOAA climatology")

    print("4. API over a long range: forecast -> own climatology -> NOAA")
    serve.Handler.store = serve.Store(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    body = json.loads(urllib.request.urlopen(base + "/v1/path-profiles?lat=40.7128&lon=-74.006&from=2026-09-25&days=60").read())
    seq = [(e["date"], e["event"], e["source"]) for e in body["events"]]
    by = {}
    for d, ev, s_ in seq:
        by.setdefault(s_, []).append(f"{d} {ev}")
    for s_, lst in by.items():
        print(f"     {s_:17s} {len(lst):3d} events  {lst[0]} .. {lst[-1]}")
    check(len(seq) == 120, "every sunrise and sunset in 60 days is covered")
    check(by.get("forecast") == ["2026-10-03 sunset"], "the forecast covers only its own window (Oct 3 sunset)")
    check(by["climatology-gfs"][0].startswith("2026-10-0") and by["climatology-gfs"][-1].startswith("2026-10-31"),
          "own climatology used exactly while October is the nearer month (Oct 1 .. Oct 31)")
    check("climatology-ncep" in by and by["climatology-ncep"][-1].startswith("2026-11-23"), "NOAA fills the rest")
    check(set(body["sources"]) == {"forecast", "climatology-gfs", "climatology-ncep"}, "sources described")
    e_ncep = next(e for e in body["events"] if e["source"] == "climatology-ncep")
    lv = e_ncep["profiles"][0]["levels"]
    check(lv[0]["h"] > 30 and all(lv[i]["h"] < lv[i + 1]["h"] for i in range(len(lv) - 1)),
          f"NOAA profile: near-surface level at {lv[0]['h']} m, heights increasing")
    st = json.loads(urllib.request.urlopen(base + "/v1/status").read())
    check(st["climatology"]["nyc"]["gfs"]["months_ready"] == 1, f"status reports climatology progress {st['climatology']['nyc']['gfs']['days_per_month'][9]} days in October")
    srv.shutdown()
    if json_out:
        json_out.write_text(json.dumps(body))

    print("5. NOAA reanalysis climatology from recent years (rolling window)")
    meta_path = clim_dir(cfg, "ncep", "nyc") / "meta.json"
    old = json.loads(meta_path.read_text()); old.pop("target", None); meta_path.write_text(json.dumps(old))
    check(build_prior.regions_missing(cfg) == [], "1991-2020 mode: an older prior without a window is kept")
    ycfg = json.loads(json.dumps(cfg)); ycfg["climatology"]["ncep_years"] = 2
    oct26 = date(2026, 10, 8)
    check(build_prior.target(ycfg, oct26) == [2024, 2025], "October 2026: window 2024-2025")
    check(build_prior.target(ycfg, date(2027, 2, 10)) == [2024, 2025], "February 2027: still 2024-2025 (NOAA finishing 2026)")
    check(build_prior.target(ycfg, date(2027, 3, 1)) == [2025, 2026], "March 2027: window moves to 2025-2026")
    check(build_prior.regions_missing(ycfg, oct26) == [REGION], "yearly mode: the 1991-2020 prior is rebuilt")
    ydir = tmp / "ncep-yearly"
    for y, off in ((2022, 0.0), (2023, 1.0), (2024, 2.0), (2025, 3.0)):
        make_ncep(ydir, year=y, offset=off)
    make_ncep(ydir, year=2026, offset=4.0, days=60)             # an unfinished year
    build_prior.build(ycfg, build_prior.regions_missing(ycfg, oct26), ydir, today=oct26)
    n = ClimGrid(clim_dir(ycfg, "ncep", "nyc"))
    check(n.meta["target"] == [2024, 2025] and n.meta["years"] == "2024-2025", f"built from {n.meta['years']}")
    got = float(n.values[0, 0, n.fields.index("t2m"), ri, ci])
    expect = np.mean([near_surface(d, 0) for d in range(31)]) - 0.26 + 2.5
    check(abs(got - expect) < 0.01, f"January 00Z = mean of 2024 and 2025 ({got - 273.15:.2f} C, expected {expect - 273.15:.2f})")
    t1000 = float(n.values[0, 0, n.fields.index("t1000"), ri, ci])
    check(270 < t1000 < 300, f"packed yearly temperatures unpacked ({t1000:.2f} K)")
    jul = float(n.values[6, 2, n.fields.index("t2m"), ri, ci])
    expect_jul = np.mean([near_surface(d, 12) for d in range(181, 212)] + [near_surface(d, 12) for d in range(182, 213)]) - 0.26 + 2.5
    check(abs(jul - expect_jul) < 0.02, f"July 12Z across a leap year ({jul - 273.15:.2f} C, expected {expect_jul - 273.15:.2f})")
    check(build_prior.regions_missing(ycfg, oct26) == [], "nothing to rebuild within the same window")
    cached = sorted(p.name for p in build_prior.cache_dir(ycfg).glob("*.npz"))
    check(cached == ["2024.npz", "2025.npz"], f"yearly summaries kept: {cached}")
    mar27 = date(2027, 3, 5)
    check(build_prior.regions_missing(ycfg, mar27) == [REGION], "March 2027: rebuild for 2025-2026")
    for f in ydir.glob("*.2025.nc"):
        f.unlink()                                           # 2025 must now come from its stored summary
    build_prior.build(ycfg, build_prior.regions_missing(ycfg, mar27), ydir, today=mar27)
    n = ClimGrid(clim_dir(ycfg, "ncep", "nyc"))
    check(n.meta["years"] == "2024-2025" and n.meta["target"] == [2025, 2026],
          f"unfinished 2026 skipped, 2024 used instead, 2025 from its summary ({n.meta['years']})")
    check(build_prior.regions_missing(ycfg, mar27) == [], "no hourly retries while 2026 stays unfinished")
    print("OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", type=Path)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        run(Path(d), a.json_out)
