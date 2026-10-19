#!/usr/bin/env python3
"""Observed visible sunrises from Chaim Keller's files, as the CSV era5_profiles.py and royzmanimwebsite's
_denoDebug/validateSunrise.mjs read: `date,time,event,code`, local standard time.

The files are in github.com/chaimkeller/software, observations-all-places/ (the data of Keller & Hall 2000
and 2022):

  DRUK9396.DAT          Rabbi Avraham Druk, Armon HaNatziv, 1993-96 (day-of-year rows, a column per year,
                        each time with a letter: the observer's code, kept as `code`)
  DRUK.001              the same, 1985-92 (quoted cells, "#N/A" where none)
  DRUK_10 yr. observations.XLS   the fullest list, 1985-96 (1,921 sunrises; 1993-96 has 521, the .DAT only
                        368): a column per year, times as fractions of a day, 42 where none, a Hebrew-letter
                        code column after each year (needs xlrd)
  Golan_YYYY.csv        Yisroel Golan, Nevei Yaakov, 2009-13 ("hebrew date,Mon-DD-YYYY,H:MM:SS")
  Zuriel_*.csv          Rabbi Yosef Zuriel, Bnei Brak, 1961-92 ("HH:MM:SS,MM/DD/YY"; places surmised)

All are Israel standard time (UTC+2) the whole year: June sunrises at Armon HaNatziv read 04:34. So the
output's time zone is Etc/GMT-2 (POSIX sign: UTC+2), written as a comment line and to pass as --tz.

    python3 observations.py "DRUK_10 yr. observations.XLS" --out druk.csv      # all of Druk's, 1985-96
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


XLS_CODES = {"ב": "B", "כ": "K", "מ": "M"}


def druk_xls(path: Path):
    """The spreadsheet: row 6 names the years (column 4 on), rows from 9 are days of the year (column 1,
    1-366 with 29 February as day 60); a time is a fraction of a day, 42 = no observation."""
    import xlrd
    sh = xlrd.open_workbook(str(path)).sheets()[0]
    years = {c: int(sh.cell_value(6, c)) for c in range(sh.ncols)
             if isinstance(sh.cell_value(6, c), float) and 1900 < sh.cell_value(6, c) < 2100}
    for r in range(9, sh.nrows):
        doy = sh.cell_value(r, 1)
        if not isinstance(doy, float) or not 1 <= doy <= 366:
            continue
        m, d = _leap_doy(int(doy))
        for c, year in years.items():
            v = sh.cell_value(r, c)
            if not isinstance(v, float) or not 0 < v < 1:
                continue
            try:
                day = date(year, m, d)
            except ValueError:                           # 29 February in a common year
                continue
            sec = round(v * 86400)
            code = XLS_CODES.get(str(sh.cell_value(r, c + 1)).strip(), "")
            yield day, f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}", code


def _leap_doy(doy: int) -> tuple[int, int]:
    """Month and day of a day of the year counted in a leap year (as Druk's tables are: 60 = 29 Feb)."""
    t = date(2000, 1, 1).toordinal() + doy - 1
    x = date.fromordinal(t)
    return x.month, x.day


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
    name = path.name.lower()
    if name.endswith(".xls"):
        return list(druk_xls(path))
    text = path.read_text(errors="replace")
    if name.endswith(".dat"):
        return list(druk_dat(text))
    if name.endswith(".001"):
        return list(druk_001(text))
    if name.startswith("golan"):
        return list(golan(text))
    if name.startswith("zuriel"):
        return list(zuriel(text))
    raise SystemExit(f"{path}: unknown format (DRUK*.DAT, DRUK*.001, DRUK*.XLS, Golan_*.csv, Zuriel_*.csv)")


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
