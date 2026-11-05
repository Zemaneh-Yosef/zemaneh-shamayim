#!/usr/bin/env python3
"""Caching in front of the API (Cloudflare or any CDN) and in browsers: the headers, and purging.

Every cacheable answer gets two lifetimes:

    Cache-Control:      public, max-age=<min(lifetime, cdn.browser_max_age_s)>, stale-if-error=<cdn.stale_if_error_s>
    CDN-Cache-Control:  public, max-age=<lifetime>, stale-if-error=<cdn.stale_if_error_s>

so a long-lived answer (a horizon: 30 days) stays long at the CDN, which can be purged, and only a day in browsers,
which cannot. stale-if-error lets the CDN keep serving the last good answer while the server is down. Each answer
also carries a Cache-Tag (Cloudflare strips it before the browser) naming what it was made from, so a change
purges just those answers:

    terrain   horizons, terrain versions, terrain tiles, Cesium terrain
    areas     /v1/area, and the area=auto answers (horizons, terrain versions, light pollution)
    weather   /v1/path-profiles
    lightpol  /v1/light-pollution
    haze      /v1/haze-calibration

The server purges by itself (watch(), from serve.py): it keeps what the terrain and areas were made from in
<data_dir>/cdn-state.json, checks at start and every cdn.check_every_s, and when the terrain layers, the fine
terrain, the horizon algorithm, a merged tile's revision (dem_layers.py build --force) or areas.json changed,
purges those tags at Cloudflare. By hand:

    python3 cdn.py purge terrain [areas ...]      # the answers made from these
    python3 cdn.py purge all                       # every answer of this API (its "zsky" tag)
    python3 cdn.py purge --everything              # the whole Cloudflare zone (website included)
    python3 cdn.py status                          # what the state file holds and whether purging is set up

Purging needs cdn.cloudflare_zone_id and an API token with the Zone / Cache Purge permission, in
cdn.cloudflare_api_token or the CLOUDFLARE_API_TOKEN environment variable. Without them a change is logged
(purge it in the dashboard) and nothing else happens.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("cdn")

TAGS = ("terrain", "areas", "weather", "lightpol", "haze")
API = "https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache"


def _c(cfg: dict) -> dict:
    return cfg.get("cdn") or {}


def tag(cfg: dict, *names: str) -> str:
    """The Cache-Tag value: the API's own tag (for "purge all") plus these, each with cdn.tag_prefix."""
    p = _c(cfg).get("tag_prefix") or "zsky"
    return ",".join([p] + [f"{p}-{n}" for n in names])


def cache_headers(cfg: dict, cache_s: int, tags: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """The caching headers for an answer good for cache_s seconds (0: not to be kept anywhere)."""
    if not cache_s:
        return [("Cache-Control", "no-store")]
    c = _c(cfg)
    browser = min(int(cache_s), int(c.get("browser_max_age_s", 86400)))
    sie = int(c.get("stale_if_error_s", 86400))
    tail = f", stale-if-error={sie}" if sie > 0 else ""
    return [("Cache-Control", f"public, max-age={browser}{tail}"),
            ("CDN-Cache-Control", f"public, max-age={int(cache_s)}{tail}"),
            ("Cache-Tag", tag(cfg, *tags))]


# ------------------------------------------------------------------------------------------------
# Purging
# ------------------------------------------------------------------------------------------------
def credentials(cfg: dict) -> tuple[str, str] | None:
    c = _c(cfg)
    zone = c.get("cloudflare_zone_id") or ""
    token = os.environ.get("CLOUDFLARE_API_TOKEN") or c.get("cloudflare_api_token") or ""
    return (zone, token) if zone and token else None


def purge(cfg: dict, names: list[str] | None = None, everything: bool = False, opener=urllib.request.urlopen) -> bool:
    """Purge these tags (None: every answer of this API) at Cloudflare. True when done; False (logged) when
    purging is not set up or Cloudflare refused."""
    cred = credentials(cfg)
    if cred is None:
        log.warning("CDN purge needed (%s) but cdn.cloudflare_zone_id / CLOUDFLARE_API_TOKEN are not set: "
                    "purge it in the Cloudflare dashboard", "everything" if everything else ", ".join(names or ["all"]))
        return False
    zone, token = cred
    if everything:
        payload = {"purge_everything": True}
    else:
        p = _c(cfg).get("tag_prefix") or "zsky"
        payload = {"tags": [f"{p}-{n}" for n in names] if names else [p]}
    req = urllib.request.Request(API.format(zone=zone), data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with opener(req, timeout=30) as r:
            res = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read() or b"{}")
        except ValueError:
            res = {"errors": [{"message": f"HTTP {e.code}"}]}
    except (OSError, ValueError) as e:
        log.warning("CDN purge %s failed: %s", payload, e)
        return False
    if not res.get("success"):
        log.warning("CDN purge %s refused: %s", payload, res.get("errors"))
        return False
    log.info("CDN purged %s", payload)
    return True


# ------------------------------------------------------------------------------------------------
# What the answers were made from, and watching it
# ------------------------------------------------------------------------------------------------
def terrain_state(horizons) -> str:
    """One hash of everything a horizon or terrain tile is made from that can change under the same URL."""
    import dem_layers
    import terrain
    revs = {}
    if horizons.layers_out is not None and Path(horizons.layers_out).is_dir():
        revs = {f.stem: f.read_text().strip() for f in sorted(Path(horizons.layers_out).glob("*.rev"))}
    blob = {"layers": horizons.dem, "fine": horizons.fine and horizons.fine["sig"], "algo": terrain.ALGO_VERSION,
            "fine_v": dem_layers.FINE_VERSION, "revs": revs}
    return hashlib.sha1(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:12]


def areas_state(index) -> str | None:
    index.refresh()                            # reloads areas.json when it changed on disk
    return index.meta.get("built") or (index.stamp and f"{index.stamp[0]}-{index.stamp[1]}") or None


def state_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "cdn-state.json"


def check(cfg: dict, now: dict, purger=purge) -> list[str]:
    """Compare now ({"terrain": .., "areas": ..}) with the stored state; purge what changed. Returns the changed
    tags. The state is saved only once the purge is done (or purging is not set up: nothing would ever do it), so a
    refused purge is tried again at the next check."""
    path = state_path(cfg)
    try:
        before = json.loads(path.read_text())
    except (OSError, ValueError):
        before = None
    if before is None:                         # first run: nothing to compare with, so nothing cached is known stale
        changed = []
    else:
        changed = [k for k in ("terrain", "areas") if before.get(k) != now.get(k)]
    if changed:
        log.info("changed since the last check: %s", ", ".join(changed))
        if not purger(cfg, changed) and credentials(cfg) is not None:
            return changed                     # refused: keep the old state, retry next time
    if before != now:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(now))
        tmp.replace(path)
    return changed


def watch(cfg: dict, horizons, area_index_fn) -> threading.Thread:
    """Check now and every cdn.check_every_s in a background thread (serve.py starts it once listening)."""
    every = float(_c(cfg).get("check_every_s", 600))

    def loop():
        while True:
            try:
                check(cfg, {"terrain": terrain_state(horizons), "areas": areas_state(area_index_fn())})
            except Exception:
                log.exception("CDN state check failed")
            if every <= 0:
                return
            time.sleep(every)

    t = threading.Thread(target=loop, name="cdn-watch", daemon=True)
    t.start()
    return t


def main(argv=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import load_config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("purge", help="purge cached answers at Cloudflare")
    p.add_argument("what", nargs="*", help=f"tags: {', '.join(TAGS)}, or all")
    p.add_argument("--everything", action="store_true", help="the whole zone (the website on it too)")
    sub.add_parser("status", help="the stored state and whether purging is set up")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    if a.cmd == "status":
        try:
            print(state_path(cfg).read_text())
        except OSError:
            print(f"no {state_path(cfg)} yet (written by the API at start)")
        print("purging:", "set up" if credentials(cfg) else "not set up (cdn.cloudflare_zone_id, CLOUDFLARE_API_TOKEN)")
        return 0
    if not a.everything and not a.what:
        ap.error("purge what? e.g. `purge terrain`, `purge all`, `purge --everything`")
    bad = [w for w in a.what if w not in TAGS + ("all",)]
    if bad:
        ap.error(f"unknown: {', '.join(bad)} (known: {', '.join(TAGS)}, all)")
    names = None if "all" in a.what else a.what
    return 0 if purge(cfg, names, everything=a.everything) else 1


if __name__ == "__main__":
    raise SystemExit(main())
