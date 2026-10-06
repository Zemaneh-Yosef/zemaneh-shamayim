#!/usr/bin/env python3
"""Work out a region box from the places you serve, and optionally save it into config.json.

    python3 suggest_region.py Israel                                # look a place up by name
    python3 suggest_region.py "New York City" --save                # ...and save it into config.json
    python3 suggest_region.py NAME LAT,LON [LAT,LON ...]            # just print the box
    python3 suggest_region.py NAME LAT,LON [LAT,LON ...] --save     # also write it into config.json
    python3 suggest_region.py NAME --place "New York City" --save   # look the place up by name
    python3 suggest_region.py --list                                # show the regions in config.json
    python3 suggest_region.py --remove NAME                         # delete a region from config.json

    python3 suggest_region.py israel 31.78,35.22 32.08,34.78 29.56,34.95 32.97,35.50 --save

--place looks a name up and uses its outline box; repeat it for several places in one region. Check
the printed match - "Georgia" may not be the one you meant. Sources, tried in this order (or pick one
with --source):
  nominatim  OpenStreetMap's Nominatim - any place, no key
  geonames   GeoNames searchJSON - any place; needs "geonames_username" in config.json (free account
             with web services enabled)
  countries  country boxes from github.com/sandstrom/country-bounding-boxes - countries only, by name
             or ISO code; downloaded once and cached next to config.json

--save adds the region, or replaces the one with the same name. The config file is the one in
$ZMANIM_SKY_CONFIG, else config.json next to this script (override with --config).

Margins follow the actual sunrise / sunset directions over the year at each place, so the box covers
every path the API will sample (400 km by default, the farthest profile distance).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import HERE, destination


def config_path(arg: str | None) -> Path:
    return Path(arg or (os.environ.get("ZMANIM_SKY_CONFIG") or os.environ.get("REFRACTION_CONFIG")) or HERE / "config.json")


def read_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} does not exist; copy config.example.json to it first")
    return json.loads(path.read_text())


def write_config(path: Path, cfg: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, path)
    except PermissionError:
        raise SystemExit(f"no permission to write {path}. Run it as the user that owns the server, e.g.\n"
                         f"  sudo -u zmanim-sky python3 suggest_region.py ...")


def cache_dir_for(cpath: Path) -> Path:
    """Somewhere writable for the country table: next to config.json if possible, else ~/.cache."""
    for d in (cpath.parent, Path.home() / ".cache" / "zmanim-sky-server"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("")
            probe.unlink()
            return d
        except OSError:
            continue
    return Path(tempfile.gettempdir())


NOMINATIM = "https://nominatim.openstreetmap.org/search"
GEONAMES = "https://secure.geonames.org/searchJSON"
COUNTRY_BOXES = "https://raw.githubusercontent.com/sandstrom/country-bounding-boxes/master/bounding-boxes.json"


class NotFound(Exception):
    pass


def _contact_ok(contact: str) -> bool:
    return bool(contact) and "@" in contact and not contact.endswith("@example.com")


def _get_json(url: str, contact: str = ""):
    ua = "zmanim-sky-server/0.1 suggest_region" + (f" ({contact})" if contact else "")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = " ".join(e.read(400).decode("utf-8", "replace").split())
        except Exception:
            pass
        raise ValueError(f"HTTP {e.code}" + (f" - {body[:200]}" if body else "")) from None


def from_nominatim(query: str, cfg: dict, cache_dir: Path):
    contact = cfg.get("contact", "")
    try:
        results = _get_json(NOMINATIM + "?" + urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1}),
                            contact)
    except ValueError as e:
        if "HTTP 403" in str(e):
            hint = ("set a real \"contact\" e-mail in config.json (Nominatim refuses unidentified requests)"
                    if not _contact_ok(contact) else
                    "Nominatim blocks some server/hosting IP ranges; use --source countries or geonames")
            raise ValueError(f"{e} [{hint}]") from None
        raise
    time.sleep(1.1)                                   # Nominatim policy: at most 1 request per second
    if not results:
        raise NotFound
    m = results[0]
    south, north, west, east = (float(v) for v in m["boundingbox"])
    return (south, north, west, east), m.get("display_name") or m.get("name") or query


def from_geonames(query: str, cfg: dict, cache_dir: Path):
    user = cfg.get("geonames_username")
    if not user:
        raise NotFound("no geonames_username in config.json")
    data = _get_json(GEONAMES + "?" + urllib.parse.urlencode(
        {"q": query, "maxRows": 1, "username": user, "inclBbox": "true"}))
    if "status" in data:                              # GeoNames reports errors as {"status": {"message": ...}}
        raise NotFound(f"GeoNames: {data['status'].get('message', data['status'])}")
    hits = data.get("geonames") or []
    if not hits or "bbox" not in hits[0]:
        raise NotFound
    g, b = hits[0], hits[0]["bbox"]
    label = ", ".join(x for x in (g.get("name"), g.get("adminName1"), g.get("countryName")) if x)
    return (float(b["south"]), float(b["north"]), float(b["west"]), float(b["east"])), label or query


def from_countries(query: str, cfg: dict, cache_dir: Path):
    cache = cache_dir / "country-bboxes.json"
    if not cache.exists():
        cache.write_text(json.dumps(_get_json(COUNTRY_BOXES)))
    table = json.loads(cache.read_text())             # {"IL": ["Israel", [west, south, east, north]], ...}
    q = query.strip().lower()
    for code, (name, (west, south, east, north)) in table.items():
        if q in (code.lower(), name.lower()):
            return (float(south), float(north), float(west), float(east)), f"{name} ({code}, country table)"
    raise NotFound


SOURCES = {"nominatim": from_nominatim, "geonames": from_geonames, "countries": from_countries}


def lookup_place(query: str, cfg: dict, cache_dir: Path, source: str = "auto"):
    """Seed points (corners + centre of the place's bounding box) and the matched name."""
    order = list(SOURCES) if source == "auto" else [source]
    notes = []
    for name in order:
        try:
            (south, north, west, east), label = SOURCES[name](query, cfg, cache_dir)
        except NotFound as e:
            notes.append(f"{name}: {str(e) or 'not found'}")
            continue
        except (OSError, ValueError, KeyError, TypeError) as e:   # network errors, unexpected replies
            notes.append(f"{name}: {e}")
            continue
        if east - west > 180 or east < west:
            raise SystemExit(f"{label!r} spans the 180th meridian; use smaller places")
        clat, clon = (south + north) / 2, (west + east) / 2
        pts = [(south, west), (south, east), (north, west), (north, east), (clat, clon)]
        return pts, f"{label}  [{name}]"
    raise SystemExit(f"no place found for {query!r}:\n  " + "\n  ".join(notes))


def path_extent(lat: float, lon: float, reach_km: float):
    """All points the sunrise/sunset paths can reach from (lat, lon) over a year."""
    pts = [(lat, lon)]
    max_dec = 23.44
    for dec in [-max_dec + i * (2 * max_dec / 12) for i in range(13)]:
        c = math.sin(math.radians(dec)) / math.cos(math.radians(lat))   # cos(azimuth) at rise/set
        if not -1 <= c <= 1:
            continue                                                   # polar day/night at this declination
        az = math.degrees(math.acos(c))
        for a in (az, 360 - az):                                       # sunrise, sunset
            for d in (reach_km / 4, reach_km / 2, reach_km):
                pts.append(destination(lat, lon, a, d))
    return pts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?")
    ap.add_argument("places", nargs="*", help="LAT,LON pairs (no spaces inside a pair)")
    ap.add_argument("--place", action="append", default=[], metavar="NAME",
                    help="look a place up by name (OpenStreetMap); may be repeated")
    ap.add_argument("--source", choices=["auto", *SOURCES], default="auto",
                    help="where --place looks names up (default: try each in turn)")
    ap.add_argument("--reach-km", type=float, default=400)
    ap.add_argument("--save", action="store_true", help="write the region into the config file")
    ap.add_argument("--list", action="store_true", help="show the regions in the config file")
    ap.add_argument("--remove", metavar="NAME", help="delete a region from the config file")
    ap.add_argument("--config", help="config file (default: $ZMANIM_SKY_CONFIG or ./config.json)")
    a = ap.parse_args()
    cpath = config_path(a.config)
    if a.list or a.remove:
        cfg = read_config(cpath)
        regions = cfg.get("regions", [])
        if a.remove:
            kept = [r for r in regions if r["name"] != a.remove]
            if len(kept) == len(regions):
                raise SystemExit(f"no region named {a.remove!r} in {cpath}")
            cfg["regions"] = kept
            write_config(cpath, cfg)
            print(f"removed {a.remove!r} from {cpath}")
        for r in cfg["regions"]:
            print(json.dumps(r))
        return
    if not a.name:
        ap.error("give a place name, e.g.  suggest_region.py Israel")
    if not a.places and not a.place:
        # just a name: look it up, and use it (lower-case, dashes) as the region label
        a.place = [a.name]
        a.name = "-".join(a.name.lower().replace(",", " ").split())
    seeds = []
    for s in a.places:
        try:
            lat, lon = (float(v) for v in s.split(","))
        except ValueError:
            a.place.append(s)                         # not LAT,LON: treat it as a place name
            continue
        seeds.append((lat, lon))
    if a.place:
        cfg = read_config(cpath) if cpath.exists() else {}
        for q in a.place:
            pts_q, label = lookup_place(q, cfg, cache_dir_for(cpath), a.source)
            print(f"# {q!r} -> {label}")
            seeds += pts_q
    pts = []
    for lat, lon in seeds:
        pts += path_extent(lat, lon, a.reach_km)
    north = math.ceil(max(p[0] for p in pts) * 4) / 4 + 0.25           # snap outward to the 0.25 deg grid
    south = math.floor(min(p[0] for p in pts) * 4) / 4 - 0.25
    west = math.floor(min(p[1] for p in pts) * 4) / 4 - 0.25
    east = math.ceil(max(p[1] for p in pts) * 4) / 4 + 0.25
    if east - west > 180:
        raise SystemExit("places span the 180th meridian or half the globe; split them into separate regions")
    region = {"name": a.name, "north": north, "south": south, "west": west, "east": east}
    npts = (round((north - south) / 0.25) + 1) * (round((east - west) / 0.25) + 1)
    per_hour_mb = npts * 31 * 4 / 1e6
    print(json.dumps(region))
    if len(a.places) + len(a.place) > 1 and (east - west > 40 or north - south > 30):
        print("# warning: this box is very large - if these places are far apart, save each as its own region")
    print(f"# {npts:,} grid points; ~{per_hour_mb:.1f} MB per forecast hour on disk, "
          f"~{per_hour_mb * 137 / 1000:.2f} GB per stored cycle (default forecast hours)")
    if a.save:
        cfg = read_config(cpath)
        regions = [r for r in cfg.get("regions", []) if r["name"] != a.name]
        replaced = len(regions) != len(cfg.get("regions", []))
        cfg["regions"] = regions + [region]
        write_config(cpath, cfg)
        print(f"{'replaced' if replaced else 'added'} region {a.name!r} in {cpath}; "
              "the fetcher downloads it on its next run")


if __name__ == "__main__":
    main()
