#!/usr/bin/env python3
"""Fill your own GFS climatology with past GFS runs, so it covers every month from the start and
averages several years instead of the few days the server has seen so far.

Past runs come from NOAA's GFS archive on AWS Open Data (s3://noaa-gfs-bdp-pds, from 2021; no key or
account). Unlike NOMADS that archive cannot cut out a region, so each forecast hour is downloaded for
the whole globe - but only the needed fields (the .idx files give their byte ranges): about 20-25 MB
per forecast hour. To keep that manageable the backfill samples:

* one day out of every `--every` days (default 4);
* on each sampled day, forecast hours 1, 4, 7 and 10 of the 00Z and 12Z runs - one hour in each
  3-hour slot of the day. Each counts as a whole day in its slot (the fetcher adds three hours per slot
  per day), so backfilled and live days weigh the same.

2021 to today at the default spacing is about 520 days x 8 hours, roughly 80-100 GB downloaded over
the run (nothing large is kept: each file is decoded, cropped to your regions and deleted). It can be
stopped and restarted at any time, or run again later to catch up: the sampled days are a fixed
calendar (every `--every`-th day from 2021-01-01), runs already added are skipped (the same list the
fetcher keeps), so nothing is downloaded or counted twice, and it can run alongside the hourly fetcher.
A damaged archive file is logged and skipped.

    python3 backfill_gfs.py [--config config.json] [--start 2021-01-01] [--end YYYY-MM-DD]
                            [--every 4] [--parallel 4] [--dry-run]

Run fetch_gfs.py at least once first: the backfill crops to the same grid as the live data.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import climatology
from common import Cycle, field_names, gfs_dir, load_config
from fetch_gfs import decode

log = logging.getLogger("backfill_gfs")

DEFAULT_URL = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
FIRST_DAY = date(2021, 1, 1)                 # the archive's first day
RUNS = (0, 12)
HOURS = (1, 4, 7, 10)                        # one forecast hour in each 3-hour slot of a 12-hour run


class NotThere(Exception):
    pass


class BadFile(Exception):
    """The archive's file (or its .idx) is damaged: skip this forecast hour."""


def ua(cfg: dict) -> str:
    return "zmanim-sky-server/0.1" + (f" ({cfg['contact']})" if cfg.get("contact") else "")


def get(cfg: dict, url: str, byte_range: tuple[int, int | None] | None = None, attempts: int = 4) -> bytes:
    headers = {"User-Agent": ua(cfg)}
    if byte_range:
        a, b = byte_range
        headers["Range"] = f"bytes={a}-{'' if b is None else b}"
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):             # S3 answers 403 for a missing key without list rights
                raise NotThere(url) from None
            log.warning("HTTP %s (attempt %d): %s", e.code, i + 1, url)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            log.warning("%s (attempt %d): %s", e, i + 1, url)   # incl. a transfer cut short
        time.sleep(min(60, 5 * 2 ** i))
    raise RuntimeError(f"giving up on {url}")


def wanted(levels_mb: list[int]) -> set[tuple[str, str]]:
    w = {("TMP", "2 m above ground"), ("TMP", "surface"), ("PRES", "surface"), ("HGT", "surface"),
         ("LAND", "surface")}
    for p in levels_mb:
        w |= {("TMP", f"{p} mb"), ("HGT", f"{p} mb")}
    return w


def ranges_from_idx(idx: str, want: set[tuple[str, str]]) -> list[tuple[int, int | None]]:
    """Byte ranges (inclusive; None = to the end) of the wanted messages, adjacent ones merged."""
    rows = []
    for line in idx.splitlines():
        parts = line.split(":")
        if len(parts) >= 6 and parts[1].isdigit():
            rows.append((int(parts[1]), parts[3], parts[4]))
    out: list[list] = []
    seen = set()
    for k, (off, var, lev) in enumerate(rows):
        if (var, lev) not in want or (var, lev) in seen:
            continue
        seen.add((var, lev))
        end = rows[k + 1][0] - 1 if k + 1 < len(rows) else None
        if out and out[-1][1] is not None and out[-1][1] + 1 == off:
            out[-1][1] = end
        else:
            out.append([off, end])
    missing = want - seen
    if missing:
        raise ValueError(f"fields not in the file: {sorted(missing)}")
    return [tuple(r) for r in out]


def file_urls(base: str, cycle: Cycle, hour: int) -> list[str]:
    name = f"gfs.t{cycle.time:%H}z.pgrb2.0p25.f{hour:03d}"
    day = f"gfs.{cycle.time:%Y%m%d}/{cycle.time:%H}"
    return [f"{base}/{day}/atmos/{name}", f"{base}/{day}/{name}"]   # before GFS v16 (Mar 2021): no atmos/


def fetch_hour(cfg: dict, base: str, cycle: Cycle, hour: int, want, dest: Path) -> int:
    """Download the wanted messages of one forecast hour into dest. Returns bytes, or 0 if absent."""
    for url in file_urls(base, cycle, hour):
        try:
            idx = get(cfg, url + ".idx").decode("ascii", "replace")
        except NotThere:
            continue
        try:
            ranges = ranges_from_idx(idx, want)
        except ValueError as e:
            raise BadFile(f"{url}: {e}") from None
        parts = [get(cfg, url, r) for r in ranges]
        for (a, _), part in zip(ranges, parts):     # every range must start a GRIB message
            if part[:4] != b"GRIB" or part[-4:] != b"7777":
                raise BadFile(f"{url}: bytes {a}- are not whole GRIB messages (.idx out of step with the file)")
        data = b"".join(parts)
        dest.write_bytes(data)
        return len(data)
    return 0


def region_grids(cfg: dict) -> dict[str, dict]:
    """The grid (lats, lons, fields, levels) of each region's live data, from its climatology or the
    newest stored cycle."""
    out = {}
    fields = field_names(cfg["pressure_levels_mb"])
    cycles = sorted((p for p in gfs_dir(cfg).glob("[0-9]" * 10) if (p / "complete").exists()), reverse=True) \
        if gfs_dir(cfg).exists() else []
    for r in cfg["regions"]:
        cands = [climatology.clim_dir(cfg, "gfs", r["name"]) / "meta.json"] + [c / r["name"] / "meta.json" for c in cycles]
        for m in cands:
            if not m.exists():
                continue
            meta = json.loads(m.read_text())
            if meta.get("region") == r and meta.get("levels_mb") == cfg["pressure_levels_mb"] \
                    and meta.get("fields") == fields:
                out[r["name"]] = {k: meta[k] for k in ("lats", "lons", "fields", "levels_mb")}
                break
        else:
            raise SystemExit(f"no stored GFS data for region {r['name']} with the current configuration: "
                             "run fetch_gfs.py once first")
    return out


def crop_index(glats: np.ndarray, glons: np.ndarray, lats: list[float], lons: list[float]):
    """Indices into the global grid of the region's lats / lons (exact grid points)."""
    gl = np.round(glats * 4).astype(int)
    go = {int(v): k for k, v in enumerate(np.round(glons * 4))}
    rows, cols = [], []
    for la in lats:
        k = int(np.searchsorted(gl, round(la * 4)))
        if k >= len(gl) or gl[k] != round(la * 4):
            raise ValueError(f"latitude {la} is not on the archive's grid")
        rows.append(k)
    for lo in lons:
        k = go.get(round(((lo + 180) % 360 - 180) * 4))
        if k is None:
            raise ValueError(f"longitude {lo} is not on the archive's grid")
        cols.append(k)
    return np.array(rows), np.array(cols)


def sample_days(start: date, end: date, every: int, phase: int) -> list[date]:
    """The days from end back to start (newest first) whose distance from the archive's first day is
    `phase` modulo `every`: the same calendar whenever the backfill runs, so a later run only adds days
    that are new (or were missed), never a shifted second set."""
    out = []
    d = end - timedelta(days=((end - FIRST_DAY).days - phase) % every)
    while d >= start:
        out.append(d)
        d -= timedelta(days=every)
    return out


def detect_phase(targets: dict[str, Path], every: int) -> int:
    """The phase most runs already in the climatology have (a backfill started before the calendar was
    fixed counted back from its own end day); 0 for a fresh climatology. The fetcher's own runs cover
    every day alike, so they don't sway it."""
    votes = [0] * every
    for p in targets.values():
        for cid in climatology.archived_cycles(p):
            try:
                d = datetime.strptime(cid[:8], "%Y%m%d").date()
            except ValueError:
                continue
            votes[(d - FIRST_DAY).days % every] += 1
    return max(range(every), key=lambda k: (votes[k], -k))


def backfill(cfg: dict, start: date, end: date, every: int, parallel: int, base: str, dry_run: bool,
             phase: int | None = None) -> int:
    grids = region_grids(cfg)
    want = wanted(cfg["pressure_levels_mb"])
    targets = {r["name"]: climatology.clim_dir(cfg, "gfs", r["name"]) for r in cfg["regions"]}
    if phase is None:
        phase = detect_phase(targets, every)
    days = sample_days(start, end, every, phase % every)
    cycles = [Cycle(datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc)) for d in days for h in RUNS]
    done = set.intersection(*(climatology.archived_cycles(p) for p in targets.values())) if targets else set()
    todo = [c for c in cycles if c.id not in done]
    log.info("%s .. %s, every %d days (phase %d): %d runs, %d already in the climatology, %d to add "
             "(~%.0f GB to download)", start, end, every, phase % every, len(cycles), len(cycles) - len(todo), len(todo),
             len(todo) * len(HOURS) * 22 / 1024)
    if dry_run or not todo:
        return 0
    t0, nbytes, added_runs = time.time(), 0, 0
    with tempfile.TemporaryDirectory() as tmpd, ThreadPoolExecutor(parallel) as pool:
        for n, cycle in enumerate(todo, 1):
            paths = [Path(tmpd) / f"f{h:03d}.grib2" for h in HOURS]
            def one(hp):
                try:
                    return fetch_hour(cfg, base, cycle, hp[0], want, hp[1])
                except BadFile as e:
                    log.warning("%s f%03d skipped: %s", cycle.id, hp[0], e)
                    return -1
            sizes = list(pool.map(one, zip(HOURS, paths)))
            if not any(z > 0 for z in sizes):
                log.warning("%s: not in the archive, skipped", cycle.id)
                continue
            nbytes += sum(z for z in sizes if z > 0)
            per_region = {name: [] for name in targets}
            for h, path, size in zip(HOURS, paths, sizes):
                if size <= 0:
                    if size == 0:
                        log.warning("%s f%03d: not in the archive", cycle.id, h)
                    continue
                try:
                    arr, glats, glons = decode(path, cfg["pressure_levels_mb"])
                except Exception as e:
                    log.warning("%s f%03d: %s", cycle.id, h, e)
                    continue
                finally:
                    path.unlink(missing_ok=True)
                for r in cfg["regions"]:
                    g = grids[r["name"]]
                    rows, cols = crop_index(glats, glons, g["lats"], g["lons"])
                    per_region[r["name"]].append((cycle.time + timedelta(hours=h), arr[:, rows][:, :, cols]))
            for r in cfg["regions"]:
                with climatology.locked(targets[r["name"]]):
                    out = climatology.open_gfs_clim(cfg, r, grids[r["name"]], log)
                    if cycle.id in climatology.archived_cycles(out):
                        continue                  # the fetcher got there first
                    # each sampled hour stands for that day's 3 hours in its slot, as the fetcher adds
                    k = climatology.add_samples(out, cycle.id, per_region[r["name"]], climatology.GFS_SLOT_HOURS)
                log.debug("%s %s: %d hours", cycle.id, r["name"], k)
            added_runs += 1
            el = time.time() - t0
            log.info("%s added (%d/%d); %.1f GB so far, %.1f MB/s, about %.1f h left", cycle.id, n, len(todo),
                     nbytes / 2**30, nbytes / 2**20 / max(el, 1e-9), el / n * (len(todo) - n) / 3600)
    log.info("done: %d runs added", added_runs)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--start", type=date.fromisoformat, default=FIRST_DAY, help="first day (default 2021-01-01)")
    ap.add_argument("--end", type=date.fromisoformat, help="last day (default: 8 days ago)")
    ap.add_argument("--every", type=int, default=4, help="sample one day in this many (default 4)")
    ap.add_argument("--parallel", type=int, default=4, help="downloads at once (default 4)")
    ap.add_argument("--url", help=f"archive base URL (default {DEFAULT_URL})")
    ap.add_argument("--phase", type=int, help="which day of each --every to take, counted from 2021-01-01 "
                    "(default: the one most runs already added have, else 0)")
    ap.add_argument("--dry-run", action="store_true", help="only report what would be downloaded")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    end = a.end or (datetime.now(timezone.utc).date() - timedelta(days=8))
    base = (a.url or cfg.get("climatology", {}).get("backfill_url") or DEFAULT_URL).rstrip("/")
    if a.every < 1 or a.start > end:
        ap.error("need --every >= 1 and --start <= --end")
    return backfill(cfg, a.start, end, a.every, max(1, a.parallel), base, a.dry_run, a.phase)


if __name__ == "__main__":
    sys.exit(main())
