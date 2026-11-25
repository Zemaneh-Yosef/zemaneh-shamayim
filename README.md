# zemaneh-shamayim

**The zmanim sky server** (`zmanim-sky-server`): real-world sky and horizon data for zmanim, the times of
day that Jewish law ties to the Sun and stars.

Zmanim (Hebrew for "times") include sunrise and sunset, dawn, and nightfall, when the stars come out. They
are defined by what an observer actually sees: the Sun's edge clearing the real horizon, the stars coming
out over a real sky. Tables built on a flat horizon and a standard atmosphere get these times wrong by
minutes in hills, on coasts, and under city lights. This server supplies the real-world inputs that the
ROYZmanim website and app (the `royzmanimwebsite` repository) use to correct for that: the terrain on the
horizon, the air the sunlight bends through, and how bright the night sky is.

<details>
<summary>New to the terms? A short glossary</summary>

* **Visible sunrise / sunset**: when the Sun's edge is actually seen over the real terrain, rather than
  over an imagined flat sea-level horizon.
* **Refraction**: the air bends sunlight, so the Sun is seen above the horizon before it geometrically
  rises. How much depends on the air's temperature near the ground; a **temperature inversion** (cold air
  under warm, common at dawn) bends it much more.
* **Path profile**: the air's temperature at many heights, sampled at points along the line from the
  observer towards the rising or setting Sun. The app traces the light through it ("ray tracing").
* **GFS / GEFS**: NOAA's global weather forecast model, and its ensemble of ~30 slightly different runs.
* **Climatology**: typical values for a month and time of day, used for dates beyond the forecast.
* **DEM / bare earth**: a digital elevation model; a bare-earth one has buildings and trees removed, so
  only the ground counts as horizon.
* **Official area**: the municipality or neighbourhood a point is in. Everyone in it gets one time, the
  earliest sunrise seen anywhere in it (the Chai Tables' rule).
* **Birkat halevana**: the blessing over the new Moon, said when the Moon is actually seen; hence the
  moonrise / moonset horizons.
* **Nightfall by the stars**: when a given number of stars become visible, which depends on light
  pollution and haze.

</details>

## What it serves

| endpoint | what it returns |
|---|---|
| `/v1/path-profiles` | air-temperature profiles along the Sun's azimuth (NOAA GFS forecast or climatology) for ray-traced refraction, optionally with ensembles |
| `/v1/horizon` | the terrain horizon from bare-earth elevation data, for visible sunrise / sunset (and moonrise / moonset with `moon=1`), for a point or a whole neighbourhood; with each, the land the horizon lies on without the mountains (`baseCandidates`), for the elevated sunrise's base level |
| `/v1/terrain-version` | which terrain a horizon would be built from, without computing it: tells a client whether its stored horizon is current |
| `/v1/area` | the official municipality or neighbourhood containing a point |
| `/v1/light-pollution` | sky brightness from the World Atlas, brought forward to today, for when the stars become visible |
| `/v1/haze-calibration` | a factor correcting the CAMS haze forecast at a place, from NASA's AERONET measurements |
| `/v1/climate-envelope` | the range of near-surface air a place's 5° cell has in each month (NOAA reanalysis), for the website's guardrails |
| `/v1/terrain-tiles/{z}/{x}/{y}.png` | the same terrain as web-map tiles |
| `/v1/cesium-terrain/layer.json`, `/{z}/{x}/{y}.terrain` | the same terrain in Cesium's format (quantized-mesh, with surface normals), for the website's 3D horizon view |
| `/v1/status` | the live forecast cycle and how much climatology each region holds |

No API keys are needed: every source is free public data (NOAA, USGS, NRCan, IGN, FABDEM, Deltares, JAXA, ESA,
OpenStreetMap, the US Census, the World Atlas, AERONET). It's Python with a small set of dependencies and runs
on a modest VPS behind nginx. Some of that data is licensed for non-commercial use only; see
[Licences](#licences).

**Contents**

1. [Try it locally](#try-it-locally): a horizon in five minutes, no root, no weather data
2. [What's in the repository](#whats-in-the-repository)
3. [Setting up a server](#setting-up-a-server): install, regions, the services
4. [Running it](#running-it): what runs when, backfill, terrain data, upgrading, caching, resources,
   troubleshooting
5. [Development](#development): tests, caveats, licences and data credits

Deeper reading, in [`docs/`](docs/):

* [How it works](docs/how-it-works.md): the weather along the path, ensembles, terrain horizons, elevation
  data, official areas, light pollution, haze calibration, terrain for the 3D view
* [Validation](docs/validation.md): checking the computed sunrise against sunrises people timed

## Try it locally

The terrain horizon needs nothing but numpy and an internet connection: elevation tiles are downloaded on
first use.

```sh
git clone https://github.com/Zemaneh-Yosef/zemaneh-shamayim.git
cd zemaneh-shamayim
python3 -m venv venv
venv/bin/pip install numpy             # Windows: venv\Scripts\pip
cp config.example.json config.json     # then set "data_dir" to a folder you can write, e.g. "./data"
venv/bin/python serve.py               # listens on 127.0.0.1:8787
```

In another terminal:

```sh
curl 'http://127.0.0.1:8787/v1/status'
curl 'http://127.0.0.1:8787/v1/horizon?lat=31.7486&lon=35.2374'    # Armon HaNatziv, Jerusalem
```

The first horizon takes several seconds while the elevation tiles around the point (about nine, a few MB
each) download; after that it is cached. The answer, shortened:

```jsonc
{"mode": "point",
 "points": [{"lat": 31.7486, "lon": 35.2374, "ground": 755.4, "height": 757.1,
             "sunrise": [{"azimuthDeg": 59.11, "distanceKm": 61.086, "heightM": 968.2, "floorM": 0.0},
                         ...],                       // one entry per 0.1° of azimuth
             "sunset": [...], "wins": [...]}],
 "params": {"radiusKm": 0.0, "eyeM": 1.7, "minKm": 1.0, "maxKm": 150.0, ...},
 "terrain": {"layers": "7e51933456", "sources": [...], "algo": 4}}
```

Each entry is the highest point on the horizon in that direction: how far away it is and how high. The app
adds Earth curvature and the day's refraction. [How it works](docs/how-it-works.md#terrain-horizons-v1horizon)
explains the fields.

What a local run without more setup does *not* have:

* `/v1/path-profiles` answers `"no forecast or climatology data yet"` until `fetch_gfs.py` has run, which
  needs `eccodes` (its wheels ship the ecCodes library on Linux and macOS) and a region in `config.json`.
  `pip install -r requirements.txt` installs everything.
* Bare-earth merged tiles, fine terrain and light pollution need `rasterio`; without it the server logs a
  warning and uses the default elevation tiles.
* `/v1/area` and `area=auto` need `areas.sqlite`, built by `build_areas.py` (a few GB of downloads).

`make_horizon.py` computes the same horizon without the server:
`venv/bin/python make_horizon.py 31.7486 35.2374 --out armon.json`.

## What's in the repository

All the Python files sit at the top level and share `config.json` (or the file in `$ZMANIM_SKY_CONFIG`).

| file | role |
|---|---|
| `serve.py` | the HTTP API (all the `/v1/...` endpoints) |
| `common.py` | config, storage layout, sun position and geodesy shared by everything |
| **Weather** | |
| `fetch_gfs.py` | downloads each new GFS forecast cycle for your regions (run by a timer) |
| `fetch_gefs.py` | downloads the GEFS ensemble (optional, its own timer) |
| `backfill_gfs.py` | fills your own climatology from NOAA's archive of past GFS runs (by hand, once) |
| `build_prior.py` | builds the NOAA reanalysis climatology, the fallback before your own has data |
| `climatology.py` | typical profiles by month and time of day; builds a path profile from any source |
| `weather_guard.py` | rejects weather files with physically impossible values |
| `climate_envelope.py` | the range of near-surface air per 5° cell and month, for the website's check on forecasts (`/v1/climate-envelope`) |
| `suggest_region.py` | works out a region box from the places you serve and saves it to the config |
| **Terrain** | |
| `terrain.py` | the horizon computation: elevation tiles, lines of sight, vantage-point search |
| `dem_layers.py` | merged bare-earth tiles (`terrain.layers`) and fine terrain (`terrain.fine`); `build`, `info`, `test-fine` |
| `make_horizon.py` | `/v1/horizon` from the command line |
| `terrain_tiles.py`, `cesium_terrain.py` | the terrain as web-map tiles and as Cesium quantized-mesh, for the 3D view |
| **Areas and the night sky** | |
| `areas.py`, `build_areas.py` | official areas: the lookup, and building `areas.sqlite` for the whole world |
| `light_pollution.py`, `build_sky_measurements.py` | sky brightness from the World Atlas, and Globe at Night's meter readings |
| `haze_calibration.py` | the CAMS haze factor from AERONET stations |
| `cdn.py` | caching headers and Cloudflare purges |
| **Validation only** | |
| `era5_profiles.py`, `sonde_profiles.py`, `observations.py`, `ims_dawn.py` | past air (ERA5, radiosondes, IMS stations) and observed sunrises, for [Validation](docs/validation.md) |
| **Folders** | |
| `prebuild.py` | terrain and horizons ahead of requests ([Building ahead](#building-ahead)) |
| `deploy/` | systemd units and timers, the nginx location, `prebuild.sh` and its lists of places and regions |
| `sources/` | GDAL descriptions of remote elevation sources (IGN's WMS) |
| `tests/` | the test suite ([Tests](#tests)) |
| `docs/` | [How it works](docs/how-it-works.md) and [Validation](docs/validation.md) |

## Setting up a server

Commands below run on the server from `/opt/zmanim-sky-server` as the `zmanim-sky` user. The scripts read
the `config.json` next to them (or the file in `$ZMANIM_SKY_CONFIG`). The settings named below
(`terrain.answer_within_s` and the like) are keys in [`config.example.json`](config.example.json); one left
out of `config.json` takes its default.

### Install (Debian/Ubuntu)

From a clone of this repository:

```sh
sudo useradd --system --home /opt/zmanim-sky-server zmanim-sky
sudo mkdir -p /opt/zmanim-sky-server /var/lib/zmanim-sky
sudo rsync -a --exclude .git ./ /opt/zmanim-sky-server/
sudo chown -R zmanim-sky: /opt/zmanim-sky-server /var/lib/zmanim-sky
cd /opt/zmanim-sky-server
sudo -u zmanim-sky python3 -m venv venv
sudo -u zmanim-sky venv/bin/pip install -r requirements.txt
sudo -u zmanim-sky cp config.example.json config.json   # edit: contact e-mail, regions
```

### Regions

Weather data is fetched for regions: each one lat/lon box (west < east, -180..180; no crossing the 180°
meridian), fetched separately. A request is served by the first region that contains the observer.
Profiles reach 400 km along the sunrise/sunset direction, so a box needs that margin east and west (about
4.7° of longitude at 40°N, 6.3° at 55°N) and 2–2.5° north and south. Paths that leave the box are cut at
its edge and lose accuracy rather than failing. (Terrain, areas and light pollution work anywhere; only
the weather needs a region.)

Let the helper compute the box from the places you serve (a few outlying ones are enough) and save
it into config.json. You never enter paths or azimuths: the API computes those per request.

```sh
sudo -u zmanim-sky venv/bin/python suggest_region.py israel 31.78,35.22 32.08,34.78 29.56,34.95 32.97,35.50 --save
sudo -u zmanim-sky venv/bin/python suggest_region.py france 51.09,2.55 48.38,-4.79 43.39,-1.78 42.43,3.17 43.70,7.27 48.97,8.23 41.39,9.16 --save
sudo -u zmanim-sky venv/bin/python suggest_region.py --list          # what is configured
sudo -u zmanim-sky venv/bin/python suggest_region.py --remove conus  # drop one
```

Or by name. It tries OpenStreetMap's Nominatim (free, no key), then GeoNames (if `geonames_username`
is set in config.json; free account with web services enabled), then a country table
([sandstrom/country-bounding-boxes](https://github.com/sandstrom/country-bounding-boxes); countries only, by name or ISO code such as `IL`).
`--source nominatim|geonames|countries` picks one. It shows which place and source matched, so check
that "Georgia" is the one you meant. A country with territories elsewhere (France) spans the globe: give
points instead, as above.

```sh
sudo -u zmanim-sky venv/bin/python suggest_region.py Israel --save              # saved as "israel"
sudo -u zmanim-sky venv/bin/python suggest_region.py "New York City" --save     # saved as "new-york-city"
sudo -u zmanim-sky venv/bin/python suggest_region.py tristate "New York City" Philadelphia --save
```

Without `--save` it only prints the box. Several small boxes cost less than one huge one: save far-apart
places as separate regions (the helper warns when a box gets very large). Data © OpenStreetMap
contributors; the lookup sends one request per place, within Nominatim's usage policy. Changing regions takes
effect on the next fetcher run: it fetches the stored cycle again, keeping the regions it already holds
unchanged and downloading only new ones, and the API switches over when that finishes.

### The services

A first run by hand shows it working (10–20 minutes for a full cycle of the default regions):

```sh
sudo -u zmanim-sky venv/bin/python fetch_gfs.py
```

Then the services:

```sh
sudo cp deploy/zmanim-sky-fetch.service deploy/zmanim-sky-fetch.timer deploy/zmanim-sky-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zmanim-sky-fetch.timer zmanim-sky-api.service
# with "ensemble": {"enabled": true} in config.json (see docs/how-it-works.md, "Ensembles"):
sudo cp deploy/zmanim-sky-gefs.service deploy/zmanim-sky-gefs.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now zmanim-sky-gefs.timer
curl http://127.0.0.1:8787/v1/status
```

Expose it through your web server for HTTPS (`deploy/nginx-location.conf`; it allows 120 s per request).
The API sets `Access-Control-Allow-Origin` from `api.allow_origin` in the config (default `*`). Behind
Cloudflare, add the Cache Rule in [Cloudflare in front](#cloudflare-in-front): without it nothing but the
terrain files is cached.

Then, as you need them:

* your own climatology from past GFS runs: [Backfilling your own climatology](#backfilling-your-own-climatology)
* bare-earth terrain and fine terrain near the observer: [Terrain data](#terrain-data)
* official areas for `area=auto`: [Official areas](docs/how-it-works.md#official-areas-areaauto) (setup: `build_areas.py`)
* the haze factor: [Haze calibration](docs/how-it-works.md#haze-calibration-for-nightfall-by-the-stars) (setup: `haze_calibration.py`)

## Running it

### What runs when

| what | when | log |
|---|---|---|
| `zmanim-sky-api` (`serve.py`) | always | `journalctl -u zmanim-sky-api` |
| `fetch_gfs.py` | hourly at :17 (timer); fetches a new GFS cycle when there is one (00Z, 12Z), then adds it to the climatology | `journalctl -u zmanim-sky-fetch` |
| `fetch_gefs.py` | hourly at :40 (timer), only with `ensemble.enabled` | `journalctl -u zmanim-sky-gefs` |
| `build_prior.py` | by the fetcher, when the NOAA reanalysis climatology is missing or its 10-year window moved (each March) | the fetcher's |
| `climate_envelope.py` | by the fetcher, once (when `<data_dir>/climate-envelope.npz` is missing) | the fetcher's |
| `backfill_gfs.py` | by hand, once (and again after upgrades that add climatology fields) | its own (`nohup`) |
| `deploy/prebuild.sh` (`prebuild.py`) | by hand, after every deploy (and to prepare terrain ahead of requests) | its own (`nohup`) |
| `build_areas.py`, `haze_calibration.py` | by hand, about once a year | its own |

Only one `fetch_gfs.py` (and one `fetch_gefs.py`) runs at a time: one started by hand while the timer's is
running says so and exits. Everything else can run alongside the fetcher.

### Backfilling your own climatology

After the first fetcher run, `backfill_gfs.py` adds past GFS runs from NOAA's archive on AWS Open Data
(`s3://noaa-gfs-bdp-pds`, from 2021; no key or account), so every month of a yearly calendar gets
GFS-detail typical profiles averaged over recent years, instead of the NOAA fallback until the server has
run for a year:

```sh
sudo -u zmanim-sky venv/bin/python backfill_gfs.py --dry-run
sudo -u zmanim-sky nohup venv/bin/python backfill_gfs.py > backfill.log 2>&1 &
tail -n 5 backfill.log                      # "... added (37/1054); 1.4 GB so far, 3.2 MB/s, about 11.5 h left"
```

The archive cannot cut out a region, so only the needed fields of each hour are downloaded for the whole
globe (byte ranges from the `.idx` files, ~20–25 MB per hour), decoded, cropped to your regions and
deleted. It samples one day in `--every` (default 4) and, on each, one hour in each 3-hour slot (hours 1,
4, 7, 10 of the 00Z and 12Z runs); each counts as a full live day. From 2021 that is roughly 80–100 GB of
download in total, over several hours to a day or two depending on bandwidth, with negligible disk use.
`--start` / `--end` limit the range (e.g. `--start 2023-01-01` for about half). The sampled days are a
fixed calendar (every 4th day counted from 2021-01-01), so it can be stopped, restarted, or run again
later to catch up (e.g. from a weekly timer) and only fetches days it hasn't added yet; it runs safely
alongside the fetcher, and runs already in the climatology are never downloaded or counted again. A
damaged file in the archive (an `.idx` out of step with its file) is logged and skipped. Since the
server's own days count one for one and the backfill samples, the average leans towards the most recent
years. The last line is `done: N runs added`.

The 80 m / 100 m air and the ensembles' `gfs-moments` keep their own lists of runs, so after an upgrade that
adds them a backfill re-run fills them for the runs you already have, downloading only those fields (~2 MB
and ~8 MB per forecast hour; together about 40 GB from 2021). GFS has the 80 m / 100 m air from v16
(22 March 2021); earlier runs are noted in that store without data. A new region needs one fetcher run
first (it reports "no stored GFS data for region …" otherwise), then a backfill re-run fills it alone.

### Terrain data

Horizons work from the default elevation tiles with no setup. For bare earth (no buildings or trees) and
finer data near the observer, see [Elevation data](docs/how-it-works.md#elevation-data) for what each source is; the setup:

```sh
sudo -u zmanim-sky venv/bin/pip install rasterio      # GDAL included; needed to BUILD merged tiles and for /v1/light-pollution
```

**FABDEM** (bare earth worldwide, 1″) needs no download: the example config reads each 1×1° tile straight
out of Bristol's 10×10° zips over HTTP (`/vsizip//vsicurl/…`, only that tile's ~26 MB, when its merged tile is
built), so nothing but the merged tiles you use is kept. Where FABDEM has no data (north of 80°N) or Bristol's
server can't be reached, the tile is built from ALOS AW3D30 outside forest (ESA WorldCover trees) and the default
tiles (SRTM) in forest instead; a tile built during an outage is marked provisional and rebuilt on first use an
hour later, until FABDEM answers ([Bare-earth layers](docs/how-it-works.md#bare-earth-layers-terrainlayers-dem_layerspy)).

<details>
<summary>Optional: read FABDEM from local files instead of Bristol's server</summary>

Download the zips from https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn and unzip the `.tif`
files into a folder, then set the source's path to e.g. `/var/lib/zmanim-sky/fabdem/{name}_FABDEM_V1-2.tif`:

```sh
mkdir -p ~/fabdem-dl && cd ~/fabdem-dl
wget -c "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/N40E000-N50E010_FABDEM_V1-2.zip"   # one per zip
for f in *.zip; do unzip -tq "$f"; done
for f in *.zip; do sudo unzip -j -o "$f" '*.tif' -d /var/lib/zmanim-sky/fabdem/; done
sudo chown -R zmanim-sky: /var/lib/zmanim-sky/fabdem && cd ~ && rm -rf ~/fabdem-dl
```

</details>

<details>
<summary>Optional: DeltaDTM on low coasts (local mirror)</summary>

**DeltaDTM v1.1** (Deltares; CC BY 4.0, no key) is a bare-earth DTM of coastal land up to 30 m, 1×1° tiles in
EGM2008. Below 20 m it is closer to lidar than FABDEM (mean error 0.86 against 1.36 m on 71k points in 8 coastal
cities; [why](docs/how-it-works.md#bare-earth-layers-terrainlayers-dem_layerspy)), so the example config has it
(`deltadtm-v1-1`) just before FABDEM with `"max_valid": 20`: DeltaDTM writes 30.0 wherever the ground is above
its 30 m clip, and without `max_valid` that would flatten the hills behind every coast to 30 m. It is
`"enabled": false` until you have the files; a tile with no DeltaDTM file is simply left to the next source.

It can't be read over HTTP as FABDEM is: the tiles are inside per-continent zips on 4TU.ResearchData
(https://data.4tu.nl/datasets/1da2e70f-6c4d-4b03-86bd-b53e789cc629), whose file URLs have no extension (GDAL
won't range-read them under `CPL_VSIL_CURL_ALLOWED_EXTENSIONS`), and 4TU blocks heavy downloaders (it blocked a
server after ~30 GB). So mirror it once, gently: `fetch_deltadtm.py` reads only the wanted tiles out of the zips
with HTTP range requests, one connection at a time (never run several at once), CRC-checks them and resumes when
run again. By default it skips the 1,512 North American tiles USGS 3DEP 1″ and NRCan MRDEM already cover entirely
(`sources/deltadtm-na-covered.txt`: every bit of land in the tile, widened ~2 km, is the US with a USGS 1″ tile
there, or Canada), which come first in the layers so DeltaDTM would never be read there. The rest of the world
is ~5,900 tiles, ~17 GB:

```sh
sudo -u zmanim-sky nohup venv/bin/python fetch_deltadtm.py /var/lib/zmanim-sky/deltadtm-v1-1 > deltadtm.log 2>&1 &
sudo -u zmanim-sky venv/bin/python fetch_deltadtm.py /var/lib/zmanim-sky/deltadtm-v1-1 --continent Europe   # or one zip
```

Then set `"enabled": true` on `deltadtm-v1-1`. (A mirror rewritten with everything at or above 20 m set to
nodata, as the production server's `deltadtm-v1-1-below20` is, is equivalent to `"max_valid": 20`.)

**Rebuilding after turning it on.** The new source changes the layers' signature, so a new tile folder starts
and every horizon is recomputed. Only tiles where DeltaDTM has data need building again: a tile without DeltaDTM
data built afresh is bit-identical to the old one, so hard-link those (and their `.sea` / `.plain` markers and
`.rev`) from the old signature folder into the new one (same file system) instead of rebuilding them:

```sh
cd /var/lib/zmanim-sky/dem-layers
OLD=<old signature>; NEW=<new signature>        # the folder names; NEW = dem_layers.signature() of the new config
sudo -u zmanim-sky mkdir -p $NEW
for t in $(ls $OLD | grep -E '^[NS][0-9]{2}[EW][0-9]{3}\.' | cut -d. -f1 | sort -u); do
  [ -e /var/lib/zmanim-sky/deltadtm-v1-1/DeltaDTM_v1_1_$t.tif ] && continue     # DeltaDTM there: rebuild
  [ -e $OLD/$t.provisional ] && continue                                        # provisional: rebuild
  for x in npy json sea plain rev; do [ -e $OLD/$t.$x ] && sudo ln $OLD/$t.$x $NEW/$t.$x; done
done
```

Then build the rest (`dem_layers.py build`, below) and restart the API.

</details>

<details>
<summary>Optional: Chaim Keller's 10 m Jerusalem DTM</summary>

For Jerusalem and its surroundings, the bare-earth DTM in Chaim Keller's software
([chaimkeller/software](https://github.com/chaimkeller/software); downloads at
https://www.chaitables.com/download_eng.php) is much closer to the ground than FABDEM (against 76k ICESat-2
ground segments: median −0.27 m, NMAD 1.6 m, against FABDEM's −1.16 m and 3.1 m). Converted to a GeoTIFF in
the Israeli Cassini grid (EPSG:28191, nodata outside its coverage), put it at
`/var/lib/zmanim-sky/israel-dtm/jerusalem_dtm_10m.tif` and set `"enabled": true` on `jerusalem-dtm-10m` in
**both** `terrain.layers` (after NRCan) and `terrain.fine` (the last fine source): the 1″ tiles and the fine
terrain near the observer then both use it.

</details>

**Merged 1″ tiles** are built when a request first needs them (a 150 km horizon touches ~9 tiles, so that
first request can take minutes); build the cities you serve ahead (`--horizon`: only the tiles its rays cross,
in the year's sun and moon directions; `--radius-km` for a city's own extent; without terrain.layers sources
`build` downloads the plain 1″ tiles instead):

```sh
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 40.609283058016736 -73.96828881865329   # New York: tiles for a 150 km horizon
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 34.09 -118.40 --horizon --radius-km 20   # Los Angeles: those its rays cross
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 31.7683 35.2137    # Jerusalem
sudo -u zmanim-sky venv/bin/python dem_layers.py info N40W074                      # which source filled what
sudo -u zmanim-sky venv/bin/python dem_layers.py slim      # tiles built before .plain markers: copies of the plain tile -> markers
```

Adding a source's files later (FABDEM for a new country, where the tiles were built from the default tiles
until then) doesn't change tiles already built. Rebuild those, then restart the API (it keeps tiles in
memory); only horizons reaching a tile whose heights changed are recomputed and re-fetched by the app:

```sh
sudo -u zmanim-sky venv/bin/python dem_layers.py build --bbox 41 -6 52 10 --force --existing   # France's built tiles
sudo systemctl restart zmanim-sky-api
```

**Fine terrain** near the observer (US, Canada, France) is off until `config.json` has `terrain.fine`
sources. Try a source at a place first (nothing is kept), then build the places you serve, then turn it on:

```sh
sudo -u zmanim-sky venv/bin/python dem_layers.py test-fine 48.8566 2.3522 --example    # Paris, with the example's sources
sudo -u zmanim-sky nohup deploy/prebuild.sh --fine --example > prebuild.log 2>&1 &   # the main communities (~750 chunks, ~3.5 GB)
sudo -u zmanim-sky venv/bin/python dem_layers.py build-fine --around 48.8566 2.3522 --km 25 --example   # or one place
sudo cp config.json config.json.bak
sudo -u zmanim-sky venv/bin/python -c "
import json
c = json.load(open('config.json')); ex = json.load(open('config.example.json'))
c.setdefault('terrain', {})['fine'] = ex['terrain']['fine']
json.dump(c, open('config.json', 'w'), indent=2, ensure_ascii=False)"
sudo systemctl restart zmanim-sky-api
```

* **`test-fine`** builds that 0.1° chunk in a temporary folder, says which source filled it and how long it
  took, and compares it with the 1″ data your server uses (a median difference over 3 m means a datum or
  units problem, or the wrong layer).
* **Why build ahead.** Chunks are otherwise built when a horizon first needs them, one at a time: ~9 for a
  point (the 10 km around it), more for a city's area, which can take minutes. Such a request is answered
  after `terrain.answer_within_s` (45 s) with 503 `{"computing": true, "etaS", "retryAfterS"}` while the
  server carries on (the website asks again by itself, and the calendar shows its times without the horizon
  meanwhile).
* **`build-fine`** skips chunks already built and lists failed ones (run it again). `--km` is the radius to
  cover: the area's own radius plus `terrain.fine.max_km` (10). `--example` uses the example config's
  sources before `config.json` has them; the chunks go where the server will look once it has the same ones.
* **`deploy/prebuild.sh --fine`** runs `build-fine` for the listed communities (`deploy/prebuild-places.txt`:
  New York, Monsey, Lakewood, Los Angeles, Baltimore, Chicago, Toronto, Paris, ...; `--only NAME` for one; edit
  the list for your own places), each over its official area where that is wider than its listed radius. With
  `--tiles` it also prepares each place's 1″ tiles (`dem_layers.py build --horizon`): about 330 for the whole
  list, many of them sea; up to ~15 GB as merged tiles, so check the disk first or use `--only`. See
  [Building ahead](#building-ahead) for the rest of what it does.

Visitors get the new horizons at their app's next daily terrain check, only where fine data exists.

#### The base level

Each horizon also carries the land its sunrise and sunset lie on with the mountains taken out
([`terrain_base.py`](terrain_base.py)): the website measures the observer's height for elevated sunrise and sunset
from that land instead of from the sea. The server samples the bare-earth heights on a 2 km grid, raises the
sea floor to sea level (except in the listed depressions, whose land below sea level is real), fills dips narrower than `fill_km` (5), removes hills narrower than `width_km` (80),
and sends, for every half degree of the sunrise and sunset directions, the few points of that surface that can be
the horizon in some air (`baseCandidates`, `[distance km, height m]`), each spot from its own coordinates.

The grid's cells lie on one world lattice (rows every 2 km of latitude; columns every 2 km of longitude at the
middle of each whole degree of latitude), and each surface reaches far enough that no point of a 150 km view
feels its edge. So a spot's candidates depend on that spot alone, as its sunrise and sunset do, and not on the
area it was asked from: a grid centred on the area moved a spot's times by up to ~15 s with where its cells fell.
Spots near each other share one surface (around the lattice node every 20 km nearest them, `base.anchors`:
`[band, row, column, weight]`). Within 0.05° of a whole degree of latitude, where the column spacing changes, the
surfaces of both bands are mixed by distance (half each on the line), so the times change smoothly across it:
on one grid alone they jumped by up to 34 s there (Philadelphia, 40° N). The website's calculator picks the horizon among them in
the day's air and turns its dip into the base. Each point says how it went in `base`: `status` `ok`, or `none` /
`no-bare-earth` with a `reason`, and `flagSeconds` / `flagged` where widening the removal to `check_km` (120) would
still move the times by `flag_s` (30) seconds or more; such a place is decided by hand.

Set in `terrain.base`:

* `width_km`, `fill_km`, `check_km`, `flag_s`: as above.
* `max_non_bare` (0.01): the share of a tile's land that may come from something other than bare earth (the skadi
  fallback, or a layer source marked `"bare_earth": false`, as ALOS is) before the base is withheld there. A
  surface model raises the land under cities by the buildings, which can be the whole adjustment, so the website
  keeps its last good base instead.
* `overrides`: `[{"lat", "lon", "radius_km", "width_km": 120}]` for a flagged place given another width, or
  `{..., "none": true}` for no base.
* `depressions`: `[{"name", "bbox": [south, west, north, east]}]`, land below sea level that keeps its height
  (default: the Jordan rift, Death Valley, the Salton Sea and Imperial Valley, Qattara, Turpan; each box holds no
  sea coast). Everywhere else heights below 0 are the tiles' sea floor and become sea level. A list rather than
  a test of whether the low land reaches the sea, because such a test depends on how far the grid reaches.
* `enabled: false` turns it off.

The settings are part of the stored horizons' key and of `/v1/terrain-version` (`base`), so changing them
recomputes. A surface reads about 30 1° tiles and takes 5-10 s to build. It is kept on disk
(`<data_dir>/base-surfaces`, ~50-100 KB each) and rebuilt only when a merged tile under it is first built or
rebuilt. `deploy/prebuild.sh` builds them ahead of time ([Building ahead](#building-ahead)): all of Israel
(`deploy/prebuild-regions.txt`; ~260 surfaces, ~30-40 min, ~20 MB) and, through the horizons it asks for, every
place visitors use; or one place with `venv/bin/python terrain_base.py build --around LAT LON --radius-km R`
(`--bbox S,W,N,E` for a region, `--count` to only count). A surface reads the 1″ tiles alone, not the fine
terrain, out to ~245 km (150 km of view plus the 80 km it removes), so building one far from the places
already served downloads tiles past their horizons' 150 km. Where the tiles can't be read, the horizon is still
served, with `base.status` `none`.

#### Building ahead

`deploy/prebuild.sh` (`prebuild.py`; `--help` for all of it) prepares terrain and horizons before visitors
ask, so they don't wait for them. Run it after every deploy that changes the horizons' cache key (a new
algorithm version, terrain sources or base settings: every horizon is then computed again on its first request),
and now and then to cover new places:

```sh
sudo -u zmanim-sky nohup deploy/prebuild.sh > prebuild.log 2>&1 &                  # what a deploy needs
sudo -u zmanim-sky nohup deploy/prebuild.sh --region-areas > prebuild.log 2>&1 &   # and every area in Israel
sudo -u zmanim-sky deploy/prebuild.sh --dry-run                                    # only say what it would do
```

* **Where**: the *demand*, every horizon in the API's cache (`<data_dir>/horizons`), so the places visitors
  actually asked for, anywhere; the *places*, `deploy/prebuild-places.txt`, the main communities, for a first
  visitor there; the *regions*, `deploy/prebuild-regions.txt`, whole regions (Israel).
* **What**, with no option: every horizon of the demand and of the places' official areas, asked of the running
  API `terrain.max_parallel` at a time (a 503 "computing" is asked again when it says, as the website does).
  The API builds what each needs, tiles, fine chunks and base surfaces, as for a request, and the answer is then
  in its cache; for a place visitors use, that alone does everything below. And the regions' base surfaces
  (`--regions`), for a first visit anywhere there.
* **After a deploy that changed the cache key**, the old files are still in `<data_dir>/horizons` under their old
  keys: they are the demand. Keep them until this has run once; the old ones can then be removed (they are no
  longer read).
* `--region-areas` also asks for the horizon of every official area in the regions (Israel: a few thousand, hours
  of CPU), one per area the city rule picks.
* For the listed places only: `--tiles` (1″ tiles), `--fine` (fine terrain, only where its sources have data:
  the US, Canada, France), `--base` (base surfaces) and `--3d` (the 3D view's tiles, 15-80 min a city, so never
  by default). Each covers the place's official area where that is wider than its listed radius.
* `--only NAME` limits it to the listed places and regions whose name has NAME (no demand); `--api URL` and
  `--parallel N` if the API isn't on `api.port` or should be asked more at a time. `deploy/prebuild-fine.sh`,
  the old name, still takes its old options (`--tiles`, `--tiles-only`, `--3d`, `--3d-only`, `--base`,
  `--base-only`) and passes them on.

### Upgrading

Run these from your clone of this repository (not from your home directory: the copy line copies the
folder you are in). `config.json`, `venv` and the data in `/var/lib/zmanim-sky` are kept, and new config
keys fall back to their defaults.

```sh
cd ~/zemaneh-shamayim                  # your clone of this repository
git pull
sudo systemctl stop zmanim-sky-fetch.timer zmanim-sky-api.service
sudo rsync -a --exclude config.json --exclude venv --exclude .git ./ /opt/zmanim-sky-server/
sudo chown -R zmanim-sky: /opt/zmanim-sky-server
cd /opt/zmanim-sky-server
sudo -u zmanim-sky venv/bin/pip install -r requirements.txt
diff <(sudo -u zmanim-sky python3 -c 'import json;print(*sorted(json.load(open("config.example.json"))),sep="\n")') \
     <(sudo -u zmanim-sky python3 -c 'import json;print(*sorted(json.load(open("config.json"))),sep="\n")')   # sections new in the example
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start zmanim-sky-fetch.timer zmanim-sky-api.service
curl http://127.0.0.1:8787/v1/status
```

A running backfill or fetch keeps its old code until it ends. If an upgrade adds fields to the GFS fetch,
the next fetcher run sees that the stored cycle lacks them and downloads it again; until then the API
serves the old cycle. Fields added to the climatology have their own list of runs: run the backfill again
to fill them for past runs. Opt-in features (`terrain.fine`, the GEFS ensemble, `follow_terrain`) stay as
your `config.json` has them.

### Cloudflare in front

Optional, but recommended: a horizon costs seconds to minutes to compute and stays good for a month.

<details>
<summary>What the caching headers say</summary>

Each answer says how long it stays good, twice (`cdn.py`): `CDN-Cache-Control` with its full lifetime (a
horizon 30 days, a forecast 30 minutes) for Cloudflare, which can be purged, and `Cache-Control` with at
most `cdn.browser_max_age_s` (a day) for browsers, which can't. Both carry `stale-if-error`
(`cdn.stale_if_error_s`, a day): while the server is down, Cloudflare keeps serving the last good answer.
Answers not to keep (`/v1/status`, a horizon still computing) are `no-store`; a horizon built while a
fine-terrain chunk could not be (it is built again on a later request) is kept 10 minutes.

</details>

Cloudflare caches by file extension unless told otherwise, so `/v1/horizon?…` and the rest pass through
uncached. One Cache Rule (Caching → Cache Rules) fixes that:

* **When**: hostname equals your API's host and URI path starts with the API's path (e.g. `/selfhost/v1/`)
* **Then**: Eligible for cache; Edge TTL "Use cache-control header if present, bypass cache if not";
  Browser TTL "Respect origin"; the default cache key (with the query string)

Also turn on Tiered Cache (Caching → Tiered Cache): a miss at one Cloudflare location is then filled from
another before it reaches the server. Check with `curl -sI '<url>' | grep -i cf-cache-status`: `HIT` on the
second request, not `DYNAMIC`.

**Purging.** Answers carry a `Cache-Tag` (Cloudflare strips it): `zsky-terrain` (horizons, terrain versions,
terrain tiles, Cesium terrain), `zsky-areas` (`/v1/area` and `area=auto` answers), `zsky-weather`,
`zsky-lightpol`, `zsky-haze`, `zsky-climate`, and `zsky` on all of them. The API purges by itself: at start and every
`cdn.check_every_s` (10 minutes) it compares what the terrain and areas are made from with
`<data_dir>/cdn-state.json`, and purges `zsky-terrain` when the `terrain.layers` or `terrain.fine` sources, the
horizon algorithm (an upgrade) or a merged tile's revision (`dem_layers.py build --force`) changed, and
`zsky-areas` when `areas.sqlite` was rebuilt. A refused purge is tried again at the next check. For that, set
`cdn.cloudflare_zone_id` (the zone's Overview page) and a token with the Zone → Cache Purge permission, in
`cdn.cloudflare_api_token` or `Environment=CLOUDFLARE_API_TOKEN=…` in a `systemctl edit zmanim-sky-api`
override. Without them a change is only logged ("CDN purge needed"); purge in the dashboard then. By hand:

```sh
sudo -u zmanim-sky venv/bin/python cdn.py purge terrain        # or areas, weather, lightpol, haze, climate; all
sudo -u zmanim-sky venv/bin/python cdn.py purge --everything   # the whole zone, website included
sudo -u zmanim-sky venv/bin/python cdn.py status
```

Browsers keep what they have for up to a day after a purge. The apps' stored horizons don't depend on
either: they ask `/v1/terrain-version`.

### Resources

A modest VPS is enough: the server mostly needs disk and bandwidth, not CPU.

<details>
<summary>Download, disk and CPU for the default config (continental US + Israel, 2 cycles kept)</summary>

* Download: roughly 200–400 MB per cycle, about 280 requests spaced 2 s apart, twice a day. France's box
  adds ~0.1 GB per stored cycle.
* Disk: about 0.7 GB per stored cycle for the US box (float32 arrays), ~2 GB peak with two kept plus
  one being fetched. Fewer forecast hours (`forecast_hours.max`) or a smaller box reduce this.
* Your own climatology: 12 months x 8 slots of the same fields, about 330 MB for the US box, plus the
  80 m / 100 m store and `gfs-moments` (~670 MB for the US box, a few MB for Israel); fixed size, it does
  not grow over time. The NOAA climatology is a few MB per region.
* NOAA reanalysis build: about 6 GB download once (10 years, ~600 MB of disk at a time), then ~600 MB
  each March; ~250 MB of yearly summaries kept. Adding a region downloads nothing.
* Backfill: roughly 80–100 GB download from 2021 at the default spacing, once (+ ~40 GB for the 80 m /
  100 m air and `gfs-moments`); no lasting disk use beyond the climatology itself.
* Terrain: default tiles 2–8 MB each; merged 1″ tiles ~52 MB per land tile; fine terrain ~4.7 MB per 0.1°
  chunk (`deploy/prebuild-places.txt`'s list: ~3.5 GB).
* GEFS (optional): ~1250 requests per region per run, ~0.7 GB stored for the US box.
* CPU / RAM: negligible; arrays are memory-mapped. While first horizons are computed, up to
  `terrain.max_parallel` worker processes use a core and ~0.3–0.5 GB each (their own tiles in memory).

</details>

### Troubleshooting

* **"another fetch_gfs.py is running"**: the timer's fetch was already at work; let it finish.
* **"no stored GFS data for region X with the current configuration"** (backfill): the region was added
  after the last fetch; run `fetch_gfs.py` once, then the backfill.
* **A cycle "not complete on the server yet"**: NOAA publishes a cycle over a few hours; the timer tries
  again within the hour.
* **`dem_layers.py info` shows `skadi (fallback)` where you have FABDEM files**: the tile was built before
  the files arrived; rebuild it (`build --force --existing`, above) and restart the API.
* **"leaving out fabdem-v1-2 for now"** and `"provisional_without"` in `dem_layers.py info`: Bristol's server
  (or OpenTopography's, for ALOS) didn't answer while the tile was built. The tile uses the next sources
  meanwhile and is rebuilt on its first use after an hour (`build --bbox … --force` retries now).
* **GDAL's "HTTP response code: 404" while building terrain**: a per-tile source (USGS) has no file for that
  place; expected outside its country.
* **A first horizon request times out**: its fine chunks or merged tiles were being built; the server
  carries on and the next request is quick. Build the place ahead (`build-fine`, `build`).
* **A fine source fails** (`not built (...)` in the log, `-partial` in a horizon's version): the 1″ data is
  used meanwhile and the chunk is retried after an hour; `test-fine` at that place shows GDAL's reason.

## Development

### Tests

The tests need no network; each is a plain script. Those marked (rasterio) need it installed. From a checkout,
one test or all of them:

```sh
python3 tests/test_e2e.py
for t in tests/test_*.py; do python3 "$t" > /dev/null 2>&1 || echo "FAILED: $t"; done
```

<details>
<summary>What each test checks</summary>

| test | checks |
|---|---|
| `test_e2e.py` | the forecast pipeline on synthetic GRIB files shaped like the grib filter's output, the API, and a region added to a stored cycle (the others kept, not fetched again) |
| `test_backfill.py` | the backfill against a local fake of the AWS archive (byte ranges, the pre-2021 layout, cropping, weighting, no double counting with the fetcher) |
| `test_climatology.py` | the NOAA climatology build on synthetic NetCDF files laid out like NOAA's (the 1991–2020 means and the rolling window of yearly files, including an unfinished year, a leap year and the stored yearly summaries), your own archive, and date ranges across all three sources |
| `test_follow_terrain.py` | columns moved onto the real ground (heights, pressures, depressions and offshore depths, the fade) |
| `test_profile_levels.py` | every served column rises with falling pressure: a pressure level within 15 m of the 2 m air or of a fixed-height level (80 m, 100 m) left out, as a climatology's separately averaged fields can put the two out of order |
| `test_ensemble.py` | the representative variations (weights, mean, the modes' spread), the `gfs-moments` store, and GEFS members from `fetch_gefs.py` on synthetic files |
| `test_weather_guard.py` | the guardrails: record extremes kept, impossible values (units mixed up, a land fraction of 7, levels out of order) refused, a refused run leaving the previous one serving, a run that jumps away from the serving one held back and an ordinary update published |
| `test_dem_layers.py` (rasterio, pyproj) | merged bare-earth tiles (priority per pixel, reprojection from the Israeli grid, open sea, aborting on unreadable sources), tile revisions on rebuilds, `max_valid` (a DeltaDTM-like 30 m clip plateau falls through to FABDEM, with nothing blended in at the cut), and a missing local mirror skipped |
| `test_dem_fallback.py` (rasterio) | FABDEM read from a remote zip (a local HTTP server stands in for Bristol's), a missing file told apart from an outage, provisional tiles kept and retried after the hour (also when read through terrain.Tiles, as the server does), ALOS masked by land cover (forest falls through to the plain tiles), `.plain` markers where no source has data, and `slim` |
| `test_fine_terrain.py` (rasterio) | near-range fine terrain on a synthetic 1/3″ source (a narrow ridge the 1″ tiles miss, the max_km cut-off, chunks and markers), per-place versions and cache keys |
| `test_sun_range.py` | sunrise / sunset ranges widened past high horizons only as far as the terrain hides the solstice Sun (both hemispheres, the tropics, a ridge that ends), and a point's horizon and wins over them; a crest between two distance steps found by the refinement |
| `test_terrain_base.py` | the base level on a stub terrain: a ridge under the observer removed, a wide plateau kept as land, a narrow river filled, candidates every half degree, the flag, an override with no base, no candidates where the land isn't bare earth, and the world lattice (the same profile from neighbouring nodes; a spot's candidates the same whatever area it was asked from) |
| `test_terrain_alternates.py` | alternate horizon points on a stub terrain (a near ridge highest in standard refraction, a farther, higher one overtaking it in strong refraction) and `floorM` |
| `test_moon_horizon.py` | moon horizons on analytic terrain: the Sun's output unchanged by `moon=1`, every moonrise / moonset direction of the 18.6-year cycle covered, the percentile in an area, the cache key |
| `test_horizon_workers.py` | horizons computed in worker processes: real searches on a synthetic tile outside the API process, one computation per place when 8 ask at once (more than `api.max_threads`), cached answers quick while two vantage searches run, a killed worker reported once and the pool started anew, errors carried back, the search time learned from the worker |
| `test_horizon_pending.py` | a slow first horizon: answered 503 "computing" in time with an estimate from the work left (the tiles its rays cross), computed once in the background however often it is asked for, then served; the timings learned and kept; a failure reported once, then retried |
| `test_area_answer.py` | an area's horizon answer: the same bytes as merging the parsed answer, kept in memory, quick when asked again |
| `test_areas.py` | build_areas.py (whole countries, the municipal and finer geoBoundaries levels, the big sources streamed, `--only`, a failed source keeping a country's areas, a killed build resumed), the lookup by both rules, `area=auto` / `finest` (with and without `outline=1`), a rebuilt file swapped in |
| `test_light_pollution.py` (rasterio) | the light-pollution lookup on synthetic atlas tiles (pixel indices against the tiles' georeferencing, reads across tile edges, area masks and percentiles, LERC copies, downloads filling gaps, conversion on download, the HTTP endpoint) |
| `test_sky_measurements.py` | build_sky_measurements.py's filters on made-up Globe at Night rows, and the Moon |
| `test_climate_envelope.py` | the climate envelope on synthetic NOAA long-term means (each cell's range against the synthetic pattern, land carried to sea level, places without data, the endpoint's 200 / 400 / 404 / 503) |
| `test_haze_calibration.py` | the AERONET / CAMS calibration on a fake AERONET file and CAMS series (500 -> 550 nm, resuming, the factor's falloff, the endpoint) |
| `test_terrain_tiles.py` | `/v1/terrain-tiles` on a synthetic 1″ tile (heights at the pixel centres, the quarter-metre Paeth-filtered Terrarium encoding, the disk cache and its revisions, re-encoding the previous format, 404s with and without `pending`) |
| `test_cesium_terrain.py` | `/v1/cesium-terrain` on a synthetic 1″ tile, decoded independently: vertex heights, edges, winding, normals against the slope, `layer.json`, AWS's heights for coarse levels and missing data (kept apart until the server has the data), the disk cache, `build` ahead of time (each level within its reach, out to the earth's curve, with parents) |
| `test_cdn.py` | the caching headers (browsers a day, the CDN the full lifetime, stale-if-error, tags; a partial horizon 10 minutes), purge requests as Cloudflare's API takes them, and purging only what changed (a tile revision, a new areas.sqlite), a refused purge retried |
| `test_prebuild.py` | `prebuild.py`: real horizons computed by the API on a synthetic slope (a point, a height with its own eye, a radius, a small box, the moon's, an official area's) read back from its cache files and asked for again, each the same cache entry with nothing new computed (an area's box asked for as the area); a 503 asked again after its `retryAfterS`; one horizon per area the city rule picks in a region; and a dry run doing nothing |
| `test_era5.py` | ERA5 profiles for past dates on synthetic NetCDF files in both CDS layouts (what is requested, time interpolation, ground-following, the command line) |
| `test_sonde.py` | radiosonde profiles on a synthetic IGRA file (missing heights and values IGRA's quality control removed, the three versions, which sounding a date gets) |

`tests/make_synthetic.py` and `tests/make_synthetic_ncep.py` write the fake GRIB and NetCDF files the weather
tests read.

</details>

### Caveats

* The NOAA reanalysis reader was tested on files built to NOAA's documented layout only. `build_prior.py`
  logs each file's variables, units and a sanity summary (near-surface temperature range): check that log
  on the first run.
* `fetch_gefs.py` has run only against synthetic files; watch its first real runs (`journalctl -u zmanim-sky-gefs`).
* If NOAA changes the grib filter URLs, edit `grib_filter` in the config (see
  https://nomads.ncep.noaa.gov/ for the current ones). NOMADS throttles heavy users; keep
  `request_delay_s` at 1–2 s.
* GFS at 0.25° (~25 km) resolves the air over land vs. water along the path, not individual valleys
  or a few-km sea breeze.
* All the validation is in Jerusalem; sea paths (e.g. Brooklyn's sunrise over the Atlantic) and the
  fine terrain have not been checked against observed sunrises.

### Licences

Two of the data sources may be used for **non-commercial purposes only**:

* **FABDEM** (bare-earth elevation, used by the example config's `terrain.layers`): CC BY-NC-SA 4.0.
* **The World Atlas of Artificial Night Sky Brightness** (`/v1/light-pollution`): CC BY-NC 4.0.

A commercial deployment has to leave both out (remove FABDEM from `terrain.layers.sources`, and don't serve
`/v1/light-pollution`). The rest is open data under the licences credited below, most of which ask for
attribution.

<details>
<summary>Data credits</summary>

* **Weather:** GFS and GEFS: NOAA/NCEP. Reanalysis climatology: NCEP/NCAR Reanalysis 1, data provided by the
  NOAA PSL, Boulder, Colorado, USA, from their website at https://psl.noaa.gov.
* **Elevation:** NASA SRTM and USGS 3DEP (via the AWS Terrain Tiles open dataset). With `terrain.layers`:
  USGS 3DEP, Natural Resources Canada MRDEM (Open Government Licence - Canada), FABDEM V1-2 (Hawker et al.
  2022, University of Bristol; CC BY-NC-SA 4.0, contains modified Copernicus Service information), ALOS
  World 3D-30m (AW3D30, © JAXA; via OpenTopography) and ESA WorldCover 2021 (© ESA WorldCover project 2021 /
  Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium; CC BY 4.0). With
  `terrain.fine`: Natural Resources Canada HRDEM (Open Government Licence - Canada), USGS 3DEP 1/3″, and IGN
  RGE ALTI (Licence Ouverte Etalab 2.0; "IGN - RGE ALTI"). Where enabled: DeltaDTM v1.1 (Pronk, M. et al.
  2024, DeltaDTM: a global coastal digital terrain model, Scientific Data 11, 273; Deltares, via
  4TU.ResearchData; CC BY 4.0), and the Jerusalem 10 m DTM from Chaim Keller's software
  (https://www.chaitables.com, https://github.com/chaimkeller/software).
* **Light pollution:** Falchi, F. et al. (2016), Supplement to: The New World Atlas of Artificial Night Sky
  Brightness, GFZ Data Services, doi:10.5880/GFZ.1.4.2016.001 (CC BY-NC 4.0, non-commercial only), via the
  tiles in [astertaylor/halakhic_calc](https://github.com/astertaylor/halakhic_calc); growth of skyglow:
  Kyba, C. C. M. et al. (2023), Science 379, 265; average sky vs zenith: Bará, S. et al. (2022),
  arXiv:2202.07526; Ramon Crater readings: Israel Nature and Parks Authority, Ramon Crater International
  Dark Sky Park 2020 annual report; sky-meter readings: Globe at Night, NSF NOIRLab (CC BY 4.0).
* **Haze:** AERONET (NASA GSFC) and its station PIs; CAMS (Copernicus Atmosphere Monitoring Service) via
  Open-Meteo.
* **Place lookup:** © OpenStreetMap contributors (Nominatim), GeoNames; country boxes from
  [sandstrom/country-bounding-boxes](https://github.com/sandstrom/country-bounding-boxes).
* **Official areas:** NYC Department of City Planning (Neighborhood Tabulation Areas), Los Angeles Times
  "Mapping L.A.", US Census Bureau TIGER/Line, Israel Central Bureau of Statistics (statistical areas 2022),
  Israel Ministry of Interior boundaries via OpenStreetMap contributors (ODbL, extract by Geofabrik),
  Statistics Canada (2021 Census boundary files, Open Government Licence - Canada), the cities of Toronto,
  Ottawa, Hamilton, Calgary, Edmonton and Winnipeg, Halifax Regional Municipality and Ville de Montréal
  (CC BY 4.0) under their open-data licences, the cities of Chicago, Houston, Dallas, Phoenix, San Diego,
  Austin, Seattle and Baltimore, and geoBoundaries (CC BY 4.0 / per-country licences; see each area's
  `source`).
* **Radiosondes** (`sonde_profiles.py`): Durre, I. et al., Integrated Global Radiosonde Archive (IGRA) v2,
  NOAA NCEI.
* **Past air for checking against observations** (`era5_profiles.py`): Hersbach, H. et al. (2020), ERA5
  hourly data on pressure levels / single levels, Copernicus Climate Change Service (C3S) Climate Data
  Store.
* **Observed sunrises:** Rabbi Avraham Druk, Yisroel Golan and Rabbi Yosef Zuriel, via Chaim Keller's
  [chaimkeller/software](https://github.com/chaimkeller/software).

</details>
