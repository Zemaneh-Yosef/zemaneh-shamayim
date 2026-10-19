#!/usr/bin/env python3
"""Download the GFS ensemble (GEFS, 0.5 deg) for the configured regions: how sure each forecast sunrise is.

GFS is one forecast. GEFS runs the same model ~30 more times from slightly different starts; where the
members agree on the morning's air the sunrise is well forecast, where they spread (an inversion that
may or may not form, a front that may come early) it isn't. /v1/path-profiles?...&ensemble=1 then adds,
to each forecast sunrise / sunset GEFS covers, the path profiles of every member, and the app traces
them all: their spread is the forecast's error bar.

GEFS's two files (pgrb2a + pgrb2b) together have exactly the fields GFS's 0.25 deg file gives here (13
pressure levels, 2 m / 80 m / 100 m air, surface), at 0.5 deg and every 3 hours, from NOMADS's grib filter
(region cut out, no key). The control run (c00) and the first `ensemble.members` perturbed members
(p01.., default 10 of 30) are fetched, by default from the 00Z run only, out to `ensemble.max_hours`:

    two requests per member and forecast hour: 11 members x 57 hours x 2 = ~1250 per region per run,
    ~45 min at request_delay_s 2 s; ~0.7 GB per stored run for the US box, ~15 MB for Israel.

Run it from the same timer as fetch_gfs.py (it does nothing when the newest run is stored). Storage:

    <data_dir>/gefs/<YYYYMMDDHH>/<region>/<member>/f<FFF>.npy (+ meta.json), <data_dir>/gefs/current.json

    python3 fetch_gefs.py [--config config.json] [--cycle YYYYMMDDHH] [--source-dir DIR]

--source-dir reads <region>/<member>/<file name> GRIB files (a and b) from a directory instead (tests).
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from common import Cycle, cycle_field_names, load_config, write_json_atomic
from fetch_gfs import decode, download, latest_cycles

log = logging.getLogger("fetch_gefs")


def gefs_dir(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "gefs"


def members(cfg: dict) -> list[str]:
    return ["c00"] + [f"p{k:02d}" for k in range(1, int(cfg["ensemble"]["members"]) + 1)]


def hours(cfg: dict) -> list[int]:
    e = cfg["ensemble"]
    return list(range(0, int(e["max_hours"]) + 1, int(e.get("step_hours", 3))))


def file_name(cycle: Cycle, member: str, hour: int, part: str) -> str:
    return f"ge{member}.t{cycle.time:%H}z.pgrb2{part}.0p50.f{hour:03d}"


def filter_url(cfg: dict, cycle: Cycle, member: str, hour: int, part: str, region: dict) -> str:
    q = {
        "dir": f"/gefs.{cycle.time:%Y%m%d}/{cycle.time:%H}/atmos/pgrb2{part}p5",
        "file": file_name(cycle, member, hour, part),
        "var_TMP": "on", "var_HGT": "on", "var_PRES": "on", "var_LAND": "on",
        "lev_surface": "on", "lev_2_m_above_ground": "on",
        "subregion": "", "leftlon": region["west"], "rightlon": region["east"],
        "toplat": region["north"], "bottomlat": region["south"],
    }
    for p in cfg["pressure_levels_mb"]:
        q[f"lev_{p}_mb"] = "on"
    for h in cfg.get("near_surface_m", []):
        q[f"lev_{h}_m_above_ground"] = "on"
    return cfg["ensemble"]["grib_filter"][part] + "?" + urllib.parse.urlencode(q)


def fetch_cycle(cfg: dict, cycle: Cycle, source_dir: Path | None = None) -> bool:
    root = gefs_dir(cfg)
    final = root / cycle.id
    if (final / "complete").exists():
        log.info("GEFS %s already stored", cycle.id)
        return True
    work = root / (cycle.id + ".partial")
    work.mkdir(parents=True, exist_ok=True)
    delay = float(cfg.get("request_delay_s", 2.0))
    names = cycle_field_names(cfg)
    hrs, mems = hours(cfg), members(cfg)

    def get(member: str, hour: int, part: str, region: dict, dest: Path) -> bool:
        if source_dir is not None:
            src = source_dir / region["name"] / member / file_name(cycle, member, hour, part)
            if not src.exists():
                return False
            shutil.copyfile(src, dest)
            return True
        ok = download(cfg, filter_url(cfg, cycle, member, hour, part, region), dest)
        time.sleep(delay)
        return ok

    with tempfile.TemporaryDirectory() as tmpd:
        a, b = Path(tmpd) / "a.grib2", Path(tmpd) / "b.grib2"
        if not get(mems[-1], hrs[-1], "a", cfg["regions"][0], a):          # cheap availability probe
            log.info("GEFS %s not complete on the server yet", cycle.id)
            shutil.rmtree(work, ignore_errors=True)
            return False
        for region in cfg["regions"]:
            for member in mems:
                mdir = work / region["name"] / member
                mdir.mkdir(parents=True, exist_ok=True)
                for hour in hrs:
                    dest = mdir / f"f{hour:03d}.npy"
                    if dest.exists():
                        continue
                    if not get(member, hour, "a", region, a):
                        raise RuntimeError(f"GEFS {cycle.id} {member} f{hour:03d} {region['name']}: missing on server")
                    if not get(member, hour, "b", region, b):
                        b.write_bytes(b"")
                    both = Path(tmpd) / "ab.grib2"
                    both.write_bytes(a.read_bytes() + b.read_bytes())
                    arr, lats, lons = decode(both, names)
                    np.save(dest, arr)
                    if not (work / region["name"] / "meta.json").exists():
                        write_json_atomic(work / region["name"] / "meta.json", {
                            "lats": lats.tolist(), "lons": lons.tolist(), "fields": names,
                            "levels_mb": cfg["pressure_levels_mb"], "region": region, "members": mems})
                log.info("GEFS %s %s %s stored", cycle.id, region["name"], member)
    (work / "complete").write_text("")
    write_json_atomic(work / "cycle.json", {"cycle": cycle.id, "hours": hrs, "members": mems,
                                            "regions": [r["name"] for r in cfg["regions"]]})
    if final.exists():
        shutil.rmtree(final)
    work.rename(final)
    write_json_atomic(root / "current.json", {"cycle": cycle.id, "published": time.time()})
    done = sorted(p for p in root.iterdir() if p.is_dir() and (p / "complete").exists())
    for p in done[:-int(cfg["ensemble"].get("cycles_to_keep", 1))]:
        shutil.rmtree(p, ignore_errors=True)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--cycle", help="YYYYMMDDHH (default: newest available)")
    ap.add_argument("--source-dir", type=Path)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    if not cfg["ensemble"].get("enabled"):
        log.info("ensemble.enabled is off in the config: nothing to do")
        return 0
    gefs_dir(cfg).mkdir(parents=True, exist_ok=True)
    ecfg = {**cfg, "cycles": cfg["ensemble"].get("cycles", [0])}
    cycles = [Cycle.parse(a.cycle)] if a.cycle else latest_cycles(ecfg, datetime.now(timezone.utc))
    for cycle in cycles:
        try:
            if fetch_cycle(cfg, cycle, a.source_dir):
                return 0
        except Exception:
            log.exception("GEFS %s failed", cycle.id)
            return 1
    log.warning("no complete GEFS run available")
    return 2


if __name__ == "__main__":
    sys.exit(main())
