#!/usr/bin/env python3
"""Path profiles from radiosonde soundings, for checking the calculation against observed sunrises.

Keller & Hall (2022, Computers & Geosciences 161, 105044, Fig. 8) took Bet Dagan soundings near sunrise and
shifted them to follow a smoothed curve of the terrain along the first 80 km of the ray, so the air near
the ground lay along the ground. They reported the winter error at Armon HaNatziv falling from 32 s to 12 s.
This writes the same kind of profiles, in /v1/path-profiles' JSON, for royzmanimwebsite's
_denoDebug/validateSunrise.mjs, in three versions:

  raw    the sounding as measured, the same all along the path
  kh-t   shifted their way: each level raised by (smoothed ground - station height); temperatures kept,
         pressure recomputed hydrostatically for the real height
  kh-tp  shifted with temperatures AND pressures kept, as the paper words it

Soundings come from NOAA's IGRA v2 (one station's whole record, e.g. ISM00040179-data.txt for Bet Dagan:
https://www.ncei.noaa.gov/data/integrated-global-radiosonde-archive/access/data-por/). For each date the
launch nearest sunrise from --before hours before to --after hours after is used (default 6 / 1.5: in
winter the 05-06Z launch just after sunrise, in summer the 23Z one before it, as Keller & Hall did).

    python3 sonde_profiles.py --igra ISM00040179-data.txt --lat 31.748553 --lon 35.237436 \\
        --dates druk.csv --out-prefix druk-sonde        # -> druk-sonde-raw.json, -kh-t.json, -kh-tp.json

The smoothed terrain is a local quadratic fit (Gaussian weights, --sigma-km, default 3) of the elevation
tiles along the Sun's azimuth: unlike a plain average it keeps the trend up to the observer (Armon HaNatziv:
738 m, the Dead Sea -430 m, Moab ~750 m, as Keller & Hall's Fig. 12). Beyond --shift-km (80) the offset is
held. Result at Armon HaNatziv (README, "Checking against observed sunrises"): every shifted version was
worse than the raw sounding, which was worse than ERA5.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from common import destination, load_config, sun_event

log = logging.getLogger("sonde_profiles")
G, R_DRY = 9.80665, 287.05
DISTANCES = [0, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 100, 150, 200, 300, 400]


def read_igra(path: Path, years: set[int]) -> list[dict]:
    """Soundings of those years: [{time (release, UTC), levels: [{p mb, z m, t C, surface}] bottom up}]."""
    out, cur = [], None
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                if cur and cur["levels"]:
                    out.append(cur)
                cur = None
                y = int(line[13:17])
                if y not in years or int(line[24:26]) == 99:
                    continue
                day = datetime(y, int(line[18:20]), int(line[21:23]), tzinfo=timezone.utc)
                rel = line[27:31]
                if rel[:2] != "99" and rel.strip() not in ("", "9999"):
                    t = day + timedelta(hours=int(rel[:2]), minutes=int(rel[2:]) if rel[2:] != "99" else 0)
                else:
                    t = day + timedelta(hours=int(line[24:26]))
                cur = {"time": t, "levels": []}
                continue
            if cur is None:
                continue
            p, z, temp = int(line[9:15]), int(line[16:21]), int(line[22:27])
            if p < 0 or temp == -9999:
                continue
            cur["levels"].append({"p": p / 100, "z": None if z == -9999 else float(z), "t": temp / 10,
                                  "surface": line[1] == "1"})
    if cur and cur["levels"]:
        out.append(cur)
    for s in out:
        s["levels"] = _heights(s["levels"])
    return [s for s in out if len(s["levels"]) >= 3 and s["levels"][0]["surface"] and s["levels"][-1]["z"] >= 3000]


def _heights(levels: list[dict]) -> list[dict]:
    """Heights for levels without one, hydrostatically from the nearest known level below; bottom up."""
    lv = sorted(levels, key=lambda l: -l["p"])
    known = [i for i, l in enumerate(lv) if l["z"] is not None]
    if not known:
        return []
    for i, l in enumerate(lv):
        if l["z"] is None:
            k = max([j for j in known if j < i], default=known[0])
            a = lv[k]
            l["z"] = a["z"] + R_DRY * ((a["t"] + l["t"]) / 2 + 273.15) / G * math.log(a["p"] / l["p"])
    out = []
    for l in lv:
        if not out or l["z"] > out[-1]["z"] + 0.5:
            out.append(l)
    return out


def smoothed_ground(tiles, lat: float, lon: float, az: float, shift_km: float, sigma_km: float):
    """Ground along the azimuth as a local quadratic fit; a function of distance (km), held beyond shift_km."""
    d = np.arange(0, shift_km + 20.01, 0.5)
    pts = [destination(lat, lon, az, x) for x in d]
    h = tiles.heights(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
    hs = np.empty_like(h)
    for i, x0 in enumerate(d):
        w = np.sqrt(np.exp(-0.5 * ((d - x0) / sigma_km) ** 2))
        A = np.vstack([np.ones_like(d), d - x0, (d - x0) ** 2]).T
        hs[i] = np.linalg.lstsq(A * w[:, None], h * w, rcond=None)[0][0]
    return lambda x: float(np.interp(min(x, shift_km), d, hs))


def column(sounding: dict, offset: float, mode: str, dist: float, lat: float, lon: float) -> dict:
    lv = sounding["levels"]
    if mode == "raw" or offset == 0:
        levels = [{"h": round(l["z"], 1), "t": l["t"], "p": l["p"]} for l in lv]
    elif mode == "kh-tp":
        levels = [{"h": round(l["z"] + offset, 1), "t": l["t"], "p": l["p"]} for l in lv]
    else:
        p = lv[0]["p"] * math.exp(-G * offset / (R_DRY * (lv[0]["t"] + 273.15)))
        levels = []
        for i, l in enumerate(lv):
            if i:
                p *= math.exp(-G * (l["z"] - lv[i - 1]["z"]) / (R_DRY * ((lv[i - 1]["t"] + l["t"]) / 2 + 273.15)))
            levels.append({"h": round(l["z"] + offset, 1), "t": l["t"], "p": round(p, 2)})
    return {"distanceKm": dist, "lat": round(lat, 4), "lon": round(lon, 4), "water": False,
            "levels": [x for x in levels if x["h"] < 20000]}


def make(soundings, dates, lat, lon, tiles, before=6.0, after=1.5, shift_km=80.0, sigma_km=3.0) -> dict:
    files = {m: {"cycle": None, "location": {"lat": lat, "lon": lon}, "sources": {"sonde": f"radiosonde, {m}"},
                 "events": []} for m in ("raw", "kh-t", "kh-tp")}
    for d in dates:
        se = sun_event(d, lat, lon, "sunrise")
        if se is None:
            continue
        sr, az = se
        cands = [s for s in soundings if -before <= (s["time"] - sr).total_seconds() / 3600 <= after]
        if not cands:
            continue
        s = min(cands, key=lambda s: abs((s["time"] - sr).total_seconds()))
        ground = smoothed_ground(tiles, lat, lon, az, shift_km, sigma_km)
        station = s["levels"][0]["z"]
        for m, body in files.items():
            profs = [column(s, ground(dist) - station, m, dist, *destination(lat, lon, az, dist)) for dist in DISTANCES]
            body["events"].append({"date": d.isoformat(), "event": "sunrise", "source": f"sonde-{m}",
                                   "timeUtc": sr.strftime("%Y-%m-%dT%H:%M:%SZ"), "azimuthDeg": round(az, 2),
                                   "sondeTime": s["time"].isoformat(), "profiles": profs})
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--igra", type=Path, required=True, help="an IGRA v2 station file (unzipped)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--dates", type=Path, required=True, help="file with a date (YYYY-MM-DD) at the start of each line")
    ap.add_argument("--out-prefix", required=True)
    ap.add_argument("--before", type=float, default=6.0)
    ap.add_argument("--after", type=float, default=1.5)
    ap.add_argument("--shift-km", type=float, default=80.0)
    ap.add_argument("--sigma-km", type=float, default=3.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    dates = sorted({date.fromisoformat(m.group(1)) for line in a.dates.read_text().splitlines()
                    if (m := re.match(r"\s*(\d{4}-\d{2}-\d{2})", line))})
    import serve
    tiles = serve.Horizons(cfg).tiles
    soundings = read_igra(a.igra, {d.year for d in dates} | {d.year - 1 for d in dates})
    files = make(soundings, dates, a.lat, a.lon, tiles, a.before, a.after, a.shift_km, a.sigma_km)
    for m, body in files.items():
        Path(f"{a.out_prefix}-{m}.json").write_text(json.dumps(body, separators=(",", ":")))
    log.info("%d of %d dates have a sounding -> %s-{raw,kh-t,kh-tp}.json", len(files["raw"]["events"]), len(dates),
             a.out_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
