#!/usr/bin/env python3
"""Calibrate the CAMS haze forecast against AERONET's measurements.

The app reads the evening haze (aerosol optical depth, AOD, at 550 nm) from CAMS through Open-Meteo for the
star-visibility nightfall. CAMS is a model; AERONET (NASA, aeronet.gsfc.nasa.gov) measures the real AOD with
sun photometers at hundreds of stations (many across the US, Canada's AEROCAN, several in Israel). A model's
errors follow the kind of haze a region has - desert dust, city pollution, wildfire smoke - so a station
says much about its surroundings. This script finds, for every AERONET station inside the configured
regions, how far CAMS is off on average:

    ratio = exp(median over days of ln(AERONET AOD / CAMS AOD))      (daily daytime means)

and writes them to <data_dir>/haze-calibration.json. /v1/haze-calibration then estimates the factor at a place
from the stations by kriging (calibration_at below): the textbook estimate between scattered measurements, its
weights set by how alike the stations' ratios actually are at each distance (their variogram, fitted to them).

    python3 haze_calibration.py [--config config.json] [--years 2] [--max-sites N] [--level 1.5]

Data: AERONET version 3 daily averages, Level 1.5 (cloud-screened; Level 2.0 is quality-assured but trails
by months) - no key. AERONET measures at 500 nm; it is carried to 550 nm with the station's 440-870 nm
Angstrom exponent. CAMS: Open-Meteo's air-quality API, hourly, averaged over the same day's daylight hours
(local solar time 8-16 h, when AERONET measures). Each station's CAMS series is one request for the whole
period, which Open-Meteo counts as many calls (~25 per year of data): with the default two years and the
free limit of 10,000 calls a day, about 150 stations fit in a day. The requests are spaced out
(haze_calibration.request_delay_s), the CAMS series are kept under <data_dir>/haze-cal-cache/, and a stopped
run (or --max-sites) picks up where it left off: run it again until it reports no station left. Rebuild
once a year.

AERONET's data policy asks that use of the data acknowledge the AERONET network and its station PIs.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from common import load_config, write_json_atomic

log = logging.getLogger("haze_calibration")

AERONET_URL = "https://aeronet.gsfc.nasa.gov/cgi-bin/print_web_data_v3"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
CAMS_START = date(2022, 8, 1)                    # the CAMS archive Open-Meteo serves starts here
MISSING = -999.0
DEFAULT_AE = 1.3                                 # Angstrom exponent when a day has none (continental aerosol)


def cal_cfg(cfg: dict) -> dict:
    return cfg.get("haze_calibration", {})


def calibration_path(cfg: dict) -> Path:
    f = cal_cfg(cfg).get("file") or ""
    return Path(f) if f else Path(cfg["data_dir"]) / "haze-calibration.json"


def http_get(url: str, contact: str, timeout: int = 300, attempts: int = 3) -> str:
    ua = "zmanim-sky-server/0.1" + (f" ({contact})" if contact else "")
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and i + 1 < attempts:
                log.warning("rate limited; waiting 65 s")
                time.sleep(65)
                continue
            if e.code < 500 or i + 1 >= attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i + 1 >= attempts:
                raise
        time.sleep(10 * (i + 1))
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------------------------
# AERONET
# ---------------------------------------------------------------------------------------------
def aeronet_url(box: tuple[float, float, float, float], start: date, end: date, level: str) -> str:
    s, w, n, e = box
    q = {"year": start.year, "month": start.month, "day": start.day,
         "year2": end.year, "month2": end.month, "day2": end.day,
         f"AOD{level.replace('.', '')}": 1, "AVG": 20, "if_no_html": 1,
         "lat1": s, "lon1": w, "lat2": n, "lon2": e}
    return AERONET_URL + "?" + urllib.parse.urlencode(q)


def parse_aeronet(text: str) -> dict[str, dict]:
    """AERONET v3 daily file -> {site: {lat, lon, days: {iso date: AOD at 550 nm}}}."""
    lines = [ln.strip() for ln in text.replace("<br>", "\n").splitlines()]
    hi = next((i for i, ln in enumerate(lines) if "Date(dd:mm:yyyy)" in ln), None)
    if hi is None:
        return {}
    cols = lines[hi].split(",")

    def col(*names, contains=None):
        for k, c in enumerate(cols):
            if c in names or (contains and contains in c):
                return k
        return None

    i_site = col("AERONET_Site", "AERONET_Site_Name")
    i_date = col("Date(dd:mm:yyyy)")
    i_aod = col("AOD_500nm")
    i_ae = col("440-870_Angstrom_Exponent")
    i_lat = col(contains="Site_Latitude")
    i_lon = col(contains="Site_Longitude")
    if None in (i_site, i_date, i_aod, i_lat, i_lon):
        raise ValueError(f"unexpected AERONET columns: {cols[:12]}...")
    out: dict[str, dict] = {}
    for ln in lines[hi + 1:]:
        f = ln.split(",")
        if len(f) <= max(i_site, i_date, i_aod, i_lat, i_lon):
            continue
        try:
            aod500 = float(f[i_aod])
            if aod500 == MISSING or aod500 < 0:
                continue
            ae = float(f[i_ae]) if i_ae is not None else MISSING
            if ae == MISSING or not -1 < ae < 4:
                ae = DEFAULT_AE
            d, m, y = f[i_date].split(":")
            iso = date(int(y), int(m), int(d)).isoformat()
            lat, lon = float(f[i_lat]), float(f[i_lon])
        except ValueError:
            continue
        site = out.setdefault(f[i_site], {"lat": lat, "lon": lon, "days": {}})
        site["days"][iso] = aod500 * (550 / 500) ** (-ae)
    return out


# ---------------------------------------------------------------------------------------------
# CAMS (Open-Meteo)
# ---------------------------------------------------------------------------------------------
def cams_url(lat: float, lon: float, start: date, end: date) -> str:
    return (f"{AQ_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly=aerosol_optical_depth"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}&timeformat=unixtime&timezone=GMT")


def cams_daytime(j: dict, lon: float) -> dict[str, float]:
    """Hourly CAMS -> {iso date: mean AOD over local solar time 8-16 h}."""
    times = j.get("hourly", {}).get("time", []) or []
    vals = j.get("hourly", {}).get("aerosol_optical_depth", []) or []
    acc: dict[str, list[float]] = {}
    for t, v in zip(times, vals):
        if v is None or v < 0:
            continue
        local = datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=lon / 15)
        if 8 <= local.hour + local.minute / 60 <= 16:
            acc.setdefault(local.date().isoformat(), []).append(float(v))
    return {d: sum(x) / len(x) for d, x in acc.items() if len(x) >= 4}


# ---------------------------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------------------------
def site_ratio(aero: dict[str, float], cams: dict[str, float], min_days: int) -> dict | None:
    """Median log ratio over common days; None with too few days."""
    logs = sorted(math.log(aero[d] / cams[d]) for d in aero.keys() & cams.keys()
                  if aero[d] > 0.01 and cams[d] > 0.02)
    if len(logs) < min_days:
        return None
    med = logs[len(logs) // 2] if len(logs) % 2 else (logs[len(logs) // 2 - 1] + logs[len(logs) // 2]) / 2
    mad = sorted(abs(x - med) for x in logs)[len(logs) // 2]
    return {"ratio": round(math.exp(med), 3), "n": len(logs), "spreadLog": round(mad, 3)}


def boxes(cfg: dict) -> list[tuple[float, float, float, float]]:
    m = float(cal_cfg(cfg).get("margin_deg", 2.0))
    return [(max(-90, r["south"] - m), max(-180, r["west"] - m), min(90, r["north"] + m), min(180, r["east"] + m))
            for r in cfg["regions"]]


def build(cfg: dict, years: float, level: str, max_sites: int | None, get=None, sleep=time.sleep,
          today: date | None = None) -> dict:
    get = get or (lambda url: http_get(url, cfg.get("contact", "")))
    cc = cal_cfg(cfg)
    today = today or datetime.now(timezone.utc).date()
    end = today - timedelta(days=2)
    start = max(CAMS_START, end - timedelta(days=int(365.25 * years)))
    min_days = int(cc.get("min_days", 60))
    delay = float(cc.get("request_delay_s", 8))
    cache = Path(cfg["data_dir"]) / "haze-cal-cache"
    cache.mkdir(parents=True, exist_ok=True)

    sites: dict[str, dict] = {}
    for box in boxes(cfg):
        log.info("AERONET stations in %s, %s .. %s (Level %s)", box, start, end, level)
        for name, s in parse_aeronet(get(aeronet_url(box, start, end, level))).items():
            sites.setdefault(name, s)["days"].update(s["days"])
    usable = {k: v for k, v in sites.items() if len(v["days"]) >= min_days}
    log.info("%d stations, %d with at least %d days of measurements", len(sites), len(usable), min_days)

    path = calibration_path(cfg)
    old = json.loads(path.read_text()) if path.exists() else {}
    same = old.get("period") == [start.isoformat(), end.isoformat()]
    done = {s["site"]: s for s in old.get("sites", [])} if same else {}
    skipped = set(old.get("skipped", [])) if same else set()
    out_sites = list(done.values())
    todo = [k for k in sorted(usable) if k not in done and k not in skipped]
    fetched = 0
    for name in todo:
        if max_sites is not None and fetched >= max_sites:
            break
        s = usable[name]
        cfile = cache / f"{name}_{start}_{end}.json"
        if cfile.exists():
            cams = json.loads(cfile.read_text())
        else:
            try:
                cams = cams_daytime(json.loads(get(cams_url(s["lat"], s["lon"], start, end))), s["lon"])
            except Exception as e:
                log.warning("%s: CAMS request failed (%s); will retry next run", name, e)
                continue
            cfile.write_text(json.dumps(cams))
            fetched += 1
            sleep(delay)
        r = site_ratio(s["days"], cams, min_days)
        if r is None:
            log.info("%s: too few days with both measurement and model", name)
            skipped.add(name)
            continue
        out_sites.append({"site": name, "lat": s["lat"], "lon": s["lon"], **r})
        log.info("%s (%.2f, %.2f): AERONET / CAMS = %.2f over %d days", name, s["lat"], s["lon"], r["ratio"], r["n"])
    left = [k for k in todo if k not in {x["site"] for x in out_sites} and k not in skipped]
    result = {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "period": [start.isoformat(), end.isoformat()], "level": level,
              "sites": sorted(out_sites, key=lambda x: x["site"]), "pending": len(left), "skipped": sorted(skipped),
              "source": "AERONET v3 daily (NASA GSFC) vs CAMS via Open-Meteo"}
    write_json_atomic(path, result)
    log.info("%d stations calibrated%s -> %s", len(out_sites),
             f"; {len(left)} left (run again)" if left else "", path)
    return result


# ---------------------------------------------------------------------------------------------
# Lookup (used by serve.py)
# ---------------------------------------------------------------------------------------------
def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------------------------
# A factor for a place: kriging
#
# z = ln(ratio) at each station. Its spatial structure is the variogram gamma(h) = E[(z(x) - z(x + h))^2] / 2,
# estimated from the station pairs (Matheron's estimator) and fitted with the exponential model
# c0 + c1 (1 - exp(-h / a)) by weighted least squares, weights N(h) / gamma(h)^2 (Cressie 1985, Math. Geol.
# 17:563). Lags as Journel & Huijbregts (1978, Mining Geostatistics, p. 194) advise: at least 30 pairs each, out
# to half the largest distance. Each station's own ratio is uncertain - the median of n daily log ratios with
# spread MAD has variance pi/2 (1.4826 MAD)^2 / n (the sample median's, normal errors) - which the nugget c0
# holds on average; kriging with measurement error (Cressie 1993, Statistics for Spatial Data, sec. 3.2.1) puts
# each station's own on its diagonal and the rest of c0 as small-scale variation. The mean is the generalised
# least squares mean of all stations (the kriging mean), so far from every station the factor is the network's
# average, not 1. Stations beyond the practical range (3a: correlation under 5%) are left out.
# ---------------------------------------------------------------------------------------------
def _error_var(s: dict) -> float:
    sd = 1.4826 * float(s.get("spreadLog") or 0.0)
    return math.pi / 2 * sd * sd / max(int(s.get("n") or 1), 1)


def fit_variogram(sites: list[dict]) -> dict:
    """{"nugget": c0, "sill": c1, "rangeKm": a, "lags": [[h, gamma, pairs], ...]} from the stations' log ratios.
    With too few pairs to fit (under two lags), a pure nugget: no spatial correlation."""
    z = [math.log(s["ratio"]) for s in sites]
    pairs = sorted((distance_km(a["lat"], a["lon"], b["lat"], b["lon"]), (z[i] - z[j]) ** 2 / 2)
                   for i, a in enumerate(sites) for j, b in enumerate(sites) if j > i)
    var = sum((v - sum(z) / len(z)) ** 2 for v in z) / max(len(z) - 1, 1) if z else 0.0
    if not pairs:
        return {"nugget": var, "sill": 0.0, "rangeKm": 1.0, "lags": []}
    half = pairs[-1][0] / 2
    pairs = [p for p in pairs if p[0] <= half]
    lags = []
    for k in range(0, len(pairs) - 29, 30):
        chunk = pairs[k:k + 30] if len(pairs) - k >= 60 else pairs[k:]
        lags.append([sum(p[0] for p in chunk) / len(chunk), sum(p[1] for p in chunk) / len(chunk), len(chunk)])
        if len(chunk) > 30:
            break
    if len(lags) < 2:
        return {"nugget": var, "sill": 0.0, "rangeKm": 1.0, "lags": lags}
    best = None
    for k in range(200):                                    # a from 5 km to 5000 km, evenly in log
        a = 5.0 * 1000 ** (k / 199)
        rows = [(1.0, 1 - math.exp(-h / a), g, n / max(g, 1e-12) ** 2) for h, g, n in lags]
        # weighted least squares for c0, c1 >= 0 (the two-variable normal equations, then the bounds)
        sw = sum(w for *_, w in rows)
        sx = sum(w * x for _, x, _, w in rows); sxx = sum(w * x * x for _, x, _, w in rows)
        sy = sum(w * y for _, _, y, w in rows); sxy = sum(w * x * y for _, x, y, w in rows)
        det = sw * sxx - sx * sx
        cands = []
        if det > 0:
            c0, c1 = (sy * sxx - sx * sxy) / det, (sw * sxy - sx * sy) / det
            if c0 >= 0 and c1 >= 0:
                cands.append((c0, c1))
        cands += [(0.0, max(sxy / sxx, 0.0) if sxx else 0.0), (max(sy / sw, 0.0), 0.0)]
        for c0, c1 in cands:
            sse = sum(w * (c0 + c1 * x - y) ** 2 for _, x, y, w in rows)
            if best is None or sse < best[0]:
                best = (sse, c0, c1, a)
    _, c0, c1, a = best
    return {"nugget": c0, "sill": c1, "rangeKm": a, "lags": [[round(h, 1), round(g, 5), n] for h, g, n in lags]}


def _solve(m: list[list[float]], v: list[float]) -> list[float]:
    """m x = v by Gaussian elimination with partial pivoting (m symmetric positive definite here)."""
    n = len(v)
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        for r in range(c + 1, n):
            f = a[r][c] / a[c][c]
            for k in range(c, n + 1):
                a[r][k] -= f * a[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][k] * x[k] for k in range(r + 1, n))) / a[r][r]
    return x


def kriging_model(cal: dict) -> dict:
    """The fitted variogram and the network's mean log ratio (cached in `cal`)."""
    if "_model" in cal:
        return cal["_model"]
    sites = [s for s in cal.get("sites", []) if s.get("ratio", 0) > 0]
    vg = fit_variogram(sites) if sites else {"nugget": 0.0, "sill": 0.0, "rangeKm": 1.0, "lags": []}
    errs = [_error_var(s) for s in sites]
    micro = max(vg["nugget"] - (sum(errs) / len(errs) if errs else 0.0), 0.0)
    cov = lambda d: vg["sill"] * math.exp(-d / vg["rangeKm"])   # noqa: E731
    mean = 0.0
    if sites:
        n = len(sites)
        K = [[cov(distance_km(a["lat"], a["lon"], b["lat"], b["lon"])) + (micro + errs[i] if i == j else 0.0)
              for j, b in enumerate(sites)] for i, a in enumerate(sites)]
        z = [math.log(s["ratio"]) for s in sites]
        ki1, kiz = _solve(K, [1.0] * n), _solve(K, z)
        mean = sum(kiz) / sum(ki1)
    cal["_model"] = {"sites": sites, "errs": errs, "micro": micro, "variogram": vg, "meanLog": mean}
    return cal["_model"]


def calibration_at(cal: dict, lat: float, lon: float) -> dict:
    """The factor at a place by simple kriging of ln(ratio) about the network's mean (see above), with its
    standard error (sdLog, in ln units) and the stations it used."""
    m = kriging_model(cal)
    vg, sites = m["variogram"], m["sites"]
    reach = 3 * vg["rangeKm"] if vg["sill"] > 0 else 0.0
    near = [(i, distance_km(lat, lon, s["lat"], s["lon"])) for i, s in enumerate(sites)]
    near = [(i, d) for i, d in near if d <= reach]
    cov = lambda d: vg["sill"] * math.exp(-d / vg["rangeKm"])   # noqa: E731
    z, var0 = m["meanLog"], vg["sill"] + m["micro"]
    weights = []
    if near:
        K = [[cov(distance_km(sites[i]["lat"], sites[i]["lon"], sites[j]["lat"], sites[j]["lon"]))
              + (m["micro"] + m["errs"][i] if i == j else 0.0) for j, _ in near] for i, _ in near]
        k = [cov(d) for _, d in near]
        weights = _solve(K, k)
        z += sum(w * (math.log(sites[i]["ratio"]) - m["meanLog"]) for w, (i, _) in zip(weights, near))
        var0 -= sum(w * c for w, c in zip(weights, k))
    return {
        "factor": round(math.exp(z), 3),
        "sdLog": round(math.sqrt(max(var0, 0.0)), 3),
        "stations": [{"site": sites[i]["site"], "lat": sites[i]["lat"], "lon": sites[i]["lon"], "distanceKm": round(d, 1),
                      "ratio": sites[i]["ratio"], "days": sites[i]["n"], "weight": round(w, 3)}
                     for w, (i, d) in sorted(zip(weights, near), key=lambda x: x[1][1])],
        "networkFactor": round(math.exp(m["meanLog"]), 3),
        "variogram": {"nugget": round(vg["nugget"], 5), "sill": round(vg["sill"], 5), "rangeKm": round(vg["rangeKm"], 1)},
        "period": cal.get("period"), "built": cal.get("built"), "source": cal.get("source"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--years", type=float, help="period of comparison, years back from now (default 2)")
    ap.add_argument("--level", choices=["1.5", "2.0"], help="AERONET level (default 1.5)")
    ap.add_argument("--max-sites", type=int, help="CAMS requests this run (default: all left)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    cc = cal_cfg(cfg)
    build(cfg, a.years or float(cc.get("years", 2)), a.level or str(cc.get("level", "1.5")),
          a.max_sites if a.max_sites is not None else cc.get("max_sites_per_run"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
