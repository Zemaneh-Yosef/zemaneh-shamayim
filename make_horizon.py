#!/usr/bin/env python3
"""Build terrain horizons for visible sunrise / sunset from SRTM elevation tiles (command-line version
of the server's /v1/horizon; same output).

    python3 make_horizon.py LAT LON [--tiles DIR] [--height M | --eye 1.7] [--radius-km 0.8] [--out horizon.json]
    python3 make_horizon.py --bbox SOUTH,WEST,NORTH,EAST [--grid-m 100] [--out neighbourhood.json]
    add --moon for moonrise / moonset horizons too (vantage spots at --moon-percentile, default 90)

Tiles (SRTM .hgt / .hgt.gz, 1" or 3", named like N31E035.hgt.gz) are read from DIR and, unless
--no-download, fetched there as needed from https://s3.amazonaws.com/elevation-tiles-prod/skadi/
(free, no key). Where that tile set has no tile, the area is open sea (0 m).

Output (a HorizonSet; pass it straight to getVisibleSunrise / getVisibleSunset / getSunrises):
    {"mode": "point" | "vantage",
     "points": [{"lat", "lon", "ground", "height",
                 "sunrise": [{"azimuthDeg", "distanceKm", "heightM"}, ...], "sunset": [...],
                 "wins": {"sunrise": [[az1, az2], ...], "sunset": [...]}}, ...], "params": {...}}

--radius-km 0 (default): the exact coordinates. With a radius (or --bbox), a grid of candidate spots (--grid-m)
within it is searched and the few that see lowest in some direction are kept; the calculator then
gives the earliest sunrise / latest sunset among them, each from its own position and height.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import terrain


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float, nargs="?")
    ap.add_argument("lon", type=float, nargs="?")
    ap.add_argument("--bbox", help="vantage search inside SOUTH,WEST,NORTH,EAST (lat/lon default to its centre)")
    ap.add_argument("--tiles", type=Path, default=Path("tiles"), help="tile folder (default ./tiles)")
    ap.add_argument("--no-download", action="store_true", help="use only tiles already in --tiles")
    ap.add_argument("--eye", type=float, default=1.7, help="eye height above the ground, m (default 1.7)")
    ap.add_argument("--height", type=float,
                    help="observer height above sea level, m (point mode; default ground + eye)")
    ap.add_argument("--radius-km", type=float, default=0.0, help="vantage search radius (default 0: this point)")
    ap.add_argument("--grid-m", type=float, default=100.0, help="vantage grid spacing, m (default 100)")
    ap.add_argument("--min-km", type=float, default=1.0,
                    help="ignore terrain closer than this (the observer's own surroundings), default 1")
    ap.add_argument("--max-km", type=float, default=150.0)
    ap.add_argument("--moon", action="store_true",
                    help="also moonrise / moonset horizons (the Moon's wider range of directions)")
    ap.add_argument("--moon-percentile", type=float, default=90.0,
                    help="vantage mode: moon spots at this percentile of horizon height (default 90)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    bbox = [float(v) for v in a.bbox.split(",")] if a.bbox else None
    if bbox is None and (a.lat is None or a.lon is None):
        ap.error("give LAT LON, or --bbox")
    logging.basicConfig(level=logging.INFO, format="# %(message)s")
    tiles = terrain.Tiles(a.tiles, None if a.no_download else terrain.SKADI)
    try:
        res = terrain.horizon_set(tiles, a.lat, a.lon, radius_km=a.radius_km, bbox=bbox, grid_m=a.grid_m, eye=a.eye,
                                  height=a.height, min_km=a.min_km, max_km=a.max_km,
                                  moon=a.moon, moon_percentile=a.moon_percentile)
    except (FileNotFoundError, RuntimeError) as e:
        sys.exit(str(e))
    text = json.dumps(res, separators=(",", ":"))
    pts = res["points"]
    print(f"# {res['mode']}: {len(pts)} point(s); "
          + ", ".join(f"{p['lat']:.5f},{p['lon']:.5f} ground {p['ground']} m eye at {p['height']} m" for p in pts[:3])
          + (" ..." if len(pts) > 3 else ""), file=sys.stderr)
    if res["seaTiles"]:
        print(f"# open sea (no elevation tile): {', '.join(res['seaTiles'])}", file=sys.stderr)
    if a.out:
        a.out.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
