"""Ensembles in /v1/path-profiles (&ensemble=1): GEFS members for forecasts (fetch_gefs.py) and the
climatology's representative variations (gfs-moments, climatology.sigma_members). No network needed.

    python3 tests/test_ensemble.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fetch_gefs  # noqa: E402
import fetch_gfs  # noqa: E402
import serve  # noqa: E402
from climatology import ClimGrid, MOMENT_VARS, clim_dir, sigma_members  # noqa: E402
from common import Cycle, forecast_hours, load_config  # noqa: E402
from make_synthetic import write  # noqa: E402

REGION = {"name": "nyc", "north": 43.0, "south": 38.0, "west": -78.0, "east": -68.0}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def main():
    print("1. sigma_members: weights, mean and spread")
    rng = np.random.default_rng(1)
    names = MOMENT_VARS
    x = rng.normal(size=(5000, len(names))) @ rng.normal(size=(len(names), len(names))) * 0.8 + 10
    mom = {f"m:{a}": float(x[:, i].mean()) for i, a in enumerate(names)}
    for i, a in enumerate(names):
        for j in range(i, len(names)):
            mom[f"m:{a}*{names[j]}"] = float((x[:, i] * x[:, j]).mean())
    f = {a: mom[f"m:{a}"] + 273.15 for a in names}
    f["other"] = 1.0
    mem = sigma_members(f, mom)
    w = np.array([m[0] for m in mem])
    X = np.array([[m[1][a] for a in names] for m in mem])
    check(len(mem) == 7 and abs(w.sum() - 1) < 1e-9, f"7 members, weights sum to 1 ({w.round(3).tolist()})")
    check(np.allclose(w @ X, [f[a] for a in names]), "weighted mean = the mean column")
    cov = np.cov(x.T, bias=True)
    lam = np.sort(np.linalg.eigvalsh(cov))[::-1]
    D = X - np.array([f[a] for a in names])
    got = np.sort(np.linalg.eigvalsh((D.T * w) @ D))[::-1]
    check(np.allclose(got[:3], lam[:3], rtol=1e-6), f"their spread is the 3 strongest modes' ({lam[:3].round(2).tolist()})")
    check(all(m[1]["other"] == 1.0 for m in mem), "other fields untouched")

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfgp = tmp / "config.json"
        cfgp.write_text(json.dumps({
            "data_dir": str(tmp / "data"), "regions": [REGION],
            "forecast_hours": {"hourly_until": 11, "step_after": 3, "max": 11}, "request_delay_s": 0,
            "climatology": {"min_days": 1, "ncep_prior": False},
            "ensemble": {"enabled": True, "members": 2, "max_hours": 48, "step_hours": 3}}))
        cfg = load_config(str(cfgp))

        print("2. gfs-moments from runs with weak and strong inversions")
        src = tmp / "grib"
        runs = [(Cycle(datetime(2026, 10, day, 0, tzinfo=timezone.utc)), k) for day, k in ((3, 0.0), (4, 4.0))]
        for c, inv in runs:
            for h in forecast_hours(cfg):
                write(src / REGION["name"] / fetch_gfs.grib_file_name(c, h), REGION, t2m_land=283.15, t2m_sea=285.15,
                      skin_sea=289.15, coast_lon=-74.0, near_inversion_k=inv)
            assert fetch_gfs.main(["--config", str(cfgp), "--cycle", c.id, "--source-dir", str(src)]) == 0
        mg = ClimGrid(clim_dir(cfg, "gfs-moments", "nyc"))
        check(mg.counts[9].tolist() == [6] * 4 + [0] * 4, f"moments counted like the main store {mg.counts[9].tolist()}")
        m = mg.fields_at(datetime(2026, 10, 16, 4, 30, tzinfo=timezone.utc), 40.7, -75.0, 3)
        var80 = m["m:t80m*t80m"] - m["m:t80m"] ** 2
        check(abs(var80 - (0.8 * 4 / 2) ** 2) < 0.02, f"80 m air varies with the inversion: variance {var80:.2f} K^2")

        print("3. &ensemble=1 on a climatology date: the representative variations")
        store = serve.Store(cfg)
        store.refresh()
        body = serve.path_profiles(store, 40.7128, -74.006, datetime(2026, 10, 20).date(), 1, ensemble=True)
        e = next(x for x in body["events"] if x["source"] == "climatology-gfs")
        en = e["ensemble"]
        check(en["kind"] == "climatology" and len(en["members"]) == 7, f"{len(en['members'])} members")
        check(abs(sum(mm["weight"] for mm in en["members"]) - 1) < 1e-5, "weights sum to 1")
        check(en["members"][0]["profiles"] == e["profiles"], "the first member is the profile itself")
        t80 = [next(lv["t"] for lv in mm["profiles"][-1]["levels"] if lv["h"] == 80.0) for mm in en["members"]]
        check(max(t80) - min(t80) > 2, f"the 80 m air spread over the members: {min(t80)}..{max(t80)} C")
        plain = serve.path_profiles(store, 40.7128, -74.006, datetime(2026, 10, 20).date(), 1)
        check(all("ensemble" not in x for x in plain["events"]), "without &ensemble=1: as before")

        print("4. GEFS: fetch_gefs.py stores the members; forecast events carry them")
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        cyc = Cycle(now - timedelta(hours=now.hour % 6 + 6))
        for h in forecast_hours({**cfg, "forecast_hours": {"hourly_until": 48, "step_after": 3, "max": 48}}):
            write(src / REGION["name"] / fetch_gfs.grib_file_name(cyc, h), REGION, t2m_land=283.15, t2m_sea=285.15,
                  skin_sea=289.15, coast_lon=-74.0)
        cfg2 = json.loads(cfgp.read_text())
        cfg2["forecast_hours"] = {"hourly_until": 48, "step_after": 3, "max": 48}
        cfgp.write_text(json.dumps(cfg2))
        assert fetch_gfs.main(["--config", str(cfgp), "--cycle", cyc.id, "--source-dir", str(src)]) == 0
        gsrc = tmp / "gefs"
        for k, member in enumerate(fetch_gefs.members(cfg)):
            for h in fetch_gefs.hours(cfg):
                write(gsrc / REGION["name"] / member / fetch_gefs.file_name(cyc, member, h, "a"), REGION,
                      t2m_land=283.15 + k, t2m_sea=285.15 + k, skin_sea=289.15, coast_lon=-74.0)
        assert fetch_gefs.main(["--config", str(cfgp), "--cycle", cyc.id, "--source-dir", str(gsrc)]) == 0
        check(json.loads((tmp / "data/gefs/current.json").read_text())["cycle"] == cyc.id, "GEFS run published")
        cfg = load_config(str(cfgp))
        store = serve.Store(cfg)
        store.refresh()
        ens = serve.EnsembleStore(cfg)
        ens.refresh()
        body = serve.path_profiles(store, 40.7128, -74.006, now.date(), 2, ensemble=True, ens=ens)
        fc = [x for x in body["events"] if x["source"] == "forecast"]
        check(fc and all(x.get("ensemble", {}).get("kind") == "gefs" for x in fc), f"{len(fc)} forecast events with GEFS")
        mem = fc[0]["ensemble"]["members"]
        t2 = [mm["profiles"][-1]["levels"][0]["t"] for mm in mem]
        check([mm["member"] for mm in mem] == ["c00", "p01", "p02"] and t2 == sorted(t2) and t2[2] - t2[0] > 1.9,
              f"control + 2 members, each its own air ({t2})")
        check("ensemble" in body["sources"], "source described")
    print("OK")


if __name__ == "__main__":
    main()
