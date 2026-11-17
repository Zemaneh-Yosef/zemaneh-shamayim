# Validation: checking against observed sunrises

How well the computed visible sunrise matches sunrises people actually timed, and the tools used to check
it. See [How it works](how-it-works.md) for what is being checked.

**Summary:** on Rav Druk's 521 observed sunrises at Armon HaNatziv (1993–96), the computed visible sunrise
with ERA5's air along the path is unbiased and 10.6 s rms (11.7 s with Jerusalem's monthly normals and
Keller & Hall's inversion rule); his 1,400 earlier sunrises and Yisroel Golan's 200 at Nevei Yaakov rank the
same. There are no observations yet from outside Jerusalem.

## ERA5 profiles for past dates (`era5_profiles.py`)

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

## The observations (`observations.py`)

`--dates` takes any file with a date at the start of each line (the observations CSV itself:
`date,time[,event]`, local time). `observations.py` writes that CSV from Chaim Keller's observation files
([chaimkeller/software](https://github.com/chaimkeller/software), `observations-all-places/`): Rabbi Druk's at Armon HaNatziv 1985–96
(`DRUK_10 yr. observations.XLS`, the fullest; also `DRUK.001`, `DRUK9396.DAT`), Yisroel Golan's at Nevei
Yaakov 2009–13, Rabbi Zuriel's in Bnei Brak 1961–92; all in standard time all year (`--tz Etc/GMT-2`).
Druk's spot is 31.748553, 35.237436 (Keller's DRUK program), eye 754.9 m above sea level (Keller & Hall).
The spreadsheet needs `xlrd` (in requirements.txt):

```sh
venv/bin/python observations.py "DRUK_10 yr. observations.XLS" --out druk.csv     # all of Druk's, 1985-96
venv/bin/python observations.py Golan_20*.csv --out golan.csv
```

## Results

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
at Armon HaNatziv (a figure used earlier) fits clearly worse.

These numbers use horizons from `terrain.py` version 3 (with `alternates` and `floorM`; version 4's crest
refinement moves Armon HaNatziv's sunrises 1.4 s later on average and has not been re-scored yet) and the client's terrain rays
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

## Columns on the real ground (`follow_terrain`) and radiosondes

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
