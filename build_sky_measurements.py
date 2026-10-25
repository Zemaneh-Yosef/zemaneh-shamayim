"""Globe at Night's sky-meter readings, as measurements for light_pollution.py.

    python3 build_sky_measurements.py [--config config.json] [--since 2020] [--out FILE]

Globe at Night (NSF NOIRLab, globeatnight.org; data CC BY 4.0) collects observations of the night sky from the
public every year, many with a Sky Quality Meter reading of the zenith's brightness (mag/arcsec^2). This
downloads the yearly files from --since on (CSV; the page lists them) and keeps the readings the light-pollution
model can compare with the atlas, which is the zenith on a clear, moonless night after astronomical twilight
(Falchi et al. 2016, sec. 2):

  - a meter reading (SQMReading), within what a night sky can be: 15 to 22.5 mag/arcsec^2 (the atlas's
    brightest skies are ~15.5; a natural sky is ~22.0 and a meter reads it within a few tenths);
  - the observer's own "clear" (CloudCover);
  - the Sun's centre 18 degrees or more below the horizon at the reading's UT time;
  - the Moon below the horizon (topocentric, with its parallax), from the Astronomical Almanac's low-precision
    lunar formulae (0.3 degree).

The readings go to light_pollution.measurements.globe_at_night_file (default <data_dir>/sky-measurements-gan.json),
which the server reads beside your own readings: within measurements.radius_km of a place they replace the
atlas's level there (their median ratio to the atlas), carried to the year asked for by the regional trend.
Single citizen readings scatter by a few tenths of a magnitude; the median of several is what is used.
Rebuild once a year, when Globe at Night adds the last year's file.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from common import _sun_place, load_config, write_json_atomic

log = logging.getLogger("build_sky_measurements")

PAGE = "https://globeatnight.org/maps.php"
BASE = "https://globeatnight.org"
SQM_MIN, SQM_MAX = 15.0, 22.5
SUN_MAX_DEG = -18.0


def get(url: str, ua: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=300) as r:
        return r.read()


def yearly_files(page: str) -> dict[int, str]:
    """{year: URL} of the yearly CSV files the data page links."""
    return {int(y): BASE + href for href, y in re.findall(r'href="(/documents/\d+/GaN(\d{4})\.csv)"', page)}


def _jd(t: datetime) -> float:
    return t.timestamp() / 86400 + 2440587.5


def _alt(ra: float, dec: float, gast: float, lat: float, lon: float) -> float:
    H = gast + math.radians(lon) - ra
    phi = math.radians(lat)
    return math.degrees(math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(H)))


def sun_altitude(t: datetime, lat: float, lon: float) -> float:
    ra, dec, gast = _sun_place(_jd(t))
    return _alt(ra, dec, gast, lat, lon)


def moon_altitude(t: datetime, lat: float, lon: float) -> float:
    """Topocentric altitude (deg) of the Moon's centre: the Astronomical Almanac's low-precision formulae
    (ecliptic longitude, latitude and horizontal parallax; ~0.3 deg), parallax taken off."""
    jd = _jd(t)
    T = (jd - 2451545.0) / 36525
    s = lambda a, b: math.sin(math.radians(a + b * T))   # noqa: E731
    c = lambda a, b: math.cos(math.radians(a + b * T))   # noqa: E731
    lam = (218.32 + 481267.881 * T + 6.29 * s(135.0, 477198.87) - 1.27 * s(259.3, -413335.36)
           + 0.66 * s(235.7, 890534.22) + 0.21 * s(269.9, 954397.74) - 0.19 * s(357.5, 35999.05)
           - 0.11 * s(186.5, 966404.03))
    beta = (5.13 * s(93.3, 483202.02) + 0.28 * s(228.2, 960400.89) - 0.28 * s(318.3, 6003.15)
            - 0.17 * s(217.6, -407332.21))
    par = (0.9508 + 0.0518 * c(135.0, 477198.87) + 0.0095 * c(259.3, -413335.36) + 0.0078 * c(235.7, 890534.22)
           + 0.0028 * c(269.9, 954397.74))
    lam, beta = math.radians(lam), math.radians(beta)
    eps = math.radians(23.4393 - 0.0130 * T)
    ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps), math.cos(lam))
    dec = math.asin(math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam))
    gast = _sun_place(jd)[2]
    alt = _alt(ra, dec, gast, lat, lon)
    return alt - par * math.cos(math.radians(alt))


def readings(text: str) -> tuple[list[dict], dict]:
    """The usable readings of one yearly file, and how many each test left out."""
    out, why = [], {"no reading": 0, "out of range": 0, "not clear": 0, "twilight": 0, "moon up": 0, "bad row": 0}
    for r in csv.DictReader(io.StringIO(text)):
        try:
            raw = (r.get("SQMReading") or "").strip()
            if not raw:
                why["no reading"] += 1
                continue
            sqm = float(raw)
            if not SQM_MIN <= sqm <= SQM_MAX:
                why["out of range"] += 1
                continue
            if (r.get("CloudCover") or "").strip().lower() != "clear":
                why["not clear"] += 1
                continue
            lat, lon = float(r["Latitude"]), float(r["Longitude"])
            t = datetime.strptime(f"{r['UTDate']} {r['UTTime']}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            why["bad row"] += 1
            continue
        if sun_altitude(t, lat, lon) > SUN_MAX_DEG:
            why["twilight"] += 1
            continue
        if moon_altitude(t, lat, lon) > 0:
            why["moon up"] += 1
            continue
        out.append({"lat": round(lat, 5), "lon": round(lon, 5), "date": r["UTDate"], "sqm": sqm,
                    "note": f"Globe at Night {r.get('ID', '')} (citizen SQM reading)"})
    return out, why


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--since", type=int, default=2020, help="first year's file to use (default 2020)")
    ap.add_argument("--out", help="default: light_pollution.measurements.globe_at_night_file")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    meas = cfg.get("light_pollution", {}).get("measurements", {})
    out = Path(a.out or meas.get("globe_at_night_file") or Path(cfg["data_dir"]) / "sky-measurements-gan.json")
    ua = f"zmanim-sky-server/0.1 ({cfg.get('contact') or 'no contact'})"
    files = {y: u for y, u in yearly_files(get(PAGE, ua).decode("utf-8", "replace")).items() if y >= a.since}
    if not files:
        log.error("no yearly files from %d on found on %s", a.since, PAGE)
        return 1
    allr, years = [], []
    for y, url in sorted(files.items()):
        rows, why = readings(get(url, ua).decode("utf-8", "replace"))
        log.info("%d: %d readings kept; left out: %s", y, len(rows), why)
        allr += rows
        years.append(y)
    write_json_atomic(out, {
        "about": f"Globe at Night sky-meter readings {years[0]}-{years[-1]} (globeatnight.org, NSF NOIRLab, CC BY 4.0), "
                 "clear, Sun below -18 deg, Moon down; built by build_sky_measurements.py",
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "measurements": allr})
    log.info("%d readings written to %s", len(allr), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
