#!/usr/bin/env python3
"""Download the latest GFS 0.25 deg cycle for the configured regions and store it as numpy arrays.

Uses NOAA's NOMADS "grib filter", which cuts out only the requested variables, levels and region,
so each forecast hour is a small download. No API key or registration is needed.

Run it from a systemd timer / cron every hour or two; it does nothing when the newest available cycle
is already stored. A cycle becomes current only after every forecast hour of every region is stored.

    python3 fetch_gfs.py [--config config.json] [--cycle YYYYMMDDHH] [--source-dir DIR]

--source-dir reads <file name> GRIB files from a local directory instead of NOMADS (used by the tests).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from common import Cycle, field_names, forecast_hours, gfs_dir, load_config, write_json_atomic

log = logging.getLogger("fetch_gfs")

# GRIB2 identification: (discipline, parameterCategory, parameterNumber)
TMP, HGT, PRES, LAND = (0, 0, 0), (0, 3, 5), (0, 3, 0), (2, 0, 0)
SURFACE, ISOBARIC, ABOVE_GROUND = 1, 100, 103


def grib_file_name(cycle: Cycle, hour: int) -> str:
    return f"gfs.t{cycle.time:%H}z.pgrb2.0p25.f{hour:03d}"


def filter_url(cfg: dict, cycle: Cycle, hour: int, region: dict) -> str:
    gf = cfg["grib_filter"]
    base = gf["hourly"] if hour <= cfg["forecast_hours"]["hourly_until"] else gf["three_hourly"]
    q = {
        "dir": f"/gfs.{cycle.time:%Y%m%d}/{cycle.time:%H}/atmos",
        "file": grib_file_name(cycle, hour),
        "var_TMP": "on", "var_HGT": "on", "var_PRES": "on", "var_LAND": "on",
        "lev_surface": "on", "lev_2_m_above_ground": "on",
        "subregion": "",
        "leftlon": region["west"], "rightlon": region["east"],
        "toplat": region["north"], "bottomlat": region["south"],
    }
    for p in cfg["pressure_levels_mb"]:
        q[f"lev_{p}_mb"] = "on"
    return base + "?" + urllib.parse.urlencode(q)


def download(cfg: dict, url: str, dest: Path, attempts: int = 4) -> bool:
    """True if a GRIB file was saved; False if NOMADS says the file is not there (yet)."""
    ua = "zmanim-sky-server/0.1" + (f" ({cfg['contact']})" if cfg.get("contact") else "")
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if data[:4] == b"GRIB":
                dest.write_bytes(data)
                return True
            # NOMADS answers "data file is not present" with an HTML page
            log.debug("not a GRIB response: %r", data[:200])
            return False
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            log.warning("HTTP %s (attempt %d): %s", e.code, i + 1, url)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log.warning("%s (attempt %d)", e, i + 1)
        time.sleep(min(60, 5 * 2 ** i))
    raise RuntimeError(f"giving up on {url}")


def decode(path: Path, levels_mb: list[int]):
    """Decode one grib-filter file into (array[field, lat, lon] float32, lats ascending, lons)."""
    import eccodes

    names = field_names(levels_mb)
    index = {n: i for i, n in enumerate(names)}
    out = None
    lats = lons = None
    flip = False
    with open(path, "rb") as f:
        while True:
            h = eccodes.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                gi = lambda k: eccodes.codes_get(h, k, ktype=int)  # noqa: E731 (numeric codes, not mnemonics)
                key = (gi("discipline"), gi("parameterCategory"), gi("parameterNumber"))
                surf = gi("typeOfFirstFixedSurface")
                level = gi("level")
                name = None
                if surf == ISOBARIC and level in levels_mb:
                    name = {TMP: f"t{level}", HGT: f"z{level}"}.get(key)
                elif surf == SURFACE:
                    name = {TMP: "tsfc", PRES: "psfc", HGT: "orog", LAND: "land"}.get(key)
                elif surf == ABOVE_GROUND and level == 2 and key == TMP:
                    name = "t2m"
                if name is None:
                    continue
                ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
                vals = eccodes.codes_get_values(h).reshape(nj, ni)
                if eccodes.codes_get(h, "bitmapPresent"):
                    miss = eccodes.codes_get(h, "missingValue")
                    vals = np.where(vals == miss, np.nan, vals)
                if out is None:
                    la = eccodes.codes_get_array(h, "latitudes").reshape(nj, ni)[:, 0]
                    lo = eccodes.codes_get_array(h, "longitudes").reshape(nj, ni)[0, :]
                    flip = la[0] > la[-1]
                    lats = la[::-1] if flip else la
                    lons = (lo + 180) % 360 - 180
                    out = np.full((len(names), nj, ni), np.nan, dtype=np.float32)
                out[index[name]] = vals[::-1] if flip else vals
            finally:
                eccodes.codes_release(h)
    if out is None:
        raise ValueError(f"{path}: no usable GRIB messages")
    missing = [n for n in names if np.isnan(out[index[n]]).all()]
    if missing:
        raise ValueError(f"{path}: missing fields {missing}")
    return out, lats.astype(np.float64), lons.astype(np.float64)


def latest_cycles(cfg: dict, now: datetime) -> list[Cycle]:
    """Candidate cycles, newest first (GFS output is complete ~5 h after the cycle time)."""
    out = []
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    for _ in range(8):
        if t.hour in cfg["cycles"]:
            out.append(Cycle(t))
        t -= timedelta(hours=6)
    return out


def stored_matches_config(cfg: dict, cdir: Path, hours: list[int], partial: bool = False) -> bool:
    """True if a stored (or partly stored) cycle was made with the current regions, levels and hours."""
    try:
        if not partial:
            info = json.loads((cdir / "cycle.json").read_text())
            if info["hours"] != hours or info["regions"] != [r["name"] for r in cfg["regions"]]:
                return False
        for r in cfg["regions"]:
            meta_path = cdir / r["name"] / "meta.json"
            if not meta_path.exists():
                if partial:
                    continue                      # not started yet
                return False
            meta = json.loads(meta_path.read_text())
            if meta["region"] != r or meta["levels_mb"] != cfg["pressure_levels_mb"]:
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def fetch_cycle(cfg: dict, cycle: Cycle, source_dir: Path | None = None) -> bool:
    hours = forecast_hours(cfg)
    root = gfs_dir(cfg)
    final = root / cycle.id
    if (final / "complete").exists():
        if stored_matches_config(cfg, final, hours):
            log.info("cycle %s already stored", cycle.id)
            return True
        log.info("cycle %s was stored with different regions/levels/hours; fetching again", cycle.id)
    work = root / (cycle.id + ".partial")
    if work.exists() and not stored_matches_config(cfg, work, hours, partial=True):
        shutil.rmtree(work)                       # leftover from an older configuration
    work.mkdir(parents=True, exist_ok=True)
    delay = float(cfg.get("request_delay_s", 2.0))

    def get(hour: int, region: dict, dest: Path) -> bool:
        if source_dir is not None:
            src = source_dir / region["name"] / grib_file_name(cycle, hour)
            if not src.exists():
                return False
            shutil.copyfile(src, dest)
            return True
        ok = download(cfg, filter_url(cfg, cycle, hour, region), dest)
        time.sleep(delay)
        return ok

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd) / "x.grib2"
        # cheap availability probe: the last hour of the first region
        if not get(hours[-1], cfg["regions"][0], tmp):
            log.info("cycle %s not complete on the server yet", cycle.id)
            shutil.rmtree(work, ignore_errors=True)
            return False
        for region in cfg["regions"]:
            rdir = work / region["name"]
            rdir.mkdir(exist_ok=True)
            for hour in hours:
                dest = rdir / f"f{hour:03d}.npy"
                if dest.exists():
                    continue
                if not get(hour, region, tmp):
                    raise RuntimeError(f"{cycle.id} f{hour:03d} {region['name']}: missing on server")
                arr, lats, lons = decode(tmp, cfg["pressure_levels_mb"])
                np.save(dest, arr)
                meta = rdir / "meta.json"
                if not meta.exists():
                    write_json_atomic(meta, {
                        "lats": lats.tolist(), "lons": lons.tolist(),
                        "fields": field_names(cfg["pressure_levels_mb"]),
                        "levels_mb": cfg["pressure_levels_mb"], "region": region,
                    })
                log.info("%s %s f%03d stored", cycle.id, region["name"], hour)
    (work / "complete").write_text("")
    write_json_atomic(work / "cycle.json", {"cycle": cycle.id, "hours": hours,
                                            "regions": [r["name"] for r in cfg["regions"]]})
    if final.exists():
        shutil.rmtree(final)
    work.rename(final)
    write_json_atomic(root / "current.json", {"cycle": cycle.id, "published": time.time()})
    prune(cfg)
    return True


def prune(cfg: dict) -> None:
    root = gfs_dir(cfg)
    done = sorted(p for p in root.iterdir() if p.is_dir() and (p / "complete").exists())
    for p in done[:-int(cfg.get("cycles_to_keep", 2))]:
        log.info("removing old cycle %s", p.name)
        shutil.rmtree(p, ignore_errors=True)
    for p in root.glob("*.partial"):
        if p.stat().st_mtime < time.time() - 2 * 86400:
            shutil.rmtree(p, ignore_errors=True)


def after_fetch(cfg: dict, cycle: Cycle, prior_source_dir: Path | None = None) -> None:
    """Climatology upkeep; failures here never fail the forecast fetch."""
    ccfg = cfg.get("climatology", {})
    try:
        import climatology
        climatology.archive_cycle(cfg, cycle, gfs_dir(cfg) / cycle.id, log)
    except Exception:
        log.exception("adding %s to the climatology failed", cycle.id)
    if ccfg.get("ncep_prior", True):
        try:
            import build_prior
            missing = build_prior.regions_missing(cfg)
            if missing:
                log.info("building the NOAA reanalysis climatology for %s", [r["name"] for r in missing])
                build_prior.build(cfg, missing, prior_source_dir)
        except Exception:
            log.exception("building the NOAA reanalysis climatology failed (will retry next run)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--cycle", help="YYYYMMDDHH (default: newest available)")
    ap.add_argument("--source-dir", type=Path, help="read GRIB files from here instead of NOMADS")
    ap.add_argument("--prior-source-dir", type=Path, help="read the NOAA reanalysis files from here (tests)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    gfs_dir(cfg).mkdir(parents=True, exist_ok=True)
    cycles = [Cycle.parse(args.cycle)] if args.cycle else latest_cycles(cfg, datetime.now(timezone.utc))
    for cycle in cycles:
        try:
            if fetch_cycle(cfg, cycle, args.source_dir):
                after_fetch(cfg, cycle, args.prior_source_dir)
                return 0
        except Exception:
            log.exception("cycle %s failed", cycle.id)
            return 1
    log.warning("no complete cycle available")
    return 2


if __name__ == "__main__":
    sys.exit(main())
