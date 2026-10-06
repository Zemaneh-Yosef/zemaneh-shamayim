#!/usr/bin/env python3
"""Artificial night-sky brightness at a place, from the World Atlas of Artificial Night Sky Brightness.

The atlas (Falchi et al. 2016, GFZ Data Services, doi:10.5880/GFZ.1.4.2016.001, CC BY-NC 4.0) gives the
artificial zenith brightness in mcd/m^2 on a 30 arc-second grid (43200 x 17406 pixels, 85.054 N to
59.996 S). This is the light-pollution term (`Blp`) of the star-visibility nightfall model in
astertaylor/halakhic_calc (calc_time.py), which reads it from the same 4096 x 4096 pixel tiles
(`lp_{row}_{col}.tif`, row / col = the tile's first pixel). Either those original tiles or re-compressed
copies (e.g. LERC) can be used: GDAL (bundled with rasterio) reads both.

    lp = LightPollution(cfg)
    lp.point(31.7767, 35.2345)                    # {'artificialMcdM2': 5.756, ...}
    lp.area([31.70, 35.15, 31.85, 35.30], polygons, percentile=90)

Pixels are located from the tiles' own georeferencing. calc_time.py's index math assumes the grid starts
at exactly 85 N and spans 145 deg; it actually starts at 85.0542 N with 1/120 deg pixels, so the original
reads a pixel 3-6 rows (3-6 km) north of the observer: at Jerusalem 4.82 instead of 5.76 mcd/m^2, at
Monsey 1.41 instead of 2.42.

Tiles are looked for in light_pollution.dir (named by light_pollution.file) and, if missing and
light_pollution.url is set, downloaded on first use (by default the originals from the halakhic_calc
repository: 2 KB-38 MB each, 488 MB for the world, lossless zstd). With light_pollution.convert set
(default: LERC_ZSTD, max_z_error 0.001 mcd/m^2) a download is re-compressed straight away and saved under
light_pollution.file - about 13% of the original size (~65 MB for the world) - and the original is
deleted; the conversion is checked (same grid, every pixel within max_z_error) before it replaces
anything. Without convert, a download is kept as is under the URL's own file name. Either way a folder of
your own copies that leaves out some tiles is completed.

How much loss is acceptable: what the nightfall model sees is the total sky brightness (artificial +
natural 0.174 mcd/m^2 + twilight), so an absolute error of 0.001 mcd/m^2 is at most 0.57% of it (at a
pristine site), and far less where light pollution matters. In calc_time.py's nightfall, 1% of the
artificial brightness moved the time by at most 7 s (3 small stars, Monsey, March; most cases 0 s).

For an area (an official area from areas.json, or a box), every pixel whose centre lies inside it is
read, and the value used is the `percentile` (default 90) of their brightness: a brighter sky means a
later nightfall, so the 90th percentile is a time by which about 90% of the area has the stars - the
same convention as the moonrise horizons (terrain.moon_percentile).

Command line:
    light_pollution.py 31.7767 35.2345
    light_pollution.py --bbox 40.60,-73.98,40.62,-73.955 [--percentile 90]
    light_pollution.py --download-all          # all 55 tiles into light_pollution.dir (converting them,
                                               # and any originals already there, when convert is set)
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

log = logging.getLogger("light_pollution")

# The atlas grid, as georeferenced in its tiles (checked against each tile when it is opened)
TOP = 85.0541668645001          # north edge of row 0
LEFT = -180.00000000000003      # west edge of column 0
PX = 0.008333330000000002       # pixel size, deg (30")
ROWS, COLS = 17406, 43200
TILE = 4096
BOTTOM = TOP - ROWS * PX        # -59.9958

NATURAL_MCD_M2 = 0.174          # natural zenith brightness the atlas is referred to (22.0 mag/arcsec^2)
SOURCE = ("Falchi et al. 2016, The new world atlas of artificial night sky brightness "
          "(GFZ Data Services, doi:10.5880/GFZ.1.4.2016.001), CC BY-NC 4.0")


def mag_arcsec2(mcd_m2: float) -> float:
    """Luminance (mcd/m^2) to surface brightness, mag/arcsec^2 (V)."""
    return -2.5 * math.log10(mcd_m2 * 1e-3 / 108000.0)


def sqm_to_artificial(mag: float) -> float:
    """A sky-meter reading (mag/arcsec^2, zenith, moonless astronomical night) to artificial mcd/m^2."""
    return max(108000.0e3 * 10 ** (-0.4 * float(mag)) - NATURAL_MCD_M2, 0.0)


# Skyglow is brighter away from the zenith (toward the light sources on the horizon). The measured ratio of
# horizontal illuminance to zenith luminance (the "Posch ratio") is pi for a uniform sky and peaks at
# 2.25 pi for light-polluted skies (Bara et al. 2022, arXiv:2202.07526), i.e. the sky's average luminance is
# about 2.25 x its zenith luminance. A luminance growing with airmass, L(z) = L_zenith * X(z), gives 2 pi.
SKY_AVERAGE_FACTOR = 2.25
POSCH = ("Bara et al. 2022, Estimating linear radiance indicators from the zenith night sky brightness: on "
         "the Posch ratio for natural and light polluted skies (arXiv:2202.07526)")


def describe(mcd: float, sky_factor: float = SKY_AVERAGE_FACTOR) -> dict:
    """The fields every answer carries, for an artificial zenith brightness in mcd/m^2."""
    mcd = max(float(mcd), 0.0)
    return {
        "artificialMcdM2": round(mcd, 4),                 # at the zenith (what a sky meter reads, minus nature)
        "artificialCdM2": mcd * 1e-3,
        # Blp for calc_time.py's nightfall(), which applies one value to every star: the sky's AVERAGE
        # artificial luminance, sky_factor x the zenith. calc_time.py uses pi x the zenith, reading the
        # literature's horizontal-illuminance / zenith-luminance ratio (~pi) as a luminance ratio; but a sky
        # of uniform luminance already has that ratio = pi, so the average-to-zenith luminance ratio is the
        # measured ratio / pi: ~2.25 for light-polluted skies.
        "blpCdM2": mcd * 1e-3 * sky_factor,
        "skyAverageFactor": sky_factor,
        "ratioToNatural": round(mcd / NATURAL_MCD_M2, 3),
        "totalMagArcsec2": round(mag_arcsec2(mcd + NATURAL_MCD_M2), 2),   # zenith, natural sky included
    }


# --- from the atlas (2014 satellite data) to the sky of a given year ------------------------------------
KYBA = ("Kyba et al. 2023, Citizen scientists report global rapid reductions in the visibility of stars "
        "from 2011 to 2022, Science 379, 265 (doi:10.1126/science.abq7781)")
DEFAULT_TREND = {
    "base_year": 2014,              # the atlas's VIIRS data
    "data_until": 2022,             # the trend's own data; later years are extrapolated (and flagged)
    "rate": 0.096,                  # per year, worldwide (mostly European and North American observers)
    "regions": [                    # first box containing the point wins
        {"name": "north-america", "rate": 0.104, "south": 7.0, "north": 85.0, "west": -170.0, "east": -50.0},
        {"name": "europe", "rate": 0.065, "south": 35.0, "north": 72.0, "west": -25.0, "east": 45.0},
    ],
}


class Trend:
    """Growth of artificial sky brightness since the atlas, from naked-eye star counts (Kyba et al. 2023):
    9.6%/yr worldwide, 10.4% North America, 6.5% Europe, 2011-2022. Satellites (and so the atlas) see far
    less (~2%/yr): they miss light emitted sideways, which makes most skyglow, and the blue of white LEDs.
    The rate is for the whole sky as seen; applying it to the artificial part alone understates it a little."""

    def __init__(self, cfg: dict | None):
        cfg = DEFAULT_TREND if cfg is None else cfg
        self.enabled = bool(cfg) and cfg.get("enabled", True)
        self.base = int(cfg.get("base_year", DEFAULT_TREND["base_year"]))
        self.until = int(cfg.get("data_until", DEFAULT_TREND["data_until"]))
        self.rate = float(cfg.get("rate", DEFAULT_TREND["rate"]))
        self.regions = cfg.get("regions", DEFAULT_TREND["regions"])

    def region(self, lat: float, lon: float):
        for r in self.regions:
            if r["south"] <= lat <= r["north"] and r["west"] <= lon <= r["east"]:
                return r["name"], float(r["rate"])
        return "world", self.rate

    def factor(self, lat: float, lon: float, from_year: float, to_year: float) -> float:
        if not self.enabled:
            return 1.0
        return (1.0 + self.region(lat, lon)[1]) ** (to_year - from_year)


class Measurements:
    """Sky-meter readings: {"measurements": [{"lat", "lon", "date": "YYYY-MM-DD", "sqm": mag/arcsec^2
    (or "artificialMcdM2"), "note"}]}, taken at the zenith on clear, moonless nights after astronomical
    twilight. Reloaded when the file changes."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.stamp = None
        self.items: list[dict] = []

    def refresh(self) -> None:
        if self.path is None:
            return
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.stamp, self.items = None, []
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self.stamp:
            return
        with self.lock:
            items = []
            for i, m in enumerate(json.loads(self.path.read_text()).get("measurements", [])):
                try:
                    art = (float(m["artificialMcdM2"]) if "artificialMcdM2" in m else sqm_to_artificial(m["sqm"]))
                    d = str(m["date"])
                    year = int(d[:4]) + (int(d[5:7]) - 0.5) / 12 if len(d) >= 7 else int(d[:4]) + 0.5
                    items.append({"lat": float(m["lat"]), "lon": float(m["lon"]), "year": year,
                                  "artificialMcdM2": max(art, 0.0), "date": d,
                                  **({"sqm": float(m["sqm"])} if "sqm" in m else {}),
                                  **({"note": m["note"]} if m.get("note") else {})})
                except (KeyError, TypeError, ValueError) as e:
                    log.warning("%s: measurement %d skipped (%s)", self.path, i, e)
            self.items, self.stamp = items, stamp
            log.info("%d sky measurements from %s", len(items), self.path)

    def select(self, lat: float, lon: float, radius_km: float, area=None) -> list[dict]:
        """Readings within radius_km of the point, and (for an area) inside its boundary."""
        self.refresh()
        out = []
        for m in self.items:
            dy = (m["lat"] - lat) * 111.19
            dx = (m["lon"] - lon) * 111.19 * math.cos(math.radians(lat))
            near = math.hypot(dx, dy) <= radius_km
            inside = False
            if area is not None and not near:
                from areas import area_contains
                inside = area_contains(area, m["lat"], m["lon"])
            if near or inside:
                out.append(m)
        return out


def pixel_of(lat: float, lon: float):
    """(row, col) of the pixel containing the point, or None outside the atlas's latitudes."""
    if not (BOTTOM <= lat <= TOP):
        return None
    r = min(int(math.floor((TOP - lat) / PX)), ROWS - 1)
    c = min(max(int(math.floor((lon - LEFT) / PX)), 0), COLS - 1)
    return r, c


def pixel_centre(r: int, c: int):
    return TOP - (r + 0.5) * PX, LEFT + (c + 0.5) * PX


def in_polygons(lons: np.ndarray, lats: np.ndarray, polygons) -> np.ndarray:
    """areas.area_contains for many points: even-odd over each polygon's rings, any polygon."""
    out = np.zeros(lons.shape, dtype=bool)
    for rings in polygons:
        c = np.zeros(lons.shape, dtype=bool)
        for ring in rings:
            r = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
            xi, yi = r[:, 0], r[:, 1]
            xj, yj = np.roll(xi, 1), np.roll(yi, 1)
            with np.errstate(divide="ignore", invalid="ignore"):
                for k in range(len(xi)):
                    cross = (yi[k] > lats) != (yj[k] > lats)
                    if not cross.any():
                        continue
                    xint = (xj[k] - xi[k]) * (lats - yi[k]) / (yj[k] - yi[k]) + xi[k]
                    c ^= cross & (lons < xint)
        out |= c
    return out


class LightPollution:
    """Atlas tiles on disk (downloaded on first use if allowed), read one window at a time."""

    def __init__(self, cfg: dict):
        lp = cfg.get("light_pollution", {})
        self.dir = Path(lp.get("dir") or Path(cfg["data_dir"]) / "light-pollution")
        self.file = lp.get("file") or "lp_{row}_{col}.tif"
        self.url = lp.get("url") or None
        # GDAL GeoTIFF creation options for downloaded tiles, e.g. {"compress": "LERC_ZSTD", "max_z_error": 0.001}
        self.convert = {str(k).lower(): v for k, v in (lp.get("convert") or {}).items()}
        self.percentile = float(lp.get("area_percentile", 90.0))
        self.max_radius_km = float(lp.get("max_radius_km", 30.0))
        self.trend = Trend(lp.get("trend"))
        self.sky_factor = float(lp.get("sky_average_factor", SKY_AVERAGE_FACTOR))
        meas = lp.get("measurements") or {}
        self.measurements = Measurements(meas.get("file") or Path(cfg["data_dir"]) / "sky-measurements.json")
        self.meas_radius_km = float(meas.get("radius_km", 15.0))
        self.ua = f"zmanim-sky-server/0.1 ({cfg.get('contact') or 'no contact'})"
        self.lock = threading.Lock()
        self.datasets: dict = {}                          # (tr, tc) -> (dataset, lock)
        self.downloading: dict = {}                       # name -> lock
        self.memo: OrderedDict = OrderedDict()

    # --- tiles -----------------------------------------------------------------------------------
    def name(self, tr: int, tc: int) -> str:
        return self.file.format(row=tr * TILE, col=tc * TILE)

    def path(self, tr: int, tc: int) -> Path:
        """The tile's file: `file` in dir; else one downloaded earlier from `url` (converted now, if convert
        is set); else download it now (converted, or kept under the URL's own file name)."""
        name = self.name(tr, tc)
        f = self.dir / name
        if f.exists():
            return f
        if not self.url:
            raise FileNotFoundError(f"light-pollution tile {name} is not in {self.dir} (downloads disabled)")
        url = self.url.format(file=name, row=tr * TILE, col=tc * TILE)
        raw = self.dir / Path(urlparse(url).path).name
        with self.lock:
            lk = self.downloading.setdefault(name, threading.Lock())
        with lk:
            if f.exists():
                return f
            if raw.exists() and raw != f:
                if not self.convert:
                    return raw
                self.convert_file(raw, f)              # an original from before convert was set
                raw.unlink()
                return f
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"could not download light-pollution tile {name} from {url}: HTTP {e.code}") from e
            except urllib.error.URLError as e:
                raise RuntimeError(f"could not download light-pollution tile {name} from {url}: {e.reason}") from e
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = raw.with_name(raw.name + ".download")
            tmp.write_bytes(data)
            log.info("downloaded light-pollution tile %s (%.1f MB)", raw.name, len(data) / 1e6)
            if not self.convert:
                tmp.replace(raw)
                return raw
            try:
                self.convert_file(tmp, f)
            finally:
                tmp.unlink(missing_ok=True)
            return f

    def convert_file(self, src: Path, dst: Path) -> None:
        """Re-compress src into dst with the `convert` creation options; dst is written only once the
        result is checked: same size and grid, and every pixel within max_z_error (exact if lossless)."""
        try:
            import rasterio
        except ImportError as e:
            raise RuntimeError("light pollution needs rasterio (pip install rasterio)") from e
        with rasterio.open(src) as s:
            prof, a = s.profile, s.read(1)
        prof.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256, **self.convert)
        tol = float(self.convert.get("max_z_error", 0.0))
        tmp = dst.with_name(dst.name + ".part")
        try:
            with rasterio.open(tmp, "w", **prof) as d:
                d.write(a, 1)
            with rasterio.open(tmp) as c:
                b, ok_grid = c.read(1), (c.shape == a.shape and c.transform == prof["transform"])
            fin = np.isfinite(a)
            av, d = a[fin].astype(np.float64), np.abs(b[fin].astype(np.float64) - a[fin])
            err = float(d.max()) if d.size else 0.0
            # LERC keeps the bound before the value is rounded back to float32: allow one float32 step
            over = float((d - tol - np.abs(av) * 2.4e-7).max()) if d.size else 0.0
            if not ok_grid or not np.array_equal(np.isfinite(b), fin) or over > 1e-9:
                raise RuntimeError(f"converting {src.name} failed its check (max error {err:g}, "
                                   f"allowed {tol:g}, grid {'ok' if ok_grid else 'changed'})")
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)
        log.info("converted light-pollution tile %s -> %s (%.1f -> %.1f MB, max error %.4g)", src.name.removesuffix(".download"),
                 dst.name, src.stat().st_size / 1e6, dst.stat().st_size / 1e6, err)

    def dataset(self, tr: int, tc: int):
        key = (tr, tc)
        with self.lock:
            have = self.datasets.get(key)
        if have is not None:
            return have
        try:
            import rasterio
        except ImportError as e:
            raise RuntimeError("light pollution needs rasterio (pip install rasterio)") from e
        f = self.path(tr, tc)
        ds = rasterio.open(f)
        t = ds.transform
        x0, y0 = LEFT + tc * TILE * PX, TOP - tr * TILE * PX
        if abs(t.a - PX) > 1e-9 or abs(t.e + PX) > 1e-9 or abs(t.c - x0) > PX / 2 or abs(t.f - y0) > PX / 2:
            ds.close()
            raise RuntimeError(f"{f.name} is not tile ({tr * TILE}, {tc * TILE}) of the atlas grid "
                               f"(origin {t.c:.5f}, {t.f:.5f}; expected {x0:.5f}, {y0:.5f})")
        with self.lock:
            have = self.datasets.setdefault(key, (ds, threading.Lock()))
        if have[0] is not ds:
            ds.close()
        return have

    def window(self, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        """Pixels [r0..r1] x [c0..c1] (inclusive, global indices), across tiles, mcd/m^2."""
        out = np.zeros((r1 - r0 + 1, c1 - c0 + 1), dtype=np.float32)
        for tr in range(r0 // TILE, r1 // TILE + 1):
            for tc in range(c0 // TILE, c1 // TILE + 1):
                ra, rb = max(r0, tr * TILE), min(r1, tr * TILE + TILE - 1)
                ca, cb = max(c0, tc * TILE), min(c1, tc * TILE + TILE - 1)
                ds, lk = self.dataset(tr, tc)
                with lk:                                  # a rasterio dataset is not thread-safe
                    a = ds.read(1, window=((ra - tr * TILE, rb - tr * TILE + 1),
                                           (ca - tc * TILE, cb - tc * TILE + 1)))
                    nodata = ds.nodata
                a = a.astype(np.float32)
                bad = ~np.isfinite(a) | (a < 0)
                if nodata is not None:
                    bad |= a == nodata
                a[bad] = 0.0                              # no data = no artificial light (as calc_time.py)
                out[ra - r0:rb - r0 + 1, ca - c0:cb - c0 + 1] = a
        return out

    # --- answers ---------------------------------------------------------------------------------
    def _memo(self, key, fn):
        with self.lock:
            if key in self.memo:
                self.memo.move_to_end(key)
                return self.memo[key]
        res = fn()
        with self.lock:
            self.memo[key] = res
            while len(self.memo) > 4096:
                self.memo.popitem(last=False)
        return res

    def point(self, lat: float, lon: float) -> dict:
        lat, lon = round(lat, 5), round(lon, 5)
        return self._memo(("p", lat, lon), lambda: self._point(lat, lon))

    def _point(self, lat: float, lon: float) -> dict:
        """The atlas pixel as it is (2014 data, no correction)."""
        rc = pixel_of(lat, lon)
        if rc is None:
            return {"lat": lat, "lon": lon, "coverage": False, "atlasMcdM2": 0.0, "pixel": None,
                    "note": "outside the atlas (85.05 N to 60.00 S); no artificial light assumed"}
        v = float(self.window(rc[0], rc[0], rc[1], rc[1])[0, 0])
        plat, plon = pixel_centre(*rc)
        return {"lat": lat, "lon": lon, "coverage": True, "atlasMcdM2": round(v, 4),
                "pixel": {"row": rc[0], "col": rc[1], "lat": round(plat, 5), "lon": round(plon, 5)}}

    def correction(self, lat: float, lon: float, year: float, area=None):
        """(function atlas mcd/m^2 -> today's estimate, description). Sky-meter readings nearby win; else the
        regional trend from the atlas year; else the atlas as it is."""
        used = []
        for m in self.measurements.select(lat, lon, self.meas_radius_km, area):
            atlas = self.point(m["lat"], m["lon"])["atlasMcdM2"]
            # carried from the reading's date to the middle of `year`
            meas = m["artificialMcdM2"] * self.trend.factor(m["lat"], m["lon"], m["year"], year + 0.5)
            # compared as TOTAL brightness (natural included): stable at dark sites, proportional in towns
            used.append({**{k: m[k] for k in ("lat", "lon", "date", "sqm", "note") if k in m},
                         "measuredMcdM2": round(m["artificialMcdM2"], 4), "atlasMcdM2": atlas,
                         "ratio": (meas + NATURAL_MCD_M2) / (atlas + NATURAL_MCD_M2)})
        if used:
            f = float(np.median([u["ratio"] for u in used]))
            for u in used:
                u["ratio"] = round(u["ratio"], 4)
            return (lambda v: max((v + NATURAL_MCD_M2) * f - NATURAL_MCD_M2, 0.0),
                    {"method": "measured", "year": year, "factorOnTotal": round(f, 4), "measurements": used[:25],
                     "count": len(used), "radiusKm": self.meas_radius_km,
                     "note": "median ratio of measured to atlas total sky brightness of the readings within "
                             "radiusKm (or inside the area), applied to the atlas"
                             + ("; readings from other years carried to this year by the regional trend"
                                if self.trend.enabled else "")})
        if self.trend.enabled:
            region, rate = self.trend.region(lat, lon)
            f = self.trend.factor(lat, lon, self.trend.base, year)
            return (lambda v: v * f,
                    {"method": "trend", "year": year, "factor": round(f, 4), "region": region, "ratePerYear": rate,
                     "baseYear": self.trend.base, "extrapolated": year > self.trend.until + 1, "source": KYBA})
        return (lambda v: v, {"method": "none", "year": year, "factor": 1.0,
                              "note": "the atlas as it is (2014 satellite data)"})

    def estimate(self, lat: float, lon: float, *, area=None, bbox=None, percentile=None, year=None,
                 area_key=None) -> dict:
        """The answer /v1/light-pollution gives: today's (or `year`'s) estimate at the point, or over an
        area (official area dict with bbox / polygons) or bbox, with the raw atlas values alongside."""
        year = float(year) if year is not None else float(datetime.date.today().year)
        year = int(year) if year == int(year) else year
        raw = self.point(lat, lon)
        fix, corr = self.correction(lat, lon, year, area)
        point = {**raw, **describe(fix(raw["atlasMcdM2"]), self.sky_factor)}
        res = {"lat": raw["lat"], "lon": raw["lon"]}
        stats = None
        if area is not None:
            stats = self.area(area["bbox"], area.get("polygons"), percentile, key=area_key)["stats"]
            if stats is None:
                res["areaNote"] = "the area is smaller than one atlas pixel; the value at the point"
        elif bbox is not None:
            res["bbox"] = bbox
            stats = self.area(bbox, None, percentile)["stats"]
            if stats is None:
                res["areaNote"] = "the box holds no atlas pixel centre; the value at the point"
        if stats is not None:
            out = {k: (round(fix(v), 4) if k in ("min", "p10", "median", "p90", "max", "mean") else v)
                   for k, v in stats.items() if k != "value"}
            res.update(coverage=True, **describe(fix(stats["value"]), self.sky_factor), atlasMcdM2=round(stats["value"], 4),
                       stats=out, point=point)
        else:
            res = {**point, **res}
        res["correction"] = corr
        res["source"] = SOURCE
        return res

    def area(self, bbox, polygons=None, percentile: float | None = None, key=None) -> dict:
        """Brightness over the pixels whose centres lie in bbox [s, w, n, e] (and in polygons, if given)."""
        pct = self.percentile if percentile is None else min(max(float(percentile), 0.0), 100.0)
        k = ("a", key or tuple(round(v, 5) for v in bbox), round(pct, 2))
        return self._memo(k, lambda: self._area(bbox, polygons, pct))

    def _area(self, bbox, polygons, pct: float) -> dict:
        s, w, n, e = bbox
        s, n = max(s, BOTTOM + PX / 2), min(n, TOP - PX / 2)
        stats = None
        if s <= n:
            r0, r1 = int(math.floor((TOP - n) / PX)), int(math.floor((TOP - s) / PX))
            c0 = max(int(math.floor((w - LEFT) / PX)), 0)
            c1 = min(int(math.floor((e - LEFT) / PX)), COLS - 1)
            r1 = min(r1, ROWS - 1)
            a = self.window(r0, r1, c0, c1)
            lats = TOP - (np.arange(r0, r1 + 1) + 0.5) * PX
            lons = LEFT + (np.arange(c0, c1 + 1) + 0.5) * PX
            LON, LAT = np.meshgrid(lons, lats)
            m = (LAT >= s) & (LAT <= n) & (LON >= w) & (LON <= e)
            if polygons:
                m &= in_polygons(LON, LAT, polygons)
            v = a[m].astype(np.float64)
            if v.size:
                q = np.percentile(v, [0, 10, 50, 90, 100, pct])
                stats = {"pixels": int(v.size), "percentile": pct, "min": round(q[0], 4),
                         "p10": round(q[1], 4), "median": round(q[2], 4), "p90": round(q[3], 4),
                         "max": round(q[4], 4), "mean": round(float(v.mean()), 4), "value": float(q[5])}
        return {"stats": stats}


def main(argv=None):
    from common import load_config

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", nargs="?", type=float)
    ap.add_argument("lon", nargs="?", type=float)
    ap.add_argument("--bbox", help="south,west,north,east: statistics over a box")
    ap.add_argument("--percentile", type=float)
    ap.add_argument("--year", type=float, help="the sky of this year (default: this year)")
    ap.add_argument("--download-all", action="store_true", help="fetch every atlas tile into light_pollution.dir")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lp = LightPollution(load_config(args.config))
    if args.download_all:
        for tr in range(math.ceil(ROWS / TILE)):
            for tc in range(math.ceil(COLS / TILE)):
                print(lp.path(tr, tc))
        return
    if args.lat is None and not args.bbox:
        ap.error("give LAT LON, --bbox or --download-all")
    b = [float(v) for v in args.bbox.split(",")] if args.bbox else None
    lat = args.lat if args.lat is not None else (b[0] + b[2]) / 2
    lon = args.lon if args.lon is not None else (b[1] + b[3]) / 2
    print(json.dumps(lp.estimate(lat, lon, bbox=b, percentile=args.percentile, year=args.year), indent=1))


if __name__ == "__main__":
    main()
