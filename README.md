# zmanim-sky-server

Real-world sky and horizon data for zmanim: refraction, terrain, and light pollution.

Zmanim are defined by what an observer sees: the Sun's edge clearing the real horizon, the stars
coming out over a real sky. Tables built on a flat horizon and a standard atmosphere get these times
wrong by minutes in hills, on coasts, and under city lights. This server supplies the real-world
inputs that the ROYZmanim app (royzmanimwebsite) uses to correct for that:

| endpoint | what it returns |
|---|---|
| `/v1/path-profiles` | air-temperature profiles along the Sun's azimuth (NOAA GFS forecast or climatology) for ray-traced refraction, optionally with ensembles |
| `/v1/horizon` | the terrain horizon from bare-earth elevation data, for visible sunrise / sunset (and moonrise / moonset with `moon=1`), for a point or a whole neighbourhood |
| `/v1/terrain-version` | which terrain a horizon would be built from, without computing it: tells a client whether its stored horizon is current |
| `/v1/area` | the official municipality or neighbourhood containing a point |
| `/v1/light-pollution` | sky brightness from the World Atlas, brought forward to today, for when the stars become visible |
| `/v1/haze-calibration` | a factor correcting the CAMS haze forecast at a place, from NASA's AERONET measurements |
| `/v1/terrain-tiles/{z}/{x}/{y}.png` | the same terrain as web-map tiles, for the website's 3D horizon view |
| `/v1/status` | the live forecast cycle and how much climatology each region holds |

No API keys are needed: every source is free public data (NOAA, USGS, NRCan, IGN, FABDEM, OpenStreetMap,
the US Census, the World Atlas, AERONET). It's Python with a small set of dependencies and runs on a modest
VPS behind nginx.

**Contents**

1. [Setting up](#1-setting-up): install, regions, the services
2. [Running it](#2-running-it): what runs when, backfill, terrain data, upgrading, resources, troubleshooting
3. [How it works](#3-how-it-works): weather along the path, ensembles, terrain horizons, elevation data,
   official areas, light pollution, haze calibration, terrain tiles for the 3D view
4. [Validation](#4-validation-checking-against-observed-sunrises): checking against observed sunrises
5. [Development](#5-development): tests, caveats, data credits

Commands below run on the server from `/opt/zmanim-sky-server` as the `zmanim-sky` user. The scripts read
the `config.json` next to them (or the file in `$ZMANIM_SKY_CONFIG`).

## 1. Setting up

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
(github.com/sandstrom/country-bounding-boxes; countries only, by name or ISO code such as `IL`).
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
# with "ensemble": {"enabled": true} in config.json (see "Ensembles"):
sudo cp deploy/zmanim-sky-gefs.service deploy/zmanim-sky-gefs.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now zmanim-sky-gefs.timer
curl http://127.0.0.1:8787/v1/status
```

Expose it through your web server for HTTPS (`deploy/nginx-location.conf`; it allows 120 s per request).
The API sets `Access-Control-Allow-Origin` from `api.allow_origin` in the config (default `*`).

Then, as you need them:

* your own climatology from past GFS runs: [Backfilling your own climatology](#backfilling-your-own-climatology)
* bare-earth terrain and fine terrain near the observer: [Terrain data](#terrain-data)
* official areas for `area=auto`: [Official areas](#official-areas-areaauto)
* the haze factor: [Haze calibration](#haze-calibration-for-nightfall-by-the-stars)

## 2. Running it

### What runs when

| what | when | log |
|---|---|---|
| `zmanim-sky-api` (`serve.py`) | always | `journalctl -u zmanim-sky-api` |
| `fetch_gfs.py` | hourly at :17 (timer); fetches a new GFS cycle when there is one (00Z, 12Z), then adds it to the climatology | `journalctl -u zmanim-sky-fetch` |
| `fetch_gefs.py` | hourly at :40 (timer), only with `ensemble.enabled` | `journalctl -u zmanim-sky-gefs` |
| `build_prior.py` | by the fetcher, when the NOAA reanalysis climatology is missing or its 10-year window moved (each March) | the fetcher's |
| `backfill_gfs.py` | by hand, once (and again after upgrades that add climatology fields) | its own (`nohup`) |
| `dem_layers.py build` / `build-fine`, `deploy/prebuild-fine.sh` | by hand, to prepare terrain ahead of requests | its own |
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
finer data near the observer, see [Elevation data](#elevation-data) for what each source is; the setup:

```sh
sudo -u zmanim-sky venv/bin/pip install rasterio      # GDAL included; needed to BUILD merged tiles and for /v1/light-pollution
```

**FABDEM** (bare earth worldwide, 1″): download the 10×10° zips you need from
https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn (Israel / Jordan: N30E030-N40E040; France:
N40W010-N50E000, N40E000-N50E010, N50E000-N60E010; the UK: N50W010-N60E000; the US and Canada are covered by
3DEP and MRDEM) and unzip the 1×1° `.tif` files into `<data_dir>/fabdem/`. The config expects
`<name>_FABDEM_V1-2.tif` with the south-west-corner name (e.g. `N31E035_FABDEM_V1-2.tif`); adjust the
template if your files differ. Straight on the server:

```sh
mkdir -p ~/fabdem-dl && cd ~/fabdem-dl
wget -c "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/N40E000-N50E010_FABDEM_V1-2.zip"   # one per zip
for f in *.zip; do unzip -tq "$f"; done
for f in *.zip; do sudo unzip -j -o "$f" '*.tif' -d /var/lib/zmanim-sky/fabdem/; done
sudo chown -R zmanim-sky: /var/lib/zmanim-sky/fabdem && cd ~ && rm -rf ~/fabdem-dl
```

**Merged 1″ tiles** are built when a request first needs them (a 150 km horizon touches ~9 tiles, so that
first request can take minutes); build the cities you serve ahead:

```sh
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 40.609283058016736 -73.96828881865329   # New York: tiles for a 150 km horizon
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 31.7683 35.2137    # Jerusalem
sudo -u zmanim-sky venv/bin/python dem_layers.py info N40W074                      # which source filled what
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
sudo -u zmanim-sky nohup deploy/prebuild-fine.sh --example > prebuild-fine.log 2>&1 &   # the main communities (~750 chunks, ~3.5 GB)
sudo -u zmanim-sky venv/bin/python dem_layers.py build-fine --around 48.8566 2.3522 --km 25 --example   # or one place
sudo cp config.json config.json.bak
sudo -u zmanim-sky venv/bin/python -c "
import json
c = json.load(open('config.json')); ex = json.load(open('config.example.json'))
c.setdefault('terrain', {})['fine'] = ex['terrain']['fine']
json.dump(c, open('config.json', 'w'), indent=2, ensure_ascii=False)"
sudo systemctl restart zmanim-sky-api
```

`test-fine` builds that 0.1° chunk in a temporary folder, says which source filled it and how long it took,
and compares it with the 1″ data your server uses (a median difference over 3 m means a datum or units
problem, or the wrong layer). Chunks are otherwise built when a horizon first needs them, one at a time: ~9
for a point (the 10 km around it), more for a city's area, which can outlast nginx's 120 s (the server
carries on, and the next request is quick); building ahead avoids it. `build-fine` skips chunks already
built and lists failed ones (run it again). `--km` is the radius to cover: the area's own radius plus
`terrain.fine.max_km` (10). `--example` uses the example config's sources before `config.json` has them;
the chunks go where the server will look once it has the same ones. `deploy/prebuild-fine.sh` runs
`build-fine` for a list of communities (New York, Monsey, Lakewood, Los Angeles, Baltimore, Chicago,
Toronto, Paris, ...; `--only NAME` for one; edit its list for your own places). Visitors get the new
horizons at their app's next daily terrain check, only where fine data exists.

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

### Resources

Default config (continental US + Israel, 2 cycles kept):

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
  chunk (`prebuild-fine.sh`'s list: ~3.5 GB).
* GEFS (optional): ~1250 requests per region per run, ~0.7 GB stored for the US box.
* CPU / RAM: negligible; arrays are memory-mapped.

### Troubleshooting

* **"another fetch_gfs.py is running"**: the timer's fetch was already at work; let it finish.
* **"no stored GFS data for region X with the current configuration"** (backfill): the region was added
  after the last fetch; run `fetch_gfs.py` once, then the backfill.
* **A cycle "not complete on the server yet"**: NOAA publishes a cycle over a few hours; the timer tries
  again within the hour.
* **`dem_layers.py info` shows `skadi (fallback)` where you have FABDEM files**: the tile was built before
  the files arrived; rebuild it (`build --force --existing`, above) and restart the API.
* **GDAL's "HTTP response code: 404" while building terrain**: a per-tile source (USGS) has no file for that
  place; expected outside its country.
* **A first horizon request times out**: its fine chunks or merged tiles were being built; the server
  carries on and the next request is quick. Build the place ahead (`build-fine`, `build`).
* **A fine source fails** (`not built (...)` in the log, `-partial` in a horizon's version): the 1″ data is
  used meanwhile and the chunk is retried after an hour; `test-fine` at that place shows GDAL's reason.

## 3. How it works

### Weather along the path (`/v1/path-profiles`)

Serves weather-model temperature profiles along the sunrise / sunset direction for ROYZmanim's
ray-traced refraction (`createServerPathAtmosphere` in `path-atmosphere.js`).

Data: NOAA GFS 0.25° (global), fetched from NOAA's NOMADS grib filter. No API key or registration;
NOAA data has no commercial-use restriction. GFS is global, so any region works, not only the US.

For each sunrise and sunset the API returns air-temperature profiles along the Sun's direction, from
the best source it has for that date:

| source | dates | detail |
|---|---|---|
| `forecast` | the next ~7 days | latest GFS run, 0.25°, 13 levels plus the air 80 m and 100 m above the ground, water temperature |
| `climatology-gfs` | any date, once that month has data | typical values from your own archive of GFS runs (live, plus past runs from `backfill_gfs.py`), same detail |
| `climatology-ncep` | any date, from the first run | NOAA NCEP/NCAR Reanalysis 1, average of the last 10 complete years at 00/06/12/18 UTC, 2.5°, 4 levels below 700 mb |

Dates no source covers are left out, and the app uses its next provider (e.g. monthly normals).

`GET /v1/path-profiles?lat=..&lon=..[&from=YYYY-MM-DD][&days=N][&ensemble=1]` (default: yesterday to a
week ahead; up to `api.max_days`, 400) answers, for each sunrise and sunset, with profiles at 0, 10, 25,
50, 100, 150, 200, 300 and 400 km along the Sun's azimuth (`api.distances_km`), interpolated in space,
time of day and season, plus the `source` used. `GET /v1/status` shows the live cycle and, per region,
how many days of your own climatology each month holds (`days_per_month`, `months_ready`).

* **The forecast.** `fetch_gfs.py` checks for a new GFS cycle (00Z and 12Z by default) and downloads only
  the needed variables for your regions: temperature and height at 13 pressure levels (1000–500 mb,
  surface to ~5.5 km), temperature 2 m, 80 m and 100 m above the ground (`near_surface_m`), surface
  (water) temperature, surface pressure, terrain height, land/water mask. Hourly to 120 h, 3-hourly to
  168 h. Each hour is decoded and stored as a numpy array; a cycle goes live only when complete. Old
  cycles are pruned.
* **Why 80 m and 100 m.** Horizon refraction is decided by the temperature gradient in the lowest hundred
  metres or so, and near the ground the pressure levels are ~250 m apart (1000 / 975 / 950 mb), so a dawn
  inversion was only a straight line from the 2 m air to 975 mb. The two levels are GFS's own boundary-layer
  air (from its lowest model levels) in that layer; their pressure is hydrostatic from the surface.
* **Your own climatology.** After each run the fetcher adds the first 12 hours (the most accurate part) to
  running averages by month and 3-hour slot of the day. A month is used once it holds
  `climatology.min_days` days (default 8), so it covers the whole year after about a year, or at once with
  the [backfill](#backfilling-your-own-climatology). The 80 m / 100 m air has a store of its own
  (`climatology/gfs-near/`) with its own counts, so a climatology built before those levels were added
  keeps its data; until the new store fills in for a month, that month's profiles are as before.
* **The NOAA fallback.** The first time a region has no NOAA reanalysis climatology, the fetcher builds it
  (`build_prior.py`) from NOAA PSL's yearly files (no key): the last `climatology.ncep_years` complete years
  (default 10), so the fallback reflects today's climate rather than a 1991–2020 average centred on ~2005.
  Each year (~600 MB) is downloaded, reduced to a ~25 MB global summary kept under
  `climatology/ncep-years/`, and deleted: about 6 GB once, then each March (when NOAA has finished the
  previous year) the window moves forward and the prior is rebuilt with one new year downloaded. New
  regions need no download. A year NOAA has not finished is replaced by an earlier one. `ncep_years: 0`
  uses NOAA's fixed 1991–2020 means instead. Whenever the stored prior doesn't match the configured window
  (a new year, a changed `ncep_years`), the fetcher rebuilds it on its next run.
* **Columns on the real ground** (`api.follow_terrain`, **off**). A 0.25° model smooths the terrain over
  ~25 km (GFS has Jerusalem's ground at ~580 m; the city stands at 750–800 m), while the air near the
  ground, a dawn inversion above all, lies along the real ground: an observer on a ridge stands in it, not
  200 m above it. With it on, each land column within
  `max_km` (150) of the observer is moved onto the mean elevation within `ground_radius_m` (1 km) of its
  point, from the same elevation data as `/v1/horizon`: every level keeps its temperature and moves by the
  ground difference times a weight that is 1 up to `full_m` (500 m) above the model's ground and falls to
  0 at `fade_m` (2000 m); above that the column is untouched, and the moved levels' pressures are
  recomputed hydrostatically. The profile then carries `groundM` and `modelGroundM`. Ground below sea
  level counts where it is a depression (the Dead Sea rift: GFS has it at ~−300 m, ERA5 at ~+30 m); a few
  offshore depths the default tiles carry next to a coastal column count as sea level. Water columns and
  columns beyond `max_km` are left as they are; if the elevation data can't be read, the columns stay on
  the model's terrain rather than failing. This is what Keller & Hall (2022) did with radiosondes; against
  Rav Druk's observations it made the fit worse ([Validation](#4-validation-checking-against-observed-sunrises)),
  so it is off; `"follow_terrain": {"enabled": true}` turns it on.

### Ensembles (`&ensemble=1`): typical outcomes and error bars

`GET /v1/path-profiles?...&ensemble=1` adds to each event an `"ensemble": {"kind", "members": [{"member",
"weight", "profiles"}]}`. The app traces every member and sums up the times (royzmanimwebsite's
`ensemble.js`: weighted mean, standard deviation, quantiles):

* **Climatology dates** (`kind: "climatology"`): the sunrise of the typical air is not the typical
  sunrise. Refraction grows faster than linearly with an inversion's strength: on a real Jerusalem
  January profile, cooling the lowest 150 m by 4 K moved sunrise 12.7 s earlier, while warming them by 4 K
  moved it only 10.3 s later. So the own climatology also keeps a third store, `gfs-moments`: running sums
  of eight low-level temperatures (2 m, 80 m, 1000–850 mb) and of their pairwise products, i.e. their
  spread and how they vary together, by month and slot like the rest. For each column the server takes
  the three strongest ways they vary together (eigenvectors of the covariance: usually the whole column
  warmer or colder, then an inversion stronger or weaker), and sends the typical air plus the air moved
  ±2 standard deviations along each. That is seven members, with unscented-transform weights (1/4, then
  1/8 each). Their weighted mean time is the typical sunrise to second order; their spread is how far a
  given year's morning strays from it. The store fills like `gfs-near`: from live runs, and from a backfill.
* **Forecast dates** (`kind: "gefs"`): every member of the GFS ensemble. `fetch_gefs.py` (with
  `"ensemble": {"enabled": true}`; its own timer, `deploy/zmanim-sky-gefs.*`) stores the control run and
  `ensemble.members` (default 10) of GEFS's 30 perturbed runs. GEFS's two 0.5° files together have the
  same fields as GFS here (13 levels, 2 m / 80 m / 100 m, surface), every 3 hours to `max_hours` (168),
  from the 00Z run, through NOMADS's grib filter (~45 min of requests per region at 2 s apart). Where the
  members agree on the morning's air the sunrise is well forecast (a few seconds); where an inversion may
  or may not form, the spread says so.

Without `&ensemble=1` the answer is as before. Members are built by the same code as the main profiles
(the 80 m / 100 m levels, `follow_terrain`).

### Terrain horizons (`/v1/horizon`)

`GET /v1/horizon?lat=..&lon=..` returns what `getVisibleSunrise` / `getVisibleSunset` / `getSunrises`
take (fetch it with `fetchHorizon` from `horizon-client.js`). It does not need the weather data, so it
works for any place between 60°S and 60°N, inside your regions or not.

| parameter | default | meaning |
|---|---|---|
| `area` | – | `auto`: search the whole official area the point is in ([Official areas](#official-areas-areaauto)); the response's `area` names it |
| `radius_km` | 0 | 0: the exact coordinates. > 0: search a grid of spots within this radius (capped by `terrain.max_radius_km`, default 3) and return the few that see lowest in some direction |
| `bbox` | – | `south,west,north,east`: search every spot inside this box instead of a circle (e.g. a neighbourhood). `lat`/`lon` may be left out (box centre). Centre-to-corner distance is capped by `terrain.max_radius_km` (3 km); boxes with more than `terrain.max_candidates` (1200) spots get a coarser grid |
| `grid_m` | 100 | vantage grid spacing (minimum `terrain.min_grid_m`, 50) |
| `eye` | 1.7 | eye above the ground, m |
| `height` | ground + eye | observer height above sea level (point mode only), e.g. a roof or upper floor |
| `min_km` | 1 | terrain nearer than this is ignored (the observer's own building / hilltop) |
| `moon` | – | `1`: also return a `moon` horizon for visible moonrise / moonset (below) |

How it is computed: for every 0.1° of azimuth across the year's sunrise and sunset directions, out to
150 km, the terrain point with the highest apparent elevation is kept (distance and height; the
calculator adds Earth curvature and refraction for the day's air). Which point that is depends on the
refraction it is chosen with (standard air, k = 0.13): a strong inversion can make a farther, higher ridge
the one that hides the Sun. So where another coefficient from −0.2 to 0.9 would pick a different point,
the entry also lists it under `alternates` (`[[distanceKm, heightM], ...]`, usually absent); the
calculator traces the ray to each through the day's air and keeps the highest. Each entry also has
`floorM`, the lowest surface the line of sight crosses out to its farthest point (a lake's own surface,
the sea, or level ground), and rays come down no lower; before algorithm 3 the calculator used sea level
everywhere, which let rays to the far shore of Lake Michigan pass under the lake. With a radius, candidates are first
compared at 0.5° steps; the smallest set of spots that together see lowest in every direction (within
0.003°) is kept, each with the azimuth ranges where it wins (`wins`), and only those are computed in
full. The calculator then traces each winning spot whose range contains the day's sunrise direction,
from its own position and height, and takes the earliest sunrise (latest sunset). For Brooklyn, the
0.8 km vantage set (100 m grid) agrees with a brute-force search over a 49-spot grid to within ±5 s.

Each answer is cached in `<data_dir>/horizons`, so a place costs its computation once (point: ~1 s;
0.8 km vantage search: ~5–10 s; more the first time its terrain data is built). `make_horizon.py` does
the same from the command line (same output as `/v1/horizon`; tiles downloaded into `./tiles` unless
`--no-download`):

```sh
venv/bin/python make_horizon.py 31.748553 35.237436 --height 754.9 --out armon-hanatziv.json
venv/bin/python make_horizon.py 40.6092 -73.9683 --radius-km 0.8 --out brooklyn-vantage.json
venv/bin/python make_horizon.py --bbox 40.60,-73.98,40.62,-73.955 --out midwood.json
```

#### Moonrise / moonset (`moon=1`)

The Moon rises and sets over a wider arc than the Sun (its declination reaches about ±28.7° against
the Sun's ±23.4°), so with `moon=1` the response gets a `moon` entry covering every moonrise / moonset
direction of the 18.6-year cycle, widened so a Moon first seen over a horizon up to 10° high is still
inside (by then it has moved along its slanted path; the margin grows with latitude, from ~2° at the
equator and ~10° at 32°N to the full half circle by 55°N; beyond 10° the client takes the edge as
continuing): `{"lat", "lon", "percentile", "spots", "spotsUsed",
"moonrise": [...], "moonset": [...]}`, one entry every 0.1° of azimuth:
`{"azimuthDeg", "distanceKm", "heightM", "observerM", "tiltDeg"}`.

It is chosen the opposite way from the sunrise vantage points. Birkat halevana needs the Moon actually
seen, so for an area (`radius_km`, `bbox`, `area=auto`) each direction takes the spot at the
`terrain.moon_percentile` percentile (default 90) of horizon height: a high horizon, i.e. a moonrise
about 90% of the area can see, and a moonset before it is hidden from about 90% of it. The percentile
(rather than the single highest horizon) keeps a few spots at the foot of a slope from setting the
time for everyone; 100 gives the single worst spot.

It is one composite horizon rather than a list of vantage points: the 90th-percentile spot changes every
few degrees on rolling terrain, while the Moon's position barely depends on which spot within a few km
it is seen from. Each entry therefore carries its own spot's eye height (`observerM`) and `tiltDeg`, how
much higher the Moon stands from that spot than from the reference point (`lat`, `lon`): a spot s metres
away towards bearing B has its zenith tilted by s/R, so the Moon's altitude there is higher by
s/R · cos(azimuth − B). The geometric elevation of an entry, in the reference frame, is
`geometricElevation(observerM, heightM, distanceKm) − tiltDeg`. In point mode it is the point's own
horizon over the wider arc (`tiltDeg` 0).

Without `moon=1` the response has no `moon` entry and is cached separately from moon requests. With it,
the first request for a place costs roughly twice the time (one extra pass over the area's spots); then
it is cached.

### Elevation data

Three tiers, each used where it has data: the default tiles everywhere, merged bare-earth layers at 1″
(`terrain.layers`), and finer bare-earth data within 10 km of the observer (`terrain.fine`). Setup is in
[Terrain data](#terrain-data).

#### Default elevation tiles

Unless bare-earth layers are configured, all heights come from the AWS Terrain Tiles: SRTM worldwide, with
USGS 3DEP in the US, from `s3.amazonaws.com/elevation-tiles-prod/skadi` (free, no key;
`terrain.tiles_url`). They are downloaded into `terrain.tiles_dir` (default `<data_dir>/tiles`, ~2–8 MB
each) the first time a request needs them. Those tiles carry ocean depths offshore (to -85 m within a few
km of New York); the line of sight meets the water, so heights below sea level are raised to 0 and
candidate spots in the water are skipped. Places really below sea level keep their heights when the
observer stands in one (ground below -20 m: the Dead Sea, Jordan valley, Death Valley). Where the set has
no tile at all, the area is open sea. To work offline, put the tiles in `tiles_dir` yourself and set
`"tiles_url": ""`; a missing tile is then reported (HTTP 503) instead of downloaded.

#### Bare-earth layers (`terrain.layers`, dem_layers.py)

The default tiles are bare earth only in the US (3DEP). Elsewhere they are SRTM, a surface model:
buildings and forest count as terrain and raise the horizon (later sunrise, earlier sunset). With
`terrain.layers.sources` set, every point of every line of sight takes its height from the first source
in this list that has data there:

| order | source | covers | notes |
|---|---|---|---|
| 1 | USGS 3DEP 1 arc-second | US | bare earth, NAVD88; read per 1x1 degree file straight from USGS's bucket |
| 2 | NRCan MRDEM-30 DTM | Canada | bare earth, 30 m, CGVD2013; read from NRCan's VRT (Open Government Licence - Canada) |
| 3 | an Israeli DTM | Israel | a file you supply (any projection, e.g. EPSG:2039); `enabled: false` until then. The Survey of Israel and the Geological Survey of Israel (25 m DTM) have one, but neither is openly published: it has to be licensed or requested |
| 4 | FABDEM V1-2 | world (the 10° zips you download) | Copernicus 30 m with forests and buildings removed; CC BY-NC-SA 4.0, non-commercial only |
| 5 | the default tiles | world | the fallback for any pixel none of the above covers, mainly open sea |

The line of sight crosses borders (Jerusalem's sunrise is over Jordan), so each source only fills the
pixels it covers and the next one fills the rest. Differences between vertical datums (NAVD88,
CGVD2013, EGM2008, EGM96) are well under a metre here and are not corrected.

With `auto_build: true` (and rasterio installed) missing tiles are built during the first request that
needs them. Without rasterio, only prebuilt tiles are used and everything else falls back to the default
tiles. Safety: a source that doesn't cover a tile (no file there) is skipped quietly, but any other failure
(network, corrupt file, a configured file missing) aborts that tile's build - nothing is saved, the
default tiles are used, and it is retried next time. A tile is never kept half-built from a worse source.

Merged tiles go to `<data_dir>/dem-layers/<signature>/` (float32, ~52 MB per land tile, memory-mapped;
sea-only tiles are a marker file). The signature is a hash of the enabled sources: changing the source
list starts a new tile set and recomputes horizons. A rebuild (`build --force`) that changes a tile's
heights raises its revision (`<tile>.rev`); both are part of the terrain version of every horizon within
`max_km` of the tile ([Terrain versions](#terrain-versions-v1terrain-version)).

#### Fine terrain near the observer (`terrain.fine`)

A height error turns into an angle divided by its distance: 5 m at 5 km is 3.4′, about 15 s of sunrise at
32°N (more at higher latitudes), while the same 5 m at 50 km is 0.3′ (Keller & Hall 2022). A 1″ (30 m) grid
also shaves the tops off narrow ridges and hillsides. So with `terrain.fine.sources` set, the observer's
ground and all terrain within `max_km` (default 10 km) come from a finer bare-earth model, sampled every
one of its cells (`step_arcsec`, default 1/3″ ≈ 10 m); beyond that, and wherever the fine model has no
data, the 1″ tiles / layers are used as before. At Monsey (sunset over a hillside 1.2 km away) 3DEP 1/3″
moved the horizon by 1.2′ on average and up to 5.9′ against the 1″ data.

The example config has three fine sources, tried in this order point by point:

* **NRCan HRDEM** (Canada): the 2 m lidar ground model (DTM) mosaic, 56 files of 500 km on NRCan's bucket in
  Canada Atlas Lambert, read at its first overview (4 m, `"open_options": {"OVERVIEW_LEVEL": "0"}`; any GDAL
  open option can go there). The 4 m overview is within 0.1 m of the full 2 m data at 99% of the grid's points
  (at most 1.3 m, at Toronto and Montreal) and builds a chunk in ~8 s instead of ~20; its heights match
  MRDEM's (the 1″ layer) to a few cm on average. It covers the lidar surveys, most populated areas but not
  all of Canada. Opening the 56 files takes ~10 s, once per server process, the first time a chunk needs
  them. It comes first because USGS's files near the border reach into Canada with resampled coarse data
  (at Toronto they match the 1″ data to 0.4 m), which would otherwise hide the lidar; it has no data in the
  US, where 3DEP then fills in.
* **USGS 3DEP 1/3″** (US): bare earth, read per 1×1° file from USGS's bucket like the 1″ layer.
* **IGN RGE ALTI** (France, metropolitan and Corsica): the national ground model (MNT; lidar where surveyed,
  0.2–0.5 m, ~7 m on steep mountain slopes), read at 5 m from the Géoplateforme's raster WMS
  (`data.geopf.fr/wms-r`, layer `ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES`, float32 GeoTIFF in Lambert-93)
  through `sources/ign-rgealti-5m.xml`, a GDAL WMS description; a relative path in `paths` is looked up in
  the server's folder. Licence Ouverte Etalab 2.0. ~15–20 s per chunk. If the service is ever too slow or
  refuses, the alternative is IGN's per-département downloads (RGE ALTI 5 m, `.7z`) as a local source.

Against the 1″ data, the median difference was +0.4 m at Toronto and −1.1 m at Montreal (HRDEM), −1.2 m in
Paris and +0.5 m at Nice (RGE ALTI against FABDEM). Elsewhere (Israel: no free source finer than the 1″
layers; the Survey of Israel's DTM would go here, if licensed) the 1″ data is used. Any other
GDAL-readable bare-earth source works too, e.g. a state's lidar DTM.

Data is built on demand (needs rasterio) in 0.1° chunks, ~4.7 MB each at 1/3″, under
`<data_dir>/dem-fine/<signature>/`. A chunk no source covers is a marker file, so places outside the
sources cost one look. A failed read is retried after an hour and the 1″ data is used meanwhile. The
fine settings are part of a horizon's cache key only where the fine data covers it (within `max_km` of
its vantage points), so turning them on, or adding a source, recomputes the horizons there and nowhere else.

#### Terrain versions (`/v1/terrain-version`)

Each horizon names the terrain that built it: `terrain.layers` (the layers' signature, plus the revisions
of rebuilt tiles within `max_km`) and, where fine data covers it, `terrain.fine.version` (the fine
sources' signature; with `-partial` added while a chunk there failed to build, and then not cached, so the
next request tries again), as `"terrain": {"layers": <version>, "sources": [...], "fine": {"signature", "version",
"withinKm", "sources"}}`. Both versions are part of its cache key. `GET /v1/terrain-version` takes the same query as
`/v1/horizon` and answers with them alone (`{"fine": ..., "layers": ...}`), without computing the horizon
(building the fine chunks it would read if needed). royzmanimwebsite asks it about once a day for each
stored horizon and downloads the horizon again only when the two differ, so visitors get new terrain
where it changed and nothing else is downloaded again.

### Official areas (`area=auto`)

With `area=auto` the client sends only its coordinates; the server finds the official area it is in -
a neighbourhood, village, town or city - and searches that whole area, so everyone in it gets the same
visible sunrise (the earliest anywhere in it) and the same latest sunset. The answer is cached per
area, not per point. Use `fetchHorizonForArea(SERVER, lat, lon)` from `horizon-client.js`; the client
needs no list of neighbourhoods of its own.

```sh
curl 'http://127.0.0.1:8787/v1/area?lat=40.609283058016736&lon=-73.96828881865329'      # which area (no polygons)
curl 'http://127.0.0.1:8787/v1/horizon?lat=40.609283058016736&lon=-73.96828881865329&area=auto&radius_km=0.8'
```

A point in no known area, or in one bigger than `terrain.max_area_radius_km` (30 km from centre to
corner, separate from the 3 km cap on boxes clients send), gets the request as given (here the 0.8 km
radius) with `"area": null` and an `areaNote` saying why. Big areas get a coarser grid
(`terrain.max_candidates` spots in all), so a 25 km city is searched on a ~1 km grid.

The areas come from `areas.json`, built by `build_areas.py` from the Chai Tables place list
(`chaiTable.json` in the royzmanimwebsite repo). Every place becomes the official units it covers:

* a metro-area box: every unit with a quarter of its area inside the box, or whose part inside covers 3%
  of the box; units entirely covered by finer ones are dropped (New York City by its NTAs)
* a single point: the finest unit containing it
* no location, or a combined name like "Kiriat-yam-mozkin-bialik": whatever `areas_overrides.json` says
  (names, a point or a box); otherwise it is listed as unresolved
* places chaiTable lacks (Hollywood FL, Pikesville, Oak Park MI...): the `_extra` list in `areas_overrides.json`
  (a point or a box each), treated like chaiTable places. A point outside every place's areas gets no area
  (`area: null`), so add a community there when its users should get one

| where | official units (finest wins) | source |
|---|---|---|
| New York City | 2020 Neighborhood Tabulation Areas | NYC Department of City Planning |
| City of Los Angeles | "Mapping L.A." neighbourhoods | Los Angeles Times |
| Chicago, Houston, Dallas, Phoenix, San Diego, Austin, Seattle, Baltimore | the city's own layer: Chicago's 77 community areas, Houston's super neighborhoods, Dallas's council districts (it has no citywide neighbourhood layer), Phoenix's urban villages, San Diego's community plan areas, Austin's neighborhood planning areas, Seattle's Neighborhood Map Atlas neighborhoods, Baltimore's neighborhood statistical areas. Each city is one Census place of 25-55 km otherwise, at or over the area cap | each city's open-data / GIS service (see `US_CITIES` in `build_areas.py`) |
| rest of the US | incorporated places and census-designated places, then county subdivisions (towns) | US Census TIGER/Line |
| Israel | municipal jurisdictions; locality outlines for villages inside regional councils. Regional councils are never an area | OpenStreetMap, whose `admin_level=8` boundaries are the Ministry of Interior's: Geofabrik's daily Israel and Palestine extract (one download) |
| Israel, the places under "Eretz Yisrael (Neighborhoods)" (Beit Shemesh, Haifa, Jerusalem, Safed, Tiberias) | sub-quarters (תת-רובע; cities of 40,000+), or statistical areas for a listed city without them | Central Bureau of Statistics, statistical areas 2022 (downloaded from its ArcGIS service) |
| Toronto, Montreal, Ottawa, Hamilton, Halifax, Calgary, Edmonton, Winnipeg | the city's own neighbourhoods: Toronto's 158 neighbourhoods, Montreal's arrondissements, Ottawa Neighbourhood Study Gen 3, Hamilton's planning units, Halifax's communities, Calgary's community districts, Edmonton's and Winnipeg's neighbourhoods | each city's open-data portal (see `PLANS["Canada"]` in `build_areas.py`) |
| rest of Canada | 2021 census subdivisions (municipalities); amalgamated cities such as Halifax (~190 km) or Ottawa (~55 km) are far over the area cap, hence the layers above | Statistics Canada 2021 cartographic boundary file (`lcsd000b21a_e.zip`, one download, reprojected from Statistics Canada Lambert) |
| other countries | the municipal level (communes, comuni, local authority districts, ...; Paris, Lyon and Marseille by arrondissement; see `PLANS` in `build_areas.py`) | geoBoundaries (national statistics / mapping agencies) |

```sh
sudo -u zmanim-sky venv/bin/pip install pyshp osmium               # Census shapefiles; the OpenStreetMap extract
sudo -u zmanim-sky venv/bin/python build_areas.py                   # all countries; ~1-2 GB of downloads, cached in
                                                                    # <data_dir>/area-sources (delete a file to refresh it)
sudo -u zmanim-sky venv/bin/python build_areas.py --only USA --only "Eretz Yisrael (Cities)"   # rebuild just these; the other countries' areas are kept (--replace drops them)
```

It writes `<data_dir>/areas.json` (the server reloads it when it changes) and `areas_report.txt`: every
place with the units it got, the places still unresolved, chaiTable boxes that look too big (Zhytomyr,
Rome, Hamburg...), and areas over the cap. Read the report after each build; check every name match it
lists. To use an official file instead of a download (e.g. the CBS localities layer, or a city's own
neighbourhood layer), pass `--sources` with `{"Eretz Yisrael (Cities)": [{"type": "file", "path":
"localities.zip", "id_field": "<its code field>", "name_field": "<its English name field>", "kind":
"locality", "source": "Israel CBS localities"}]}` (shapefile, zipped shapefile or GeoJSON, in lon/lat).

### Light pollution for nightfall by the stars

`GET /v1/light-pollution?lat=..&lon=..` returns the artificial sky brightness at the zenith: the
light-pollution term (`Blp`) of the star-visibility nightfall model in
[astertaylor/halakhic_calc](https://github.com/astertaylor/halakhic_calc) (`calc_time.py`). Fetch it with
`fetchLightPollution` from `light-pollution-client.js`. Like `/v1/horizon` it needs no weather data.

| parameter | default | meaning |
|---|---|---|
| `area` | – | `auto`: the official area the point is in (`/v1/area`); the value is the `percentile` over every atlas pixel inside its boundary |
| `bbox` | – | `south,west,north,east`: the same over a box (`lat`/`lon` may be left out: box centre); centre to corner at most `light_pollution.max_radius_km` (30) |
| `percentile` | 90 | `light_pollution.area_percentile`. A brighter sky means a later nightfall, so 90 gives a time by which about 90% of the area has the stars (as `terrain.moon_percentile` for moonrise) |
| `year` | this year | the sky of that year (see "From the 2014 atlas to today's sky") |

```json
{"lat": 31.7767, "lon": 35.2345, "coverage": true,
 "atlasMcdM2": 5.757,                // the atlas pixel as it is (2014 data), mcd/m^2
 "artificialMcdM2": 17.30,           // today's estimate at the zenith, mcd/m^2: the value to display
 "artificialCdM2": 0.01730,          // the same in cd/m^2
 "blpCdM2": 0.03891,                 // Blp for calc_time.py's nightfall(): the sky's average, 2.25 x the zenith (below)
 "skyAverageFactor": 2.25,
 "ratioToNatural": 99.4,             // artificial / natural (0.174 mcd/m^2)
 "totalMagArcsec2": 16.98,           // zenith brightness with the natural sky, mag/arcsec^2 (what a sky meter reads)
 "pixel": {"row": 6393, "col": 25828, "lat": 31.77502, "lon": 35.23741},
 "correction": {"method": "trend", "year": 2026, "factor": 3.004, "region": "world", "ratePerYear": 0.096,
                "baseYear": 2014, "extrapolated": true, "source": "Kyba et al. 2023, ..."},
 "source": "Falchi et al. 2016, ..."}
```

With `area=auto` or `bbox` the top-level values are the area's percentile, and the response adds `area`
(or `bbox`), `stats` (`pixels`, `min`, `p10`, `median`, `p90`, `max`, `mean`, `percentile`, corrected
like the value) and `point` (the point's own answer, as above). A point in no known area, an area over the
cap, or one smaller than a pixel gets the point's value with `area: null` and an `areaNote`. Outside the
atlas (north of 85.05° N, south of 60.00° S) the answer is 0 with `"coverage": false`.

```sh
venv/bin/python light_pollution.py 31.7767 35.2345
venv/bin/python light_pollution.py --bbox 40.60,-73.98,40.62,-73.955
curl 'http://127.0.0.1:8787/v1/light-pollution?lat=40.609283058016736&lon=-73.96828881865329&area=auto'
```

#### From the 2014 atlas to today's sky

The atlas is built from 2014 satellite data, and satellites under-read skyglow: they see light sent up,
while most skyglow comes from light sent sideways, and they are blind below 500 nm, where white LEDs
emit. Naked-eye star counts from 51,000 Globe at Night observers show the sky brightening 9.6% a year
from 2011 to 2022 (10.4% in North America, 6.5% in Europe), against ~2% seen from orbit (Kyba et al.
2023, Science, doi:10.1126/science.abq7781). So each answer is corrected, in this order:

1. `"method": "measured"`: sky-meter readings (`light_pollution.measurements.file`, default
   `<data_dir>/sky-measurements.json`) within `measurements.radius_km` (15) of the place, or inside its
   area. For each reading the measured total brightness (artificial + natural) is divided by the atlas's
   at that spot; the median ratio is applied to the atlas. Readings from other years are carried to `year`
   by the trend. See `sky-measurements.example.json` for the format; it holds the Ramon Crater dark-sky
   park's 12 monitoring sites (2020). Globe at Night's public sky-meter readings are read beside your own
   (`measurements.globe_at_night_file`, default `<data_dir>/sky-measurements-gan.json`), built once a year by

   ```sh
   sudo -u zmanim-sky venv/bin/python build_sky_measurements.py [--since 2020]
   ```

   which keeps the readings taken on a clear night with the Sun 18 degrees down and the Moon below the
   horizon (2020-2025: ~3,600 readings worldwide, most in North America and Europe; 28 within 15 km of
   Lakewood, NJ, one of Jerusalem). Data: globeatnight.org, NSF NOIRLab, CC BY 4.0.
2. `"method": "trend"`: the atlas times (1 + rate)^(year − 2014), rate by region (`light_pollution.trend`;
   first box containing the point, else the worldwide 9.6%). After 2023 it is an extrapolation and says so
   (`"extrapolated": true`). The rate is for the whole sky as seen; applying it to the artificial part only
   understates it slightly. In 2026 the factor is about 3.3 in North America, 3.0 elsewhere, 2.1 in Europe.
3. `"method": "none"` with `"trend": {}` in the config: the atlas as it is.

Checked against measurements: at the Ramon Crater park's 12 SQM sites (2020) the atlas alone is on average
0.20 mag/arcsec² darker than measured; with the trend the average difference is 0.00 mag, though single
sites differ by up to ±0.6 mag (the most, 20.00 against 20.65, next to Mitzpe Ramon, whose lighting is
regulated). Local readings are the only way to be right at a particular place: a handful per community,
taken with an SQM on clear, moonless nights after astronomical twilight.

#### Average sky, not zenith (`blpCdM2`)

The atlas is the brightness overhead, but `calc_time.py`'s nightfall applies one `Blp` to every star, and
skyglow is brighter toward the horizon. Its author (Taylor 2026, arXiv:2608.04064, §3) therefore multiplies
the atlas by π, after the literature's ratio of horizontal illuminance to zenith luminance (~π). But a sky
of uniform luminance already has that ratio = π, so it means average luminance = 1 × zenith. The measured
ratio under light pollution peaks at 2.25 π (Bará et al. 2022, arXiv:2202.07526): the average sky is about
2.25 × the zenith. So `blpCdM2` = `light_pollution.sky_average_factor` (2.25) × `artificialCdM2`, about
30% less than calc_time.py's π. (`artificialMcdM2` stays the zenith value: what a sky meter reads, and what
to display.)

Better than any single factor, for a nightfall implementation that can take it: give each star its own
light-pollution luminance, `artificialCdM2 × X(Z, H)`, the airmass of the model's own Eq. 3. A luminance
growing with airmass averages to exactly 2 × the zenith (ratio 2 π, near the measured 2.25 π), and it makes
stars low in the sky harder to see than stars overhead, as they are.

What is still not modelled: which side the lights are on (the sky toward a city is brighter than the
sky away from it, at the same altitude), and the weather: the atlas assumes a clear, average atmosphere;
haze and snow cover brighten skyglow.

#### The atlas files

The World Atlas of Artificial Night Sky Brightness (Falchi et al. 2016), 30" pixels, in the 4096 x 4096
pixel tiles of halakhic_calc's `data/` folder (`lp_{row}_{col}.tif`, 55 files, 488 MB, lossless zstd). A
tile is downloaded into `light_pollution.dir` (default `<data_dir>/light-pollution`) the first time a
request needs it; `light_pollution.py --download-all` fetches them all ahead (about a minute plus the
download).

Each download is re-compressed as it arrives (`light_pollution.convert`, default LERC_ZSTD with
`max_z_error` 0.001 mcd/m²) and saved as `light_pollution.file` (`lp_{row}_{col}_lerc.tif`); the
original is deleted. The world then takes 52 MB instead of 488. Before it replaces anything the converted
file is read back and checked (same grid, every pixel within `max_z_error`); a failed conversion leaves
nothing behind and is retried on the next request. Original tiles already in the folder (downloaded
while `convert` was off, or copied in under their original names) are converted the first time they are
used (or all at once with `--download-all`). `"convert": {}` keeps downloads as they come, under the URL's
own file name. The options are GDAL GeoTIFF creation options, so another codec works too.

Why 0.001 mcd/m² is safe: the nightfall model sees the total sky brightness (artificial + natural 0.174
mcd/m² + twilight), so the error is at most 0.57% of it at a pristine site and far less where light
pollution matters. In `calc_time.py`'s own nightfall, a 1% change in the artificial brightness moved the
time by at most 7 s (3 small stars, Monsey, March; most cases 0 s). Checked against the originals at 3000
random points: max difference 0.0010 mcd/m². Lossless LERC is no smaller than the original zstd, and
storing log(brightness + natural sky) so that the error is relative saved only 10-30% more, at the cost
of a non-standard file every reader would have to decode.

To use your own copies, put them in the folder under `light_pollution.file`'s name pattern; any tile
missing there is still downloaded (and converted). `"url": ""` never downloads; a missing tile is then HTTP
503. Reading and converting need rasterio (in requirements.txt), whose bundled GDAL reads both the
original zstd tiles and LERC.

Pixels are located from the tiles' georeferencing. `calc_time.py` assumes the grid starts at exactly
85° N and spans 145°, but it starts at 85.054° N with 1/120° pixels, so the original reads a pixel 3 to 6
rows (3–6 km) north of the observer: Jerusalem 4.82 instead of 5.76 mcd/m², Monsey 1.41 instead of 2.42,
Lakewood 1.82 instead of 2.53.

### Haze calibration for nightfall by the stars

The app's star-visibility nightfall reads the evening haze (aerosol optical depth at 550 nm) from the CAMS
model through Open-Meteo. `haze_calibration.py` checks CAMS against [AERONET](https://aeronet.gsfc.nasa.gov/),
NASA's network of sun photometers that measure the real haze (hundreds of stations: many across the US,
Canada's AEROCAN, several in Israel, many in France), and `GET /v1/haze-calibration?lat=..&lon=..` gives
the app one factor to multiply CAMS by at a place:

```json
{"factor": 1.25, "radiusKm": 300.0, "period": ["2024-10-07", "2026-10-06"],
 "stations": [{"site": "CCNY", "lat": 40.821, "lon": -73.949, "distanceKm": 19.0, "ratio": 1.3, "days": 412}]}
```

For every AERONET station inside the configured regions (plus `haze_calibration.margin_deg`), the ratio is
the median over days of measured / modelled daytime haze (AERONET's daily averages, Level 1.5, carried from
500 to 550 nm with each day's Angstrom exponent; CAMS averaged over local solar 8-16 h), from stations with at
least `min_days` (60) days of both. A model's errors follow the kind of haze a region has (desert dust, city
pollution, wildfire smoke), so a station speaks for its surroundings. A place's factor is estimated from the
stations by kriging: the variogram of the stations' log ratios (how alike two stations are at each distance) is
fitted to them (exponential model, Cressie's weighted least squares), each station's own uncertainty (from its
days and spread) is counted as measurement error, and the estimate goes to the network's mean far from every
station. The answer gives the factor, its standard error (`sdLog`), each station's weight and the fitted
variogram. A region added later gets its stations on the next run.

```sh
sudo -u zmanim-sky venv/bin/python haze_calibration.py
```

No key. Each station's CAMS series is one Open-Meteo request that counts as ~25 calls per year of data, so with
the default two years about 150 stations fit in Open-Meteo's free 10,000 calls a day; requests are 8 s apart.
A run stops cleanly at `--max-sites` or any failure and the next run continues (series are cached under
`<data_dir>/haze-cal-cache/`); the log says how many stations are left. Rebuild once a year. AERONET's data
policy asks that its use acknowledge the AERONET network and the station PIs.

### Terrain tiles for the 3D view (`/v1/terrain-tiles`)

`GET /v1/terrain-tiles/{z}/{x}/{y}.png` serves the heights the horizons are computed from (the fine near-range
data, else the bare-earth layers, else the 1″ tiles) as web-Mercator tiles in the Terrarium encoding (height =
R × 256 + G + B / 256 − 32768 m, the format of the open AWS Terrain Tiles), so the website's 3D horizon view
shows the ground the horizon was found on. Zooms 8–14 only. A tile below zoom 11 spans several 1° tiles, so it
is made only from data already on the server (computing a horizon nearby brings it) and is otherwise a 404;
the website then shows the AWS tiles there. Each tile is made once and kept in
`<data_dir>/terrain-tiles/<terrain signature>/` (a new signature, from changing `terrain.layers` or
`terrain.fine`, starts a new folder; delete old ones). Tiles of a merged 1° tile rebuilt with `--force` are not
renewed by themselves: delete the folder to remake them.

## 4. Validation: checking against observed sunrises

Summary: on Rav Druk's 521 observed sunrises at Armon HaNatziv (1993–96), the computed visible sunrise
with ERA5's air along the path is unbiased and 10.6 s rms (11.7 s with Jerusalem's monthly normals and
Keller & Hall's inversion rule); his 1,400 earlier sunrises and Yisroel Golan's 200 at Nevei Yaakov rank the
same. There are no observations yet from outside Jerusalem.

### ERA5 profiles for past dates (`era5_profiles.py`)

The forecast and the climatology can't be checked against sunrises someone watched years ago. ERA5
(ECMWF's reanalysis: the air hour by hour since 1940, 0.25°, 37 pressure levels) can.
`era5_profiles.py` writes, for any past dates, exactly what `/v1/path-profiles` would have served (same
JSON, `"source": "era5"`), built by the same code: columns along the Sun's azimuth, on the real ground
(`api.follow_terrain`, unless `--no-follow-terrain`). royzmanimwebsite's `_denoDebug/validateSunrise.mjs`
then computes the visible sunrise for each observation with those profiles, with monthly normals (with and
without Keller & Hall's inversion rule) and with the standard refraction, and reports observed minus
computed: mean, mean absolute and rms, overall and by month.

```sh
venv/bin/pip install cdsapi            # and a free CDS key in ~/.cdsapirc (https://cds.climate.copernicus.eu/how-to-api)
venv/bin/python make_horizon.py 31.748553 35.237436 --height 754.9 --out armon.json
venv/bin/python era5_profiles.py --lat 31.7486 --lon 35.2374 --dates druk.csv --event sunrise --out era5.json
venv/bin/python era5_profiles.py --lat 31.7486 --lon 35.2374 --dates druk.csv --event sunrise --no-follow-terrain --out era5-flat.json
# in royzmanimwebsite:
node _denoDebug/validateSunrise.mjs --obs druk.csv --tz Asia/Jerusalem --horizon armon.json \
     --profiles era5=era5.json --profiles era5-flat=era5-flat.json --normals jerusalem-normals.json --csv out.csv
```

Where the data comes from (`--source`):

* `cds` (default): the Copernicus Climate Data Store. Only a box around the paths is requested, a month at
  a time and only the hours around the events: a few MB a year. Needs a free key, and requests queue.
* `ncar`: NSF NCAR's copy on AWS (no key; `pip install h5py fsspec requests aiohttp`). It stores the whole
  globe per hour in one compressed chunk, so each hour of temperature and geopotential means reading
  ~110 MB (only the box is kept): about 20 GB per 100 sunrises, ~1 min per month of observations with
  `--parallel 8`.

Either way the box is kept in `<data_dir>/era5`. `--nc-dir` reads files you downloaded yourself instead.
ERA5 has no 80 m / 100 m levels (its lowest are 1000 / 975 / 950 / 925 mb), so it checks the path, the
terrain and the ground-following but not the GFS boundary-layer levels.

### The observations (`observations.py`)

`--dates` takes any file with a date at the start of each line (the observations CSV itself:
`date,time[,event]`, local time). `observations.py` writes that CSV from Chaim Keller's observation files
(github.com/chaimkeller/software, `observations-all-places/`): Rabbi Druk's at Armon HaNatziv 1985–96
(`DRUK_10 yr. observations.XLS`, the fullest; also `DRUK.001`, `DRUK9396.DAT`), Yisroel Golan's at Nevei
Yaakov 2009–13, Rabbi Zuriel's in Bnei Brak 1961–92; all in standard time all year (`--tz Etc/GMT-2`).
Druk's spot is 31.748553, 35.237436 (Keller's DRUK program), eye 754.9 m above sea level (Keller & Hall).
The spreadsheet needs `xlrd` (in requirements.txt):

```sh
venv/bin/python observations.py "DRUK_10 yr. observations.XLS" --out druk.csv     # all of Druk's, 1985-96
venv/bin/python observations.py Golan_20*.csv --out golan.csv
```

### Results

On Rabbi Druk's sunrises at Armon HaNatziv from Keller's spreadsheet (521 in 1993–96, 1,400 in 1985–92;
Keller's spot, eye 754.9 m, SRTM terrain, approximate Jerusalem monthly normals; observed minus computed):

| atmosphere | 1993–96 mean | rms | rms Nov–Jan | 1985–92 mean | rms | rms Nov–Jan |
|---|---|---|---|---|---|---|
| standard 34.48′ | +6.3 s | 16.4 s | 21.7 s | +4.3 s | 17.2 s | 23.8 s |
| monthly normals | −4.3 s | 14.0 s | 25.8 s | −6.2 s | 16.2 s | 27.9 s |
| monthly normals + Keller & Hall's inversion rule | −1.1 s | 11.7 s | 19.2 s | −2.4 s | 13.4 s | 20.3 s |
| ERA5 path profiles | +1.0 s | 10.6 s | 16.9 s | −0.5 s | 12.1 s | 17.3 s |

Real air along the path does best in both periods, unbiased and with nothing tuned. The winter error is
scatter, not bias: fitted on one period and scored on the other, no winter correction (the 13 h rule at any
size, one scaled by ERA5's own inversion, by night length, or by calm clear mornings from ERA5's wind and
cloud) gained more than 0.5 s, and the rule's fixed 15 s made ERA5 worse (12.4 s and 13.0 s). Yisroel Golan's
200 sunrises at Nevei Yaakov (2009–13, from the "MA" shul, 31.842162 N 35.246491 E, ground 692.5 m) rank the
same: ERA5 8.9 s rms (mean +1.5 s), normals + rule 9.5 s, normals 10.9 s, the standard 34.48′ 15.8 s, with the
eye at ground level; an eye on the roof (+10 m) fits worse (10.2 / 10.5 / 11.1 s). An eye height of 802 m
at Armon HaNatziv, used in an earlier version of this README, fits clearly worse.

These numbers use horizons from `terrain.py` version 3 (with `alternates` and `floorM`) and the client's terrain rays
traced through the day's air (royzmanimwebsite `terrainElevation`). With the earlier one-curvature terrain
formula (`validateSunrise.mjs --terrain-compare`, row `era5:formula`) ERA5 scored +1.5 s / 10.8 s, +0.1 s /
12.2 s and +2.1 s / 9.2 s (Druk 1993–96, Druk 1985–92, Golan): the traced rays move sunrise about 0.65 s later
on average (at most 4 s) and lower the rms in all three sets. The alternates change at most 1.1 s on any
morning and leave every rms as it was (`era5:noalt`); `terrainImages.mjs` found no morning in the three sets
where a ridge near the Sun's azimuth had a second image. Nov–Jan rms is unchanged by either.

No sunrises have been timed at a shore, so the runs below only show how often the client's handling of air
over water moves the time: ERA5 air for every sunrise (sunset for Tel Aviv and Santa Monica) of 2023 and
horizons with `floorM`. Mornings changed by more than 0.5 s:

| place | refraction table near the horizon (fine against 0.1° steps) | image search on − off | early Sun images on − off |
|---|---|---|---|
| Chicago, Loyola Beach (Lake Michigan) | 127, up to 92.5 s | 1: 28 May, +100.8 s | 47, up to −57.9 s |
| Milwaukee (Lake Michigan) | 49, up to 14.7 s | 1: 11 Apr, +4.6 s | 2, up to −18.0 s |
| Duluth (Lake Superior) | 3, up to 0.9 s | 0 | 0 |
| Toronto waterfront (Lake Ontario) | 87, up to 18.5 s | 0 | 0 |
| Montreal, Côte-Saint-Luc | 0 | 0 | 0 |
| Great Neck, Steppingstone Park | 6, up to 3.8 s | 0 | 0 |
| Copenhagen, Amager Strand (Øresund) | 144, up to 12.9 s | 0 | 0 |
| Helsinki (Gulf of Finland) | 33, up to 5.7 s | 0 | 1, −3.7 s |
| Brooklyn, Manhattan Beach (Atlantic) | 32, up to 5.2 s | 0 | 0 |
| Tel Aviv beach, sunset (Mediterranean) | 0 | 0 | 0 |
| Santa Monica, sunset (Pacific) | 0 | 0 | 0 |
| Armon HaNatziv, Druk 1993–96 / 1985–92 (521 / 1,400) | 11 / 31, up to 1.3 s | 0 (1993–96) | 0 |
| Nevei Yaakov, Golan (200) | 4, up to 0.9 s | 0 | 0 |

The refraction table (`_refractionTable`) halves its 0.1° steps in the lowest degree above the sea horizon
wherever the refraction midway misses the straight line by more than 1e-4°, down to 0.1/32°, and traces
directly where it still misses (a jump between rays that run along a warm layer over the water and rays that
clear it). The image search (`setTerrainImageSearch('auto')`, on by default) finds the water's or far shore's
visible edge above rays that only skim the water. Early Sun images (`setEarlySunImages`, off by default) let a
ray aimed just above the horizon that the air bends more than the horizon ray see the Sun first. The Jerusalem
validation numbers above are unchanged by all three.

### Columns on the real ground (`follow_terrain`) and radiosondes

On the 368 sunrises in Keller's text file (`DRUK9396.DAT`), ERA5 gave +0.6 s / 13.5 s rms, and ERA5 with
`follow_terrain` on −1.8 s / 15.2 s (Nov–Jan 23.8 s against 20.2 s, pushing an already late computed
sunrise later still): the shift, done with model air, made it worse, so it is off. Keller & Hall
(2022, *Computers & Geosciences* 161, 105044, Fig. 8) reported their version bringing the winter error from
32 s to 12 s.

Their version used Bet Dagan radiosondes shifted along a smoothed curve of the terrain. `sonde_profiles.py`
rebuilds that from NOAA's IGRA file for the station (`ISM00040179-data.txt`): the sounding nearest each
sunrise (6 h before to 1.5 h after), as measured (`raw`), shifted with pressure recomputed (`kh-t`), or
shifted with pressure kept as the paper words it (`kh-tp`). On the 336 of those sunrises with a sounding,
and on the 24 calm (surface wind ≤ 2 m/s) November–February mornings whose 05–06Z sounding shows an
inversion of at least 1 K in its lowest 300 m:

| atmosphere | rms, 336 days | mean \|difference\|, 24 calm winter inversion mornings |
|---|---|---|
| ERA5 path profiles | 13.4 s | 14.8 s |
| monthly normals + 13 h rule | 13.6 s | 17.2 s |
| ERA5, `follow_terrain` on | 15.0 s | 18.2 s |
| sounding, raw | 17.4 s | 22.0 s |
| sounding, shifted, pressure kept | 22.3 s | 24.8 s |
| sounding, shifted, pressure recomputed | 23.2 s | 28.3 s |

Shifting made it worse in every version, on all days and on theirs; a column every kilometre instead of every
5–10 km changed it by 0.1 s. Their baseline was off by 32 s on such days where these are off by ~20 s, and their
exact days, tracer (REF2017) and terrain (25 m DTM) differ, so this does not disprove their result, but it
gives no reason to turn `follow_terrain` on.

## 5. Development

### Tests

No network needed; those marked (rasterio) need it installed. From a checkout: `python3 tests/<name>.py`.

| test | checks |
|---|---|
| `test_e2e.py` | the forecast pipeline on synthetic GRIB files shaped like the grib filter's output, the API, and a region added to a stored cycle (the others kept, not fetched again) |
| `test_backfill.py` | the backfill against a local fake of the AWS archive (byte ranges, the pre-2021 layout, cropping, weighting, no double counting with the fetcher) |
| `test_climatology.py` | the NOAA climatology build on synthetic NetCDF files laid out like NOAA's (the 1991–2020 means and the rolling window of yearly files, including an unfinished year, a leap year and the stored yearly summaries), your own archive, and date ranges across all three sources |
| `test_follow_terrain.py` | columns moved onto the real ground (heights, pressures, depressions and offshore depths, the fade) |
| `test_ensemble.py` | the representative variations (weights, mean, the modes' spread), the `gfs-moments` store, and GEFS members from `fetch_gefs.py` on synthetic files |
| `test_dem_layers.py` (rasterio) | merged bare-earth tiles (priority per pixel, reprojection from the Israeli grid, open sea, aborting on unreadable sources) and tile revisions on rebuilds |
| `test_fine_terrain.py` (rasterio) | near-range fine terrain on a synthetic 1/3″ source (a narrow ridge the 1″ tiles miss, the max_km cut-off, chunks and markers), per-place versions and cache keys |
| `test_areas.py` | build_areas.py, the lookup and `area=auto` |
| `test_light_pollution.py` (rasterio) | the light-pollution lookup on synthetic atlas tiles (pixel indices against the tiles' georeferencing, reads across tile edges, area masks and percentiles, LERC copies, downloads filling gaps, conversion on download, the HTTP endpoint) |
| `test_haze_calibration.py` | the AERONET / CAMS calibration on a fake AERONET file and CAMS series (500 -> 550 nm, resuming, the factor's falloff, the endpoint) |
| `test_era5.py` | ERA5 profiles for past dates on synthetic NetCDF files in both CDS layouts (what is requested, time interpolation, ground-following, the command line) |
| `test_terrain_tiles.py` | `/v1/terrain-tiles` on a synthetic 1″ tile (heights at the pixel centres, the Terrarium encoding, the disk cache, 404 for low zooms without the data) |
| `test_sonde.py` | radiosonde profiles on a synthetic IGRA file (missing heights, the three versions, which sounding a date gets) |

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

### Data credits

GFS and GEFS: NOAA/NCEP. Elevation: NASA SRTM and USGS 3DEP (via the AWS Terrain Tiles open dataset); with
`terrain.layers`: USGS 3DEP, Natural Resources Canada MRDEM (Open Government Licence - Canada), and
FABDEM V1-2 (Hawker et al. 2022, University of Bristol; CC BY-NC-SA 4.0, contains modified Copernicus
Service information). With `terrain.fine`: Natural Resources Canada HRDEM (Open Government Licence -
Canada), USGS 3DEP 1/3″, and IGN RGE ALTI (Licence Ouverte Etalab 2.0; "IGN - RGE ALTI"). Reanalysis
climatology: NCEP/NCAR Reanalysis 1, data provided by the NOAA PSL, Boulder, Colorado, USA, from their
website at https://psl.noaa.gov. Light pollution: Falchi, F. et al. (2016), Supplement to: The New World
Atlas of Artificial Night Sky Brightness, GFZ Data Services, doi:10.5880/GFZ.1.4.2016.001 (CC BY-NC 4.0,
non-commercial only), via the tiles in github.com/astertaylor/halakhic_calc; growth of skyglow: Kyba,
C. C. M. et al. (2023), Science 379, 265; average sky vs zenith: Bará, S. et al. (2022), arXiv:2202.07526;
Ramon Crater readings: Israel Nature and Parks Authority, Ramon Crater International Dark Sky Park 2020
annual report. Haze: AERONET (NASA GSFC) and its station PIs; CAMS (Copernicus Atmosphere Monitoring
Service) via Open-Meteo. Place lookup: © OpenStreetMap contributors (Nominatim), GeoNames.

* Radiosondes (`sonde_profiles.py`): Durre, I. et al., Integrated Global Radiosonde Archive (IGRA) v2, NOAA NCEI.
* Past air for checking against observations (`era5_profiles.py`): Hersbach, H. et al. (2020), ERA5 hourly
  data on pressure levels / single levels, Copernicus Climate Change Service (C3S) Climate Data Store.
* Observed sunrises: Rabbi Avraham Druk, Yisroel Golan and Rabbi Yosef Zuriel, via Chaim Keller's
  github.com/chaimkeller/software.
* Official areas: NYC Department of City Planning (Neighborhood Tabulation Areas), Los Angeles Times
  "Mapping L.A.", US Census Bureau TIGER/Line, Israel Central Bureau of Statistics (statistical areas 2022),
  Israel Ministry of Interior boundaries via OpenStreetMap contributors (ODbL, extract by Geofabrik),
  Statistics Canada (2021 Census boundary files, Open Government Licence - Canada), the cities of Toronto,
  Ottawa, Hamilton, Calgary, Edmonton and Winnipeg, Halifax Regional Municipality and Ville de Montréal
  (CC BY 4.0) under their open-data licences, the cities of Chicago, Houston, Dallas, Phoenix, San Diego,
  Austin, Seattle and Baltimore, and geoBoundaries (CC BY 4.0 / per-country licences; see each area's
  `source`).
