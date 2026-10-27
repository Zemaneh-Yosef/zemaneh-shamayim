"""Guardrails on the weather data taken in (weather_guard.py): impossible values refused, a run that jumps away from
the serving one held back, real weather kept. Synthetic GRIB files, no network.

    python3 tests/test_weather_guard.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fetch_gfs  # noqa: E402
import weather_guard as wg  # noqa: E402
from common import Cycle, field_names, forecast_hours, load_config  # noqa: E402
from make_synthetic import write  # noqa: E402

REGION = {"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0}
COAST = -74.0


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def column_fields(t2m=283.0, psfc=101000.0, shape=(3, 4)):
    """A standard-lapse column on a small grid, in the stored units."""
    levels = [1000, 850, 700, 500]
    names = field_names(levels)
    vals = {"t2m": t2m, "tsfc": t2m + 2, "psfc": psfc, "orog": 50.0, "land": 1.0}
    for p in levels:
        t = (t2m + 0.013) * (p * 100 / psfc) ** (1 / 5.2559)
        vals[f"t{p}"], vals[f"z{p}"] = t, 50 + (t2m + 0.013 - t) / 0.0065
    return names, np.stack([np.full(shape, vals[n], dtype=np.float32) for n in names])


def run(tmp: Path):
    print("1. impossible values")
    names, f = column_fields()
    check(wg.problems(names, f) == [], "a standard column: nothing refused")
    for name, real in (("t2m", 184.0), ("t2m", 329.0), ("psfc", 33000.0), ("t500", 185.0)):
        g = f.copy()
        g[names.index(name)] = real
        check(wg.problems(names, g) == [], f"{name} = {real:g}: a record extreme, kept")
    for name, bad, why in (("t2m", 10.0, "2 m air in Celsius"), ("psfc", 1013.0, "surface pressure in mb"),
                           ("t850", 0.0, "zero Kelvin"), ("orog", 12000.0, "ground above Everest"),
                           ("land", 7.0, "a land fraction of 7")):
        g = f.copy()
        g[names.index(name)][1, 2] = bad
        check(any(name in p for p in wg.problems(names, g)), f"{why} refused")
    g = f.copy()
    g[names.index("z500")] = g[names.index("z700")] - 10
    check(any("z500 not above z700" in p for p in wg.problems(names, g)), "500 mb below 700 mb refused")
    g = f.copy()
    g[names.index("t2m")][0, 0] = np.nan
    check(wg.problems(names, g) == [], "a missing value (NaN) is not a refusal")
    check(wg.problems(names, np.moveaxis(np.stack([f, f]), 1, 2), axis=2) == [], "field axis given (the climatology's)")

    print("2. a fetched run with impossible values is not published")
    cfg_path = tmp / "config.json"
    cfg = {"data_dir": str(tmp / "data"), "regions": [REGION],
           "forecast_hours": {"hourly_until": 12, "step_after": 3, "max": 30},
           "request_delay_s": 0, "api": {"host": "127.0.0.1", "port": 0}, "climatology": {"ncep_prior": False}}
    cfg_path.write_text(json.dumps(cfg))
    full = load_config(str(cfg_path))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    first = Cycle(now - timedelta(hours=now.hour % 6 + 12))
    second = Cycle(first.time + timedelta(hours=6))

    def make(cycle, name, dt=0.0, t2m_land=283.15):
        src = tmp / name
        for h in forecast_hours(full):
            write(src / REGION["name"] / fetch_gfs.grib_file_name(cycle, h), REGION, t2m_land=t2m_land + dt,
                  t2m_sea=285.15 + dt, skin_sea=289.15 + dt, coast_lon=COAST)
        return src

    def fetch(cycle, src):
        return fetch_gfs.main(["--config", str(cfg_path), "--cycle", cycle.id, "--source-dir", str(src)])

    def serving():
        return json.loads((fetch_gfs.gfs_dir(full) / "current.json").read_text())["cycle"]

    check(fetch(first, make(first, "a")) == 0 and serving() == first.id, f"run {first.id} published")
    check(fetch(second, make(second, "celsius", t2m_land=12.0)) != 0 and serving() == first.id,
          "a run with 2 m air of 12 K (Celsius read as Kelvin): refused, the previous run still serving")

    print("3. a run that jumps away from the serving one is held back")
    check(fetch(second, make(second, "jump", dt=8.0)) != 0 and serving() == first.id
          and not (fetch_gfs.gfs_dir(full) / (second.id + ".partial")).exists(),
          "8 K warmer everywhere 6 h later: held back (fetched afresh next time), the previous run still serving")
    check(fetch(second, make(second, "update", dt=0.6)) == 0 and serving() == second.id,
          "0.6 K warmer: an ordinary update, published")
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
