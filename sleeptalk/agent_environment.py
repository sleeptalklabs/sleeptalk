"""Keep voice service credentials out of unrelated agent subprocesses."""
from __future__ import annotations

from collections.abc import Mapping
import os

VOICE_CREDENTIAL_VARIABLES = frozenset({
    "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "ELEVENLABS_API_KEY",
    "PLAYHT_API_KEY", "PLAYHT_USER_ID", "RIME_API_KEY", "HUME_API_KEY",
    "DISCORD_BOT_TOKEN", "DISCORD_TOKEN",
})


def agent_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an independent subprocess environment, preserving agent auth."""
    source = os.environ if environment is None else environment
    return {name: value for name, value in source.items() if not is_voice_credential(name)}


def is_voice_credential(name: str) -> bool:
    return name in VOICE_CREDENTIAL_VARIABLES or name.endswith("_DISCORD_BOT_TOKEN") or (
        name.startswith("DISCORD_") and (name.endswith("_TOKEN") or name.endswith("_SECRET"))
    )


def sdk_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """SDKs merge overrides into os.environ, so overwrite inherited secrets."""
    source = dict(os.environ)
    if overrides:
        source.update(overrides)
    result = agent_environment(source)
    result.update({name: "" for name in source if is_voice_credential(name)})
    return result
