# How it works

What each endpoint computes, from which data, and why. For installing and running the server see the
[README](../README.md); for how well the results match sunrises people actually watched see
[Validation](validation.md).

**Contents**

* [Weather along the path](#weather-along-the-path-v1path-profiles), with its
  [guardrails](#guardrails-on-the-weather-data-weather_guardpy) (with the climate envelope) and
  [ensembles](#ensembles-ensemble1-typical-outcomes-and-error-bars)
* [Terrain horizons](#terrain-horizons-v1horizon) and [moonrise / moonset](#moonrise--moonset-moon1)
* [Elevation data](#elevation-data): default tiles, bare-earth layers, fine terrain, terrain versions
* [Official areas](#official-areas-areaauto)
* [Light pollution](#light-pollution-for-nightfall-by-the-stars) and
  [haze calibration](#haze-calibration-for-nightfall-by-the-stars), for nightfall by the stars
* Terrain for the website's 3D view: [web-map tiles](#terrain-tiles-for-the-3d-view-v1terrain-tiles) and
  [Cesium's format](#terrain-for-the-3d-view-in-cesiums-format-v1cesium-terrain)

## Weather along the path (`/v1/path-profiles`)

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

`GET /v1/path-profiles?lat=..&lon=..[&from=YYYY-MM-DD][&days=N][&ensemble=1][&typical=1]` (default: yesterday to a
week ahead; up to `api.max_days`, 400) answers, for each sunrise and sunset, with profiles at 0, 10, 25,
50, 100, 150, 200, 300 and 400 km along the Sun's azimuth (`api.distances_km`), interpolated in space,
time of day and season, plus the `source` used. With `typical=1` the forecast is skipped and every date
comes from climatology, so a printed calendar that runs past the forecast has no step where the forecast
ends. `GET /v1/status` shows the live cycle and, per region,
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
  the [backfill](../README.md#backfilling-your-own-climatology). The 80 m / 100 m air has a store of its own
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
  Rav Druk's observations it made the fit worse ([Validation](validation.md#columns-on-the-real-ground-follow_terrain-and-radiosondes)),
  so it is off; `"follow_terrain": {"enabled": true}` turns it on.

## Guardrails on the weather data (`weather_guard.py`)

Every GFS and GEFS file is checked as it is decoded, and the NOAA reanalysis climatology before it is saved, for
values no air or ground can have: the limits are the Earth's recorded extremes with a margin (2 m air 178–334 K,
from the −89.2 °C Vostok and 56.7 °C Death Valley records; surface pressure 300–1100 mb; ground −500 to 9,000 m;
air on pressure levels 160–350 K, room left for GFS's extrapolation below high ground; level heights rising as the
pressure falls). A file that fails is treated as damaged: the forecast run is not published and the previous one
keeps serving (the next fetch tries again), the backfill skips that hour so nothing of it reaches the climatology,
and the climatology in use is not replaced. Before a new GFS run is published, its first day is also compared with
the serving run's forecast for the same hours: consecutive runs differ there by well under 1 K and 1 mb over most
of the grid, so a median difference over a region of more than 3 K (2 m air), 3 mb (surface pressure) or 40 m
(500 mb height) means a damaged or misread run, and it is held back and fetched afresh next time. The log says
which fields and by how much. Real weather is never refused: a cold wave, a heat wave or a deep storm moves only
part of a region, and the limits are beyond any record.

The website checks what it receives again (`atmosphere-guard.js` in the website's KosherZmanim library): the
same physical limits per value, a path profile's levels against the hypsometric equation, temperature gradients
no air sustains, haze outside 0–10, and a forecast more than 45 °C from the place's normal; what fails goes to
the next source, and with none left the standard refraction model applies.

The place's normal comes from the monthly normals the website downloads (Open-Meteo's ERA5, else NOAA's station
normals). When neither arrives, `GET /v1/climate-envelope?lat=..&lon=..` (`climate_envelope.py`) is its reference:
for the place's 5° cell and each month, the lowest and highest near-surface temperature in NOAA's 1991–2020 daily
long-term means (4 times a day) over every 2.5° reanalysis point on or inside the cell, carried to sea level at
6.5 K/km over the reanalysis terrain (`seaLevelC.min` / `.max`, `lapseKPerM`). An envelope rather than a normal, so
a coarse cell never refuses real air in a valley or on a plateau inside it: the website carries it to the
forecast's height and refuses only a value more than 45 °C outside the whole range (−50 °C in a New York July).
It is built once by the fetcher (~70 MB from NOAA, deleted after; with `climatology.ncep_prior`) and answers
anywhere on Earth; 503 until it has been built.

## Ensembles (`&ensemble=1`): typical outcomes and error bars

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

## Terrain horizons (`/v1/horizon`)

`GET /v1/horizon?lat=..&lon=..` returns what `getVisibleSunrise` / `getVisibleSunset` / `getSunrises`
take (fetch it with `fetchHorizon` from `horizon-client.js`). It does not need the weather data, so it
works for any place between 60°S and 60°N, inside your regions or not.

| parameter | default | meaning |
|---|---|---|
| `area` | – | `auto` (or `city`): the whole official area the point is in; `finest`: the finest official unit. See [Official areas](#official-areas-areaauto); the response's `area` names it |
| `outline` | – | `1`: with `area`, the response's `area` also carries its boundary (`polygons`), so a client can tell which later fixes share the answer |
| `radius_km` | 0 | 0: the exact coordinates. > 0: search a grid of spots within this radius (capped by `terrain.max_radius_km`, default 3) and return the few that see lowest in some direction |
| `bbox` | – | `south,west,north,east`: search every spot inside this box instead of a circle (e.g. a neighbourhood). `lat`/`lon` may be left out (box centre). Centre-to-corner distance is capped by `terrain.max_radius_km` (3 km); boxes with more than `terrain.max_candidates` (1200) spots get a coarser grid |
| `grid_m` | 100 | vantage grid spacing (minimum `terrain.min_grid_m`, 50) |
| `eye` | 1.7 | eye above the ground, m |
| `height` | ground + eye | observer height above sea level (point mode only), e.g. a roof or upper floor |
| `min_km` | 1 | terrain nearer than this is ignored (the observer's own building / hilltop) |
| `moon` | – | `1`: also return a `moon` horizon for visible moonrise / moonset (below) |

### How a horizon is computed

For every 0.1° of azimuth across the year's sunrise and sunset directions, out to 150 km, the terrain
point with the highest apparent elevation is kept. Each kept point is its distance and height; the
calculator adds Earth curvature and refraction for the day's air.

* **Which directions.** 3° past the solstices' rise and set directions, and further where the terrain is
  high: over a high horizon the winter Sun is first seen well south of its sea-level rise direction (north
  in the southern hemisphere), having moved along its slanted path. So each edge is widened in 0.5° steps
  while the terrain there (an area's lowest horizon among its spots) stands within 0.5° of the solstice
  Sun's altitude, and 1° past the last such step. Nothing is added where the horizon is low. At Salt Lake
  City's point horizon (6° to the south-east) the sunrise range gained 3.5°, and with it the 36 December
  mornings it used to lose.
* **How finely.** Distances are stepped 0.4% apart (200 m at 50 km, 600 m at 150 km). Each direction is
  then sampled again, finely, two steps either side of its three highest ridges (and of each alternate's).
  Without that, a narrow crest between two steps was read low, by up to 1.3′ at Armon HaNatziv and 4′ at
  Salt Lake City against 5 m sampling (sunrise up to 6 s and 21 s early in those directions); with it the
  worst direction at either is within 0.2′ (under a second), at about the same cost. Over a year at Armon
  HaNatziv this moves the visible sunrise 1.4 s later on average, at most 6.2 s.
* **`alternates`.** Which point is highest depends on the refraction it is chosen with (standard air,
  k = 0.13): a strong inversion can make a farther, higher ridge the one that hides the Sun. So where
  another coefficient from −0.2 to 0.9 would pick a different point, the entry also lists it under
  `alternates` (`[[distanceKm, heightM], ...]`, usually absent); the calculator traces the ray to each
  through the day's air and keeps the highest.
* **`floorM`.** The lowest surface the line of sight crosses out to its farthest point (a lake's own
  surface, the sea, or level ground); rays come down no lower. Using sea level everywhere instead let rays
  to the far shore of Lake Michigan pass under the lake.
* **Areas (`radius_km`, `bbox`, `area`).** Candidates are first compared at 0.5° steps; the smallest set
  of spots that together see lowest in every direction (within 0.003°) is kept, each with the azimuth
  ranges where it wins (`wins`), and only those are computed in full. The calculator then traces each
  winning spot whose range contains the day's sunrise direction, from its own position and height, and
  takes the earliest sunrise (latest sunset). For Brooklyn, the 0.8 km vantage set (100 m grid) agrees
  with a brute-force search over a 49-spot grid to within ±5 s.

The response's `terrain.algo` numbers these versions of the method: 3 added `alternates` and `floorM`, 4
the widened directions and the crest refinement.

### Caching, slow first answers and worker processes

Each answer is cached in `<data_dir>/horizons`, so a place costs its computation once (point: ~1 s;
0.8 km vantage search: ~5–10 s; minutes the first time its terrain data is downloaded and built). A computation
runs in the background: a request waits for it up to `terrain.answer_within_s` (45 s, under nginx's 120 s and
Cloudflare's 100 s) and is otherwise answered 503 with `"computing": true` and `"retryAfterS"` (also the
`Retry-After` header); asking again joins the same computation instead of starting another, and once it is done
the answer is the stored file. A computation that fails is reported to the next request, and the one after
tries again.

The computations run in `terrain.max_parallel` (2) worker processes, not in the API process. A first horizon
is CPU-bound Python and numpy for seconds to minutes, and the API process answers every request in threads
that share one GIL, so a few first horizons at once (as right after a launch) would otherwise slow every other
reply, cached ones too. The API's thread for a computation only waits for a worker's result.

* The pool starts with the first computation, with the `forkserver` start method (fresh processes, not forks
  of the threaded server). Each worker keeps its own tiles in memory (up to ~12 × 26 MB) and shares the files
  on disk.
* The workers report each download and build they time, and when the search proper starts, so `etaS` and
  `horizon-timing.json` (below) learn from them.
* Two processes never download or build the same tile or fine chunk at once: an `flock` on a file in a
  `.locks` folder beside the data, with files written under a temporary name and then renamed. A fine chunk
  that failed to build leaves a `.failed` marker, so every process waits the same hour before trying again.
* If a worker dies (killed, out of memory), the computations under way are answered 503 with an error once,
  the next request starts them again, and the pool is started anew.
* `terrain.worker_processes: false` computes in the API process's threads instead (the tests that replace
  `terrain.horizon_set` use it).
* The API handles at most `api.max_threads` (64) requests at once. More wait in the listen backlog for a free
  thread instead of each starting one more thread that slows the rest. A request waiting for a horizon holds
  its thread for up to `terrain.answer_within_s`.

The 503 says how long it should still take: `etaS`, from what is left (`work`: the 1″ tiles its rays cross not
yet on disk, the fine chunks not yet built, the spots to search) times how long each takes on this server.
Those times are learned from the real ones (`<data_dir>/horizon-timing.json`; until measured: 15 s a download,
the median of the merged tiles built so far or 60 s, 6 s a fine chunk, ~1 s + 0.04 s a spot), so the estimate
gets closer as the server works. `retryAfterS` is `etaS` within 3–60 s. `make_horizon.py` does
the same from the command line (same output as `/v1/horizon`; tiles downloaded into `./tiles` unless
`--no-download`):

```sh
venv/bin/python make_horizon.py 31.748553 35.237436 --height 754.9 --out armon-hanatziv.json
venv/bin/python make_horizon.py 40.6092 -73.9683 --radius-km 0.8 --out brooklyn-vantage.json
venv/bin/python make_horizon.py --bbox 40.60,-73.98,40.62,-73.955 --out midwood.json
```

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

Without `moon=1` the response has no `moon` entry and is cached separately from moon requests. With it,
the first request for a place costs roughly twice the time (one extra pass over the area's spots); then
it is cached.

## Elevation data

Three tiers, each used where it has data: the default tiles everywhere, merged bare-earth layers at 1″
(`terrain.layers`), and finer bare-earth data within 10 km of the observer (`terrain.fine`). Setup is in
[Terrain data](../README.md#terrain-data) in the README.

### Default elevation tiles

Unless bare-earth layers are configured, all heights come from the AWS Terrain Tiles: SRTM worldwide, with
USGS 3DEP in the US, from `s3.amazonaws.com/elevation-tiles-prod/skadi` (free, no key;
`terrain.tiles_url`). They are downloaded into `terrain.tiles_dir` (default `<data_dir>/tiles`, ~2–8 MB
each) the first time a request needs them. Those tiles carry ocean depths offshore (to -85 m within a few
km of New York); the line of sight meets the water, so heights below sea level are raised to 0 and
candidate spots in the water are skipped. Places really below sea level keep their heights when the
observer stands in one (ground below -20 m: the Dead Sea, Jordan valley, Death Valley). Where the set has
no tile at all, the area is open sea. To work offline, put the tiles in `tiles_dir` yourself and set
`"tiles_url": ""`; a missing tile is then reported (HTTP 503) instead of downloaded.

### Bare-earth layers (`terrain.layers`, dem_layers.py)

The default tiles are bare earth only in the US (3DEP). Elsewhere they are SRTM, a surface model:
buildings and forest count as terrain and raise the horizon (later sunrise, earlier sunset). With
`terrain.layers.sources` set, every point of every line of sight takes its height from the first source
in this list that has data there:

| order | source | covers | notes |
|---|---|---|---|
| 1 | USGS 3DEP 1 arc-second | US | bare earth, NAVD88; read per 1x1 degree file straight from USGS's bucket |
| 2 | NRCan MRDEM-30 DTM | Canada | bare earth, 30 m, CGVD2013; read from NRCan's VRT (Open Government Licence - Canada) |
| 3 | an Israeli DTM | Israel | a file you supply (any projection, e.g. EPSG:2039); `enabled: false` until then. The Survey of Israel and the Geological Survey of Israel (25 m DTM) have one, but neither is openly published: it has to be licensed or requested |
| 4 | FABDEM V1-2 | world, 60°S–80°N | Copernicus 30 m with forests and buildings removed; each 1×1° tile read from inside Bristol's 10×10° zip over HTTP when its merged tile is built (nothing stored); CC BY-NC-SA 4.0, non-commercial only |
| 5 | ALOS AW3D30, outside forest | world, 82°S–82°N | JAXA's surface model (2006–11), from OpenTopography's bucket; pixels ESA WorldCover 2021 calls trees are left to the next source. Only where FABDEM has nothing (north of 80°N) or can't be reached |
| 6 | the default tiles | world | SRTM (2000) outside the US: in forest where 4 has nothing; and open sea |

Why 5 and 6 in that order: against lidar (USGS 3DEP 1″ on nine 1×1° tiles from Phoenix and Los Angeles to
New York and the Smokies), FABDEM was the closest on every tile and land-cover class (median error −1.3 to
+1.0 m, RMSE 2.9–6.8 m). The two surface models read high: ALOS beat SRTM in open, dry and Mediterranean
terrain (RMSE 3.8–4.3 m against 4.9–5.7 m in Phoenix and Los Angeles) but was worse in forest (Smokies: 16.5 m
against 14.0 m, 10–12 m high under trees), so each is used where it does better; together they were within
about 1′ of the better of the two on horizons at every site. On Rabbi Druk's sunrises at Armon HaNatziv the
far horizon (Moab, 45–60 km) is the same in all three within 0.3′; a hillside 0.5 km away is where they
differ (summer sunrises over it: SRTM 68 s rms, ALOS 46 s, FABDEM 14 s).

The line of sight crosses borders (Jerusalem's sunrise is over Jordan), so each source only fills the
pixels it covers and the next one fills the rest. Differences between vertical datums (NAVD88,
CGVD2013, EGM2008, EGM96) are well under a metre here and are not corrected.

With `auto_build: true` (and rasterio installed) missing tiles are built during the first request that
needs them. Without rasterio, only prebuilt tiles are used and everything else falls back to the default
tiles. Safety: a source that doesn't cover a tile (no file there) is skipped quietly, but any other failure
(network, corrupt file, a configured file missing) aborts that tile's build - nothing is saved, the
default tiles are used, and it is retried next time. A remote file counts as missing only when its server
says so (404, or S3's 403 `<Error>`; for a file inside a remote zip, when the zip's table of contents has no
such file): no answer, a 5xx or a refusing proxy is a failure.

A source with `"if_unavailable": "skip"` (FABDEM and ALOS in the example) is the exception: when it can't be
read, the tile is built from the sources after it and kept, marked provisional (`<tile>.provisional`,
`"provisional_without"` in `dem_layers.py info`), as that is still better than the default tiles alone. On its
first use an hour later it is rebuilt; if the source answers, the provisional mark goes, and the tile's
revision rises if its heights changed. A source's `"mask"` (`paths` to a land-cover raster, read by
majority onto the grid, and the classes to `exclude`) gives up those pixels to the next source; it is part
of the signature.

Template fields for per-tile paths: `{name}` (N31E035), `{ns}{lat_top:02d}{ew}{lon_left:03d}` (USGS's
north-west-corner naming, n32e035), `{NS_sw}{lat_sw:03d}{EW}{lon_left:03d}` (ALOS: N031E035), `{name3}`
(the 3×3° block, WorldCover: N30E033) and `{name10}-{name10_end}` (the 10×10° block, FABDEM's zips:
N30E030-N40E040).

Merged tiles go to `<data_dir>/dem-layers/<signature>/` (float32, ~52 MB per land tile, memory-mapped).
A tile no source has data for is only a marker: `.sea` where there is no plain tile either, `.plain` where the
plain tile is all there is (open sea, where it holds depths: the line of sight meets the water at 0 m
whatever the source, so the plain tile is read instead of a 52 MB copy of it). `dem_layers.py slim` swaps
such copies built before this for markers. The signature is a hash of the enabled sources: changing the source
list starts a new tile set and recomputes horizons. A rebuild (`build --force`) that changes a tile's
heights raises its revision (`<tile>.rev`); both are part of the terrain version of every horizon within
`max_km` of the tile ([Terrain versions](#terrain-versions-v1terrain-version)).

### Fine terrain near the observer (`terrain.fine`)

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

### Terrain versions (`/v1/terrain-version`)

Each horizon names the terrain that built it: `terrain.layers` (the layers' signature, plus the revisions
of rebuilt tiles within `max_km`) and, where fine data covers it, `terrain.fine.version` (the fine
sources' signature; with `-partial` added while a chunk there failed to build, and then not cached, so the
next request tries again), as `"terrain": {"layers": <version>, "sources": [...], "fine": {"signature", "version",
"withinKm", "sources"}}`. Both versions are part of its cache key. `GET /v1/terrain-version` takes the same query as
`/v1/horizon` and answers with them alone (`{"fine": ..., "layers": ...}`), without computing the horizon
(building the fine chunks it would read if needed). royzmanimwebsite asks it about once a day for each
stored horizon and downloads the horizon again only when the two differ, so visitors get new terrain
where it changed and nothing else is downloaded again.

## Official areas (`area=auto`)

With `area=auto` the client sends only its coordinates; the server finds the official area it is in and
searches that whole area, so everyone in it gets the same visible sunrise (the earliest anywhere in it) and
the same latest sunset. The answer is cached per area, not per point. Use `fetchHorizonForArea(SERVER, lat,
lon)` from `horizon-client.js`; the client needs no list of places of its own. Areas cover the whole world.

Which area, by the Chai Tables' rule (their introduction, after the *nivreshes* and Rabbi Yosi's praise of
Tiberias): a city has one sunrise - the earliest seen anywhere in it ("ein osim agudos agudos") - and
Jerusalem, about 16 km by 15 km, is such a city. Only a bigger city is divided, by the neighbourhood its
people live in. So:

* `area=auto` (or `area=city`): the town, village or city containing the point, whole, when it is no bigger
  than Jerusalem (`areas.city_radius_km`, 13 km from the centre of its box to a corner: Jerusalem is
  11.2 km by the CBS locality outline and 12.8 km by its municipal boundary); a bigger one by the official neighbourhood or district containing the point (New
  York's NTAs, Toronto's neighbourhoods, Johannesburg's wards...), or whole where it has none
* `area=finest`: the finest official unit containing the point - a Jerusalem sub-quarter, a New York NTA,
  a village rather than the town around it - for those who divide even a small city

```sh
curl 'http://127.0.0.1:8787/v1/area?lat=40.609283058016736&lon=-73.96828881865329'               # which area (no polygons)
curl 'http://127.0.0.1:8787/v1/area?lat=31.7767&lon=35.2345&area=finest'                          # the finest unit
curl 'http://127.0.0.1:8787/v1/horizon?lat=40.609283058016736&lon=-73.96828881865329&area=auto&radius_km=0.8'
```

A point in no official area (the sea, a regional council's open land), or in one bigger than
`terrain.max_area_radius_km` (30 km from centre to corner, separate from the 3 km cap on boxes clients
send), gets the request as given (here the 0.8 km radius) with `"area": null` and an `areaNote` saying why.
Most of a country whose municipalities are bigger than that (Russia's districts, Saudi Arabia's
governorates) falls back this way.

The `area` in an answer is `{"id", "name", "kind", "level", "source", "country", "bbox", "radiusKm"}` without
the boundary, and neighbouring areas' boxes overlap. A client that stores the answer and wants to reuse it for
later fixes adds `outline=1` (to `/v1/horizon`, `/v1/terrain-version` or `/v1/area`): `area` then also
carries `polygons`, exactly as `areas.sqlite` stores them (a list of polygons, each a list of rings, outer
ring first then holes, each ring a flat `[lon, lat, lon, lat, ...]` list; already simplified to ~25 m by
`build_areas.py` and sent as is). A point shares the stored answer when an even-odd test over all the rings
of any polygon (`areas.area_contains`) puts it inside. The horizon is computed and cached the same either
way (and once for both rules when they pick the same area), and without `outline=1` an answer carries no
polygons. `/v1/terrain-version` adds `area` (or `"area": null` and `areaNote`) only with `outline=1`.

```sh
curl 'http://127.0.0.1:8787/v1/horizon?lat=40.609283058016736&lon=-73.96828881865329&area=auto&outline=1'
# ..., "area": {"id": "nyc-nta:BK1503", "name": "...", "level": 30, "bbox": [s, w, n, e], "radiusKm": ...,
#               "polygons": [[[lon, lat, lon, lat, ...], [hole ...]], ...]}
```

Big areas get a coarser grid (`terrain.max_candidates` spots in all), so a 25 km city is searched on a ~1 km
grid.

The areas come from `areas.sqlite`, built by `build_areas.py` from official boundaries alone - every country,
every unit, no list of places:

<details>
<summary>Which units, and from where, country by country</summary>

* **New York City**: 2020 Neighborhood Tabulation Areas. Source: NYC Department of City Planning.
* **City of Los Angeles**: "Mapping L.A." neighbourhoods. Source: Los Angeles Times.
* **Chicago, Houston, Dallas, Phoenix, San Diego, Austin, Seattle, Baltimore**: the city's own layer: Chicago's 77 community areas, Houston's super neighborhoods, Dallas's council districts (it has no citywide neighbourhood layer), Phoenix's urban villages, San Diego's community plan areas, Austin's neighborhood planning areas, Seattle's Neighborhood Map Atlas neighborhoods, Baltimore's neighborhood statistical areas. Source: each city's open-data / GIS service (see `US_CITIES` in `build_areas.py`).
* **the US, Puerto Rico and the territories**: incorporated places and census-designated places, then county subdivisions (towns). Source: US Census TIGER/Line.
* **Israel**: municipal jurisdictions; locality outlines for villages inside regional councils (regional councils are never an area); sub-quarters (תת-רובע) of the cities of 40,000+, statistical areas for Beit Shemesh, Haifa, Jerusalem, Safed and Tiberias where they have no sub-quarters. Source: OpenStreetMap, whose `admin_level=8` boundaries are the Ministry of Interior's (Geofabrik's daily Israel and Palestine extract, one download); Central Bureau of Statistics, statistical areas 2022 (its ArcGIS service).
* **Toronto, Montreal, Ottawa, Hamilton, Halifax, Calgary, Edmonton, Winnipeg**: the city's own neighbourhoods: Toronto's 158 neighbourhoods, Montreal's arrondissements, Ottawa Neighbourhood Study Gen 3, Hamilton's planning units, Halifax's communities, Calgary's community districts, Edmonton's and Winnipeg's neighbourhoods. Source: each city's open-data portal (see `COUNTRY_PLANS["CAN"]` in `build_areas.py`).
* **Canada**: 2021 census subdivisions (municipalities); amalgamated cities such as Halifax (~190 km) or Ottawa (~55 km) are far over the area cap, hence the layers above. Source: Statistics Canada 2021 cartographic boundary file (`lcsd000b21a_e.zip`, one download, reprojected from Statistics Canada Lambert).
* **every other country**: the municipal level (communes, comuni, municipalities, local authority districts, Indian villages and towns, ...), and where geoBoundaries has one a finer level inside it as neighbourhoods (South African wards, Korean dong, Portuguese freguesias, Czech and Austrian cadastral areas, ...). Chosen per country in `GB_LEVELS`; a country it does not list takes its finest level, or the one above it when the finest is named like a unit inside municipalities (wards, villages, cells, barrios...). Source: geoBoundaries (each country's national statistics or mapping agency; the source and licence are in each area's `source`).

</details>

```sh
sudo -u zmanim-sky venv/bin/pip install pyshp osmium               # Census shapefiles; the OpenStreetMap extract
sudo -u zmanim-sky venv/bin/python build_areas.py                   # the world; a few GB of downloads, cached in
                                                                    # <data_dir>/area-sources (delete a file to refresh it)
sudo -u zmanim-sky venv/bin/python build_areas.py --only USA --only ISR    # rebuild just these (ISO 3166 alpha-3
                                                                    # codes); the other countries' areas are kept
```

It writes `<data_dir>/areas.sqlite.tmp` beside the old file and swaps it in whole at the end (the server
rereads it when it changes, and deletes an `areas.json` from before). Each country is
committed to the `.tmp` as it is done, so a build that is killed (out of memory, a reboot) and started again
with the same countries and options goes on from the next country (`--restart` to start over). The big
sources - geoBoundaries, TIGER, Statistics Canada - are read a feature at a time, so no country needs all its
boundaries in memory at once. It also writes `areas_report.txt`: per country how
many areas of each kind, how many are over the cap, and slivers dropped. A country whose source fails keeps
the areas it had (the report says FAILED; the build exits 1) - run it again with `--only` for that country.
To use an official file instead of a download (e.g. a city's own neighbourhood layer), pass `--sources`
with `{"ISR": [{"type": "file", "path": "localities.zip", "id_field": "<its code field>", "name_field": "<its
English name field>", "kind": "locality", "source": "Israel CBS localities"}, ...]}` (shapefile, zipped
shapefile or GeoJSON, in lon/lat; it replaces the country's whole plan).

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

```jsonc
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

With `area` (`auto` or `finest`, as for horizons) or `bbox` the top-level values are the area's percentile, and the response adds `area`
(or `bbox`), `stats` (`pixels`, `min`, `p10`, `median`, `p90`, `max`, `mean`, `percentile`, corrected
like the value) and `point` (the point's own answer, as above). A point in no known area, an area over the
cap, or one smaller than a pixel gets the point's value with `area: null` and an `areaNote`. Outside the
atlas (north of 85.05° N, south of 60.00° S) the answer is 0 with `"coverage": false` and `"pixel": null`.

`pixel` is the atlas pixel the point's value was read from (`row`, `col` on the 30″ grid and its centre's
`lat`, `lon`). The atlas is the same everywhere on a pixel, so a client may reuse a point answer for any fix
on the same pixel, by comparing `pixel` rather than copying the atlas's grid constants.

```sh
venv/bin/python light_pollution.py 31.7767 35.2345
venv/bin/python light_pollution.py --bbox 40.60,-73.98,40.62,-73.955
curl 'http://127.0.0.1:8787/v1/light-pollution?lat=40.609283058016736&lon=-73.96828881865329&area=auto'
```

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
   park's 12 monitoring sites (2020). Globe at Night's public sky-meter readings are read beside your own
   (`measurements.globe_at_night_file`, default `<data_dir>/sky-measurements-gan.json`), built once a year by

   ```sh
   sudo -u zmanim-sky venv/bin/python build_sky_measurements.py [--since 2020]
   ```

   which keeps the readings taken on a clear night with the Sun 18 degrees down and the Moon below the
   horizon (2020-2025: ~3,600 readings worldwide, most in North America and Europe; 28 within 15 km of
   Lakewood, NJ, one of Jerusalem). Data: [globeatnight.org](https://globeatnight.org), NSF NOIRLab, CC BY 4.0.
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

### Average sky, not zenith (`blpCdM2`)

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

### The atlas files

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

## Haze calibration for nightfall by the stars

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

## Terrain tiles for the 3D view (`/v1/terrain-tiles`)

`GET /v1/terrain-tiles/{z}/{x}/{y}.png` serves the heights the horizons are computed from (the fine near-range
data, else the bare-earth layers, else the 1″ tiles) as web-Mercator tiles in the Terrarium encoding (height =
R × 256 + G + B / 256 − 32768 m, the format of the open AWS Terrain Tiles), so the website's 3D horizon view
shows the ground the horizon was found on. Zooms 8–14 only. A tile below zoom 11 spans several 1° tiles, so it
is made only from data already on the server (computing a horizon nearby brings it) and is otherwise a 404;
the website then shows the AWS tiles there. Each tile is made once and kept in
`<data_dir>/terrain-tiles/<terrain signature>/` (a new signature, from changing `terrain.layers` or
`terrain.fine`, starts a new folder; delete old ones). Tiles of a merged 1° tile rebuilt with `--force` are not
renewed by themselves: delete the folder to remake them.

## Terrain for the 3D view in Cesium's format (`/v1/cesium-terrain`)

The website's 3D horizon view (CesiumJS) reads `/v1/cesium-terrain/layer.json` and then the tiles it lists:
quantized-mesh tiles (https://github.com/CesiumGS/quantized-mesh) on Cesium's geographic tiling (level 0: two
tiles of 180°; levels 0–13, the last ~38 m between heights), each a regular 65 × 65 grid with every vertex's surface
normal, so Cesium lights the ground itself and the browser decodes nothing (`cesium_terrain.py`). The heights are the
horizons' own (fine data where it is on disk, else the bare-earth layers, else the 1″ tiles), sampled straight onto
Cesium's grid. Levels below 8 (a level-7 tile spans 1.4°), places outside 60°S–60°N and tiles whose data the server
lacks come from the AWS Terrain Tiles, downloaded once into `<data_dir>/aws-terrarium` (their sea bed set to sea
level); a detailed tile made that way starts the download of the server's own 1″ tile in the background (from level
10) and is kept apart (`<y>.aws.terrain`) only until the server has that data: each request tries the server's
heights first. Tiles are kept gzipped in
`<data_dir>/cesium-terrain/<terrain signature>/` (a new signature, from changing `terrain.layers` or `terrain.fine`,
starts a new folder and new tile URLs; delete old ones).

A tile is made when first asked for, so a first visit to a place waits for its tiles (~10 s or more). To make a
place's ahead of time, with `deploy/prebuild-fine.sh --3d` (all the listed places, after their other terrain) or:

```sh
cd /opt/zmanim-sky-server
sudo -u zmanim-sky venv/bin/python cesium_terrain.py build --around 34.09 -118.40 --radius-km 20 --count   # how many
sudo -u zmanim-sky venv/bin/python cesium_terrain.py build --around 34.09 -118.40 --radius-km 20
```

It makes the tiles a view from anywhere within `--radius-km` asks for: each level as near as Cesium asks for it
(level 13 within 15 km, level 10 within 120 km; worked out for the page's lens on a screen 1,600 px high), the
detailed levels out to ~300 km (what rises above the earth's curve: a Los Angeles view asks for tiles 284 km out),
the coarse ones further, and every tile's parents: ~2,600 tiles for Los Angeles (all 174 a sunrise view there asked
for), at about 0.3–2 s each (what the server log showed), so roughly 15–80 min, ~10–40 KB each. The 1″ tiles under
the levels from 10 are fetched first, as a visit would. The server may be running meanwhile.
