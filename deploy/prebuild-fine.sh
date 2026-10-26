#!/usr/bin/env bash
# Build fine near-range terrain (dem_layers.py build-fine) for the communities listed below, ahead of time, so
# the first horizon request there doesn't wait for it. Chunks already built are skipped, so it can be stopped
# and run again, and places added to the list later cost only their own chunks.
#
#   cd /opt/zmanim-sky-server
#   sudo -u zmanim-sky nohup deploy/prebuild-fine.sh --example > prebuild-fine.log 2>&1 &   # before config.json has terrain.fine
#   sudo -u zmanim-sky nohup deploy/prebuild-fine.sh > prebuild-fine.log 2>&1 &             # once it has
#   sudo -u zmanim-sky deploy/prebuild-fine.sh --only "Monsey"                               # one place
#   sudo -u zmanim-sky nohup deploy/prebuild-fine.sh --tiles > prebuild-fine.log 2>&1 &     # and the 1" tiles
#   sudo -u zmanim-sky deploy/prebuild-fine.sh --tiles-only --only "Los Angeles"             # only the 1" tiles
#
# --tiles also prepares each place's 1" tiles (dem_layers.py build --horizon): the merged bare-earth tiles where
# terrain.layers has sources, else the plain downloaded ones. Only those its horizons' rays cross (the year's
# sunrise, sunset, moonrise and moonset directions, 150 km from the edge of the place): about 330 for the whole
# list, many of them sea (a marker file, no data). Disk: ~52 MB a merged land tile, 5-12 MB a plain one, so up to
# ~15 GB merged for the whole list: check `df -h /var/lib/zmanim-sky` first, or use --only for the places you
# serve. Time: a few seconds a plain tile, about a minute a merged one. --tiles-only skips the fine terrain (e.g.
# with no terrain.fine sources).
#
# Each line: name, latitude, longitude, km (the community's own radius + terrain.fine.max_km, 10). ~4.7 MB a
# chunk on disk; the whole list is roughly 900 chunks (~4 GB): about 45 min for the US and Canada (USGS and
# NRCan answer in a few seconds a chunk), about 45 more for France (IGN: ~15-20 s a chunk).
set -u
cd "$(dirname "$0")/.."

EXTRA=()
ONLY=""
TILES=0
FINE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    --tiles) TILES=1; shift ;;
    --tiles-only) TILES=1; FINE=0; shift ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

PLACES="
New York City (5 boroughs, Five Towns, Great Neck)|40.70|-73.92|36
Great Neck|40.80|-73.73|12
Long Island (Woodmere to West Hempstead)|40.66|-73.66|15
Monsey and New Square|41.11|-74.07|15
Kiryas Joel|41.34|-74.17|12
Teaneck and Bergen County|40.89|-74.01|15
Passaic and Clifton|40.86|-74.13|12
Lakewood and Toms River|40.06|-74.19|17
Los Angeles (Pico-Robertson, Hancock Park, Beverly Hills, the Valley)|34.09|-118.40|30
Baltimore (Park Heights, Pikesville)|39.36|-76.70|18
Silver Spring, Washington and Potomac|39.00|-77.05|20
Philadelphia and Lower Merion|40.00|-75.22|18
Boston and Brookline|42.33|-71.13|15
Chicago (West Rogers Park, Skokie, Lincolnwood)|42.01|-87.72|20
Detroit (Southfield, Oak Park)|42.48|-83.20|15
Cleveland Heights|41.50|-81.55|14
Pittsburgh (Squirrel Hill)|40.43|-79.92|12
Cincinnati|39.20|-84.45|12
St. Louis (University City)|38.66|-90.33|12
Scranton|41.40|-75.66|12
Miami (Miami Beach, North Miami Beach, Aventura, Hollywood)|25.90|-80.15|22
Boca Raton|26.36|-80.12|15
Atlanta (Toco Hills)|33.82|-84.33|15
Dallas|32.90|-96.78|15
Houston (Meyerland)|29.69|-95.45|15
Denver|39.73|-104.98|15
Phoenix and Scottsdale|33.55|-111.95|15
Las Vegas|36.13|-115.20|15
San Francisco Bay|37.80|-122.32|20
Seattle|47.60|-122.28|15
Toronto|43.73|-79.43|20
Montreal|45.49|-73.65|15
Paris and its suburbs|48.86|2.35|25
Lyon|45.76|4.84|18
Marseille|43.30|5.38|22
Nice|43.70|7.27|15
Strasbourg|48.58|7.75|15
"

failed=()
while IFS='|' read -r name lat lon km; do
  [ -z "$name" ] && continue
  [ -n "$ONLY" ] && [[ "$name" != *"$ONLY"* ]] && continue
  echo "=== $name ($lat, $lon, $km km)"
  if [ "$FINE" = 1 ] && ! venv/bin/python dem_layers.py build-fine --around "$lat" "$lon" --km "$km" "${EXTRA[@]}"; then
    failed+=("$name")
  fi
  # the place's own radius is km less terrain.fine.max_km (10)
  if [ "$TILES" = 1 ] && ! venv/bin/python dem_layers.py build --around "$lat" "$lon" --horizon \
       --radius-km "$(awk -v k="$km" 'BEGIN { r = k - 10; print (r > 0 ? r : 0) }')"; then
    failed+=("$name (1\" tiles)")
  fi
done <<< "$PLACES"

if [ ${#failed[@]} -gt 0 ]; then
  echo "=== some terrain failed in: ${failed[*]} - run this again to retry them"
  exit 1
fi
echo "=== all done"
