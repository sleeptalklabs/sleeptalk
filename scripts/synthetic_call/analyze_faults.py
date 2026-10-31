"""Score a `faults` run: after each fault, did the caller hear something,
and what did the page show?

    python3 scripts/synthetic_call/analyze_faults.py <run_dir>

A step passes when audio played after the caller's question, or the page
showed an assistant line; silence with nothing on the page fails.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LOUD = 0.01


def main(run: Path) -> int:
    data = json.loads((run / "out" / "caller.json").read_text())
    marks = [(row["t"], row["name"].split(":", 1)[1]) for row in data["log"]
             if row.get("e") == "mark" and row["name"].startswith("fault_start:")]
    ends = [e["t"] for e in data["events"] if e.get("e") == "user_end"]
    failed = 0
    for index, (start, name) in enumerate(marks):
        stop = marks[index + 1][0] if index + 1 < len(marks) else float("inf")
        asked = next((t for t in ends if start <= t < stop), start)
        first = next((t for t, level in data["levels"] if asked < t < stop and level > LOUD), None)
        loud_ms = 20 * sum(1 for t, level in data["levels"] if asked < t < stop and level > LOUD)
        seen: list[str] = []
        before: set[str] = set()
        for message in data["display"]:
            envelope = json.loads(message["data"])
            if envelope.get("type") != "call.presentation":
                continue
            lines = [f"{line.get('kind', '?')}: {line.get('text', '')}" for line in envelope["payload"].get("lines", [])]
            if message["t"] < start:
                before = set(lines)
            elif message["t"] < stop:
                seen += [line for line in lines if line not in before and line not in seen]
        said = [line for line in seen if not line.startswith("user:")]
        ok = first is not None or any(line.startswith("assistant:") for line in said)
        failed += not ok
        heard = f"audio at +{first - asked} ms, {loud_ms / 1000:.1f} s" if first is not None else "SILENT"
        print(f"{'ok  ' if ok else 'FAIL'} {name:18} {heard}")
        for line in said[:4]:
            print(f"       {line[:150]}")
    print(f"SUMMARY faults {len(marks) - failed}/{len(marks)} not silent")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
