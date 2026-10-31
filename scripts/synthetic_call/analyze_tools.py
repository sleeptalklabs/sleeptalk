"""Score a `script` run in the seeded workspace (start_call.py --seed).

    python3 scripts/synthetic_call/analyze_tools.py <run_dir>

For each step: what the page showed, whether anything was heard, and the
step's check (the answer, or what the agent did to the workspace).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LOUD = 0.01


def _said(result: dict) -> str:
    return " ".join(line.split(": ", 1)[-1] for line in result.get("lines", [])
                    if not line.startswith("user:")).lower()


def _file(run: Path, name: str) -> str | None:
    path = run / "workspace" / name
    return path.read_text(errors="replace") if path.is_file() else None


# Step name -> (description, check(result, run, all_results) -> bool | None).
# None means "not judged" (shown only).
CHECKS = {
    "tool_read": ("names the project (Lantern)", lambda r, run, _: "lantern" in _said(r)),
    "tool_write": ("notes.txt holds hello", lambda r, run, _: "hello" in (_file(run, "notes.txt") or "").lower()),
    "tool_shell": ("says three Python files", lambda r, run, _: bool(re.search(r"\b(3|three)\b", _said(r)))),
    "tool_status": ("answers mid-work, then the recommendations arrive",
                    lambda r, run, _: sum(w in _said(r) for w in ("dimming", "fog", "storm", "dawn", "surge")) >= 2),
    "tool_stop": ("acknowledges the stop (read the lines: did the work end?)",
                  lambda r, run, _: bool(re.search(r"stop|cancel|halt|okay|sure|done|nothing", _said(r)))),
    "search": ("weather answer", lambda r, run, _: bool(re.search(r"paris|°|degrees|rain|cloud|sun", _said(r)))),
    "perm_yes": ("approved.txt written after yes", lambda r, run, _: "yes" in (_file(run, "approved.txt") or "").lower()),
    "perm_no": ("denied.txt not written after no", lambda r, run, _: _file(run, "denied.txt") is None),
    "sub_parallel": ("both answers: Lantern and three",
                     lambda r, run, _: "lantern" in _said(r) and bool(re.search(r"\b(3|three)\b", _said(r)))),
    "sub_cancel": ("stops: no review results after cancel", lambda r, run, _: None),
    "sub_fail": ("reports the missing file and the README",
                 lambda r, run, _: "lantern" in _said(r)
                 and bool(re.search(r"missing|not found|doesn.t exist|does not exist|no such|couldn", _said(r)))),
}


def main(run: Path) -> int:
    data = json.loads((run / "out" / "caller.json").read_text())
    results = [row for row in data["log"] if row.get("e") == "step_result"]
    errors = [row for row in data["log"] if row.get("e") in {"scenario_error", "page_error"}]
    failed = 0
    for result in results:
        name = result["name"]
        description, check = CHECKS.get(name, ("", lambda *_: None))
        verdict = check(result, run, results)
        silent = result.get("heardMs") is None and not any(
            line.startswith(("assistant:", "progress:")) for line in result.get("lines", []))
        if result.get("heardMs") is None and "after" not in name and verdict is None and silent:
            verdict = False
        failed += verdict is False
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[verdict]
        heard = f"heard +{result['heardMs']} ms" if result.get("heardMs") is not None else "(talked over / no wait)"
        print(f"{mark} {name:14} {heard:24} {description}")
        for line in result.get("lines", [])[-5:]:
            print(f"       {line[:160]}")
        for line in result.get("other", [])[:4]:
            print(f"       ~ {line[:160]}")
    for error in errors:
        print(f"ERROR {error}")
    print(f"SUMMARY tools {len(results) - failed}/{len(results)} ok" + (f", {len(errors)} errors" if errors else ""))
    return 1 if failed or errors else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
