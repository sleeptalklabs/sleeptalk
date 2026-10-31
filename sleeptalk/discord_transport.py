"""Compat shim: the Discord transport lives at transports/discord.py.

The implementation moved (r2-discord-adapter-refactor); this module keeps
the historical import path (``sleeptalk.discord_transport``) working —
the server shim at ``app.discord_transport`` re-exports through here.
"""

from sleeptalk.transports.discord import DiscordTransport, transport_summary

__all__ = ["DiscordTransport", "transport_summary"]
