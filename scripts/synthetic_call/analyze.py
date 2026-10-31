"""Score a synthetic-caller run: python3 analyze.py <run_dir>

Reads <run_dir>/out/caller.json (what the caller heard and when) and
<run_dir>/home/logs/voice_latency.jsonl (what the host did), aligns them on
wall-clock time, and prints one block per scenario plus a verdict line.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

LOUD = 0.01      # RMS above this = agent audio playing
MERGE_MS = 400   # quieter gaps than this inside speech are the same segment


def segments(levels, lo, hi):
    out = []
    for t, v in levels:
        if not (lo <= t < hi) or v <= LOUD:
            continue
        if out and t - out[-1][1] <= MERGE_MS:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def main(run: Path) -> None:
    c = json.loads((run / "out/caller.json").read_text())
    clips = Path(c["clips"])
    expect = json.loads((clips / "manifest.json").read_text()).get("_expect") or {"first_word": "banana", "barge_answer": "paris"}
    host = [json.loads(line) for line in (run / "home/logs/voice_latency.jsonl").read_text().splitlines() if line.strip()]
    marks = [row for row in c["log"] if row["e"] == "mark"]
    offset = marks[0]["wall"] - marks[0]["t"]  # page time -> wall ms
    for row in host:
        at = row.get("loggedAt")
        row["_t"] = datetime.fromisoformat(at).timestamp() * 1000 - offset if at else None

    windows = []
    for m in marks:
        if m["name"].startswith("start:"):
            end = next((x["t"] for x in marks if x["name"] == "end:" + m["name"][6:]), 1e12)
            windows.append((m["name"][6:], m["t"], end + 1500))

    def lines_at(t):
        last = None
        for d in c["display"]:
            if d["t"] > t:
                break
            try:
                env = json.loads(d["data"])
            except ValueError:
                continue
            if env.get("type") == "call.presentation":
                last = env["payload"].get("lines", [])
        return last or []

    verdicts = []
    for name, lo, hi in windows:
        users = [e for e in c["events"] if e["e"] in ("user_start", "user_end") and lo <= e["t"] < hi]
        segs = segments(c["levels"], lo, hi)
        before, after = lines_at(lo), lines_at(hi)
        new = after[len(before):] if after[: len(before)] == before else [line for line in after if line not in before]
        heard = [line["text"] for line in new if line.get("kind") == "user"]
        said = [line["text"] for line in new if line.get("kind") == "assistant"]
        progress = [line["text"] for line in new if line.get("kind") == "progress"]
        turns = [(r.get("turnId"), r.get("status")) for r in host if r.get("stage") == "turn_complete" and r["_t"] and lo <= r["_t"] < hi]
        barges = [r["stage"] + (":" + str(r.get("reason") or r.get("disposition") or r.get("action") or "")) for r in host
                  if r["_t"] and lo <= r["_t"] < hi and ("barge" in (r.get("stage") or "") or "interrupt" in (r.get("stage") or "") or "cancel" in (r.get("stage") or ""))]
        last_user_end = max((e["t"] for e in users if e["e"] == "user_end"), default=lo)
        first_user_end = min((e["t"] for e in users if e["e"] == "user_end"), default=lo)
        reply = next((s for s in segs if s[0] > first_user_end), None)
        speech_ms = sum(b - a + 20 for a, b in segs if a > first_user_end)
        gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1) if segs[i][0] > first_user_end]
        chars = sum(len(s) for s in said)

        print(f"\n=== {name}")
        print(f"  caller said: {[e['clip'] for e in users if e['e'] == 'user_start']}")
        print(f"  host heard:  {heard}")
        print(f"  progress:    {progress}")
        print(f"  answer:      {[s[:160] + ('…' if len(s) > 160 else '') for s in said]}  ({chars} chars)")
        print(f"  turns:       {turns}")
        if barges:
            print(f"  barge/cancel events: {barges[:12]}")
        print(f"  agent audio: {len(segs)} segments, {speech_ms / 1000:.1f}s after the caller's first line; "
              f"first audio {((reply[0] - first_user_end) / 1000) if reply else float('nan'):.2f}s after it; "
              f"longest gap {max(gaps, default=0) / 1000:.1f}s")

        problems = []
        statuses = [s for _, s in turns]
        if name == "first_words":
            if not heard or not re.match(r"\W*(" + expect["first_word"] + ")", heard[0], re.I):
                problems.append(f"first words lost: heard {heard[:1]}")
        if name in ("short", "long_silent", "resume", "split", "search", "first_words"):
            if not said:
                problems.append("no answer")
            # A grace continuation cancels the first half's turn and re-asks
            # with both halves: only the last turn has to complete.
            final = statuses[-1:] if name == "split" else statuses
            if not final or any(s != "completed" for s in final):
                problems.append(f"turn not completed: {statuses}")
        if name == "long_silent" and chars:
            # Grok at speed 1.0 speaks roughly 14-18 characters a second.
            expected = chars / 16
            if speech_ms / 1000 < expected * 0.6:
                problems.append(f"answer audio {speech_ms / 1000:.1f}s is short for {chars} chars (~{expected:.0f}s): cut off?")
        if name == "split":
            merged = len(heard) > 1 and all(later.startswith(earlier.rstrip("。.,，")) for earlier, later in zip(heard, heard[1:]))
            if merged:
                print(f"  note: ended early {len(heard) - 1}x, each re-asked with the rest (grace continuation)")
            elif len(heard) > 1:
                problems.append(f"split into {len(heard)} turns: {heard}")
        if name == "barge":
            barge_start = next((e["t"] for e in users if e["e"] == "user_start" and e["clip"] == "barge.wav"), None)
            if barge_start is not None:
                playing = [s for s in segs if s[0] < barge_start <= s[1] + MERGE_MS]
                stop = playing[0][1] if playing else None
                if stop is None:
                    problems.append("agent wasn't talking when the caller barged in")
                else:
                    lat = stop - barge_start
                    print(f"  barge: agent went quiet {lat} ms after the caller started talking")
                    if lat > 1200:
                        problems.append(f"slow to stop: {lat} ms")
            if not any(re.search(expect["barge_answer"], s, re.I) for s in said):
                problems.append("no answer to the barge-in question (the capital of France)")
        if name == "backchannel":
            if any(s != "completed" for s in statuses):
                problems.append(f"'mm-hmm' ended the answer: {statuses}")
        print("  verdict:", "PASS" if not problems else "FAIL — " + "; ".join(problems))
        verdicts.append((name, not problems))
    errors = [row for row in c["log"] if row["e"] in ("page_error", "console_error", "scenario_error")]
    if errors:
        print("\npage errors:", [e.get("text") or e.get("err") for e in errors][:10])
    print("\nSUMMARY", " ".join(f"{n}={'ok' if ok else 'FAIL'}" for n, ok in verdicts))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
