"""A terminal save regenerates the profile from its answers. Settings made on
the settings page, by `host`, or by hand that the answers don't carry must
survive it while their table still means the same thing."""
from __future__ import annotations

import tomllib

from sleeptalk.profile_edit import keep_unmodelled_settings

REALTIME_BEFORE = """
[route]
type = "hybrid"
provider = "openai-realtime"
model = "gpt-realtime-2.1"
voice = "marin"
specialist_tool = true
audio_first = false

[transport]
type = "discord"
guild_id = "1"
speaker_ids = "11,12"
listener_ids = "13"

[stt]
provider = "openrouter"
model = "nvidia/parakeet-tdt-0.6b-v3"
language = "en-US"

[tts]
provider = "openrouter"
voice_id = "eve"
speed = 1.0
"""

REALTIME_AFTER = """
[route]
type = "hybrid"
provider = "openai-realtime"
model = "gpt-realtime-2.1"
voice = "cedar"

[transport]
type = "discord"
guild_id = "1"

[tts]
speed = 1.25
"""


def test_realtime_options_discord_lists_and_speech_settings_survive() -> None:
    kept = tomllib.loads(keep_unmodelled_settings(REALTIME_AFTER, REALTIME_BEFORE))
    assert kept["route"]["voice"] == "cedar"  # the wizard's own answer wins
    assert kept["route"]["specialist_tool"] is True and kept["route"]["audio_first"] is False
    assert (kept["transport"]["speaker_ids"], kept["transport"]["listener_ids"]) == ("11,12", "13")
    # Switching to Realtime keeps the speech service, so switching back finds it.
    assert kept["stt"]["provider"] == "openrouter"
    assert (kept["tts"]["provider"], kept["tts"]["voice_id"], kept["tts"]["speed"]) == ("openrouter", "eve", 1.25)


def test_a_switched_owner_leaves_its_extras_behind() -> None:
    before = """
[route]
type = "hybrid"
specialist_tool = true

[transport]
type = "discord"
speaker_ids = "11"

[transport.options]
delivery = "tailnet"
native_voice = true

[stt]
provider = "deepgram"
model = "nova-3"
keyterms = "sleeptalk"
endpointing_ms = 800
"""
    after = """
[route]
type = "cascade"

[transport]
type = "web"

[stt]
provider = "openrouter"
model = "nvidia/parakeet-tdt-0.6b-v3"
"""
    kept = tomllib.loads(keep_unmodelled_settings(after, before))
    assert "specialist_tool" not in kept["route"]
    assert "speaker_ids" not in kept["transport"] and "options" not in kept["transport"]
    assert "keyterms" not in kept["stt"] and "endpointing_ms" not in kept["stt"]


def test_same_owner_keeps_extras_but_not_what_the_wizard_turned_off() -> None:
    before = """
[transport]
type = "web"

[transport.options]
delivery = "tailnet"
relay_region = "eu"

[stt]
provider = "deepgram"
model = "nova-3"
keyterms = "sleeptalk"

[log]
latency = true
path = "logs/x.jsonl"
"""
    after = """
[transport]
type = "web"

[stt]
provider = "deepgram"
model = "nova-2"

[log]
latency = false
"""
    kept = tomllib.loads(keep_unmodelled_settings(after, before))
    # Tailnet turned off in the terminal stays off; the rest of the table stays.
    assert kept["transport"]["options"] == {"relay_region": "eu"}
    assert (kept["stt"]["model"], kept["stt"]["keyterms"]) == ("nova-2", "sleeptalk")
    assert kept["log"] == {"latency": False, "path": "logs/x.jsonl"}
