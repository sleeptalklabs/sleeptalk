"""Where each turn's time goes, across runs: python3 latency.py <run_dir> [<run_dir> ...]

Per turn, from the host's latency log (trace-joined by turn) plus the caller's
own clock:
  endpoint   caller stops talking -> the host commits the turn
  stt        turn committed -> transcript ready
  agent      prompt sent -> agent's first text
  tts        first speakable sentence -> first answer audio queued
  ack        turn committed -> first audio of any kind (preamble/ack) queued
  answer     turn committed -> first answer audio queued
  heard      caller stops talking -> caller hears any audio (end to end)
Prints medians per run and the slowest turns.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

LOUD = 0.01


def turns_of(run: Path):
    c = json.loads((run / "out/caller.json").read_text())
    host = [json.loads(line) for line in (run / "home/logs/voice_latency.jsonl").read_text().splitlines() if line.strip()]
    mark = next(r for r in c["log"] if r["e"] == "mark")
    offset = mark["wall"] - mark["t"]
    for r in host:
        r["_t"] = datetime.fromisoformat(r["loggedAt"]).timestamp() * 1000 - offset if r.get("loggedAt") else None
    by_trace: dict[str, dict[str, float]] = {}
    for r in host:
        tid = r.get("traceId")
        if tid and r.get("stage") and r["_t"] is not None:
            by_trace.setdefault(tid, {}).setdefault(r["stage"], r["_t"])
    ends = [e["t"] for e in c["events"] if e["e"] == "user_end"]
    loud = [t for t, v in c["levels"] if v > LOUD]
    out = []
    for tid, st in by_trace.items():
        commit = st.get("utterance_ready") or st.get("turn_committed")
        if commit is None or "turn_complete" not in st:
            continue
        user_end = max((e for e in ends if e <= commit + 50), default=None)
        heard = next((t for t in loud if user_end is not None and t > user_end), None)

        def d(a, b):
            return round(b - a) if a is not None and b is not None and b >= a else None

        out.append({
            "trace": tid,
            "endpoint": d(user_end, commit),
            "stt": d(commit, st.get("stt_completed")),
            "agent": d(st.get("agent_prompt_sent"), st.get("agent_first_text_delta")),
            "tts": d(st.get("agent_first_speakable_segment"), st.get("answer_first_output_enqueue")),
            "ack": d(commit, st.get("acknowledgement_first_output_enqueue") or st.get("answer_first_output_enqueue")),
            "answer": d(commit, st.get("answer_first_output_enqueue")),
            "heard": d(user_end, heard),
        })
    return out


def main(runs: list[Path]) -> None:
    cols = ["endpoint", "stt", "agent", "tts", "ack", "answer", "heard"]
    print(f"{'run':28} {'turns':>5} " + " ".join(f"{c:>9}" for c in cols) + "   (median ms)")
    worst = []
    for run in runs:
        try:
            turns = turns_of(run)
        except (OSError, StopIteration, ValueError) as exc:
            print(f"{run.name:28} skipped ({type(exc).__name__})")
            continue
        med = {c: statistics.median([t[c] for t in turns if t[c] is not None]) if any(t[c] is not None for t in turns) else None for c in cols}
        print(f"{run.name:28} {len(turns):>5} " + " ".join(f"{(round(med[c]) if med[c] is not None else '-'):>9}" for c in cols))
        worst += [(t["heard"] or 0, run.name, t) for t in turns]
    print("\nslowest heard (caller stops -> hears audio):")
    for heard, name, t in sorted(worst, key=lambda x: -x[0])[:8]:
        print(f"  {heard:>6} ms  {name:24} " + " ".join(f"{c}={t[c]}" for c in cols[:-1]))


if __name__ == "__main__":
    main([Path(p) for p in sys.argv[1:]])
