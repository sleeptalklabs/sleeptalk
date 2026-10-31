"""Settings the synthetic-call scripts share.

SYNTH_OPENROUTER_PROFILE  a profile whose saved OpenRouter key the runs reuse
                          (its [credentials.refs] reference; the value is never
                          copied or printed). Required.
SYNTH_KEYS_ENV            optional .env with DEEPGRAM_API_KEY, CARTESIA_API_KEY
                          and/or OPENAI_API_KEY, read into the process
                          environment only (for --speech deepgram|cartesia|realtime).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SPEECH_KEYS = ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "OPENAI_API_KEY")


def openrouter_ref() -> str:
    raw = os.environ.get("SYNTH_OPENROUTER_PROFILE", "").strip()
    if not raw:
        raise SystemExit("Set SYNTH_OPENROUTER_PROFILE to a profile that has a saved OpenRouter key.")
    refs = tomllib.loads(Path(raw).expanduser().read_text()).get("credentials", {}).get("refs", {})
    ref = refs.get("OPENROUTER_API_KEY", "")
    if not ref:
        raise SystemExit(f"{raw} has no saved OpenRouter key reference.")
    return ref


def load_speech_keys() -> None:
    """Other services' keys into this process only; a call host started from here inherits them."""
    raw = os.environ.get("SYNTH_KEYS_ENV", "").strip()
    if not raw:
        return
    from dotenv import dotenv_values

    for name, value in dotenv_values(Path(raw).expanduser()).items():
        if name in SPEECH_KEYS and value:
            os.environ[name] = value
