# Validation: checking against observed sunrises

How well the computed visible sunrise matches sunrises people actually timed, and the tools used to check
it. See [How it works](how-it-works.md) for what is being checked.

**Summary:** on Rav Druk's 519 usable sunrises at Armon HaNatziv (1993–96; terrain v4, full run of 20 Nov 2026),
the computed visible sunrise with ERA5's air along the path is −0.2 s on average and 10.5 s rms (11.4 s with
Jerusalem's monthly normals and Keller & Hall's inversion rule); his 1,400 sunrises of 1985–92 and Yisroel
Golan's 200 at Nevei Yaakov rank the same. Rav Zuriel's 88 sunrises in Bnei Brak (1961–92), on the coast,
do not: there ERA5 and the normals do not help, and the standard refraction is best (10.6 s rms). With
terrain v3 the ERA5 figures are 10.6 s (1993–96) and 12.1 s (1985–92).

## ERA5 profiles for past dates (`era5_profiles.py`)

The forecast and the climatology can't be checked against sunrises someone watched years ago. ERA5
(ECMWF's reanalysis: the air hour by hour since 1940, 0.25°, 37 pressure levels) can.
`era5_profiles.py` writes, for any past dates, exactly what `/v1/path-profiles` would have served (same
JSON, `"source": "era5"`), built by the same code: columns along the Sun's azimuth, on the real ground
(when the config enables `api.follow_terrain`, which is off by default; `--no-follow-terrain` turns it off for one run). royzmanimwebsite's `_denoDebug/validateSunrise.mjs`
then computes the visible sunrise for each observation with those profiles, with monthly normals (with and
without Keller & Hall's inversion rule) and with the standard refraction, and reports observed minus
computed: mean, mean absolute and rms, overall and by month.

The commands for the three sites (from the server folder; the dates files are the observation CSVs below):

```sh
venv/bin/pip install cdsapi            # and a CDS key in ~/.cdsapirc (https://cds.climate.copernicus.eu/how-to-api)
# horizons: Keller's spots, point mode, eye height above sea level from --height. --tiles is the server's tile cache, read only; a fresh machine omits --no-download and fetches the tiles there
venv/bin/python make_horizon.py 31.748553 35.237436 --height 754.9 --tiles /var/lib/zmanim-sky/tiles --no-download --out armon.json
venv/bin/python make_horizon.py 31.842162 35.246491 --height 692.5 --tiles /var/lib/zmanim-sky/tiles --no-download --out golan-horizon.json
venv/bin/python make_horizon.py 32.084319 34.832159 --height 52 --tiles /var/lib/zmanim-sky/tiles --no-download --out zuriel-horizon.json
# ERA5 path profiles (--cache keeps the downloaded box, so a rerun downloads nothing)
venv/bin/python era5_profiles.py --lat 31.748553 --lon 35.237436 --dates druk.csv --event sunrise --cache era5-cache --out era5-druk.json -v
venv/bin/python era5_profiles.py --lat 31.842162 --lon 35.246491 --dates golan.csv --event sunrise --cache era5-cache --out era5-golan.json -v
venv/bin/python era5_profiles.py --lat 32.084319 --lon 34.832159 --dates zuriel.csv --event sunrise --cache era5-cache --out era5-zuriel.json -v
# in royzmanimwebsite (Keller's times are Israel standard time all year, so --tz Etc/GMT-2):
node _denoDebug/validateSunrise.mjs --obs druk-9396.csv --tz Etc/GMT-2 --horizon armon.json \
     --profiles era5=era5-druk.json --normals jerusalem-normals.json --csv out.csv
```

The observations are in Israel standard time all year, so use `--tz Etc/GMT-2`. With `Asia/Jerusalem` the daylight-saving dates come out an hour
off: 266 of the 521 observations of 1993–96 differ by more than 300 s (mean −1,827 s).

For the 1993–96 subset, split the dates: `druk-9396.csv` holds the rows of `druk.csv` dated 1993 or later and
`druk-8592.csv` those up to 1992 (the same header lines, then `date,time,event,code`). `validateSunrise.mjs`
does not drop outliers: the figures below drop, after the run, every observation where any method differs
from the observation by more than 300 s.

Where the ERA5 data comes from (`--source`):

* `cds` (default): the Copernicus Climate Data Store. Only a box around the paths is requested, a month at
  a time and only the hours around the events: a few MB a year. Needs a free account, its key, and the ERA5
  licences accepted on that account (a key alone is refused); requests queue.
* `ncar`: NSF NCAR's copy on AWS (no key; `pip install h5py fsspec requests aiohttp`). It stores the whole
  globe per hour in one compressed chunk, so each hour of temperature and geopotential means reading
  ~110 MB (only the box is kept): about 20 GB per 100 sunrises, ~1 min per month of observations with
  `--parallel 8`.

Either way the box is kept in `--cache` (default `<data_dir>/era5`). `--nc-dir` reads files you downloaded
yourself instead. ERA5 has no 80 m / 100 m levels (its lowest are 1000 / 975 / 950 / 925 mb), so it checks
the path, the terrain and the ground-following but not the GFS boundary-layer levels.

## The observations (`observations.py`)

`--dates` takes any file with a date at the start of each line (the observations CSV itself:
`date,time[,event]`, local time). `observations.py` writes that CSV from Chaim Keller's observation files
([chaimkeller/software](https://github.com/chaimkeller/software), `observations-all-places/`): Rabbi Druk's at Armon HaNatziv 1985–96
(`DRUK_10 yr. observations.XLS`, the fullest: 1,921 sunrises; also `DRUK.001`, `DRUK9396.DAT`), Yisroel Golan's at Nevei
Yaakov 2009–13 (200), Rabbi Zuriel's in Bnei Brak 1961–92 (88); all in standard time all year (`--tz Etc/GMT-2`).
The spreadsheet needs `xlrd` (in requirements.txt):

```sh
venv/bin/python observations.py "DRUK_10 yr. observations.XLS" --out druk.csv     # all of Druk's, 1985-96
venv/bin/python observations.py Golan_20*.csv --out golan.csv
venv/bin/python observations.py Zuriel_*.csv --out zuriel.csv
```

Places and eye heights (above sea level):

| observations | latitude, longitude | eye |
|---|---|---|
| Druk, Armon HaNatziv (Keller's DRUK program; Keller & Hall) | 31.748553, 35.237436 | 754.9 m |
| Golan, Nevei Yaakov ("MA" shul) | 31.842162, 35.246491 | 692.5 m (ground) |
| Zuriel, Bnei Brak | 32.084319, 34.832159 | 52 m |

Zuriel's point is Keller's "fictitious observation point", an average of three places near Ponevezh.

The monthly normals are Rabbi Keller's monthly minimum and mean temperatures in `Drukfrm.frm`
(`observations-all-places/`, routine `cmdRunNetzski6_Click`), one pair of rows per site (Druk, Golan, Zuriel):
`jerusalem-normals.json`, `golan-normals.json`, `bneibrak-normals.json`. All 12 × 2 values of each were
checked against the `.frm`.

Keller's file has two mis-recorded Druk sunrises, 1993-02-17 (+578 s) and 1993-04-27 (−1,210 s). They are
dropped in the figures below (|difference| > 300 s); no other observation reaches that.

## Results

Observed minus computed, in seconds (positive: the Sun was seen later than computed): mean / rms. Terrain v4,
SRTM horizons at the spots above (point mode, 100 m grid, 1–150 km), Keller's normals, ERA5 profiles with
`follow_terrain` off, as the server runs; run of 20 Nov 2026.

| atmosphere | Druk 1993–96 (519) | Druk 1985–92 (1,400) | all Druk (1,919) | Golan (200) | Zuriel (88) |
|---|---|---|---|---|---|
| standard 34.48′ | +5.0 / 16.0 | +2.9 / 17.1 | +3.4 / 16.8 | +8.3 / 14.8 | −2.0 / 10.6 |
| monthly normals | −4.2 / 13.5 | −6.0 / 15.7 | −5.5 / 15.1 | −1.2 / 10.5 | −7.3 / 12.6 |
| monthly normals + Keller & Hall's inversion rule | −1.0 / 11.4 | −2.3 / 13.2 | −1.9 / 12.7 | +0.4 / 9.3 | −7.3 / 12.6 |
| ERA5 path profiles | −0.2 / 10.5 | −1.8 / 12.3 | −1.4 / 11.9 | −0.3 / 8.8 | −6.5 / 12.3 |

Nov–Jan rms (November, December, January only; the same observations; Druk 1993–96 98 of them, 1985–92 299, all 397, Golan 18, Zuriel 30):

| atmosphere | Druk 1993–96 | Druk 1985–92 | all Druk | Golan | Zuriel |
|---|---|---|---|---|---|
| standard 34.48′ | 22.6 s | 24.9 s | 24.4 s | 14.7 s | 12.3 s |
| monthly normals | 25.1 s | 27.0 s | 26.5 s | 16.7 s | 14.5 s |
| monthly normals + Keller & Hall's inversion rule | 19.0 s | 19.9 s | 19.7 s | 6.7 s | 14.5 s |
| ERA5 path profiles | 16.8 s | 18.3 s | 18.0 s | 10.5 s | 14.1 s |

In Jerusalem and Nevei Yaakov real air along the path does best or ties, nearly unbiased and with nothing
tuned. Without dropping the two bad entries the 1993–96 rms is about 60 s for every method.

Bnei Brak is different. The standard refraction is best there; ERA5 and the normals leave the computed sunrise
6.5–7 s too early. The inversion rule never triggers (its row equals the normals'): the Sun rises over hills,
with the horizon above 0.01°. Split at 1979, when ERA5's satellite data begin (mean / rms):

| atmosphere | 1961–78 (61) | 1979–92 (27) |
|---|---|---|
| standard 34.48′ | −3.2 / 11.4 | +0.7 / 8.2 |
| monthly normals (with or without the rule) | −8.0 / 13.4 | −5.6 / 10.5 |
| ERA5 path profiles | −7.5 / 13.2 | −4.5 / 10.9 |

After 1979 every method is 2–3 s closer in rms, and ERA5 against the normals is unchanged: the better air after
1979 does not show at this site.

### Terrain v3 comparisons (October 2026 run)

The numbers in this subsection are from a terrain v3 run in October 2026 that used approximate Jerusalem normals (not Keller's recorded ones), not from the 20 Nov 2026 run above. They are comparisons between methods, not current scores.

Real air along the path did best in both periods, unbiased and with nothing tuned. The winter error is
scatter, not bias: fitted on one period and scored on the other, no winter correction (the 13 h rule at any
size, one scaled by ERA5's own inversion, by night length, or by calm clear mornings from ERA5's wind and
cloud) gained more than 0.5 s, and the rule's fixed 15 s made ERA5 worse (12.4 s and 13.0 s). Yisroel Golan's
200 sunrises at Nevei Yaakov (2009–13, from the "MA" shul, 31.842162 N 35.246491 E, ground 692.5 m) rank the
same: ERA5 8.9 s rms (mean +1.5 s), normals + rule 9.5 s, normals 10.9 s, the standard 34.48′ 15.8 s, with the
eye at ground level; an eye on the roof (+10 m) fits worse (10.2 / 10.5 / 11.1 s). An eye height of 802 m
at Armon HaNatziv fits clearly worse than 754.9 m.

These v3 numbers use horizons from `terrain.py` version 3 (with `alternates` and `floorM`; version 4's crest
refinement puts Armon HaNatziv's sunrises 1.4 s later on average, and v4 is scored in the Results above) and the client's terrain rays
traced through the day's air (royzmanimwebsite `terrainElevation`). With the one-curvature terrain
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
ray aimed just above the horizon that the air bends more than the horizon ray see the Sun first. On the
terrain-v3 run all three leave the Jerusalem validation numbers unchanged; not checked with v4.

## Columns on the real ground (`follow_terrain`) and radiosondes

On the 368 sunrises in Keller's text file (`DRUK9396.DAT`), ERA5 gave +0.6 s / 13.5 s rms, and ERA5 with
`follow_terrain` on −1.8 s / 15.2 s (Nov–Jan 23.8 s against 20.2 s, pushing an already late computed
sunrise later still): the shift, done with model air, made it worse, so it is off. On the Results sets above it does the same: with `follow_terrain` on, ERA5 gives
+1.2 / 10.8, +0.2 / 13.1, +0.5 / 12.5, +1.1 / 9.1 and −6.5 / 12.5 s (Druk 1993–96, 1985–92, all, Golan, Zuriel),
and 18.1, 19.4, 19.1, 12.8 and 14.4 s rms in Nov–Jan. Keller & Hall
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
