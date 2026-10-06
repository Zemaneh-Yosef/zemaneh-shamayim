#!/usr/bin/env python3
"""Merged bare-earth elevation tiles from several sources, best source first, pixel by pixel.

Why: the default AWS "skadi" tiles are USGS 3DEP (bare earth) in the US but mainly SRTM elsewhere, and
SRTM is a surface model - buildings and forest count as terrain and raise the horizon. This builds 1x1
degree tiles on the server's own grid (1 arc-second, 3601 x 3601, WGS84, like .hgt) where every pixel
comes from the highest-priority source that has data there, e.g.

    USGS 3DEP (US)  ->  NRCan MRDEM DTM (Canada)  ->  an Israeli DTM, if you have one  ->  FABDEM  ->  skadi

A source can be any raster GDAL reads, in any projection: a local GeoTIFF, a folder glob of tiles, a
VRT, a /vsicurl/ URL, or a per-tile template like USGS's 1x1 degree files (see tile_fields()). Pixels a source doesn't cover (its nodata, e.g. outside a country) fall through
to the next. Tiles are written as float32 .npy into terrain.layers.dir/<signature>/, so changing the
source list starts a fresh set automatically.

Needs rasterio (pip install rasterio); the server itself only needs it if terrain.layers.auto_build is
on. Usage:

    venv/bin/python dem_layers.py build --around 40.609283058016736 -73.96828881865329     # tiles a 150 km horizon needs
    venv/bin/python dem_layers.py build --bbox 31 34 33 36.5            # south west north east
    venv/bin/python dem_layers.py info N40W074                          # which source filled what

Vertical datums differ slightly between sources (NAVD88, CGVD2013, EGM2008, EGM96); the differences
are well under a metre in these regions and are not corrected.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

from common import load_config
import terrain

log = logging.getLogger("dem_layers")

N = 3601                    # samples per side, as in 1 arc-second .hgt (edges shared with neighbours)
STEP = 1.0 / 3600.0
LAYERS_VERSION = 1          # bump if the tile format or the merge rule changes


class SourceError(RuntimeError):
    """A source that should have data couldn't be read (network, corrupt file, bad config). The tile is
    then NOT saved - a tile quietly built from the next-best source would be kept for good - and the
    server uses its plain tiles for it until a later build succeeds."""


def _is_missing(err: Exception) -> bool:
    """True when a per-tile file simply doesn't exist (the source doesn't cover this tile)."""
    msg = str(err)
    return any(k in msg for k in ("404", "No such file", "does not exist", "not exist in the file system"))


def enabled_sources(cfg_layers: dict | None) -> list[dict]:
    return [s for s in (cfg_layers or {}).get("sources", []) if s.get("enabled", True) and s.get("paths")]


def signature(cfg_layers: dict | None) -> str | None:
    """Short hash of the enabled sources (names + paths, in order); None when layering is off."""
    srcs = enabled_sources(cfg_layers)
    if not srcs:
        return None
    blob = json.dumps({"v": LAYERS_VERSION, "s": [[s["name"], s["paths"]] for s in srcs]}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def layers_dir(cfg: dict) -> Path | None:
    lay = cfg["terrain"].get("layers")
    sig = signature(lay)
    if not sig:
        return None
    base = Path(lay.get("dir") or Path(cfg["data_dir"]) / "dem-layers")
    return base / sig


class Builder:
    """Builds merged tiles. Thread-safe; datasets are opened lazily and kept open."""

    def __init__(self, cfg_layers: dict, out_dir: Path, base_tiles: "terrain.Tiles | None"):
        import rasterio                                    # noqa: F401  (fail early if missing)
        self.sources = enabled_sources(cfg_layers)
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.base = base_tiles
        self.lock = threading.Lock()
        self._datasets: dict[str, list] = {}
        self.env = {                                       # sensible defaults for remote VRTs / COGs
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.vrt,.TIF,.zip",
            "GDAL_HTTP_MAX_RETRY": "4", "GDAL_HTTP_RETRY_DELAY": "2",
            "VSI_CACHE": "TRUE", "GDAL_CACHEMAX": 512,
        }

    # -- sources -----------------------------------------------------------------------------------------
    def _open(self, src: dict, lat_i: int, lon_i: int) -> list:
        """Datasets of a source that may cover this tile. Static paths / globs are opened once and kept;
        templated paths ("{...}") are opened for this tile only (the caller closes them)."""
        import rasterio
        static = [p for p in src["paths"] if "{" not in p]
        templated = [p.format(**tile_fields(lat_i, lon_i)) for p in src["paths"] if "{" in p]

        if src["name"] not in self._datasets:
            paths: list[str] = []
            for p in static:
                if any(ch in p for ch in "*?[") and not p.startswith("/vsi"):
                    paths += sorted(glob.glob(os.path.expanduser(p)))
                else:
                    paths.append(os.path.expanduser(p))
            kept = []
            for p in paths:
                try:
                    kept.append(rasterio.open(p))
                except Exception as e:
                    for ds in kept:
                        ds.close()
                    raise SourceError(f"source {src['name']}: cannot open {p}: {e}") from e
            if static and not kept:
                raise SourceError(f"source {src['name']}: no files match {static}")
            self._datasets[src["name"]] = kept

        per_tile = []
        for p in templated:
            try:
                per_tile.append(rasterio.open(p))
            except Exception as e:
                if _is_missing(e):                         # e.g. no USGS tile outside the US: expected
                    log.debug("source %s: no %s", src["name"], p)
                    continue
                for ds in per_tile:
                    ds.close()
                raise SourceError(f"source {src['name']}: cannot read {p}: {e}") from e
        return self._datasets[src["name"]] + per_tile

    @staticmethod
    def _overlaps(ds, west, south, east, north) -> bool:
        from rasterio.warp import transform_bounds
        try:
            b = transform_bounds("EPSG:4326", ds.crs, west, south, east, north, densify_pts=21)
        except Exception:
            return True                                    # can't tell: let the warp decide
        return not (b[2] < ds.bounds.left or b[0] > ds.bounds.right or b[3] < ds.bounds.bottom or b[1] > ds.bounds.top)

    # -- building -----------------------------------------------------------------------------------------
    def path(self, lat_i: int, lon_i: int) -> Path:
        return self.out / f"{terrain.tile_name(lat_i, lon_i)}.npy"

    def build(self, lat_i: int, lon_i: int, force: bool = False) -> "np.ndarray | None":
        """The merged tile (float32, N x N, row 0 = north edge), built and saved if not there yet;
        None for open sea (no source has data and there is no skadi tile), like Tiles.tile()."""
        import rasterio
        from rasterio.transform import Affine
        from rasterio.warp import Resampling, reproject

        f = self.path(lat_i, lon_i)
        sea = f.with_suffix(".sea")
        if not force and sea.exists():
            return None
        if f.exists() and not force:
            return np.load(f, mmap_mode="r")
        with self.lock:
            if not force and sea.exists():
                return None
            if f.exists() and not force:
                return np.load(f, mmap_mode="r")
            t0 = time.time()
            # sample centres on the 1" grid lines, edges included (like .hgt)
            transform = Affine(STEP, 0, lon_i - STEP / 2, 0, -STEP, lat_i + 1 + STEP / 2)
            west, south, east, north = lon_i - STEP, lat_i - STEP, lon_i + 1 + STEP, lat_i + 1 + STEP
            dst = np.full((N, N), np.nan, dtype=np.float32)
            stats: dict[str, float] = {}
            with rasterio.Env(**self.env):
                for src in self.sources:
                    if not np.isnan(dst).any():
                        break
                    filled = 0
                    opened = self._open(src, lat_i, lon_i)
                    kept = self._datasets.get(src["name"], [])
                    for ds in opened:
                        if not self._overlaps(ds, west, south, east, north):
                            continue
                        tmp = np.full((N, N), np.nan, dtype=np.float32)
                        try:
                            reproject(source=rasterio.band(ds, 1), destination=tmp,
                                      src_nodata=ds.nodata, dst_transform=transform, dst_crs="EPSG:4326",
                                      dst_nodata=np.nan, resampling=Resampling.bilinear)
                        except Exception as e:
                            for o in opened:
                                if not any(o is k for k in kept):
                                    o.close()
                            raise SourceError(f"source {src['name']}: reading {ds.name} for "
                                              f"{terrain.tile_name(lat_i, lon_i)} failed: {e}") from e
                        if src.get("min_valid") is not None:  # e.g. sentinel values some products use
                            tmp[tmp < float(src["min_valid"])] = np.nan
                        take = np.isnan(dst) & np.isfinite(tmp)
                        dst[take] = tmp[take]
                        filled += int(take.sum())
                    for ds in opened:                          # per-tile files are not reused
                        if not any(ds is k for k in kept):
                            ds.close()
                    if filled:
                        stats[src["name"]] = filled / dst.size
            rest = np.isnan(dst)
            if rest.any():
                base = self.base.tile(lat_i, lon_i) if self.base is not None else None
                if base is not None and base.shape == dst.shape:
                    dst[rest] = base[rest]
                    stats["skadi (fallback)"] = float(rest.sum()) / dst.size
                else:                                      # no tile anywhere: open sea
                    dst[rest] = 0.0
                    stats["sea (no data)"] = float(rest.sum()) / dst.size
            if stats.get("sea (no data)") == 1.0:              # all sea: a marker, not 52 MB of zeros
                sea.write_text("")
                log.info("%s is open sea", terrain.tile_name(lat_i, lon_i))
                return None
            tmpf = f.with_name(f.stem + ".tmp.npy")
            np.save(tmpf, dst)
            os.replace(tmpf, f)
            f.with_suffix(".json").write_text(json.dumps(
                {"tile": terrain.tile_name(lat_i, lon_i), "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "seconds": round(time.time() - t0, 1), "fraction_by_source": {k: round(v, 4) for k, v in stats.items()}},
                indent=1))
            log.info("built %s in %.1f s: %s", terrain.tile_name(lat_i, lon_i), time.time() - t0,
                     ", ".join(f"{k} {v:.0%}" for k, v in stats.items()))
            return np.load(f, mmap_mode="r")


def tile_fields(lat_i: int, lon_i: int) -> dict:
    """Fields for templated source paths, for the 1x1 degree tile with south-west corner (lat_i, lon_i):
    {ns}/{ew}: n/s and e/w of the NORTH-WEST corner, {lat_top:02d}/{lon_left:03d}: its absolute values
    (USGS 3DEP naming, e.g. n41w074 = 40..41N, 74..73W); {NS}/{EW} upper-case; {lat}/{lon}: lat_i/lon_i;
    {name}: the server's own tile name (e.g. N40W074, south-west corner, like SRTM / FABDEM)."""
    top = lat_i + 1
    return {"ns": "n" if top >= 0 else "s", "ew": "e" if lon_i >= 0 else "w",
            "NS": "N" if top >= 0 else "S", "EW": "E" if lon_i >= 0 else "W",
            "lat_top": abs(top), "lon_left": abs(lon_i), "lat": lat_i, "lon": lon_i,
            "name": terrain.tile_name(lat_i, lon_i)}


def make_builder(cfg: dict, base_tiles: "terrain.Tiles | None") -> "Builder | None":
    out = layers_dir(cfg)
    if out is None:
        return None
    return Builder(cfg["terrain"]["layers"], out, base_tiles)


def tiles_around(lat: float, lon: float, km: float) -> list[tuple[int, int]]:
    dlat = km / 111.19
    dlon = km / (111.19 * max(0.05, math.cos(math.radians(lat))))
    return tiles_in(lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def tiles_in(s: float, w: float, n: float, e: float) -> list[tuple[int, int]]:
    return [(i, j) for i in range(math.floor(s), math.floor(n) + 1) for j in range(math.floor(w), math.floor(e) + 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="config.json (default: next to this script)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build merged tiles")
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--around", nargs=2, type=float, metavar=("LAT", "LON"))
    g.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"))
    b.add_argument("--km", type=float, default=None, help="radius for --around (default terrain.max_km)")
    b.add_argument("--force", action="store_true", help="rebuild tiles that exist")
    i = sub.add_parser("info", help="show which sources a built tile came from")
    i.add_argument("tile", help="e.g. N40W074")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(a.config)
    t = cfg["terrain"]
    out = layers_dir(cfg)
    if out is None:
        raise SystemExit("terrain.layers.sources has no enabled source in the config: nothing to build")
    if a.cmd == "info":
        f = out / f"{a.tile}.json"
        print(f.read_text() if f.exists() else f"{a.tile} is not built yet (in {out})")
        return
    base = terrain.Tiles(Path(t["tiles_dir"] or Path(cfg["data_dir"]) / "tiles"), t["tiles_url"] or None)
    builder = make_builder(cfg, base)
    todo = tiles_around(a.around[0], a.around[1], a.km or float(t["max_km"])) if a.around else tiles_in(*a.bbox)
    log.info("%d tiles -> %s", len(todo), out)
    for lat_i, lon_i in todo:
        builder.build(lat_i, lon_i, force=a.force)


if __name__ == "__main__":
    main()
