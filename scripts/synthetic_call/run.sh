#!/bin/sh
# One synthetic call, end to end: start it, run every scenario, score it.
#   sh scripts/synthetic_call/run.sh <agent> <run_dir> [start_call.py options]
# Clips for the call's --language are made on first use under $SYNTH_OUT/clips.
# SYNTH_ONLY=barge,split runs just those scenarios. SYNTH_ONLY=faults with
# SYNTH_FAULT_PROXY and SYNTH_FAULT_PLAN runs failure injection (README).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT=${SYNTH_OUT:-${TMPDIR:-/tmp}/sleeptalk-synthetic-call}
AGENT=$1
RUN=$2
shift 2
LANGUAGE=en-US
SPEECH=""
prev=""
for arg in "$@"; do
  [ "$prev" = "--language" ] && LANGUAGE=$arg
  [ "$prev" = "--speech" ] && SPEECH=$arg
  prev=$arg
done
CLIPS=${SYNTH_CLIPS:-$OUT/clips/$LANGUAGE}
mkdir -p "$RUN"
cd "$REPO" || exit 1
if [ ! -f "$CLIPS/manifest.json" ]; then
  uv run python "$HERE/make_clips.py" "$CLIPS" "$LANGUAGE" > /dev/null || exit 1
fi
uv run python "$HERE/start_call.py" "$RUN" --agent "$AGENT" "$@" > "$RUN/receipt.json" 2> "$RUN/start.err"
JOIN=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('join_url',''))" "$RUN/receipt.json" 2>/dev/null)
if [ -z "$JOIN" ]; then
  echo "[$AGENT $(basename "$RUN")] call did not start:"; tail -3 "$RUN/receipt.json" "$RUN/start.err"
  [ -f "$RUN/model_ref.txt" ] && uv run python "$HERE/remove_model_key.py" "$RUN"
  exit 1
fi
SYNTH_CLIPS=$CLIPS node "$HERE/caller.mjs" "$JOIN" "$RUN/out" ${SYNTH_ONLY:-} > "$RUN/caller.out" 2>&1
ANALYZER=analyze.py
[ "$SPEECH" = "live" ] && ANALYZER=analyze_live.py
python3 "$HERE/$ANALYZER" "$RUN" > "$RUN/report.txt" 2>&1
if [ -n "${SYNTH_SCRIPT:-}" ]; then
  python3 "$HERE/analyze_tools.py" "$RUN" > "$RUN/tools.txt" 2>&1
  echo "[$AGENT $(basename "$RUN")] $(grep SUMMARY "$RUN/tools.txt")"
fi
if [ -n "${SYNTH_FAULT_PLAN:-}" ]; then
  python3 "$HERE/analyze_faults.py" "$RUN" > "$RUN/faults.txt" 2>&1
  echo "[$AGENT $(basename "$RUN")] $(grep SUMMARY "$RUN/faults.txt")"
fi
[ -f "$RUN/model_ref.txt" ] && uv run python "$HERE/remove_model_key.py" "$RUN"
echo "[$AGENT $(basename "$RUN")] $(grep SUMMARY "$RUN/report.txt")"
