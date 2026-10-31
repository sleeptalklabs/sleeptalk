"""Measure browser GPT-Live evidence without inventing completed voice turns.

Usage: python3 scripts/synthetic_call/analyze_live.py RUN_DIRECTORY
These checks prove transport overlap and visible replies, not speech quality,
semantic interruption latency, or production specialist correctness.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def analyze(run: Path) -> dict:
    call = json.loads((run / "out/caller.json").read_text())
    marks = {row["name"]: row["t"] for row in call["log"] if row["e"] == "mark"}
    results = {}
    for marker, start in marks.items():
        if not marker.startswith("start:"):
            continue
        name = marker[6:]
        end = marks.get("end:" + name, start)
        latest, baseline = {}, {}
        for message in call["display"]:
            if message["t"] > end + 1500:
                continue
            envelope = json.loads(message["data"])
            if envelope.get("type") != "call.presentation":
                continue
            for line in envelope["payload"].get("lines", []):
                key = (line.get("kind"), line.get("callId", line.get("text")))
                (baseline if message["t"] < start else latest)[key] = line.get("text", "")
        assistant = " ".join(text[len(baseline.get(key, "")):] if text.startswith(baseline.get(key, "")) else text
                             for key, text in latest.items() if key[0] == "assistant")
        windows = []
        for event in call["events"]:
            if event["e"] != "user_start" or not start <= event["t"] < end:
                continue
            finish = next((e["t"] for e in call["events"] if e["e"] == "user_end"
                           and e["clip"] == event["clip"] and e["t"] > event["t"]), end)
            overlap = sum(20 for t, level in call["levels"] if event["t"] <= t < finish and level > .01)
            windows.append({"clip": event["clip"], "bot_audio_during_caller_ms": overlap})
        audio = any(start <= t < end and level > .01 for t, level in call["levels"])
        passed = audio and bool(assistant)
        if name in {"short", "stop_resume"}:
            passed = passed and bool(re.search(r"\b(four|4)\b", assistant, re.I))
        if name in {"barge", "backchannel"}:
            passed = passed and any(w["clip"] == name + ".wav" and w["bot_audio_during_caller_ms"] > 0 for w in windows)
        if name == "barge":
            passed = passed and "paris" in assistant.lower()
        results[name] = {"transport_and_reply_pass": passed, "caller_windows": windows,
                         "assistant_caption": assistant}
    return results


if __name__ == "__main__":
    results = analyze(Path(sys.argv[1]))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print("SUMMARY " + " ".join(f"{name}={'ok' if row['transport_and_reply_pass'] else 'FAIL'}"
                                 for name, row in results.items()))
    raise SystemExit(0 if results and all(row["transport_and_reply_pass"] for row in results.values()) else 1)
