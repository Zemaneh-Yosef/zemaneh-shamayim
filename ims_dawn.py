#!/usr/bin/env python3
"""Dawn temperatures at Israel Meteorological Service stations for the mornings of observed sunrises, to see
whether the cold air that pools in the Jordan valley before sunrise (which ERA5 and GFS smooth away)
explains what remains of the error against Rav Druk's and Yisroel Golan's sunrises.

IMS's observation API (https://api.ims.gov.il, 10-minute data) needs a free token: request one on IMS's
"Observation Data API" page (ims.gov.il). Only the standard library is used, so it runs anywhere.

    python3 ims_dawn.py --token TOKEN --list                       # every station: id, name, place, channels
    python3 ims_dawn.py --token TOKEN --obs golan.csv --stations 23,178,65 --out dawn.csv

--obs: observations as observations.py writes them (date,time,...; Israel standard time, UTC+2). For each
date and station the output has the temperature (channel TD, deg C) at the 10-minute record nearest to the
observed sunrise and the lowest in the two hours before it, as <id>_t and <id>_min (empty where the station
has no data). Responses are kept in --cache (default ./ims-cache), so a rerun only asks for what's missing.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.ims.gov.il/v1/envista"
IST = timezone(timedelta(hours=2))


def get(path: str, token: str, cache: Path | None = None):
    f = cache / (path.strip("/").replace("/", "_") + ".json") if cache else None
    if f and f.exists():
        return json.loads(f.read_text())
    req = urllib.request.Request(API + path, headers={"Authorization": f"ApiToken {token}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):                     # no data for that day
                raw = b"null"
                break
            if e.code in (401, 403):
                raise SystemExit(f"IMS refused the token ({e.code}): check --token")
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    data = json.loads(raw or b"null")
    if f:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))
    return data


def list_stations(token: str) -> None:
    for s in get("/stations", token) or []:
        loc = s.get("location") or {}
        chans = ",".join(m.get("name", "?") for m in s.get("monitors", []) if m.get("active", True))
        print(f'{s.get("stationId")}\t{s.get("name")}\t{loc.get("latitude")}\t{loc.get("longitude")}\t'
              f'{"active" if s.get("active") else "closed"}\t{chans}')


def read_obs(path: Path):
    rows = (line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#"))
    for r in csv.DictReader(rows):
        d = date.fromisoformat(r["date"])
        h, m, *s = (int(x) for x in r["time"].split(":"))
        yield d, datetime(d.year, d.month, d.day, h, m, s[0] if s else 0, tzinfo=IST)


def temps(day_data) -> list[tuple[datetime, float]]:
    out = []
    for rec in (day_data or {}).get("data", []):
        t = datetime.fromisoformat(rec["datetime"])
        for ch in rec.get("channels", []):
            if ch.get("name") == "TD" and ch.get("valid", True) and ch.get("value") is not None:
                out.append((t, float(ch["value"])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", required=True)
    ap.add_argument("--list", action="store_true", help="list the stations and exit")
    ap.add_argument("--obs", type=Path)
    ap.add_argument("--stations", help="station ids, comma-separated (from --list)")
    ap.add_argument("--out", type=Path, default=Path("dawn.csv"))
    ap.add_argument("--cache", type=Path, default=Path("ims-cache"))
    a = ap.parse_args()
    if a.list:
        list_stations(a.token)
        return 0
    if not a.obs or not a.stations:
        ap.error("--obs and --stations are needed (or --list)")
    ids = [s.strip() for s in a.stations.split(",") if s.strip()]
    obs = list(read_obs(a.obs))
    with a.out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "observed"] + [f"{i}_{k}" for i in ids for k in ("t", "min")])
        for n, (d, t_obs) in enumerate(obs, 1):
            row = [d.isoformat(), t_obs.strftime("%H:%M:%S")]
            for sid in ids:
                # the API's days are local (Israel) days; dawn falls well inside one
                rec = temps(get(f"/stations/{sid}/data/daily/{d:%Y/%m/%d}", a.token, a.cache))
                near = min(rec, key=lambda x: abs((x[0] - t_obs).total_seconds()), default=None)
                before = [v for t, v in rec if timedelta(0) <= t_obs - t <= timedelta(hours=2)]
                ok = near and abs((near[0] - t_obs).total_seconds()) <= 1800
                row += [f"{near[1]:.1f}" if ok else "", f"{min(before):.1f}" if before else ""]
            w.writerow(row)
            if n % 20 == 0:
                print(f"{n}/{len(obs)}", file=sys.stderr)
    print(f"{len(obs)} mornings written to {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
