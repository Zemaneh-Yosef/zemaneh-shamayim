"""The climate envelope (climate_envelope.py) and /v1/climate-envelope, on synthetic NOAA long-term means
(make_synthetic_ncep.py), no network.

    python3 tests/test_climate_envelope.py
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import climate_envelope as ce  # noqa: E402
import serve  # noqa: E402
from common import load_config  # noqa: E402
from make_synthetic_ncep import LATS, LONS, make as make_ncep, near_surface  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def expected(south: float, west: float, month: int, res: float = 5.0):
    """floor(min), ceil(max) of the synthetic sea-level air over the points on or in a cell, in a month (1-12).
    make_synthetic_ncep's sig995 air is near_surface - 0.26 K, and 1.3 K colder on its 200 m land: carried to sea
    level at 6.5 K/km the land comes back to the sea's value."""
    lon180 = (LONS + 180) % 360 - 180
    lats = [la for la in LATS if south - 1e-6 <= la <= south + res + 1e-6]
    lons = [lo for lo in lon180 if west - 1e-6 <= lo <= west + res + 1e-6]
    days = [d for d in range(365) if (date(2001, 1, 1) + timedelta(d)).month == month]
    assert lats and lons
    vals = [near_surface(d, h) - 0.26 - 273.15 for d in days for h in (0, 6, 12, 18)]
    return math.floor(min(vals)), math.ceil(max(vals))


def run(tmp: Path):
    src = tmp / "ncep"
    make_ncep(src)
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({"data_dir": str(tmp / "data"),
                                "regions": [{"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0}],
                                "api": {"host": "127.0.0.1", "port": 0}}))
    cfg = load_config(str(cfgp))

    print("1. building")
    out = ce.build(cfg, src)
    env = ce.load(out)
    check(env["lo"].shape == (36, 72, 12) and env["res"] == 5.0, "a 5 degree grid, 12 months")
    check(src.exists() and any(src.iterdir()), "files from --source-dir are left alone")

    print("2. values")
    for (s, w) in ((40.0, -75.0), (35.0, -80.0), (45.0, -65.0)):        # land + sea, land only, sea only
        for m in (1, 7):
            got = ce.at(env, s + 2, w + 2)["seaLevelC"]
            exp = expected(s, w, m)
            check((got["min"][m - 1], got["max"][m - 1]) == exp,
                  f"cell {s},{w} month {m}: {got['min'][m - 1]}..{got['max'][m - 1]} C (expected {exp[0]}..{exp[1]})")
    a = ce.at(env, 40.71, -74.01)
    check(a["cell"] == {"south": 40.0, "west": -75.0, "res": 5.0} and a["lapseKPerM"] == ce.LAPSE, "New York's cell")
    check(all(lo <= hi for lo, hi in zip(a["seaLevelC"]["min"], a["seaLevelC"]["max"])), "min <= max every month")

    print("3. places without data, bad input")
    for lat, lon in ((0.0, 0.0), (-90.0, -180.0), (90.0, 180.0)):
        try:
            ce.at(env, lat, lon)
            check(False, f"{lat},{lon} should have no envelope")
        except LookupError:
            check(True, f"{lat},{lon}: outside the synthetic data, no answer")
    try:
        ce.at(env, 91, 0)
        check(False, "lat 91 should be refused")
    except ValueError:
        check(True, "lat 91 refused")
    try:
        ce.envelope(np.full((48, 2, 2), 400.0), np.repeat(np.arange(12), 4), np.array([40.0, 37.5]),
                    np.array([285.0, 287.5]))
        check(False, "400 C should be refused")
    except ValueError:
        check(True, "temperatures in the wrong units refused")

    print("4. /v1/climate-envelope")
    serve.Handler.store = serve.Store(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1/climate-envelope"

    def status(q):
        try:
            r = urllib.request.urlopen(f"{base}?{q}")
            return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, None, e.headers

    code, body, headers = status("lat=40.71&lon=-74.01")
    check(code == 200 and body == a, "the endpoint answers New York's cell")
    check("max-age" in (headers.get("Cache-Control") or ""), f"cacheable ({headers.get('Cache-Control')})")
    check(status("lat=0&lon=0")[0] == 404, "404 where there is no data")
    check(status("lat=abc&lon=0")[0] == 400 and status("lon=0")[0] == 400, "400 for a bad query")
    out.unlink()
    check(status("lat=40.71&lon=-74.01")[0] == 503, "503 before the envelope has been built")
    srv.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
