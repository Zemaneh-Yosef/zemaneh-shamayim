"""Caching headers and CDN purging (cdn.py) without network.

    python3 tests/test_cdn.py

1. the headers: browsers at most cdn.browser_max_age_s, the CDN the full lifetime, stale-if-error, Cache-Tag;
   no-store for answers not to keep
2. a horizon over the API: a full one kept 30 days at the CDN, a partial one (a fine-terrain chunk failed) 10 minutes
3. purge requests as Cloudflare's API takes them (tags, everything), and refusals reported
4. the state check: nothing purged on the first run, then only what changed (terrain on a tile revision, areas on a
   new areas.json); a refused purge kept for the next check; without credentials, logged and moved on
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cdn  # noqa: E402
import serve  # noqa: E402
import terrain  # noqa: E402
from common import load_config  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def headers_part():
    print("1. the headers")
    cfg = {"cdn": {"browser_max_age_s": 86400, "stale_if_error_s": 3600, "tag_prefix": "zsky"}}
    h = dict(cdn.cache_headers(cfg, 30 * 86400, ("terrain",)))
    check(h["Cache-Control"] == "public, max-age=86400, stale-if-error=3600", f"browsers: a day ({h['Cache-Control']})")
    check(h["CDN-Cache-Control"] == "public, max-age=2592000, stale-if-error=3600", "the CDN: 30 days")
    check(h["Cache-Tag"] == "zsky,zsky-terrain", "tagged for purging")
    h = dict(cdn.cache_headers(cfg, 1800, ("weather",)))
    check("max-age=1800" in h["Cache-Control"] and "max-age=1800" in h["CDN-Cache-Control"], "shorter than a day: the same for both")
    check(cdn.cache_headers(cfg, 0) == [("Cache-Control", "no-store")], "0: no-store, nothing else")
    h = dict(cdn.cache_headers({"cdn": {"stale_if_error_s": 0}}, 600))
    check("stale-if-error" not in h["Cache-Control"], "stale_if_error_s 0: left out")


def api_part(tmp: Path):
    print("2. horizons over the API")
    tiles = tmp / "tiles"
    tiles.mkdir()
    (tiles / "N31E035.hgt").write_bytes(np.full((3601, 3601), 100, dtype=">i2").tobytes())
    cfgp = tmp / "config.json"
    # computed in this process (not a worker), so the horizon_set and _prepare replaced below are the ones used
    cfgp.write_text(json.dumps({"data_dir": str(tmp / "data"),
                                "terrain": {"tiles_dir": str(tiles), "tiles_url": "", "worker_processes": False}}))
    cfg = load_config(str(cfgp))
    terrain.horizon_set = lambda *a, **k: {"mode": "point", "points": [{"lat": a[1], "lon": a[2], "sunrise": []}]}
    serve.Handler.store = serve.Store(cfg)
    serve.Handler.horizons = hz = serve.Horizons(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def hdr(path):
        with urllib.request.urlopen(base + path) as r:
            r.read()
            return r.headers

    h = hdr("/v1/horizon?lat=31.5&lon=35.5")
    check("max-age=2592000" in h["CDN-Cache-Control"] and "max-age=86400" in h["Cache-Control"]
          and "stale-if-error=" in h["Cache-Control"] and h["Cache-Tag"] == "zsky,zsky-terrain", "a full horizon")
    # a fine-terrain chunk that failed: _prepare says partial
    real = hz._prepare
    hz._prepare = lambda p: (*real(p)[:3], True)
    h = hdr("/v1/horizon?lat=31.6&lon=35.6")
    check("max-age=600" in h["Cache-Control"] and "max-age=600" in h["CDN-Cache-Control"],
          f"a partial horizon: 10 minutes everywhere ({h['CDN-Cache-Control']})")
    hz._prepare = real
    srv.shutdown()


class FakeCF:
    """Cloudflare's purge endpoint: records the requests, answers success or a refusal."""

    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, req.headers.get("Authorization"), json.loads(req.data)))
        if not self.ok:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b'{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}'))
        return io.BytesIO(b'{"success":true,"errors":[],"result":{"id":"x"}}')


def purge_part():
    print("3. purge requests")
    cfg = {"cdn": {"cloudflare_zone_id": "z1", "cloudflare_api_token": "t0k", "tag_prefix": "zsky"}}
    cf = FakeCF()
    check(cdn.purge(cfg, ["terrain", "areas"], opener=cf), "purged")
    url, auth, body = cf.calls[-1]
    check(url == "https://api.cloudflare.com/client/v4/zones/z1/purge_cache" and auth == "Bearer t0k"
          and body == {"tags": ["zsky-terrain", "zsky-areas"]}, f"by tag: {body}")
    cdn.purge(cfg, None, opener=cf)
    check(cf.calls[-1][2] == {"tags": ["zsky"]}, "all of this API: its own tag")
    cdn.purge(cfg, None, everything=True, opener=cf)
    check(cf.calls[-1][2] == {"purge_everything": True}, "--everything: the zone")
    check(not cdn.purge(cfg, ["terrain"], opener=FakeCF(ok=False)), "a refusal reported as not done")
    check(not cdn.purge({"cdn": {}}, ["terrain"], opener=cf) and len(cf.calls) == 3, "no credentials: nothing sent")


def state_part(tmp: Path):
    print("4. the state check")
    cfg = {"data_dir": str(tmp / "state"), "cdn": {"cloudflare_zone_id": "z", "cloudflare_api_token": "t"}}
    purged = []

    def ok(c, names):
        purged.append(list(names))
        return True

    check(cdn.check(cfg, {"terrain": "a", "areas": "1"}, ok) == [] and purged == [], "first run: nothing purged")
    check(cdn.check(cfg, {"terrain": "a", "areas": "1"}, ok) == [] and purged == [], "nothing changed")
    cdn.check(cfg, {"terrain": "b", "areas": "1"}, ok)
    check(purged[-1] == ["terrain"], "terrain changed: terrain purged")
    cdn.check(cfg, {"terrain": "b", "areas": "2"}, ok)
    check(purged[-1] == ["areas"], "areas changed: areas purged")
    n = len(purged)
    cdn.check(cfg, {"terrain": "c", "areas": "2"}, lambda c, names: False)
    cdn.check(cfg, {"terrain": "c", "areas": "2"}, ok)
    check(len(purged) == n + 1 and purged[-1] == ["terrain"], "a refused purge: tried again at the next check")
    no_cred = {"data_dir": cfg["data_dir"], "cdn": {}}
    cdn.check(no_cred, {"terrain": "d", "areas": "2"}, cdn.purge)
    check(json.loads(cdn.state_path(cfg).read_text())["terrain"] == "d", "without credentials: logged, state moves on")

    # terrain_state follows a merged tile's revision (dem_layers.py build --force)
    class H:
        dem, fine, layers_out = "sig", None, tmp / "layers"
    H.layers_out.mkdir()
    s0 = cdn.terrain_state(H)
    (H.layers_out / "N31E035.rev").write_text("1")
    check(cdn.terrain_state(H) != s0, "a tile's new revision changes the terrain state")


def main():
    headers_part()
    with tempfile.TemporaryDirectory() as d:
        api_part(Path(d))
    purge_part()
    with tempfile.TemporaryDirectory() as d:
        state_part(Path(d))
    print("all ok")


if __name__ == "__main__":
    main()
