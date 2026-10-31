"""A `type = "command"` agent that misbehaves on request.

    command = ["python3", "fault_agent.py", "<control.json>"]

Each turn reads "agent" from control.json (fault_proxy.py writes it):
ok (a canned answer to the caller's question, see answer_for), error (reports a failure), crash (half an answer,
then the process exits), empty (completes with no text), hang (never
answers), garbage (prints non-JSON and nothing else), slow (25 s, then
answers).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

CONTROL = Path(sys.argv[1])


def send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


BICYCLE = [
    "A bicycle turns the push of your legs into forward motion.",
    "The pedals turn a crank, and the crank turns the front chainring.",
    "The chain carries that turn back to a smaller cog on the rear wheel.",
    "Because the cog is smaller, one turn of the pedals spins the wheel several times.",
    "Gears change that ratio, trading speed for easier climbing.",
    "You balance by steering slightly into any lean, which the moving wheels make easy.",
    "Brakes squeeze pads against the rim or a disc to turn motion into heat.",
    "Put together, it is one of the most efficient machines people have built.",
]


def answer_for(text: str) -> list[str]:
    """Canned replies to the synthetic caller's questions, so a test can
    check what was answered without a model in the loop."""
    said = text.lower()
    if "bicycle" in said and ("go on" in said or "left off" in said):
        return BICYCLE[4:]
    if "bicycle" in said:
        return BICYCLE
    if "capital" in said:
        return ["Paris."]
    if "first word" in said:
        return ["Banana."]
    if "tea" in said:
        return ["Tea began in China thousands of years ago.", "Trade carried it to Europe in the seventeenth century."]
    if "two plus two" in said or "2 plus 2" in said or "2+2" in said:
        return ["Two plus two is four.", "That one is easy."]
    return ["Two plus two is four.", "That one is easy."]


def mode() -> str:
    try:
        return json.loads(CONTROL.read_text()).get("agent", "ok")
    except (OSError, ValueError):
        return "ok"


for line in sys.stdin:
    try:
        frame = json.loads(line)
    except ValueError:
        continue
    if frame.get("type") == "hello":
        send({"type": "hello.ok", "capabilities": {}})
        continue
    if frame.get("type") == "turn.abort":
        send({"type": "turn.aborted", "turnId": frame.get("turnId")})
        continue
    if frame.get("type") != "turn.prompt":
        continue
    turn, what = frame.get("turnId"), mode()
    print(f"fault agent turn {turn}: {what}", file=sys.stderr, flush=True)
    if what == "error":
        send({"type": "error", "turnId": turn, "message": "model quota exceeded"})
    elif what == "crash":
        send({"type": "assistant.delta", "turnId": turn, "text": "Two plus two is "})
        time.sleep(0.5)
        sys.exit(1)
    elif what == "empty":
        send({"type": "turn.completed", "turnId": turn})
    elif what == "hang":
        continue
    elif what == "garbage":
        sys.stdout.write("Traceback (most recent call last): something broke\n")
        sys.stdout.flush()
    else:
        if what == "slow":
            time.sleep(25)
        for sentence in answer_for(str(frame.get("text") or "")):
            send({"type": "assistant.delta", "turnId": turn, "text": sentence + " "})
        send({"type": "turn.completed", "turnId": turn})
