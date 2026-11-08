"""Remote and fallback layers (dem_layers.py): FABDEM read from its 10x10 degree zips over HTTP, an outage
told apart from a missing file, provisional tiles retried later, and a source masked by land cover
(ALOS outside forest, the plain tiles in forest). A local HTTP server plays Bristol's; no other network.

    python3 tests/test_dem_fallback.py          (needs rasterio)

Over tiles N31E035, N31E036, N32E035 (heights linear in lat/lon, so bilinear sampling reproduces them):
  fabdem  per-tile GeoTIFFs inside N30E030-N40E040.zip (N31E035 and N31E036 only), served over HTTP;
          nodata north of SEA_LAT
  alos    a local per-tile file covering everything, 3 m above FABDEM; masked by
  wc      a WorldCover-like 3x3 degree block: trees (10) east of FOREST_LON and north of SEA_LAT, else grass (30)
  skadi   the plain tiles, flat 540 m
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import dem_layers  # noqa: E402
import terrain  # noqa: E402

SEA_LAT = 31.9
FOREST_LON = 35.7
RES = 600                                   # 6" sources: small files, same bilinear result on linear heights


def fabdem_h(lat, lon):
    return 300 + 100 * (lat - 31) - 80 * (lon - 35)


def alos_h(lat, lon):
    return fabdem_h(lat, lon) + 3


SKADI_H = 540


def geotiff(path_or_buf, lat_i, lon_i, a, dtype="float32", nodata=-9999, size=1):
    import rasterio
    from rasterio.transform import from_origin
    n = a.shape[0]
    with rasterio.open(path_or_buf, "w", driver="GTiff", width=n, height=n, count=1, dtype=dtype, crs="EPSG:4326",
                       nodata=nodata, transform=from_origin(lon_i, lat_i + size, size / n, size / n)) as ds:
        ds.write(a.astype(dtype), 1)


def centres(lat_i, lon_i, n, size=1):
    lat = lat_i + size - (np.arange(n)[:, None] + 0.5) * size / n
    lon = lon_i + (np.arange(n)[None, :] + 0.5) * size / n
    return np.broadcast_arrays(lat, lon)


class Server:
    """Files from a dict, with HTTP ranges; mode "up" or "down" (every request answered 503)."""

    def __init__(self, files: dict[str, bytes]):
        self.files, self.mode, self.hits = files, "up", 0
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, body: bool):
                srv.hits += 1
                if srv.mode == "down":
                    self.send_response(503)
                    self.end_headers()
                    return
                data = srv.files.get(self.path.lstrip("/"))
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                rng = self.headers.get("Range")
                if rng:
                    a, b = rng.split("=")[1].split("-")
                    a = int(a) if a else len(data) - int(b)
                    b = min(int(b), len(data) - 1) if (b and rng.split("=")[1][0] != "-") else len(data) - 1
                    part = data[a:b + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                else:
                    part = data
                    self.send_response(200)
                self.send_header("Content-Length", str(len(part)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if body:
                    self.wfile.write(part)

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, p=""):
        return f"http://127.0.0.1:{self.port}/{p}"


def main():
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok &= bool(cond)

    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ["no_proxy"] = os.environ["NO_PROXY"] + ",127.0.0.1"
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # FABDEM zip (stored members at the root, like Bristol's)
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_STORED) as z:
            for lat_i, lon_i in ((31, 35), (31, 36)):
                lat, lon = centres(lat_i, lon_i, RES)
                a = fabdem_h(lat, lon)
                a[lat > SEA_LAT] = -9999
                b = io.BytesIO()
                geotiff(b, lat_i, lon_i, a)
                z.writestr(f"{terrain.tile_name(lat_i, lon_i)}_FABDEM_V1-2.tif", b.getvalue())
        srv = Server({"N30E030-N40E040_FABDEM_V1-2.zip": zbuf.getvalue()})

        (tmp / "alos").mkdir()
        (tmp / "wc").mkdir()
        tiles_dir = tmp / "tiles"
        tiles_dir.mkdir()
        for lat_i, lon_i in ((31, 35), (31, 36), (32, 35)):
            lat, lon = centres(lat_i, lon_i, RES)
            geotiff(tmp / "alos" / f"{terrain.tile_name(lat_i, lon_i)}.tif", lat_i, lon_i, alos_h(lat, lon))
            hgt = np.full((3601, 3601), SKADI_H, dtype=">i2")
            (tiles_dir / f"{terrain.tile_name(lat_i, lon_i)}.hgt.gz").write_bytes(gzip.compress(hgt.tobytes()))
        lat, lon = centres(30, 33, 1800, size=3)         # 6" WorldCover-like block N30E033 (30..33N, 33..36E)
        wc = np.where((lon > FOREST_LON) & (lat > SEA_LAT), 10, 30)
        geotiff(tmp / "wc" / "N30E033.tif", 30, 33, wc, dtype="uint8", nodata=0, size=3)

        layers = {"dir": str(tmp / "layers"), "auto_build": True, "sources": [
            {"name": "fabdem", "if_unavailable": "skip",
             "paths": ["/vsizip//vsicurl/" + srv.url("{name10}-{name10_end}_FABDEM_V1-2.zip/{name}_FABDEM_V1-2.tif")]},
            {"name": "alos", "if_unavailable": "skip", "paths": [str(tmp / "alos" / "{name}.tif")],
             "mask": {"paths": [str(tmp / "wc" / "{name3}.tif")], "exclude": [10]}},
        ]}
        cfg = {"data_dir": str(tmp), "terrain": {"tiles_dir": str(tiles_dir), "tiles_url": "", "layers": layers,
                                                  "max_km": 150.0}}
        base = terrain.Tiles(tiles_dir, None)
        builder = dem_layers.make_builder(cfg, base)
        out = dem_layers.layers_dir(cfg)

        def heights(lat_i, lon_i, pts):
            t = builder.build(lat_i, lon_i)
            la, lo = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
            r = (lat_i + 1 - la) * 3600
            c = (lo - lon_i) * 3600
            return np.array([t[int(round(i)), int(round(j))] for i, j in zip(r, c)])

        def info(lat_i, lon_i):
            return json.loads(builder.path(lat_i, lon_i).with_suffix(".json").read_text())

        # 1. server up: FABDEM from the zip, ALOS where FABDEM has none, plain tiles in the forest
        pts = [(31.5, 35.3), (31.95, 35.3), (31.95, 35.9)]
        h = heights(31, 35, pts)
        check(abs(h[0] - fabdem_h(*pts[0])) < 0.05, f"FABDEM read from inside the remote zip ({h[0]:.2f} m)")
        check(abs(h[1] - alos_h(*pts[1])) < 0.05, f"no FABDEM, not forest: ALOS ({h[1]:.2f} m)")
        check(abs(h[2] - SKADI_H) < 0.5, f"no FABDEM, forest: the plain tiles (SRTM) ({h[2]:.2f} m)")
        check(not builder.path(31, 35).with_suffix(".provisional").exists(), "complete tile: not provisional")
        print("    composition:", info(31, 35)["fraction_by_source"])

        # 2. a tile the zip doesn't have: confirmed missing, falls through quietly (not provisional)
        h = heights(32, 35, [(32.5, 35.3), (32.5, 35.9)])
        check(abs(h[0] - alos_h(32.5, 35.3)) < 0.05 and abs(h[1] - SKADI_H) < 0.5,
              "member absent from the zip: ALOS / plain tiles, quietly")
        check(not builder.path(32, 35).with_suffix(".provisional").exists() and "fabdem" not in info(32, 35)["fraction_by_source"],
              "  ... and not provisional")

        # 3. outage: built without FABDEM, kept, provisional (GDAL's HTTP cache emptied first, as in a process
        # that meets the outage on its first read: it then remembers the failure, and step 6 needs it forgotten)
        import rasterio.cache
        rasterio.cache.invalidate_all()
        dem_layers._zip_names.clear()
        srv.mode = "down"
        h = heights(31, 36, [(31.5, 36.3)])
        prov = builder.path(31, 36).with_suffix(".provisional")
        check(abs(h[0] - alos_h(31.5, 36.3)) < 0.05, f"FABDEM's server down: ALOS meanwhile ({h[0]:.2f} m)")
        check(prov.exists() and info(31, 36).get("provisional_without") == ["fabdem"],
              "  ... tile marked provisional, without fabdem")

        # 4. within the hour: the stored tile, no request to the server
        hits = srv.hits
        builder.build(31, 36)
        check(srv.hits == hits, "provisional tile used as is within the hour (no request)")

        # 5. after the hour, still down: tile kept, marker renewed
        old = time.time() - dem_layers.RETRY_S - 5
        os.utime(prov, (old, old))
        builder.build(31, 36)
        check(prov.exists() and time.time() - prov.stat().st_mtime < 60 and srv.hits > hits,
              "retried after the hour while still down: kept, retried again an hour later")
        check(dem_layers.tile_rev(out, 31, 36) == 0, "  ... heights unchanged, revision 0")

        # 6. server back: rebuilt with FABDEM, no longer provisional, revision raised
        srv.mode = "up"
        os.utime(prov, (old, old))
        h = heights(31, 36, [(31.5, 36.3)])
        check(abs(h[0] - fabdem_h(31.5, 36.3)) < 0.05, f"server back: rebuilt with FABDEM ({h[0]:.2f} m)")
        check(not prov.exists() and "provisional_without" not in info(31, 36), "  ... no longer provisional")
        check(dem_layers.tile_rev(out, 31, 36) == 1, "  ... revision 1 (horizons reaching it are recomputed)")

        # 7. telling missing from unreachable
        cm = dem_layers._confirm_missing
        check(cm("/vsicurl/" + srv.url("nothing.tif")), "404: missing")
        check(cm("/vsizip//vsicurl/" + srv.url("N50E050-N60E060_FABDEM_V1-2.zip/N55E055_FABDEM_V1-2.tif")),
              "no such zip (404): missing")
        srv.mode = "down"
        dem_layers._zip_names.clear()
        check(not cm("/vsicurl/" + srv.url("nothing.tif")), "503: not missing (an outage)")
        check(not cm("/vsizip//vsicurl/" + srv.url("N30E030-N40E040_FABDEM_V1-2.zip/N39E039_FABDEM_V1-2.tif")),
              "zip unreachable: not missing")
        srv.mode = "up"
        check(not cm("/vsicurl/http://127.0.0.1:9/x.tif"), "nothing listening: not missing")
        check(cm(str(tmp / "nope.tif")), "local file: missing")

        # 8. the mask goes into the signature (it changes heights)
        no_mask = {**layers, "sources": [layers["sources"][0], {k: v for k, v in layers["sources"][1].items() if k != "mask"}]}
        check(dem_layers.signature(layers) != dem_layers.signature(no_mask), "mask is part of the signature")
        skip_off = {**layers, "sources": [{k: v for k, v in s.items() if k != "if_unavailable"} for s in layers["sources"]]}
        check(dem_layers.signature(layers) == dem_layers.signature(skip_off), "if_unavailable is not")
        srv.httpd.shutdown()

    print("all passed" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
