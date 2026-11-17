#!/usr/bin/env python3
"""An area's horizon answer (serve.Horizons.with_extra): the stored answer with the area added, spliced rather than
parsed, kept in memory. No network; needs only the standard library and numpy (serve's imports)."""
import gzip
import json
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class H:
    with_extra = serve.Horizons.with_extra

    def __init__(self):
        self.extra_cache, self.extra_lock = OrderedDict(), threading.Lock()


# a stored answer as Horizons._build writes it: a vantage set of a realistic size
res = {"mode": "vantage", "candidates": 900, "gridM": 100.0,
       "points": [{"lat": 31.77 + i * 1e-3, "lon": 35.23, "ground": 750.5, "height": 752.2,
                   "sunrise": [{"azimuthDeg": round(60 + k * 0.1, 2), "distanceKm": 45.123, "heightM": 1012.4,
                                "floorM": -400.0, "alternates": [[60.5, 1100.0]]} for k in range(300)],
                   "wins": {"sunrise": [[60.0, 90.0]]}} for i in range(20)],
       "params": {"lat": 31.778, "lon": 35.2354, "bbox": [31.7, 35.1, 31.8, 35.3], "moon": True},
       "seaTiles": [], "terrain": {"layers": "abc", "sources": ["x", "y"], "algo": 4}}
body = gzip.compress(json.dumps(res, separators=(",", ":")).encode())
extra = {"area": {"id": "il:jerusalem", "name": "ירושלים", "level": 8, "bbox": [31.7, 35.1, 31.8, 35.3],
                  "radiusKm": 9.4, "polygons": [[[35.1, 31.7, 35.3, 31.7, 35.3, 31.8]]]}}
h = H()
t0 = time.perf_counter()
out = h.with_extra(body, extra)
t_first = time.perf_counter() - t0
before = json.dumps({**res, **extra}, separators=(",", ":")).encode()      # what get_ex sent until now
check(gzip.decompress(out) == before, "the same JSON bytes as merging the parsed answer")
check(json.loads(gzip.decompress(out))["area"]["name"] == "ירושלים", "non-ASCII names escaped as json.dumps does")
check(h.with_extra(body, extra) is out, "asked again: the kept answer itself")
t0 = time.perf_counter()
for _ in range(100):
    h.with_extra(body, extra)
t_hit = (time.perf_counter() - t0) / 100
check(t_hit < t_first / 5, f"kept answers are quick ({t_hit * 1e3:.2f} ms against {t_first * 1e3:.1f} ms the first time)")
note = {"area": None, "areaNote": "no official area here; used the request as given"}
check(gzip.decompress(h.with_extra(body, note)) == json.dumps({**res, **note}, separators=(",", ":")).encode(),
      "an areaNote answer too")
check(h.with_extra(body, extra) == H().with_extra(body, extra), "the same answer twice is the same gzip bytes")
small = gzip.compress(b"{}")
check(json.loads(gzip.decompress(H().with_extra(small, extra))) == extra, "an empty object takes the fields alone")
for k in range(300):
    h.with_extra(gzip.compress(json.dumps({"k": k}).encode()), extra)
check(len(h.extra_cache) <= 256, "at most 256 answers kept")

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
