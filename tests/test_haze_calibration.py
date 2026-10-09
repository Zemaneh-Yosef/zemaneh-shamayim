"""haze_calibration.py without network: a fake AERONET v3 daily file and fake CAMS series, resuming over
runs, and /v1/haze-calibration.

    python3 tests/test_haze_calibration.py
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import haze_calibration as hc  # noqa: E402
import serve  # noqa: E402
from common import load_config  # noqa: E402

TODAY = date(2026, 10, 8)
# name, lat, lon, true AOD (550 nm), CAMS bias (CAMS = true / ratio), days with data
SITES = [("CCNY", 40.821, -73.949, 0.20, 1.30, 400),       # CAMS 30% low in New York
         ("GSFC", 38.992, -76.840, 0.18, 1.10, 400),
         ("Sparse_Site", 41.0, -74.5, 0.20, 1.0, 30),      # too few days: ignored
         ("SEDE_BOKER", 30.855, 34.782, 0.25, 0.80, 400)]  # CAMS 25% high in the Negev


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def aeronet_file(box) -> str:
    s, w, n, e = box
    head = ("AERONET Version 3;\nGSFC\nVersion 3: AOD Level 1.5\nThe following data are automatically cloud cleared\n"
            "Contact: PI(s)\nDaily Averages,UNITS can be found at,,, https://aeronet.gsfc.nasa.gov/new_web/units.html\n")
    cols = ["AERONET_Site", "Date(dd:mm:yyyy)", "Time(hh:mm:ss)", "Day_of_Year", "AOD_1640nm", "AOD_500nm",
            "AOD_440nm", "440-870_Angstrom_Exponent", "Site_Latitude(Degrees)", "Site_Longitude(Degrees)",
            "Site_Elevation(m)"]
    rows = []
    for name, lat, lon, aod, _, ndays in SITES:
        if not (s <= lat <= n and w <= lon <= e):
            continue
        for k in range(ndays):
            d = TODAY - timedelta(days=3 + 2 * k)
            wobble = 1 + 0.2 * math.sin(k)
            ae = -999.0 if k % 10 == 0 else 1.5            # some days without an Angstrom exponent
            aod500 = aod * wobble * (550 / 500) ** (ae if ae != -999.0 else hc.DEFAULT_AE)
            rows.append(f"{name},{d:%d:%m:%Y},12:00:00,{d.timetuple().tm_yday},-999.,{aod500:.6f},-999.,"
                        f"{ae:.6f},{lat:.6f},{lon:.6f},10.0")
    return head + ",".join(cols) + "\n" + "\n".join(rows) + "\n"


def cams_json(lat, lon, start, end) -> str:
    site = min(SITES, key=lambda x: abs(x[1] - lat) + abs(x[2] - lon))
    _, _, slon, aod, bias, _ = site
    t0 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    t1 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    times, vals = [], []
    for t in range(t0, t1, 3600):
        d = datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=slon / 15)
        k = (TODAY - timedelta(days=3) - d.date()).days / 2
        wobble = 1 + 0.2 * math.sin(k) if k == int(k) else 1.0
        times.append(t)
        vals.append(aod * wobble / bias)
    return json.dumps({"hourly": {"time": times, "aerosol_optical_depth": vals}})


def run(tmp: Path):
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({
        "data_dir": str(tmp / "data"),
        "regions": [{"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0},
                    {"name": "israel", "north": 33.5, "south": 29.3, "west": 34.2, "east": 35.9}],
        "haze_calibration": {"request_delay_s": 0}}))
    cfg = load_config(str(cfgp))
    calls = {"aeronet": 0, "cams": 0}

    def get(url):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "aeronet" in u.netloc:
            calls["aeronet"] += 1
            check(q["AVG"] == "20" and q["AOD15"] == "1" and q["if_no_html"] == "1", "AERONET: daily averages, Level 1.5, plain text") if calls["aeronet"] == 1 else None
            return aeronet_file((float(q["lat1"]), float(q["lon1"]), float(q["lat2"]), float(q["lon2"])))
        calls["cams"] += 1
        return cams_json(float(q["latitude"]), float(q["longitude"]),
                         date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"]))

    print("1. parsing an AERONET v3 daily file")
    parsed = hc.parse_aeronet(aeronet_file((38, -80, 45, -66)))
    check(set(parsed) == {"CCNY", "GSFC", "Sparse_Site"}, f"stations in the box: {sorted(parsed)}")
    d0 = (TODAY - timedelta(days=3)).isoformat()
    check(abs(parsed["CCNY"]["days"][d0] - 0.20) < 1e-4, "500 nm carried to 550 nm with the Angstrom exponent (and the default where missing)")

    print("2. a first run limited to 2 stations, then the rest")
    r1 = hc.build(cfg, 2, "1.5", max_sites=2, get=get, sleep=lambda s: None, today=TODAY)
    check(len(r1["sites"]) == 2 and r1["pending"] == 1, f"2 stations done, 1 pending ({[s['site'] for s in r1['sites']]})")
    n_cams = calls["cams"]
    r2 = hc.build(cfg, 2, "1.5", max_sites=None, get=get, sleep=lambda s: None, today=TODAY)
    check(calls["cams"] == n_cams + 1 and r2["pending"] == 0, "the second run fetches only the station left")
    by = {s["site"]: s for s in r2["sites"]}
    check(set(by) == {"CCNY", "GSFC", "SEDE_BOKER"}, f"Sparse_Site left out (too few days): {sorted(by)}")
    check(abs(by["CCNY"]["ratio"] - 1.30) < 0.02 and abs(by["SEDE_BOKER"]["ratio"] - 0.80) < 0.02,
          f"ratios found: CCNY {by['CCNY']['ratio']}, SEDE_BOKER {by['SEDE_BOKER']['ratio']}, GSFC {by['GSFC']['ratio']}")
    n_cams = calls["cams"]
    hc.build(cfg, 2, "1.5", max_sites=None, get=get, sleep=lambda s: None, today=TODAY)
    check(calls["cams"] == n_cams, "a third run asks CAMS for nothing (all cached / done)")

    print("3. a factor for a place")
    cal = json.loads(hc.calibration_path(cfg).read_text())
    nyc = hc.calibration_at(cal, 40.65, -73.95)
    check(1.22 < nyc["factor"] < 1.30 and nyc["stations"][0]["site"] == "CCNY",
          f"Brooklyn: {nyc['factor']} from {[s['site'] for s in nyc['stations']]} (pulled a little toward 1)")
    far = hc.calibration_at(cal, 35.0, -100.0)
    check(far["factor"] == 1.0 and far["stations"] == [], "no station within 300 km: factor 1")
    jer = hc.calibration_at(cal, 31.78, 35.23)
    check(0.80 < jer["factor"] < 0.9, f"Jerusalem from Sede Boker (~105 km): {jer['factor']}")

    print("4. /v1/haze-calibration")
    serve.Handler.store = serve.Store(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    body = json.loads(urllib.request.urlopen(base + "/v1/haze-calibration?lat=40.65&lon=-73.95").read())
    check(body["factor"] == nyc["factor"] and body["radiusKm"] == 300.0, f"endpoint answers {body['factor']}")
    hc.calibration_path(cfg).unlink()
    try:
        urllib.request.urlopen(base + "/v1/haze-calibration?lat=40.65&lon=-73.95")
        check(False, "should be 503")
    except urllib.error.HTTPError as e:
        check(e.code == 503, "503 before the calibration has been built")
    srv.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
