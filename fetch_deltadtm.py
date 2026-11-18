#!/usr/bin/env python3
"""Mirror DeltaDTM v1.1 tiles (Deltares, CC BY 4.0) into one folder, for the `deltadtm-v1-1` layers source.

The tiles are inside per-continent zips on 4TU.ResearchData, which can't be read over HTTP by GDAL (the file
URLs have no extension) and which blocks heavy downloaders. This reads each wanted tile straight out of the
zips with HTTP range requests (no zip is kept on disk), ONE connection at a time, CRC-checked. Re-run it to
resume: tiles already there at the right size are skipped.

    python3 fetch_deltadtm.py /var/lib/zmanim-sky/deltadtm-v1-1                       # the world, less the tiles
                                                                                     # USGS 1" / NRCan cover
    python3 fetch_deltadtm.py DEST --continent Europe --only my-tiles.txt            # just these (N40W074 ...)

--skip (default sources/deltadtm-na-covered.txt) lists tiles not to fetch: the 1,513 North American tiles
whose land (Natural Earth countries, widened ~2 km) is all the US with a USGS 1" tile there, or Canada (NRCan
MRDEM covers all of it), where the layers never reach DeltaDTM. Needs only Python 3.9+.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "https://data.4tu.nl/file/1da2e70f-6c4d-4b03-86bd-b53e789cc629/"
ZIPS = {                                                   # 4TU file ids (dataset 1da2e70f-...)
    "Africa": "22ffa027-184b-4f67-9979-c182f3dfb1ab",
    "Antarctica": "ca957a40-34fa-41eb-b101-e45d1ccbd890",
    "Asia": "672eba4c-1334-44c6-8119-8879ded25912",
    "Europe": "cb0b8ee3-b018-4828-a74e-2fb05020b1b6",
    "North_America": "037664c6-1494-4889-9689-a56570728320",
    "Oceania": "de972de1-26bd-4303-afdf-21a90a232cff",
    "South_America": "db980f00-63cd-4a07-a4df-55ab06510594",
}


class HTTPRange(io.RawIOBase):
    """A remote file read with HTTP range requests, retried on network trouble."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        self.size = int(self._get(0, 0)[1]["Content-Range"].split("/")[1])

    def _get(self, a: int, b: int):
        for attempt in range(6):
            try:
                req = urllib.request.Request(self.url, headers={"Range": f"bytes={a}-{b}",
                                                                "User-Agent": "zmanim-sky-server"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read(), r.headers
            except Exception as e:
                if attempt == 5:
                    raise
                print(f"  network trouble ({e}); retrying in 30 s", flush=True)
                time.sleep(30)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        data = self._get(self.pos, self.pos + n - 1)[0]
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def tile_of(member: str) -> str:
    return os.path.basename(member).removeprefix("DeltaDTM_v1_1_").removesuffix(".tif")


def read_list(path) -> set[str]:
    return {line.split()[0] for line in open(path) if line.strip() and not line.startswith("#")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", help="folder for DeltaDTM_v1_1_<tile>.tif")
    ap.add_argument("--continent", action="append", choices=sorted(ZIPS), help="only these zips (repeatable)")
    ap.add_argument("--only", help="file listing the tiles to fetch (e.g. N40W074), one per line")
    ap.add_argument("--skip", default=str(HERE / "sources" / "deltadtm-na-covered.txt"),
                    help="file listing tiles not to fetch ('' for none)")
    a = ap.parse_args()
    only = read_list(a.only) if a.only else None
    skip = read_list(a.skip) if a.skip else set()
    os.makedirs(a.dest, exist_ok=True)
    t0, got, n_new = time.time(), 0, 0
    for name in a.continent or sorted(ZIPS):
        z = zipfile.ZipFile(io.BufferedReader(HTTPRange(BASE + ZIPS[name]), 8 << 20))
        members = [i for i in z.infolist() if i.filename.endswith(".tif") and tile_of(i.filename) not in skip
                   and (only is None or tile_of(i.filename) in only)]
        print(f"== {name}.zip: {len(members)} tiles", flush=True)
        for k, info in enumerate(members, 1):
            out = os.path.join(a.dest, os.path.basename(info.filename))
            if os.path.exists(out) and os.path.getsize(out) == info.file_size:
                continue
            with z.open(info) as src, open(out + ".part", "wb") as dst:   # BadZipFile on a CRC mismatch
                while chunk := src.read(4 << 20):
                    dst.write(chunk)
            os.replace(out + ".part", out)
            n_new += 1
            got += info.file_size
            if n_new % 25 == 0 or k == len(members):
                print(f"   {k}/{len(members)}; {got / 1e9:.2f} GB new, {got / (time.time() - t0) / 1e6:.1f} MB/s",
                      flush=True)
    n = len([f for f in os.listdir(a.dest) if f.endswith(".tif")])
    print(f"done: {n_new} tiles fetched, {n} in {a.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
