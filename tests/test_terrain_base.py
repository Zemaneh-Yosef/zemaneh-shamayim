"""The base level's candidates (terrain_base.py), without network or tiles: a stub terrain.

    python3 tests/test_terrain_base.py

Flat land at 0 m. The observer stands on a 600 m ridge 30 km wide running north-south (narrower than the 80 km
width: a mountain, removed). 60 km to the east a 500 m plateau 200 km wide begins (wider: land, kept). A river
3 km wide is cut 20 km to the west (narrower than the 5 km fill: filled).

  - sunrise (east): the candidates lie on the plateau at 500 m, not on the removed ridge;
  - sunset (west): the candidates are at the plain's 0 m, and the river's dip is filled (no point below 0);
  - the observer's own ridge is gone from under them: every candidate is far below the eye;
  - an override with "none" sends no candidates, with a status;
  - a tile reported as not bare earth sends no candidates (Decision 3);
  - the flag: widening to 120 km doesn't touch a 200 km plateau, so this place isn't flagged;
  - the surface is on one world lattice: a spot's profiles are the same from the surfaces of the neighbouring
    lattice nodes, and a spot's candidates are the same asked alone or as one of an area's spots.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import terrain  # noqa: E402
import terrain_base  # noqa: E402

OBS = (31.0, 35.0)
KX = 111195.0 * math.cos(math.radians(OBS[0]))


class StubTiles:
    fine = None
    sea_tiles: set = set()

    def heights(self, lat, lon):
        x = (np.asarray(lon, float) - OBS[1]) * KX          # metres east
        h = np.zeros(np.shape(x))
        h[np.abs(x) < 15000] = 600.0                        # the ridge under the observer, 30 km wide
        h[(x > 60000) & (x < 260000)] = 500.0               # the plateau, 200 km wide
        h[np.abs(x + 20000) < 1500] = -30.0                 # a river 3 km wide (cut into the plain)
        return h

    def ground_heights(self, lat, lon):
        return self.heights(lat, lon)

    def ground(self, lat, lon):
        return float(self.heights(np.array([lat]), np.array([lon]))[0])


def main():
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok &= bool(cond)

    s = terrain_base.settings(None)
    tiles = StubTiles()
    res = terrain.horizon_set(tiles, *OBS, base={"settings": s, "non_bare": None})
    hz = res["points"][0]
    check(hz["groundM"] == 600.0, f"groundM is the observer's own ground ({hz['groundM']})")
    check(hz["base"]["status"] == "ok" and hz["base"]["widthKm"] == 80.0, f"base status ok ({hz['base']})")

    e = min((q for q in hz["sunrise"] if "baseCandidates" in q), key=lambda q: abs(q["azimuthDeg"] - 90))
    cands = e["baseCandidates"]
    check(cands and all(abs(h - 500.0) < 1 for _, h in cands), f"sunrise candidates on the plateau ({cands})")
    check(all(d > 55 for d, _ in cands), "none on the removed ridge")

    w = min((q for q in hz["sunset"] if "baseCandidates" in q), key=lambda q: abs(q["azimuthDeg"] - 270))
    cw = w["baseCandidates"]
    check(cw and all(abs(h) < 1 for _, h in cw), f"sunset candidates on the plain at 0 m, river filled ({cw})")
    check(all(h < 550 for _, h in cands + cw), "the observer's ridge is gone from under them")
    n_max = len(terrain_base.K_RANGE) + 3
    check(len(cands) <= n_max and len(cw) <= n_max, f"at most one per k, plus near winners ({len(cands)}, {len(cw)})")
    every = [q for q in hz["sunrise"] if "baseCandidates" in q]
    check(all(abs(q["azimuthDeg"] * 2 - round(q["azimuthDeg"] * 2)) <= 0.1 for q in every) and len(every) >= 10,
          f"candidates every 0.5 deg of azimuth ({len(every)} entries)")
    check(hz["base"].get("flagged") is False, f"not flagged ({hz['base'].get('flagSeconds')} s)")

    terrain_base._CACHE.clear()
    s2 = terrain_base.settings({"overrides": [{"lat": OBS[0], "lon": OBS[1], "radius_km": 3, "none": True}]})
    hz2 = terrain.horizon_set(tiles, *OBS, base={"settings": s2, "non_bare": None})["points"][0]
    check(hz2["base"]["status"] == "none" and not any("baseCandidates" in q for q in hz2["sunrise"]),
          f"override none: no candidates ({hz2['base']})")

    terrain_base._CACHE.clear()
    hz3 = terrain.horizon_set(tiles, *OBS, base={"settings": s, "non_bare": lambda i, j: 0.3, "key": "x"})["points"][0]
    check(hz3["base"]["status"] == "no-bare-earth" and not any("baseCandidates" in q for q in hz3["sunrise"]),
          f"not bare earth: no candidates ({hz3['base']['status']})")

    # one world lattice: the same profile from two neighbouring nodes (a terrain that varies both ways)
    class Bumpy(StubTiles):
        def heights(self, lat, lon):
            la, lo = np.asarray(lat, float), np.asarray(lon, float)
            return (300 + 250 * np.sin(la * 7.3) * np.cos(lo * 5.1) + 120 * np.sin(la * 41) * np.sin(lo * 37)
                    + 0 * la)
    spot = (31.137, 35.081)
    b, r, c = terrain_base.anchor_for(*spot)
    a1 = terrain_base.Surface(Bumpy(), (b, r, c), s)
    a2 = terrain_base.Surface(Bumpy(), (b, r + terrain_base.ANCHOR_CELLS, c - terrain_base.ANCHOR_CELLS), s)
    az = np.arange(0, 360, 7.5)
    diff = float(np.max(np.abs(a1.profile(*spot, az) - a2.profile(*spot, az))))
    check(diff < 1e-6, f"same profile from neighbouring lattice nodes (max diff {diff:.2e} m)")
    terrain_base._CACHE.clear()
    alone = terrain.horizon_set(Bumpy(), *spot, base={"settings": s, "non_bare": None})["points"][0]
    terrain_base._CACHE.clear()
    import copy
    other = copy.deepcopy(alone)
    for e in other["sunrise"] + other["sunset"]:
        e.pop("baseCandidates", None)
    terrain.add_base(Bumpy(), spot[0] + 0.08, spot[1] - 0.06, [other], {"settings": s, "non_bare": None})
    same = all(e.get("baseCandidates") == f.get("baseCandidates")
               for e, f in zip(alone["sunrise"] + alone["sunset"], other["sunrise"] + other["sunset"]))
    check(same, "a spot's candidates don't depend on the area's centre")

    # the seam at 32 N: continuous across it, the mix ends at BLEND_DEG
    terrain_base._CACHE.clear()
    az = np.arange(0, 360, 7.5)
    lo_, eps = 35.081, 1e-7
    below = terrain_base.surface_for(Bumpy(), 32 - eps, lo_, s).profile(32 - eps, lo_, az)
    above = terrain_base.surface_for(Bumpy(), 32 + eps, lo_, s).profile(32 + eps, lo_, az)
    edge = terrain_base.anchors_for(32 + terrain_base.BLEND_DEG + 1e-9, lo_)
    mid = terrain_base.anchors_for(32 + terrain_base.BLEND_DEG / 2, lo_)
    check(float(np.max(np.abs(above - below))) < 1e-3 and len(edge) == 1
          and [round(w, 2) for _, w in mid] == [0.75, 0.25],
          f"seam: the same profile either side ({float(np.max(np.abs(above - below))):.1e} m), mix 3:1 halfway, "
          f"one band at {terrain_base.BLEND_DEG} deg")

    # below sea level: the Dead Sea's surface is kept, the Mediterranean's floor becomes the sea
    la = np.array([31.5, 31.5, 32.0]); lo = np.array([35.5, 34.3, 35.6])
    z = terrain_base.sea_level(np.array([-415.0, -800.0, 120.0]), la, lo, s["depressions"])
    check(list(z) == [-415.0, 0.0, 120.0], f"Dead Sea kept, sea floor raised, land untouched ({list(z)})")

    # the store: built once, then loaded (no terrain read), rebuilt when the tiles' revisions change
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        reads = {"n": 0}

        class Counting(Bumpy):
            def heights(self, lat, lon):
                reads["n"] += 1
                return super().heights(lat, lon)
        revs = {"v": "a"}
        base = {"settings": s, "non_bare": None, "store": Path(d), "revs": lambda tl: revs["v"]}
        terrain_base._CACHE.clear()
        first = terrain.horizon_set(Counting(), *spot, base=base)["points"][0]
        n1 = reads["n"]
        terrain_base._CACHE.clear()
        again = terrain.horizon_set(Counting(), *spot, base=base)["points"][0]
        cand = lambda h: [e.get("baseCandidates") for e in h["sunrise"] + h["sunset"]]
        surf_reads = reads["n"] - n1
        terrain_base._CACHE.clear()
        reads["n"] = 0
        terrain_base.surface_for(Counting(), *spot, s, store=Path(d), revs_fn=lambda tl: "a")
        loaded_reads = reads["n"]
        terrain_base._CACHE.clear()
        terrain_base.surface_for(Counting(), *spot, s, store=Path(d), revs_fn=lambda tl: "b")
        check(cand(first) == cand(again) and loaded_reads == 0 and reads["n"] == 1,
              f"stored surface: same candidates, loaded without reading the terrain, rebuilt on new revisions "
              f"({loaded_reads} reads loaded, {reads['n']} after)")
    nodes = terrain_base.nodes_for(s, *spot, radius_km=15)
    check(2 <= len(nodes) <= 14 and terrain_base.anchor_for(*spot) in [n for n, _ in nodes],
          f"build: the nodes a 15 km place uses, with the band below 31 N it reaches ({len(nodes)})")

    # the morphology itself: an opening removes a 1-cell spike, keeps a block wider than the disk
    a = np.zeros((41, 41)); a[20, 20] = 9; a[5:15, 5:15] = 4
    o = terrain_base.opening(a, 2)
    check(o[20, 20] == 0 and o[10, 10] == 4, "opening: spike removed, wide block kept")
    c = terrain_base.closing(np.where(np.arange(41)[None, :] == 20, -5.0, 0.0) + 0 * a, 1)
    check(float(c.min()) == 0.0, "closing: a one-cell trench filled")

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
