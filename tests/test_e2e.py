"""End-to-end test without network: synthetic GRIB -> fetch_gfs (decode/store) -> serve.py API.

    python3 tests/test_e2e.py [--keep DIR] [--json-out FILE]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fetch_gfs  # noqa: E402
import serve  # noqa: E402
from common import Cycle, load_config  # noqa: E402
from make_synthetic import write  # noqa: E402

REGION = {"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0}
COAST = -74.0          # land west of here, water east (so sunrise from NYC looks over water)


def run(tmp: Path, json_out: Path | None):
    cfg_path = tmp / "config.json"
    cfg = {"data_dir": str(tmp / "data"), "regions": [REGION],
           "forecast_hours": {"hourly_until": 48, "step_after": 3, "max": 48},
           "request_delay_s": 0, "api": {"host": "127.0.0.1", "port": 0}}
    cfg_path.write_text(json.dumps(cfg))
    now = datetime.now(timezone.utc)
    c = now.replace(minute=0, second=0, microsecond=0)
    cycle = Cycle(c - timedelta(hours=c.hour % 6 + 6))
    src = tmp / "src"
    full = load_config(str(cfg_path))
    from common import forecast_hours
    for h in forecast_hours(full):
        write(src / REGION["name"] / fetch_gfs.grib_file_name(cycle, h), REGION,
              t2m_land=283.15, t2m_sea=285.15, skin_sea=289.15, coast_lon=COAST)
    assert fetch_gfs.main(["--config", str(cfg_path), "--cycle", cycle.id, "--source-dir", str(src)]) == 0
    # second run must be a no-op
    assert fetch_gfs.main(["--config", str(cfg_path), "--cycle", cycle.id, "--source-dir", str(src)]) == 0

    serve.Handler.store = serve.Store(full)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    status = json.loads(urllib.request.urlopen(base + "/v1/status").read())
    assert status["cycle"] == cycle.id, status
    body = json.loads(urllib.request.urlopen(base + "/v1/path-profiles?lat=40.7128&lon=-74.006&days=2").read())
    assert body["events"], body
    e = next(x for x in body["events"] if x["event"] == "sunrise")
    p0, plast = e["profiles"][0], e["profiles"][-1]
    print(f"cycle {body['cycle']}: {len(body['events'])} events; first {e['date']} {e['event']} "
          f"az {e['azimuthDeg']} with {len(e['profiles'])} profiles")
    print(f"  near: water={p0['water']} levels={len(p0['levels'])} 2m T={p0['levels'][0]['t']}C")
    print(f"  far ({plast['distanceKm']} km): water={plast['water']} skin={plast.get('skinC')}C "
          f"2m T={plast['levels'][0]['t']}C, 1000mb h={plast['levels'][1]['h']} m")
    assert plast["water"] and abs(plast["skinC"] - 16.0) < 0.05
    assert abs(plast["levels"][0]["t"] - 12.0) < 0.05
    try:
        urllib.request.urlopen(base + "/v1/path-profiles?lat=10&lon=10")
        raise AssertionError("expected 404 outside region")
    except urllib.error.HTTPError as err:
        assert err.code == 404
    srv.shutdown()
    if json_out:
        json_out.write_text(json.dumps(body))
    print("OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=Path)
    ap.add_argument("--json-out", type=Path)
    a = ap.parse_args()
    if a.keep:
        a.keep.mkdir(parents=True, exist_ok=True)
        run(a.keep, a.json_out)
    else:
        with tempfile.TemporaryDirectory() as d:
            run(Path(d), a.json_out)
