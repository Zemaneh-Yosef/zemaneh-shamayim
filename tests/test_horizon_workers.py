"""Horizons computed in worker processes (serve.BuildPool), without network: the real terrain.horizon_set on a flat
synthetic tile, outside the API process; one computation per place however many ask at once; cached answers
quick while vantage searches run; a worker that dies reported once and the pool started anew; errors carried
back from the worker; the search's time learned from what the worker measured; at most api.max_threads
requests handled at once.

    python3 tests/test_horizon_workers.py
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import serve  # noqa: E402
from common import load_config  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def get(url):
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, json.loads(r.read()), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), time.monotonic() - t0


def until_done(url, limit_s=120):
    """Ask until the answer is not "computing" (as the website does); (status, body, how many 503 computing)."""
    pending, t0 = 0, time.monotonic()
    while True:
        st, body, _ = get(url)
        if not (st == 503 and body.get("computing")):
            return st, body, pending
        pending += 1
        if time.monotonic() - t0 > limit_s:
            raise AssertionError(f"still computing after {limit_s} s: {url}")
        time.sleep(0.2)


def run(tmp: Path):
    tiles = tmp / "tiles"
    tiles.mkdir()
    # one 1" tile with a few hills, so the searches have something to compare
    yy, xx = np.mgrid[0:3601, 0:3601]
    z = 100 + 300 * np.exp(-((yy - 1500) ** 2 + (xx - 2400) ** 2) / 2e5) + 200 * np.exp(-((yy - 2600) ** 2 + (xx - 900) ** 2) / 1e5)
    (tiles / "N31E035.hgt").write_bytes(z.astype(">i2").tobytes())
    cfgp = tmp / "config.json"
    cfgp.write_text(json.dumps({
        "data_dir": str(tmp / "data"),
        "terrain": {"tiles_dir": str(tiles), "tiles_url": "", "answer_within_s": 0.3, "max_km": 20.0,
                    "max_parallel": 2, "max_candidates": 400},
        "api": {"max_threads": 4}}))
    cfg = load_config(str(cfgp))
    serve.Handler.store = serve.Store(cfg)
    hz = serve.Handler.horizons = serve.Horizons(cfg)
    srv = serve.APIServer(("127.0.0.1", 0), serve.Handler, cfg["api"]["max_threads"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1/horizon?"
    stored = lambda: sorted((tmp / "data" / "horizons").glob("*.json.gz"))  # noqa: E731

    print("1. a point, computed in a worker process")
    check(hz.pool is not None and not hz.pool.processes(), "the pool starts on first use, not with the server")
    st, body, pending = until_done(base + "lat=31.5&lon=35.5")
    check(st == 200 and body["points"][0]["ground"] > 99, f"answered (after {pending} x 503 computing)")
    pids = [p.pid for p in hz.pool.processes()]
    check(pids and os.getpid() not in pids, f"by worker processes {pids}, not this one ({os.getpid()})")
    check(not hz.tiles.cache, "the API process loaded no terrain for it")
    check(len(stored()) == 1, "stored on disk")
    check("compute" in hz.timing.measured, f"the search's time, measured in the worker, learned here "
                                           f"(scale {hz.timing.v['compute']:.2f})")
    st, body2, dt = get(base + "lat=31.5&lon=35.5")
    check(st == 200 and body2 == body and not hz.jobs, f"then read from the stored file ({dt * 1000:.0f} ms)")

    print("2. many asking at once for a new place")
    url = base + "lat=31.55&lon=35.45&radius_km=0.5"
    results = []
    ts = [threading.Thread(target=lambda: results.append(until_done(url))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check(len(results) == 8 and all(r[0] == 200 and r[1] == results[0][1] for r in results),
          "all 8 answered alike (more than api.max_threads = 4 at once: the others waited for a thread)")
    check(len(stored()) == 2, "from one computation (one more file)")

    print("3. cached answers while two vantage searches run")
    areas = [base + f"lat={lat}&lon={lon}&radius_km=1.5&grid_m=50" for lat, lon in ((31.45, 35.55), (31.6, 35.4))]
    t0 = time.monotonic()
    for u in areas:
        st, body, _ = get(u)
        check(st == 503 and body.get("computing") and body["work"]["spots"] > 100,
              f"a {body['work']['spots']}-spot search started (etaS {body.get('etaS')})")
    lat_ms = []
    while hz.jobs and len(lat_ms) < 200:
        st, _, dt = get(base + "lat=31.5&lon=35.5")
        assert st == 200
        lat_ms.append(dt * 1000)
        time.sleep(0.02)
    busy = time.monotonic() - t0
    check(len(lat_ms) >= 5, f"{len(lat_ms)} cached answers during {busy:.1f} s of searching")
    worst = max(lat_ms)
    check(worst < 500, f"none slowed by the searches: median {sorted(lat_ms)[len(lat_ms) // 2]:.0f} ms, "
                       f"worst {worst:.0f} ms")
    for u in areas:
        st, body, _ = until_done(u)
        check(st == 200 and body["mode"] == "vantage", "the search answered")

    if hasattr(signal, "SIGKILL"):
        print("4. a worker that dies")
        url = base + "lat=31.4&lon=35.6&radius_km=1.5&grid_m=50"
        st, body, _ = get(url)
        check(st == 503 and body.get("computing"), "a search started")
        time.sleep(0.5)
        old = [p.pid for p in hz.pool.processes()]
        for pid in old:
            os.kill(pid, signal.SIGKILL)
        st, body, _ = until_done(url, 30)
        check(st == 503 and "worker process died" in body.get("error", "") and not body.get("computing"),
              f"reported once: {body.get('error')}")
        st, body, pending = until_done(url)
        check(st == 200 and pending >= 1, f"the next request started it anew and it was answered ({pending} x 503)")
        new = [p.pid for p in hz.pool.processes()]
        check(new and not set(new) & set(old), f"by new worker processes {new}")

    print("5. an error in the worker")
    st, body, _ = until_done(base + "lat=40.5&lon=35.5")
    check(st == 503 and "is not in" in body.get("error", ""), f"carried back: {body.get('error')}")

    srv.shutdown()
    hz.pool.shutdown()
    print("OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
