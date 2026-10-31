"""Shared Discord voice transport constants."""
from __future__ import annotations

import discord

from sleeptalk.speech.common import SpeechError
from sleeptalk.discord_native.playback import DiscordPlaybackError
from sleeptalk.agents.pi_rpc import PiRPCError

COMMAND_ERRORS = (
    PermissionError,
    SpeechError,
    DiscordPlaybackError,
    PiRPCError,
    discord.DiscordException,
    RuntimeError,
    ValueError,
)

# A pause this long in a speaker's frames means they stopped transmitting;
# the barge-in pre-roll keeps only the speech since.
PREROLL_GAP_SECONDS = 0.5
