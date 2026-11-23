#!/usr/bin/env bash
# Prepare terrain and horizons ahead of requests (prebuild.py: `deploy/prebuild.sh --help` for all of it).
#
#   cd /opt/zmanim-sky-server
#   sudo -u zmanim-sky nohup deploy/prebuild.sh > prebuild.log 2>&1 &                  # after every deploy
#   sudo -u zmanim-sky nohup deploy/prebuild.sh --region-areas > prebuild.log 2>&1 &   # and all of Israel's areas
#   sudo -u zmanim-sky nohup deploy/prebuild.sh --fine --tiles > prebuild.log 2>&1 &   # the listed places' terrain
#   sudo -u zmanim-sky deploy/prebuild.sh --3d --only "Los Angeles"                    # the 3D view, one place
#   sudo -u zmanim-sky deploy/prebuild.sh --dry-run                                    # what it would do
#
# With no job named it does what a deploy needs: every horizon visitors have asked for (the API's cache, old
# keys included: keep the old files until this has run), the listed places' areas, and the regions' base
# surfaces. The API must be running: the horizons are asked of it, terrain.max_parallel at a time.
set -u
cd "$(dirname "$0")/.."
exec venv/bin/python prebuild.py "$@"
