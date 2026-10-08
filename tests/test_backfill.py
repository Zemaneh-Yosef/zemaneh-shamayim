"""Backfill of the own GFS climatology from a fake GFS archive (local HTTP server with byte ranges and
.idx files laid out like s3://noaa-gfs-bdp-pds). No network needed.

    python3 tests/test_backfill.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import eccodes
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import backfill_gfs  # noqa: E402
import fetch_gfs  # noqa: E402
from climatology import ClimGrid, archived_cycles, clim_dir  # noqa: E402
from common import Cycle, forecast_hours, load_config  # noqa: E402
from make_synthetic import write as write_grib  # noqa: E402

REGION = {"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0}
WIDE = {"name": "wide", "north": 46.0, "south": 35.0, "west": -82.0, "east": -64.0}   # stands in for the globe
NAMES = {(0, 0, 0): "TMP", (0, 3, 5): "HGT", (0, 3, 0): "PRES", (2, 0, 0): "LAND", (0, 1, 1): "RH"}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def archive_file(path: Path, t2m_sea: float):
    """A GRIB file over WIDE with an unwanted RH message in the middle, plus its .idx."""
    write_grib(path, WIDE, t2m_land=t2m_sea - 2, t2m_sea=t2m_sea, skin_sea=t2m_sea + 4, coast_lon=-74.0)
    msgs = []
    with open(path, "rb") as f:
        while (h := eccodes.codes_grib_new_from_file(f)) is not None:
            msgs.append(eccodes.codes_get_message(h))
            if len(msgs) == 3:                               # an extra field the backfill must skip
                eccodes.codes_set(h, "parameterCategory", 1); eccodes.codes_set(h, "parameterNumber", 1)
                msgs.append(eccodes.codes_get_message(h))
            eccodes.codes_release(h)
    data, lines, off = b"", [], 0
    for n, m in enumerate(msgs, 1):
        h = eccodes.codes_new_from_message(m)
        key = tuple(eccodes.codes_get(h, k, ktype=int) for k in ("discipline", "parameterCategory", "parameterNumber"))
        tol, lev = eccodes.codes_get(h, "typeOfLevel"), eccodes.codes_get(h, "level")
        eccodes.codes_release(h)
        level = {"isobaricInhPa": f"{lev} mb", "heightAboveGround": f"{lev} m above ground"}.get(tol, "surface")
        lines.append(f"{n}:{off}:d=2025100100:{NAMES[key]}:{level}:1 hour fcst:")
        data += m
        off += len(m)
    path.write_bytes(data)
    Path(str(path) + ".idx").write_text("\n".join(lines) + "\n")


class RangeHandler(BaseHTTPRequestHandler):
    root: Path = Path(".")
    requests: list = []

    def do_GET(self):
        p = self.root / self.path.lstrip("/")
        if not p.is_file():
            self.send_response(404); self.end_headers(); return
        data = p.read_bytes()
        rng = self.headers.get("Range")
        self.requests.append((self.path, rng))
        if rng:
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), (int(b) if b else len(data) - 1)
            body = data[a:b + 1]
            self.send_response(206)
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run(tmp: Path):
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({
        "data_dir": str(tmp / "data"), "regions": [REGION],
        "forecast_hours": {"hourly_until": 11, "step_after": 3, "max": 11},
        "request_delay_s": 0, "climatology": {"min_days": 1, "ncep_prior": False}}))
    cfg = load_config(str(cfgp))

    print("1. before any live run the backfill refuses (it needs the live grid)")
    try:
        backfill_gfs.region_grids(cfg)
        check(False, "should have stopped")
    except SystemExit as e:
        check("fetch_gfs.py" in str(e), "asks to run fetch_gfs.py first")

    print("2. one live run (October 3 2026, 12 C over water)")
    src = tmp / "grib"
    live = Cycle(datetime(2026, 10, 3, 0, tzinfo=timezone.utc))
    for h in forecast_hours(cfg):
        write_grib(src / REGION["name"] / fetch_gfs.grib_file_name(live, h), REGION,
                   t2m_land=283.15, t2m_sea=285.15, skin_sea=289.15, coast_lon=-74.0)
    assert fetch_gfs.main(["--config", str(cfgp), "--cycle", live.id, "--source-dir", str(src)]) == 0
    g = ClimGrid(clim_dir(cfg, "gfs", "nyc"))
    check(g.counts[9].tolist() == [3, 3, 3, 3, 0, 0, 0, 0], f"live run: October slots {g.counts[9].tolist()}")

    print("3. fake archive: Oct 1, 5, 9 2025 at 20 C over water (Oct 5 12Z missing), Mar 1 2021 without atmos/")
    arch = tmp / "archive"
    for d in (date(2025, 10, 1), date(2025, 10, 5), date(2025, 10, 9)):
        for run_h in (0, 12):
            if d == date(2025, 10, 5) and run_h == 12:
                continue
            for fh in backfill_gfs.HOURS:
                archive_file(arch / f"gfs.{d:%Y%m%d}/{run_h:02d}/atmos/gfs.t{run_h:02d}z.pgrb2.0p25.f{fh:03d}", 293.15)
    for run_h in (0, 12):
        for fh in backfill_gfs.HOURS:
            archive_file(arch / f"gfs.20210301/{run_h:02d}/gfs.t{run_h:02d}z.pgrb2.0p25.f{fh:03d}", 273.15)
    RangeHandler.root = arch
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    idx = (arch / "gfs.20251001/00/atmos/gfs.t00z.pgrb2.0p25.f001.idx").read_text()
    rngs = backfill_gfs.ranges_from_idx(idx, backfill_gfs.wanted(cfg["pressure_levels_mb"]))
    check(len(rngs) == 2 and rngs[-1][1] is None, f"adjacent fields merged, the RH message left out: {rngs}")

    print("4. dry run, then the backfill")
    base = ["--config", str(cfgp), "--url", url, "--every", "4"]
    assert backfill_gfs.main(base + ["--start", "2025-10-01", "--end", "2025-10-09", "--dry-run"]) == 0
    check(int(ClimGrid(clim_dir(cfg, "gfs", "nyc")).counts.sum()) == 12, "dry run adds nothing")
    assert backfill_gfs.main(base + ["--start", "2025-10-01", "--end", "2025-10-09"]) == 0
    g = ClimGrid(clim_dir(cfg, "gfs", "nyc"))
    check(g.counts[9].tolist() == [12, 12, 12, 12, 6, 6, 6, 6],
          f"3 sampled days + the live run, Oct 5 12Z missing: {g.counts[9].tolist()}")
    f = g.fields_at(datetime(2026, 10, 15, 1, 30, tzinfo=timezone.utc), 40.75, -73.0, min_samples=3)
    expect = (3 * 285.15 + 9 * 293.15) / 12
    check(abs(f["t2m"] - expect) < 0.01, f"each backfilled day weighs as much as a live day ({f['t2m'] - 273.15:.2f} C)")
    f = g.fields_at(datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc), 40.75, -73.0, min_samples=3)
    check(abs(f["t2m"] - 293.15) < 0.01, "afternoon slots: archive only")
    check(abs(f["z1000"] - float(np.asarray(g.values[9, 5, g.fields.index("z1000")]).mean() / 9)) < 50,
          "pressure levels came along")
    check(g.meta["lats"] == json.loads((tmp / "data/gfs" / live.id / "nyc/meta.json").read_text())["lats"],
          "same grid as the live data (cropped from the wider archive grid)")
    check(len(archived_cycles(clim_dir(cfg, "gfs", "nyc"))) == 6, "ledger: the live run + 5 archive runs")

    print("5. rerun adds nothing; pre-2021-v16 layout (no atmos/) works")
    n_req = len(RangeHandler.requests)
    assert backfill_gfs.main(base + ["--start", "2025-10-01", "--end", "2025-10-09"]) == 0
    check(len(RangeHandler.requests) == n_req, "already-added runs are not downloaded again")
    check(ClimGrid(clim_dir(cfg, "gfs", "nyc")).counts.sum() == g.counts.sum(), "nothing counted twice")
    assert backfill_gfs.main(base + ["--start", "2021-03-01", "--end", "2021-03-01"]) == 0
    g = ClimGrid(clim_dir(cfg, "gfs", "nyc"))
    check(g.counts[2].tolist() == [3] * 8, f"March 2021 from the old layout: {g.counts[2].tolist()}")
    f = g.fields_at(datetime(2026, 3, 16, 7, tzinfo=timezone.utc), 40.75, -73.0, min_samples=3)
    check(f is not None and abs(f["t2m"] - 273.15) < 0.6, "March values from the 2021 run")

    print("6. the live fetcher skips a run the backfill already added")
    bf = Cycle(datetime(2025, 10, 9, 0, tzinfo=timezone.utc))
    for h in forecast_hours(cfg):
        write_grib(src / REGION["name"] / fetch_gfs.grib_file_name(bf, h), REGION,
                   t2m_land=283.15, t2m_sea=285.15, skin_sea=289.15, coast_lon=-74.0)
    before = ClimGrid(clim_dir(cfg, "gfs", "nyc")).counts.sum()
    assert fetch_gfs.main(["--config", str(cfgp), "--cycle", bf.id, "--source-dir", str(src)]) == 0
    check(ClimGrid(clim_dir(cfg, "gfs", "nyc")).counts.sum() == before, "no double counting between the two")
    srv.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
