#!/usr/bin/env bash
# The old name of deploy/prebuild.sh, kept so existing commands and cron entries still work: its options are
# passed on as prebuild.py's (no option: the listed places' fine terrain, as before). See deploy/prebuild.sh.
set -u
cd "$(dirname "$0")/.."
ARGS=()
FINE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --tiles) ARGS+=(--tiles); shift ;;
    --tiles-only) ARGS+=(--tiles); FINE=0; shift ;;
    --3d) ARGS+=(--3d); shift ;;
    --3d-only) ARGS+=(--3d); FINE=0; shift ;;
    --base) ARGS+=(--base --regions); shift ;;
    --base-only) ARGS+=(--base --regions); FINE=0; shift ;;
    --only) ARGS+=(--only "$2"); shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
[ "$FINE" = 1 ] && ARGS+=(--fine)
echo "prebuild-fine.sh is now deploy/prebuild.sh; running: prebuild.py ${ARGS[*]}" >&2
exec venv/bin/python prebuild.py "${ARGS[@]}"
