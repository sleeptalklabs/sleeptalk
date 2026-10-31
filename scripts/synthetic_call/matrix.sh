#!/bin/sh
# Many synthetic calls, a few at a time, then the latency table.
#   sh scripts/synthetic_call/matrix.sh [matrix.txt] [parallel]
# Each line: <agent> <run name> [start_call.py options]. Runs land in $SYNTH_OUT/runs.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${SYNTH_OUT:-${TMPDIR:-/tmp}/sleeptalk-synthetic-call}
MATRIX=${1:-$HERE/matrix.txt}
PARALLEL=${2:-4}
mkdir -p "$OUT/runs"
# Clips first, one language at a time, so parallel runs don't race to make them.
for language in $(grep -o -- '--language [^ ]*' "$MATRIX" | awk '{print $2}' | sort -u) en-US; do
  [ -f "$OUT/clips/$language/manifest.json" ] || (cd "$HERE/../.." && uv run python "$HERE/make_clips.py" "$OUT/clips/$language" "$language" > /dev/null)
done
# No trailing blank on a line: xargs -L reads one as "continued on the next line".
grep -v '^\s*#' "$MATRIX" | grep -v '^\s*$' | while read -r agent name rest; do
  printf '%s %s%s\n' "$agent" "$OUT/runs/$name" "${rest:+ $rest}"
done | xargs -P "$PARALLEL" -L 1 sh "$HERE/run.sh"
python3 "$HERE/latency.py" "$OUT"/runs/*
