#!/usr/bin/env python3
"""Observed visible sunrises from Chaim Keller's files, as the CSV era5_profiles.py and royzmanimwebsite's
_denoDebug/validateSunrise.mjs read: `date,time,event,code`, local standard time.

The files are in github.com/chaimkeller/software, observations-all-places/ (the data of Keller & Hall 2000
and 2022):

  DRUK9396.DAT          Rabbi Avraham Druk, Armon HaNatziv, 1993-96 (day-of-year rows, a column per year,
                        each time with a letter: the observer's code, kept as `code`)
  DRUK.001              the same, 1985-92 (quoted cells, "#N/A" where none)
  Golan_YYYY.csv        Yisroel Golan, Nevei Yaakov, 2009-13 ("hebrew date,Mon-DD-YYYY,H:MM:SS")
  Zuriel_*.csv          Rabbi Yosef Zuriel, Bnei Brak, 1961-92 ("HH:MM:SS,MM/DD/YY"; places surmised)

All are Israel standard time (UTC+2) the whole year: June sunrises at Armon HaNatziv read 04:34. So the
output's time zone is Etc/GMT-2 (POSIX sign: UTC+2), written as a comment line and to pass as --tz.

    python3 observations.py DRUK9396.DAT DRUK.001 --out druk.csv
    python3 observations.py Golan_20*.csv --out golan.csv

Places (Keller & Hall): Armon HaNatziv 31.748553 N 35.237436 E (Rabbi Druk's shul, from Keller's DRUK
program), observer 754.9 m above sea level; Nevei Yaakov and Bnei Brak as in their paper.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

TIME = r"(\d{1,2}):(\d{2}):(\d{2})"
MONTHS = {m: k for k, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                      "Nov", "Dec"], 1)}


def _day(year: int, ddmon: str) -> date | None:
    d, mon = ddmon.split("-")
    try:
        return date(year, MONTHS[mon[:3].title()], int(d))
    except ValueError:                                   # 29-Feb in a common year
        return None


def druk_dat(text: str):
    """DRUK9396.DAT: a header line naming the years, then `DOY DD-Mon` and a cell per year, cells split by ','."""
    years = None
    for line in text.splitlines():
        if years is None:
            ys = re.findall(r"\b(19\d\d|20\d\d)\b", line)
            if len(ys) >= 2 and "DATE" in line.upper():
                years = [int(y) for y in ys]
            continue
        m = re.match(r"\s*\d+\s+(\d{2}-[A-Za-z]{3})\s*(.*)$", line)
        if not m:
            continue
        cells = m.group(2).split(",")
        for year, cell in zip(years, cells):
            t = re.search(TIME + r"\s*([A-Za-z]?)", cell)
            d = _day(year, m.group(1))
            if t and d:
                yield d, f"{int(t.group(1)):02d}:{t.group(2)}:{t.group(3)}", t.group(4)


def druk_001(text: str):
    """DRUK.001: the second year line under 'Day of' names the real years; quoted cells per year."""
    years = None
    for line in text.splitlines():
        if years is None:
            ys = re.findall(r"\b(19\d\d)\b", line)
            if len(ys) >= 4:
                years = [int(y) for y in ys]
            continue
        cells = re.findall(r'"([^"]*)"', line)
        if len(cells) < 2 or not re.match(r"\d{2}-[A-Za-z]{3}$", cells[0]):
            continue
        for year, cell in zip(years, cells[1:]):
            t = re.match(TIME + r"([A-Za-z]?)$", cell.strip())
            d = _day(year, cells[0])
            if t and d:
                yield d, f"{int(t.group(1)):02d}:{t.group(2)}:{t.group(3)}", t.group(4)


def golan(text: str):
    for line in text.splitlines():
        m = re.match(r"[^,]*,([A-Za-z]{3})-(\d{2})-(\d{4}),\s*" + TIME, line)
        if m:
            d = date(int(m.group(3)), MONTHS[m.group(1).title()], int(m.group(2)))
            yield d, f"{int(m.group(4)):02d}:{m.group(5)}:{m.group(6)}", ""


def zuriel(text: str):
    for line in text.splitlines():
        m = re.match(r"\s*" + TIME + r",(\d{2})/(\d{2})/(\d{2})", line)
        if m:
            d = date(1900 + int(m.group(6)), int(m.group(4)), int(m.group(5)))
            yield d, f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}", ""


def read(path: Path):
    text = path.read_text(errors="replace")
    name = path.name.lower()
    if name.endswith(".dat"):
        return list(druk_dat(text))
    if name.endswith(".001"):
        return list(druk_001(text))
    if name.startswith("golan"):
        return list(golan(text))
    if name.startswith("zuriel"):
        return list(zuriel(text))
    raise SystemExit(f"{path}: unknown format (DRUK*.DAT, DRUK*.001, Golan_*.csv, Zuriel_*.csv)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    rows = {}
    for f in a.files:
        got = read(f)
        print(f"{f.name}: {len(got)} sunrises", file=sys.stderr)
        for d, t, code in got:
            rows.setdefault(d, (t, code))                # a date in two files: the first file's
    lines = ["# local standard time, UTC+2: --tz Etc/GMT-2", "date,time,event,code"]
    lines += [f"{d.isoformat()},{t},sunrise,{c}" for d, (t, c) in sorted(rows.items())]
    a.out.write_text("\n".join(lines) + "\n")
    print(f"{len(rows)} dates -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
