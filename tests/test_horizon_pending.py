"""A slow first horizon without network: answered 503 "computing" in time, computed once in the background,
then served.

    python3 tests/test_horizon_pending.py

terrain.horizon_set is replaced by one that takes 1.5 s (a place whose tiles are being built) and counts its calls;
terrain.answer_within_s is 0.3 s.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import serve  # noqa: E402
import terrain  # noqa: E402
from common import load_config  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def run(tmp: Path):
    tiles = tmp / "tiles"
    tiles.mkdir()
    (tiles / "N31E035.hgt").write_bytes(np.full((3601, 3601), 100, dtype=">i2").tobytes())
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({"data_dir": str(tmp / "data"),
                                "terrain": {"tiles_dir": str(tiles), "tiles_url": "", "answer_within_s": 0.3}}))
    cfg = load_config(str(cfgp))
    calls = {"n": 0, "fail": False}

    def slow_horizon_set(*a, **k):
        calls["n"] += 1
        time.sleep(1.5)
        if calls["fail"]:
            raise RuntimeError("could not download elevation tile N31E036")
        return {"mode": "point", "points": [{"lat": a[1], "lon": a[2], "ground": 100.0, "height": 101.7, "sunrise": []}]}

    terrain.horizon_set = slow_horizon_set
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = serve.Horizons(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1/horizon?lat=31.5&lon=35.5"

    def get(url):
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), dict(e.headers)

    print("1. a slow first horizon")
    t0 = time.monotonic()
    st, body, hdr = get(base)
    check(st == 503 and body.get("computing") is True and body.get("retryAfterS") == 15 and hdr.get("Retry-After") == "15",
          f"answered 503 computing, retry in 15 s ({body})")
    check(time.monotonic() - t0 < 1.0, "within answer_within_s, not after the computation")
    check(hdr.get("Cache-Control") == "no-store", "not cached by browsers")
    st2, _, _ = get(base)
    check(st2 == 503 and calls["n"] == 1, "asking again joins the same computation (horizon_set ran once)")
    time.sleep(1.5)
    st3, body3, _ = get(base)
    check(st3 == 200 and body3["points"][0]["ground"] == 100.0 and calls["n"] == 1, "then served, from the one computation")
    st4, _, _ = get(base)
    check(st4 == 200 and calls["n"] == 1 and not serve.Handler.horizons.jobs, "and from the stored file after that")

    print("2. a failed computation")
    calls["fail"] = True
    other = base.replace("lat=31.5", "lat=31.6")
    st, _, _ = get(other)
    time.sleep(1.6)
    st, body, _ = get(other)
    check(st == 503 and "could not download" in body["error"] and not body.get("computing"), f"its error reported once ({body['error']})")
    calls["fail"] = False
    st, body, _ = get(other)
    check(st == 503 and body.get("computing") and calls["n"] == 3, "the next request tries again")
    srv.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
