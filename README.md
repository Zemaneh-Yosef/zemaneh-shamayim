# zmanim-sky-server

Real-world sky and horizon data for zmanim: refraction, terrain, and light pollution.

Zmanim are defined by what an observer sees: the Sun's edge clearing the real horizon, the stars
coming out over a real sky. Tables built on a flat horizon and a standard atmosphere get these times
wrong by minutes in hills, on coasts, and under city lights. This server supplies the real-world
inputs that the ROYZmanim app uses to correct for that:

| endpoint | what it returns |
|---|---|
| `/v1/path-profiles` | air-temperature profiles along the Sun's azimuth (NOAA GFS forecast or climatology) for ray-traced refraction |
| `/v1/horizon` | the terrain horizon from bare-earth elevation data, for visible sunrise / sunset (and moonrise / moonset with `moon=1`), for a point or a whole neighbourhood |
| `/v1/area` | the official municipality or neighbourhood containing a point |
| `/v1/light-pollution` | sky brightness from the World Atlas, brought forward to today, for when the stars become visible |
| `/v1/status` | the live forecast cycle and how much climatology each region holds |

No API keys are needed: every source is free public data (NOAA, USGS, NRCan, FABDEM, OpenStreetMap,
the US Census, the World Atlas). It's Python with a small set of dependencies and runs on a modest
VPS behind nginx.

## Refraction profiles (`/v1/path-profiles`)

Serves weather-model temperature profiles along the sunrise / sunset direction for ROYZmanim's
ray-traced refraction (`createServerPathAtmosphere` in `path-atmosphere.js`).

Data: NOAA GFS 0.25° (global), fetched from NOAA's NOMADS grib filter. No API key or registration;
NOAA data has no commercial-use restriction. GFS is global, so any region works, not only the US.

For each sunrise and sunset the API returns air-temperature profiles along the Sun's direction, from
the best source it has for that date:

| source | dates | detail |
|---|---|---|
| `forecast` | the next ~7 days | latest GFS run, 0.25°, 13 levels, water temperature |
| `climatology-gfs` | any date, once that month has data | typical values from your own archive of GFS runs (live, plus past runs from `backfill_gfs.py`), same detail |
| `climatology-ncep` | any date, from the first run | NOAA NCEP/NCAR Reanalysis 1, average of the last 10 complete years at 00/06/12/18 UTC, 2.5°, 4 levels below 700 mb |

Dates no source covers are left out, and the app uses its next provider (e.g. monthly normals).

* `fetch_gfs.py` (hourly timer) checks for a new GFS cycle (00Z and 12Z by default), downloads only
  the needed variables for your regions: temperature and height at 13 pressure levels (1000–500 mb,
  surface to ~5.5 km), 2 m temperature, surface (water) temperature, surface pressure, terrain
  height, land/water mask. Hourly to 120 h, 3-hourly to 168 h. Each hour is decoded and stored as a
  numpy array; a cycle goes live only when complete. Old cycles are pruned.
* After each run it also adds the first 12 hours (the most accurate part) to running averages by
  month and 3-hour slot of the day: your own climatology. A month is used once it holds
  `climatology.min_days` days (default 8), so it covers the whole year after about a year.
* The first time a region has no NOAA reanalysis climatology, the fetcher builds it (`build_prior.py`)
  from NOAA PSL's yearly files (no key): the last `climatology.ncep_years` complete years (default 10),
  so the fallback reflects today's climate rather than a 1991–2020 average centred on ~2005. Each year
  (~600 MB) is downloaded, reduced to a ~25 MB global summary kept under `climatology/ncep-years/`, and
  deleted: about 6 GB once, then each March (when NOAA has finished the previous year) the window moves
  forward and the prior is rebuilt with one new year downloaded. New regions need no download. A year NOAA
  has not finished is replaced by an earlier one. `ncep_years: 0` keeps NOAA's fixed 1991–2020 means.
  An existing server with a 1991–2020 prior switches over on its next fetcher run.
* Optionally, `backfill_gfs.py` fills your own climatology with past GFS runs (see below), so every
  month has GFS-detail typical profiles from the start instead of waiting a year.
* `serve.py` also answers `GET /v1/horizon` (terrain, below). It answers `GET /v1/path-profiles?lat=..&lon=..[&from=YYYY-MM-DD][&days=N]` (default:
  yesterday to a week ahead; up to `api.max_days`, 400) with, for each sunrise and sunset, profiles at
  0, 10, 25, 50, 100, 150, 200, 300 and 400 km along the Sun's azimuth, interpolated in space, time
  of day and season, plus the `source` used. `GET /v1/status` shows the live cycle and, per region,
  how many days of your own climatology each month holds (`days_per_month`, `months_ready`).

## Install (Debian/Ubuntu)

```sh
sudo useradd --system --home /opt/zmanim-sky-server zmanim-sky
sudo mkdir -p /opt/zmanim-sky-server /var/lib/zmanim-sky
sudo cp -r ./* /opt/zmanim-sky-server/
sudo chown -R zmanim-sky: /opt/zmanim-sky-server /var/lib/zmanim-sky
cd /opt/zmanim-sky-server
sudo -u zmanim-sky python3 -m venv venv
sudo -u zmanim-sky venv/bin/pip install -r requirements.txt
sudo -u zmanim-sky cp config.example.json config.json   # edit: contact e-mail, regions
```

### Regions

Each region is one lat/lon box (west < east, -180..180; no crossing the 180° meridian), fetched
separately. A request is served by the first region that contains the observer. Profiles reach 400 km
along the sunrise/sunset direction, so a box needs that margin east and west (about 4.7° of longitude
at 40°N, 6.3° at 55°N) and 2–2.5° north and south. Paths that leave the box are cut at its edge and
lose accuracy rather than failing.

Let the helper compute the box from the places you serve (a few outlying ones are enough) and save
it into config.json. You never enter paths or azimuths: the API computes those per request.

```sh
cd /opt/zmanim-sky-server
sudo -u zmanim-sky venv/bin/python suggest_region.py israel 31.78,35.22 32.08,34.78 29.56,34.95 32.97,35.50 --save
sudo -u zmanim-sky venv/bin/python suggest_region.py --list          # what is configured
sudo -u zmanim-sky venv/bin/python suggest_region.py --remove conus  # drop one
```

Or by name. It tries OpenStreetMap's Nominatim (free, no key), then GeoNames (if `geonames_username`
is set in config.json; free account with web services enabled), then a country table
(github.com/sandstrom/country-bounding-boxes; countries only, by name or ISO code such as `IL`).
`--source nominatim|geonames|countries` picks one. It shows which place and source matched, so check
that "Georgia" is the one you meant:

```sh
sudo -u zmanim-sky venv/bin/python suggest_region.py Israel --save              # saved as "israel"
sudo -u zmanim-sky venv/bin/python suggest_region.py "New York City" --save     # saved as "new-york-city"
sudo -u zmanim-sky venv/bin/python suggest_region.py tristate "New York City" Philadelphia --save
```

Without `--save` it only prints the box. Several small boxes cost less than one huge one: save far-apart
places as separate regions (the helper warns when a box gets very large). Data © OpenStreetMap
contributors; the lookup sends one request per place, within Nominatim's usage policy. Changing regions takes
effect on the next fetcher run: it notices that the stored data was made with other boxes, downloads
again, and the API switches over when that finishes.

First run by hand to see it work (takes 10–20 minutes for a full cycle):

```sh
sudo -u zmanim-sky ZMANIM_SKY_CONFIG=/opt/zmanim-sky-server/config.json venv/bin/python fetch_gfs.py
```

Then the services:

```sh
sudo cp deploy/zmanim-sky-fetch.service deploy/zmanim-sky-fetch.timer deploy/zmanim-sky-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zmanim-sky-fetch.timer zmanim-sky-api.service
curl http://127.0.0.1:8787/v1/status
```

Expose it through your web server for HTTPS (`deploy/nginx-location.conf`). The API sets
`Access-Control-Allow-Origin` from `api.allow_origin` in the config (default `*`).

### Backfilling your own climatology (optional)

After the first fetcher run, `backfill_gfs.py` adds past GFS runs from NOAA's archive on AWS Open Data
(`s3://noaa-gfs-bdp-pds`, from 2021; no key or account), so every month of a yearly calendar gets
GFS-detail typical profiles averaged over recent years, instead of the NOAA fallback until the server has
run for a year:

```sh
sudo -u zmanim-sky ZMANIM_SKY_CONFIG=/opt/zmanim-sky-server/config.json venv/bin/python backfill_gfs.py --dry-run
sudo -u zmanim-sky ZMANIM_SKY_CONFIG=/opt/zmanim-sky-server/config.json nohup venv/bin/python backfill_gfs.py &
```

The archive cannot cut out a region, so only the needed fields of each hour are downloaded for the whole
globe (byte ranges from the `.idx` files, ~20–25 MB per hour), decoded, cropped to your regions and
deleted. It samples one day in `--every` (default 4) and, on each, one hour in each 3-hour slot (hours 1,
4, 7, 10 of the 00Z and 12Z runs); each counts as a full live day. From 2021 that is roughly 80–100 GB of
download in total, over several hours to a day or two depending on bandwidth, with negligible disk use.
`--start` / `--end` limit the range (e.g. `--start 2023-01-01` for about half). It can be stopped and
restarted at any time and runs safely alongside the fetcher: runs already in the climatology are never
downloaded or counted again. Since the server's own days count one for one and the backfill samples, the
average leans towards the most recent years.

## Resources (default config: continental US + Israel, 2 cycles kept)

* Download: roughly 200–400 MB per cycle, about 280 requests spaced 2 s apart, twice a day.
* Disk: about 0.7 GB per stored cycle for the US box (float32 arrays), ~2 GB peak with two kept plus
  one being fetched. Fewer forecast hours (`forecast_hours.max`) or a smaller box reduce this.
* Your own climatology: 12 months x 8 slots of the same fields, about 330 MB for the US box (fixed
  size; it does not grow over time). The NOAA climatology is a few MB per region.
* NOAA reanalysis build: about 6 GB download once (10 years, ~600 MB of disk at a time), then ~600 MB
  each March; ~250 MB of yearly summaries kept. Adding a region downloads nothing.
* Optional backfill of your own climatology: roughly 80–100 GB download from 2021 at the default spacing,
  once; no lasting disk use beyond the climatology itself.
* CPU / RAM: negligible; arrays are memory-mapped.

## Terrain horizons for visible sunrise / sunset

`GET /v1/horizon?lat=..&lon=..` returns what `getVisibleSunrise` / `getVisibleSunset` / `getSunrises`
take (fetch it with `fetchHorizon` from `horizon-client.js`). It does not need the weather data, so it
works for any place between 60°S and 60°N, inside your regions or not.

| parameter | default | meaning |
|---|---|---|
| `area` | – | `auto`: search the whole official area the point is in (see below); the response's `area` names it |
| `radius_km` | 0 | 0: the exact coordinates. > 0: search a grid of spots within this radius (capped by `terrain.max_radius_km`, default 3) and return the few that see lowest in some direction |
| `bbox` | – | `south,west,north,east`: search every spot inside this box instead of a circle (e.g. a neighbourhood). `lat`/`lon` may be left out (box centre). Centre-to-corner distance is capped by `terrain.max_radius_km` (3 km); boxes with more than `terrain.max_candidates` (1200) spots get a coarser grid |
| `grid_m` | 100 | vantage grid spacing (minimum `terrain.min_grid_m`, 50) |
| `eye` | 1.7 | eye above the ground, m |
| `height` | ground + eye | observer height above sea level (point mode only), e.g. a roof or upper floor |
| `min_km` | 1 | terrain nearer than this is ignored (the observer's own building / hilltop) |
| `moon` | – | `1`: also return a `moon` horizon for visible moonrise / moonset (below) |

### Moonrise / moonset (`moon=1`)

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

Without `moon=1` the response, and its cache key, are exactly as before. With it, the first request for
a place costs roughly twice the time (one extra pass over the area's spots); then it is cached.

Elevation: SRTM tiles from `s3.amazonaws.com/elevation-tiles-prod/skadi` (free, no key), downloaded into
`terrain.tiles_dir` (default `<data_dir>/tiles`, ~2–8 MB each) the first time a request needs them.
Those tiles carry ocean depths offshore (to -85 m within a few km of New York); the line of sight meets
the water, so heights below sea level are raised to 0 and candidate spots in the water are skipped.
Places really below sea level keep their heights when the observer stands in one (ground below
-20 m: the Dead Sea, Jordan valley, Death Valley). Where the set has no tile at all, the area is open sea. Each answer is cached in `<data_dir>/horizons`, so a
place costs its computation once (point: ~1 s; 0.8 km vantage search: ~5–10 s). To work offline, put
the tiles in `tiles_dir` yourself and set `"tiles_url": ""`; a missing tile is then reported (HTTP 503)
instead of downloaded.

The same from the command line (same output; tiles downloaded into `./tiles` unless `--no-download`):

```sh
venv/bin/python make_horizon.py 31.7486 35.2374 --height 802 --out armon-hanatziv.json
venv/bin/python make_horizon.py 40.6092 -73.9683 --radius-km 0.8 --out brooklyn-vantage.json
venv/bin/python make_horizon.py --bbox 40.60,-73.98,40.62,-73.955 --out midwood.json
```

### Bare-earth terrain (`terrain.layers`, dem_layers.py)

The default tiles are USGS 3DEP (bare earth) in the US but mainly SRTM elsewhere, and SRTM is a
surface model: buildings and forest count as terrain and raise the horizon (later sunrise, earlier
sunset). With `terrain.layers.sources` set, every point of every line of sight takes its height from the
first source that has data there, then the plain tiles:

| order | source | covers | notes |
|---|---|---|---|
| 1 | USGS 3DEP 1 arc-second | US | bare earth, NAVD88; read per 1x1 degree file straight from USGS's bucket |
| 2 | NRCan MRDEM-30 DTM | Canada | bare earth, 30 m, CGVD2013; read from NRCan's VRT (Open Government Licence - Canada) |
| 3 | an Israeli DTM | Israel | `enabled: false` until you have a file (any projection, e.g. EPSG:2039) |
| 4 | FABDEM V1-2 | world | Copernicus 30 m with forests and buildings removed; CC BY-NC-SA 4.0, non-commercial only |
| 5 | the plain tiles | world | as before, for whatever is left (e.g. open sea) |

The line of sight crosses borders (Jerusalem's sunrise is over Jordan), so each source only fills the
pixels it covers and the next one fills the rest. Differences between vertical datums (NAVD88,
CGVD2013, EGM2008, EGM96) are well under a metre here and are not corrected.

Setup:

```sh
sudo -u zmanim-sky venv/bin/pip install rasterio      # GDAL included; needed to BUILD merged tiles and for /v1/light-pollution
# FABDEM: download the 10x10 degree zips you need from https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn
# (Israel / Jordan: N30E030-N40E040; US / Canada borders are covered by 3DEP and MRDEM) and unzip the
# 1x1 degree .tif files into <data_dir>/fabdem/. The config expects <name>_FABDEM_V1-2.tif with the
# south-west-corner name (e.g. N31E035_FABDEM_V1-2.tif); adjust the template if your files differ.
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 40.609283058016736 -73.96828881865329   # New York: tiles for a 150 km horizon
sudo -u zmanim-sky venv/bin/python dem_layers.py build --around 31.7683 35.2137    # Jerusalem
sudo -u zmanim-sky venv/bin/python dem_layers.py info N40W074                      # which source filled what
```

With `auto_build: true` (and rasterio installed) missing tiles are built during the first request that
needs them: a 150 km horizon touches ~9 tiles, so that first request can take minutes. Prebuilding the
cities you serve avoids it. Without rasterio, only prebuilt tiles are used and everything else falls
back to the plain tiles.

Safety: a source that doesn't cover a tile (no file there) is skipped quietly, but any other failure
(network, corrupt file, a configured file missing) aborts that tile's build - nothing is saved, the
plain tiles are used, and it is retried next time. A tile is never kept half-built from a worse source.

Merged tiles go to `<data_dir>/dem-layers/<signature>/` (float32, ~52 MB per land tile, memory-mapped;
sea-only tiles are a marker file). The signature is a hash of the enabled sources, and it is part of the
horizon cache key: changing the source list starts a new tile set and recomputes horizons. Horizons
from merged data carry `"terrain": {"layers": <signature>, "sources": [...]}`.

### Official areas (`area=auto`)

With `area=auto` the client sends only its coordinates; the server finds the official area it is in -
a neighbourhood, village, town or city - and searches that whole area, so everyone in it gets the same
visible sunrise (the earliest anywhere in it) and the same latest sunset. The answer is cached per
area, not per point. Use `fetchHorizonForArea(SERVER, lat, lon)` from `horizon-client.js`; it replaces
looking the place up in a `neighborhoods.js` on the client and calling `fetchHorizonForBox`.

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
| other countries | the municipal level (communes, comuni, local authority districts, ...; see `PLANS` in `build_areas.py`) | geoBoundaries (national statistics / mapping agencies) |

```sh
venv/bin/pip install pyshp osmium               # Census shapefiles; the OpenStreetMap extract
venv/bin/python build_areas.py                   # all countries; ~1-2 GB of downloads, cached in
                                                 # <data_dir>/area-sources (delete a file to refresh it)
venv/bin/python build_areas.py --only USA --only "Eretz Yisrael (Cities)"   # rebuild just these; the other countries' areas in areas.json are kept (--replace drops them)
```

It writes `<data_dir>/areas.json` (the server reloads it when it changes) and `areas_report.txt`: every
place with the units it got, the places still unresolved, chaiTable boxes that look too big (Zhytomyr,
Rome, Hamburg...), and areas over the cap. Read the report after each build; check every name match it
lists. To use an official file instead of a download (e.g. the CBS localities layer, or a city's own
neighbourhood layer), pass `--sources` with `{"Eretz Yisrael (Cities)": [{"type": "file", "path":
"localities.zip", "id_field": "<its code field>", "name_field": "<its English name field>", "kind":
"locality", "source": "Israel CBS localities"}]}` (shapefile, zipped shapefile or GeoJSON, in lon/lat).

How it is computed: for every 0.1° of azimuth across the year's sunrise and sunset directions, out to
150 km, the terrain point with the highest apparent elevation is kept (distance and height; the
calculator adds Earth curvature and refraction for the day's air). With a radius, candidates are first
compared at 0.5° steps; the smallest set of spots that together see lowest in every direction (within
0.003°) is kept, each with the azimuth ranges where it wins (`wins`), and only those are computed in
full. The calculator then traces each winning spot whose range contains the day's sunrise direction,
from its own position and height, and takes the earliest sunrise (latest sunset).

Checked against Rav Druk's 521 observed sunrises at Armon HaNatziv (1993–96): with Jerusalem's monthly
minimum temperatures the computed times are within ±7 s of the observations in every month on average
(rms 11 s with the inversion rule). For Brooklyn, the 0.8 km vantage set (100 m grid) agrees with a
brute-force search over a 49-spot grid to within ±5 s.

## Light pollution for nightfall by the stars

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

### From the 2014 atlas to today's sky

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
   park's 12 monitoring sites (2020).
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

Average sky, not zenith (`blpCdM2`): the atlas is the brightness overhead, but `calc_time.py`'s nightfall
applies one `Blp` to every star, and skyglow is brighter toward the horizon. Its author (Taylor 2026,
arXiv:2608.04064, §3) therefore multiplies the atlas by π, after the literature's ratio of horizontal
illuminance to zenith luminance (~π). But a sky of uniform luminance already has that ratio = π, so it
means average luminance = 1 × zenith. The measured ratio under light pollution peaks at 2.25 π (Bará et
al. 2022, arXiv:2202.07526): the average sky is about 2.25 × the zenith. So `blpCdM2` =
`light_pollution.sky_average_factor` (2.25) × `artificialCdM2`, about 30% less than calc_time.py's π.
(`artificialMcdM2` stays the zenith value: what a sky meter reads, and what to display.)

Better than any single factor, for a nightfall implementation that can take it: give each star its own
light-pollution luminance, `artificialCdM2 × X(Z, H)`, the airmass of the model's own Eq. 3. A luminance
growing with airmass averages to exactly 2 × the zenith (ratio 2 π, near the measured 2.25 π), and it makes
stars low in the sky harder to see than stars overhead, as they are.

What is still not modelled: which side the lights are on (the sky toward a city is brighter than the
sky away from it, at the same altitude), and the weather: the atlas assumes a clear, average atmosphere;
haze and snow cover brighten skyglow.

Data: the World Atlas of Artificial Night Sky Brightness (Falchi et al. 2016), 30" pixels, in the 4096 x
4096 pixel tiles of halakhic_calc's `data/` folder (`lp_{row}_{col}.tif`, 55 files, 488 MB, lossless zstd).
A tile is downloaded into `light_pollution.dir` (default `<data_dir>/light-pollution`) the first time a
request needs it; `light_pollution.py --download-all` fetches them all ahead (about a minute plus the
download).

Each download is re-compressed as it arrives (`light_pollution.convert`, default LERC_ZSTD with
`max_z_error` 0.001 mcd/m²) and saved as `light_pollution.file` (`lp_{row}_{col}_lerc.tif`); the
original is deleted. The world then takes 52 MB instead of 488. Before it replaces anything the converted
file is read back and checked (same grid, every pixel within `max_z_error`); a failed conversion leaves
nothing behind and is retried on the next request. Originals already in the folder from before are
converted the first time they are used (or all at once with `--download-all`). `"convert": {}` keeps
downloads as they come, under the URL's own file name. The options are GDAL GeoTIFF creation options, so
another codec works too.

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

```sh
venv/bin/python light_pollution.py 31.7767 35.2345
venv/bin/python light_pollution.py --bbox 40.60,-73.98,40.62,-73.955
curl 'http://127.0.0.1:8787/v1/light-pollution?lat=40.609283058016736&lon=-73.96828881865329&area=auto'
```

## Tests

`python3 tests/test_dem_layers.py` checks the merged bare-earth tiles (priority per pixel, reprojection
from the Israeli grid, open sea, aborting on unreadable sources) on synthetic rasters; needs rasterio.

`python3 tests/test_areas.py` checks build_areas.py, the lookup and `area=auto` without network.

`python3 tests/test_light_pollution.py` checks the light-pollution lookup on synthetic atlas tiles (pixel
indices against the tiles' georeferencing, reads across tile edges, area masks and percentiles, LERC
copies, downloads filling gaps, conversion on download (bounded error, nothing left behind on failure),
the HTTP endpoint); needs rasterio.

`python3 tests/test_e2e.py` runs the forecast pipeline on synthetic GRIB files shaped like the grib
filter's output; `python3 tests/test_climatology.py` checks the NOAA climatology build (on synthetic
NetCDF files laid out like NOAA's: the 1991–2020 means and the rolling window of yearly files, including
an unfinished year, a leap year and the stored yearly summaries), your own archive, and date ranges across
all three sources; `python3 tests/test_backfill.py` checks the backfill against a local fake of the AWS
archive (byte ranges, the pre-2021 layout, cropping, weighting, no double counting with the fetcher). No
network needed.

## Data credits

GFS: NOAA/NCEP. Elevation: NASA SRTM and USGS 3DEP (via the AWS Terrain Tiles open dataset); with
`terrain.layers`: USGS 3DEP, Natural Resources Canada MRDEM (Open Government Licence - Canada), and
FABDEM V1-2 (Hawker et al. 2022, University of Bristol; CC BY-NC-SA 4.0, contains modified Copernicus
Service information). Reanalysis climatology: NCEP/NCAR Reanalysis 1, data provided by the NOAA PSL,
Boulder, Colorado, USA, from their website at https://psl.noaa.gov. Light pollution: Falchi, F. et al. (2016), Supplement to: The New World Atlas of
Artificial Night Sky Brightness, GFZ Data Services, doi:10.5880/GFZ.1.4.2016.001 (CC BY-NC 4.0, non-commercial
only), via the tiles in github.com/astertaylor/halakhic_calc; growth of skyglow: Kyba, C. C. M. et al. (2023),
Science 379, 265; average sky vs zenith: Bará, S. et al. (2022), arXiv:2202.07526; Ramon Crater readings: Israel Nature and Parks Authority, Ramon Crater International Dark
Sky Park 2020 annual report. Place lookup: © OpenStreetMap
contributors (Nominatim), GeoNames.

* Official areas: NYC Department of City Planning (Neighborhood Tabulation Areas), Los Angeles Times
  "Mapping L.A.", US Census Bureau TIGER/Line, Israel Central Bureau of Statistics (statistical areas 2022), Israel Ministry of Interior boundaries via
  OpenStreetMap contributors (ODbL, extract by Geofabrik), Statistics Canada (2021 Census boundary files,
  Open Government Licence - Canada), the cities of Toronto, Ottawa, Hamilton, Calgary, Edmonton and Winnipeg,
  Halifax Regional Municipality and Ville de Montréal (CC BY 4.0) under their open-data licences, the cities of
  Chicago, Houston, Dallas, Phoenix, San Diego, Austin, Seattle and Baltimore, and geoBoundaries (CC BY 4.0 / per-country licences; see each
  area's `source`).

## Caveats

* The GFS decoder is verified on a real NOMADS file; the NOAA reanalysis reader was tested on files
  built to NOAA's documented layout only. `build_prior.py` logs each file's variables, units and a
  sanity summary (near-surface temperature range) - check that log on the first run. Likewise
  `backfill_gfs.py` was tested against a fake archive built to the AWS layout: watch the first few runs
  of a real backfill (`-v`).
* If NOAA changes the grib filter URLs, edit `grib_filter` in the config (see
  https://nomads.ncep.noaa.gov/ for the current ones). NOMADS throttles heavy users; keep
  `request_delay_s` at 1–2 s.
* GFS at 0.25° (~25 km) resolves the air over land vs. water along the path, not individual valleys
  or a few-km sea breeze.
