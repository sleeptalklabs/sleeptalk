from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, voice_recv

from sleeptalk.call_provider import CallContext, CallProviderError
from sleeptalk.detached_call import DetachedCallRequest, launch_detached_call
from sleeptalk.config import SleeptalkConfig
from sleeptalk.discord.access import DiscordAccess, access_for, audience_allowed
from sleeptalk.discord.env import (
    DiscordBotProfile,
    DiscordBotSettings,
    DiscordEnv,
)
from sleeptalk.discord.helpers import (
    connected_voice_client as _connected_voice_client,
    format_error as _format_error,
    guild_voice_client as _guild_voice_client,
    parse_allowed_channel_ids,
)
from sleeptalk.discord.session_helpers import (
    _abort_kernel_agent,
    _next_preamble_clip,
    _outcome_text,
)
from sleeptalk.agents.pi_rpc import PiRPCError
from sleeptalk.discord_native.dave import has_native_dave
from sleeptalk.discord_native.playback import DiscordPlaybackController
from sleeptalk.discord_voice import DiscordRouteAudioOutput, PiDiscordLiveSession
from sleeptalk.discord_voice.constants import COMMAND_ERRORS
from sleeptalk.discord_voice.speak import _play_clip
from sleeptalk.policy import (
    VoicePolicy,
    narration_style_or_default,
    reply_length_or_default,
)
from sleeptalk.routes.base import TurnOutcome
from sleeptalk.session import RealtimeSession, TurnLifecycle
from sleeptalk.speech_speed import format_speed

import re

_VOICE_SUMMON_RE = re.compile(
    r"\b(join|come|hop)\b[^.]*\b(voice|call|channel|here|us|in)\b",
    re.IGNORECASE,
)

_VOICE_BRIEFING = (
    "Session briefing: you are on a live Discord voice call. Everything you write is "
    "spoken aloud by text-to-speech. Reply in short, natural spoken sentences. Lead with "
    "the answer immediately, no preamble. Never use markdown, code blocks, or emoji in "
    "replies. If you change code, summarize what you changed in one spoken sentence."
)

_NARRATION_STYLE_LABELS = {
    "narrated": "Narrated",
    "silence": "Silent",
    "keyboard": "Keyboard sounds",
}
_REPLY_LENGTH_LABELS = {
    "full": "Full",
    "summarized": "Brief",
}


class DiscordSessionKernel:
    """Discord participant and presentation context around the shared session."""

    def __init__(self, session: RealtimeSession) -> None:
        self.session = session
        self.route = session.route
        if self.route is None:
            raise RuntimeError("Discord requires a configured realtime route.")
        self._turn_lock = asyncio.Lock()
        self._output = None
        self._briefing = ""
        self._briefed = False
        self._active_owner = None
        self._active_turn_id: int | None = None

    async def prepare(self) -> None:
        await self.session.prepare()

    def set_briefing(self, briefing: str) -> None:
        self._briefing = briefing.strip()

    def bind_output(self, output) -> None:
        self._output = output
        self.session.set_audio_output(output)

    async def run_turn(
        self,
        *,
        text: str = "",
        pcm: bytes = b"",
        output=None,
        turn_id: int | None = None,
        owner=None,
        extras: dict | None = None,
        on_started=None,
    ) -> TurnOutcome:
        async with self._turn_lock:
            bound_output = output if output is not None else self._output
            apply_briefing = bool(text and self._briefing and not self._briefed)
            if apply_briefing:
                text = (
                    "[VOICE SESSION INSTRUCTIONS — not a literal user utterance]\n"
                    f"{self._briefing}\n"
                    f"[USER REQUEST]\n{text}"
                )

            def started(semantic_id: int) -> None:
                self._active_owner = owner
                self._active_turn_id = semantic_id
                self._output = bound_output
                if on_started is not None:
                    on_started(semantic_id)

            async def finished(semantic_id: int, outcome) -> None:
                try:
                    if apply_briefing and outcome is not None:
                        self._briefed = True
                finally:
                    if self._active_owner is owner:
                        self._active_owner = None
                        self._active_turn_id = None

            return await self.session.run_turn(
                text=text, pcm=pcm, output=bound_output, turn_id=turn_id,
                extras=extras, lifecycle=TurnLifecycle(started, finished),
            )

    async def cancel_active_turn(self, *, owner=None) -> None:
        if (
            owner is not None
            and self._active_owner is not None
            and self._active_owner is not owner
        ):
            return
        active = self.session.active_turn()
        turn_id = self._active_turn_id or (
            active.turn_id if active is not None else None
        )
        if turn_id is None and self._output is not None:
            accounting = getattr(self._output, "accounting", None)
            if callable(accounting):
                output_turn_id = getattr(accounting(), "turn_id", -1)
                if output_turn_id >= 0:
                    turn_id = output_turn_id
        if turn_id is None:
            await self.session.interrupt_presentation()
            return
        discard_turn = getattr(self._output, "discard_turn", None)
        if callable(discard_turn):
            await discard_turn(turn_id, "barge-in")
        await self.session.interrupt_presentation(turn_id)

    async def close(self) -> None:
        await self.session.aclose()


class PiDiscordBot(commands.Bot):
    def __init__(
        self,
        config: SleeptalkConfig,
        env: DiscordEnv,
        *,
        policy: VoicePolicy | None = None,
        realtime_session: RealtimeSession,
        stt_rest_timeout_seconds: float | None = None,
    ) -> None:
        from sleeptalk.discord_check import runtime_discord_intents

        # Agent output is posted to chat; it must never ping users or roles.
        super().__init__(
            command_prefix="!",
            intents=runtime_discord_intents(config),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.env = env
        self.profile = DiscordBotProfile.from_config(config)
        self.access = DiscordAccess.from_surface(self.profile.discord)
        try:
            self.access.validate()
        except ValueError:
            from sleeptalk.discord_check import DiscordCheckError

            # A configuration problem, not a crash: the CLI prints it and exits 2 (no service restart).
            raise DiscordCheckError(
                "This Discord bot isn't finished: it needs its voice channel and your Discord user ID "
                "before it starts. Run `sleeptalk settings`, open Discord setup, and choose Finish setup "
                "(about a minute). To let anyone in the server talk to it instead, set "
                "authorization_required = false in the profile's [transport]."
            ) from None
        if not self.access.required:
            print(
                "WARNING: authorization_required = false. Anyone in the server can summon "
                f"{self.profile.name} and talk to your agent, which has whatever access your "
                "agent has to the workspace."
            )
        self.settings = DiscordBotSettings.from_env(env)
        self.policy = policy or VoicePolicy()
        timeout = stt_rest_timeout_seconds
        if timeout is None:
            timeout = getattr(getattr(config, "stt", None), "rest_timeout_seconds", 8.0)
        self.stt_rest_timeout_seconds = timeout
        self.realtime_session = realtime_session
        self.kernel = DiscordSessionKernel(realtime_session)
        realtime_session.connect()
        self.usage_meter = realtime_session.usage_meter
        self.agent = realtime_session.agent
        self.stt = realtime_session.stt
        self.streaming_stt = realtime_session.streaming_stt
        self.tts = realtime_session.tts
        self.tts_fallbacks = realtime_session.tts_fallbacks
        self.session_route = realtime_session.route
        briefing = _VOICE_BRIEFING
        persona = self.profile.persona_instructions.strip()
        if persona:
            briefing = f"{briefing} Persona: {persona}"
        self.kernel.set_briefing(briefing)
        self.preamble_clips: dict[str, list[Path | bytes]] = {}
        self._preamble_rotation: dict[str, int] = {}
        self._dynamic_clips: dict[str, Path | bytes] = {}
        self._line_clips: dict[str, Path | bytes] = {}
        self.narration_clips: dict[str, Path | bytes] = {}
        sounds_dir = _resolve_sounds_dir(config)
        self.loading_sound_path = (
            _existing(sounds_dir / "Simple4b.wav") if sounds_dir is not None else None
        )
        self.allowed_channel_ids = parse_allowed_channel_ids(
            self.profile.discord.allowed_channel_ids
        )
        self.native_sessions: dict[int, PiDiscordLiveSession] = {}
        self._warmup_started = False
        self._warmup_task: asyncio.Task | None = None
        self._bot_lease: int | None = None
        # Stay mode: rejoin tasks and recent rejoin times, per server.
        self._rejoin_tasks: dict[int, asyncio.Task] = {}
        self._rejoins: dict[int, list[float]] = {}

    @property
    def surface(self) -> str:
        return f"{self.profile.slug}-discord"

    @property
    def persona(self) -> str:
        return self.profile.slug

    def register_voice_commands(self) -> app_commands.Group:
        group = build_slash_group(self)
        extra = getattr(self, "attach_extra_commands", None)
        if callable(extra):
            extra(group)
        self.tree.add_command(group)
        return group

    async def setup_hook(self) -> None:
        from sleeptalk.discord.runtime_lease import acquire_bot_lease

        if getattr(self, "user", None) is not None:
            self._bot_lease = acquire_bot_lease(self.user.id)
        self.register_voice_commands()
        self._keep_conversation()
        _kick_warmup(self)
        try:
            if self.profile.discord.guild_id.strip():
                guild = discord.Object(id=int(self.profile.discord.guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.Forbidden:
            print(
                "Could not sync slash commands (Missing Access). "
                "Re-invite the bot with applications.commands scope so /join works."
            )

    async def on_ready(self) -> None:
        print(f"{self.profile.name} Discord bot connected as {self.user}.")
        _kick_warmup(self)
        for seat in self._seats():
            self._schedule_rejoin(seat.guild_id, reason="start")

    # ---- staying in voice ----
    # Asked to join, the bot stays in that channel until someone says leave:
    # through days of silence, a voice connection Discord drops, and a
    # restart. The seat is remembered on disk (discord/stay.py).
    REJOIN_DELAY_SECONDS = 5.0
    # More rejoins than this in the window and it stops trying: something is
    # wrong (or someone keeps removing it), and /join brings it back.
    REJOIN_LIMIT = 3
    REJOIN_WINDOW_SECONDS = 30 * 60

    def _bot_id(self) -> int | None:
        user_id = getattr(getattr(self, "user", None), "id", None)
        return user_id if isinstance(user_id, int) else None

    def _seats(self) -> list:
        from sleeptalk.discord import stay

        bot_id = self._bot_id()
        return stay.seats(bot_id) if bot_id is not None else []

    def _remember_seat(self, guild_id: int, channel_id: int, text_channel_id: int = 0) -> None:
        from sleeptalk.discord import stay

        bot_id = self._bot_id()
        if bot_id is not None:
            stay.remember(bot_id, stay.Seat(int(guild_id), int(channel_id), int(text_channel_id or 0)))

    def _forget_seat(self, guild_id: int) -> None:
        from sleeptalk.discord import stay

        bot_id = self._bot_id()
        if bot_id is not None:
            stay.forget(bot_id, int(guild_id))
        task = self._rejoin_tasks.pop(int(guild_id), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _keep_conversation(self) -> None:
        """Save the agent's conversation per bot so a restart picks it up."""
        bot_id = self._bot_id()
        use = getattr(self.agent, "use_saved_session", None)
        if bot_id is None or not callable(use):
            return
        from sleeptalk.discord import stay

        try:
            if use(stay.agent_session_dir(bot_id)) is True:
                print(f"{self.profile.name}: the conversation is saved and continues after a restart.")
        except OSError as exc:
            print(f"Could not keep the conversation across restarts: {exc}")

    def _schedule_rejoin(self, guild_id: int, *, reason: str) -> None:
        if self.is_closed():
            return
        task = self._rejoin_tasks.get(guild_id)
        if task is not None and not task.done():
            return
        self._rejoin_tasks[guild_id] = asyncio.create_task(
            self._rejoin(guild_id, reason=reason), name=f"discord-rejoin-{guild_id}"
        )

    async def _rejoin(self, guild_id: int, *, reason: str) -> bool:
        """Sit in the remembered seat again, silently. True when back in voice."""
        from sleeptalk.discord import stay

        await asyncio.sleep(self.REJOIN_DELAY_SECONDS)
        bot_id = self._bot_id()
        seat = stay.seat(bot_id, guild_id) if bot_id is not None else None
        guild = self.get_guild(guild_id)
        if seat is None or guild is None or self.is_closed():
            return False
        if discord.utils.get(self.voice_clients, guild=guild) is not None:
            return True  # already back: a new /join, or discord.py reconnected
        channel = guild.get_channel(seat.channel_id)
        if channel is None:
            print(f"stay: the voice channel {seat.channel_id} is gone; not rejoining.")
            self._forget_seat(guild_id)
            return False
        now = time.monotonic()
        recent = [t for t in self._rejoins.get(guild_id, []) if now - t < self.REJOIN_WINDOW_SECONDS]
        if len(recent) >= self.REJOIN_LIMIT:
            print(f"stay: left {channel.name} {len(recent)} times in "
                  f"{self.REJOIN_WINDOW_SECONDS // 60} minutes; not rejoining until /{self.profile.slug} join.")
            self._forget_seat(guild_id)
            return False
        self._rejoins[guild_id] = [*recent, now]
        text_channel = guild.get_channel(seat.text_channel_id) if seat.text_channel_id else channel
        joined = await self._voice_only_join(guild, channel, text_channel=text_channel, greet=False)
        print(f"stay: {'back in' if joined else 'could not rejoin'} {channel.name} ({reason}).")
        return joined

    def _warm_for_arrival(self, guild_id: int) -> None:
        """Someone walked into the bot's channel: make sure it hears, and
        reopen speech connections that closed during the quiet."""
        session = self.native_sessions.get(guild_id)
        if session is not None:
            asyncio.create_task(session._ensure_listening())
        keep_warm = getattr(self.realtime_session, "keep_warm", None)
        if callable(keep_warm):
            asyncio.create_task(keep_warm())

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild_id = int(self.profile.discord.guild_id) if self.profile.discord.guild_id.strip() else 0
        if guild_id and member.guild.id != guild_id:
            return
        if self.user and member.id == self.user.id and after.channel is None:
            self._end_native_session(member.guild.id)
            # Out of voice without being asked to leave: back to the seat.
            self._schedule_rejoin(member.guild.id, reason="dropped")
            return
        voice = discord.utils.get(self.voice_clients, guild=member.guild)
        if voice is not None and not audience_allowed(self, voice):
            # Stop the player before any await; queued workspace audio must not
            # keep draining while disconnect and agent cancellation complete.
            voice._sleeptalk_access_revoked = True
            voice.stop()
            self._forget_seat(member.guild.id)
            self._end_native_session(member.guild.id)
            await voice.disconnect(force=True)
            return
        if (
            not member.bot
            and voice is not None
            and after.channel is not None
            and getattr(after.channel, "id", None) == getattr(voice.channel, "id", None)
            and getattr(before.channel, "id", None) != after.channel.id
        ):
            self._warm_for_arrival(member.guild.id)
        if not self.profile.discord.voice_only or member.bot:
            return
        if not access_for(self).operator(member.id):
            return
        if after.channel is not None and not isinstance(voice, voice_recv.VoiceRecvClient):
            if self.profile.discord.presence_auto_join:
                await self._voice_only_join(member.guild, after.channel)
        elif before.channel is not None:
            active = discord.utils.get(self.voice_clients, guild=member.guild)
            if (
                isinstance(active, voice_recv.VoiceRecvClient)
                and before.channel.id == getattr(active.channel, "id", None)
                and not [m for m in before.channel.members if not m.bot]
            ):
                # Voice-only bots leave an empty room on purpose.
                self._forget_seat(member.guild.id)
                self._end_native_session(member.guild.id)
                await active.disconnect(force=True)

    async def on_message(self, message: discord.Message) -> None:
        if (not access_for(self).required and message.guild is not None
                and message.author.bot and message.webhook_id is None and self.user):
            if message.author.id != self.user.id:
                session = self.native_sessions.get(message.guild.id)
                # Only another bot's echo in this session's own chat joins the room.
                if session is not None and message.channel.id == getattr(
                    session.text_channel, "id", None
                ):
                    session.ingest_room_echo(
                        message.author,
                        message.content,
                        event_id=str(message.id),
                    )
        if not self.profile.discord.voice_only or not self.user:
            return
        if message.author.bot or message.guild is None:
            return
        if not access_for(self).operator(message.author.id):
            return
        if not access_for(self).channel(message.guild.id, message.channel.id):
            return
        mentioned = self.user in message.mentions or message.content.strip().lower().startswith(
            self.profile.slug
        )
        if not mentioned or not _VOICE_SUMMON_RE.search(message.content):
            return
        member = message.author if isinstance(message.author, discord.Member) else None
        channel = member.voice.channel if member and member.voice else None
        if channel is None:
            return
        await self._voice_only_join(message.guild, channel)

    async def _voice_only_join(
        self, guild: discord.Guild, channel, *, text_channel=None, greet: bool = True,
    ) -> bool:
        if not access_for(self).audience(channel, bot_id=getattr(self.user, "id", None)):
            return False
        if not self.profile.discord.native_voice_enabled or not has_native_dave():
            return False
        existing = self.native_sessions.get(guild.id)
        voice = discord.utils.get(self.voice_clients, guild=guild)
        if existing or voice is not None:
            if (
                isinstance(voice, voice_recv.VoiceRecvClient)
                and voice.is_connected()
                and getattr(voice.channel, "id", None) == channel.id
            ):
                return True
            if existing:
                self._end_native_session(guild.id)
            if voice is not None:
                with contextlib.suppress(COMMAND_ERRORS):
                    await voice.disconnect(force=True)
        try:
            voice = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
        except COMMAND_ERRORS as exc:
            print(f"voice-only join failed: {_format_error(exc)}")
            return False
        if not audience_allowed(self, voice):
            await voice.disconnect(force=True)
            return False
        # The voice channel's own chat, never a server-wide channel: transcripts
        # stay visible only to people who can see this voice channel. A rejoin
        # goes back to the chat /join was typed in.
        session = PiDiscordLiveSession(self, voice, text_channel or channel)
        self.native_sessions[guild.id] = session
        _kick_warmup(self)
        try:
            session.start()
        except COMMAND_ERRORS as exc:
            self.native_sessions.pop(guild.id, None)
            print(f"voice-only session failed: {_format_error(exc)}")
            return False
        print(f"voice-only: joined {channel.name} in {guild.name}")
        self._remember_seat(guild.id, channel.id, getattr(text_channel, "id", 0) or 0)
        if not greet:
            return True
        greeting = _next_preamble_clip(self, "greeting")
        if greeting is not None:
            with contextlib.suppress(COMMAND_ERRORS):
                await _play_clip(session.playback, greeting)
        if self.profile.runtime_kind == "hermes-gateway":
            offer = _next_preamble_clip(self, "cron_offer")
            if offer is not None:
                with contextlib.suppress(COMMAND_ERRORS):
                    await _play_clip(session.playback, offer)
                session._pending_cron_offer = True
        return True

    async def _voice_join_greeting(self, session: PiDiscordLiveSession) -> None:
        await asyncio.sleep(1.0)
        prompt = (
            "[system note — not the user speaking] The user just joined the voice "
            "call. If any scheduled (cron) task results are pending or recently "
            "arrived, offer them in one short spoken sentence ('I've got a bitcoin "
            "update from this hour — want to hear it?'). If nothing is pending, "
            "greet them briefly. One sentence total."
        )
        try:
            await self.kernel.run_turn(text=prompt, output=session.route_output)
        except COMMAND_ERRORS:
            return

    def _end_native_session(self, guild_id: int) -> None:
        session = self.native_sessions.pop(guild_id, None)
        if session is not None:
            session.stop()

    async def close(self) -> None:
        await _cancel_startup_warmup(self)
        # Shutting down is not leaving: the seats stay remembered for the next start.
        for task in self._rejoin_tasks.values():
            task.cancel()
        self._rejoin_tasks.clear()
        for guild_id in list(self.native_sessions):
            self._end_native_session(guild_id)
        await self.kernel.close()
        try:
            await super().close()
        finally:
            lease = getattr(self, "_bot_lease", None)
            if lease is not None:
                from sleeptalk.discord.runtime_lease import release_bot_lease

                self._bot_lease = None
                release_bot_lease(lease)

    async def cancel_active_turn(self, *, owner=None) -> None:
        await self.kernel.cancel_active_turn(owner=owner)


def _kick_warmup(bot: PiDiscordBot) -> None:
    if bot._warmup_started:
        return
    bot._warmup_started = True
    bot._warmup_task = asyncio.create_task(_warmup(bot))


async def _cancel_startup_warmup(bot: PiDiscordBot) -> None:
    task = bot._warmup_task
    if task is None or task is asyncio.current_task():
        return
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _warmup(bot: PiDiscordBot) -> None:
    try:
        await bot.kernel.prepare()
    except (PiRPCError, TimeoutError) as exc:
        await _abort_kernel_agent(bot)
        print(f"Session warmup failed: {_format_error(exc)}")


def build_slash_group(bot: PiDiscordBot) -> app_commands.Group:
    group = app_commands.Group(
        name=bot.profile.slug,
        description=f"Talk to {bot.profile.name} in Discord.",
        guild_only=True,
    )

    @group.command(name="settings", description="Show or change voice settings.")
    @app_commands.describe(
        narration="How tool activity sounds.",
        response="How much of a long response is spoken.",
        speed="How fast speech plays.",
    )
    @app_commands.choices(
        narration=[
            app_commands.Choice(name="Narrated", value="narrated"),
            app_commands.Choice(name="Silent", value="silence"),
            app_commands.Choice(name="Keyboard sounds", value="keyboard"),
        ],
        response=[
            app_commands.Choice(name="Full", value="full"),
            app_commands.Choice(name="Brief", value="summarized"),
        ],
        speed=[
            app_commands.Choice(name="0.5x", value=0.5),
            app_commands.Choice(name="1x", value=1.0),
            app_commands.Choice(name="1.25x", value=1.25),
            app_commands.Choice(name="1.5x", value=1.5),
        ],
    )
    async def settings(
        interaction: discord.Interaction,
        narration: app_commands.Choice[str] | None = None,
        response: app_commands.Choice[str] | None = None,
        speed: app_commands.Choice[float] | None = None,
    ):
        if not await _guard_channel(bot, interaction):
            return
        session = bot.realtime_session
        if session is None:
            await interaction.response.send_message(
                "Voice settings are unavailable because the voice runtime did not start.",
                ephemeral=True,
            )
            return
        if narration is not None:
            session.set_narration_style(narration.value)
        if response is not None:
            session.set_reply_length(response.value)
        if speed is not None:
            session.set_tts_speed(speed.value)

        narration_value = narration_style_or_default(session.policy.narration_style)
        response_value = reply_length_or_default(session.policy.reply_length)
        message = (
            f"**{bot.profile.name} voice settings**\n"
            f"Tool narration: **{_NARRATION_STYLE_LABELS[narration_value]}**\n"
            f"Response length: **{_REPLY_LENGTH_LABELS[response_value]}**\n"
            f"Speech speed: **{format_speed(session.current_tts_speed())}**\n\n"
            "Applies to the current or next call. "
            f"Resets when {bot.profile.name} restarts."
        )
        await interaction.response.send_message(message, ephemeral=True)

    if bot.profile.discord.voice_only:
        return group

    @group.command(name="ask", description="Ask this agent and get a text reply.")
    @app_commands.describe(prompt="What you want the agent to answer.")
    async def ask(interaction: discord.Interaction, prompt: str):
        if not await _guard_channel(bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            reply = _outcome_text(await bot.kernel.run_turn(text=prompt))
        except COMMAND_ERRORS as exc:
            await interaction.followup.send(f"Request failed: {_format_error(exc)}", ephemeral=True)
            return
        await interaction.followup.send(reply, ephemeral=True)

    @group.command(name="speak", description="Ask this agent and play the answer in voice.")
    @app_commands.describe(prompt="What you want the agent to answer aloud.")
    async def speak(interaction: discord.Interaction, prompt: str):
        if not await _guard_channel(bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            voice = await _connected_voice_client(bot, interaction, connect_if_needed=True)
        except COMMAND_ERRORS as exc:
            await interaction.followup.send(
                f"Could not join voice: {_format_error(exc)}", ephemeral=True
            )
            return
        if voice:
            try:
                playback = DiscordPlaybackController(
                    voice, authorized=lambda: audience_allowed(bot, voice)
                )
                output = DiscordRouteAudioOutput(playback)
                reply = _outcome_text(
                    await bot.kernel.run_turn(text=prompt, output=output)
                )
            except COMMAND_ERRORS as exc:
                await interaction.followup.send(f"Request failed: {_format_error(exc)}", ephemeral=True)
                return
            await interaction.followup.send(reply, ephemeral=True)

    @group.command(name="join", description="Join your voice channel natively.")
    async def join(interaction: discord.Interaction):
        await _start_native_voice(bot, interaction)

    @group.command(name="call", description="Join your voice channel natively.")
    async def call(interaction: discord.Interaction):
        await _start_native_voice(bot, interaction)

    @group.command(name="link", description="Create a private browser voice-call link.")
    async def link(interaction: discord.Interaction):
        await _send_live_voice_link(bot, interaction)

    @group.command(name="leave", description="Disconnect native voice.")
    async def leave(interaction: discord.Interaction):
        await _end_native_voice(bot, interaction)

    @group.command(name="end", description="Disconnect native voice.")
    async def end(interaction: discord.Interaction):
        await _end_native_voice(bot, interaction)

    @group.command(name="reset", description="Reset memory for this text channel.")
    async def reset(interaction: discord.Interaction):
        if not await _guard_channel(bot, interaction):
            return
        start_new = getattr(bot.agent, "start_new_conversation", None)
        if not callable(start_new):
            await interaction.response.send_message(
                "This agent keeps no saved conversation; restart the bot to open a fresh session.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        with contextlib.suppress(Exception):
            await bot.kernel.cancel_active_turn()
        try:
            await start_new()
        except Exception as exc:  # noqa: BLE001 — the reason goes back to Discord
            await interaction.followup.send(f"Could not start a fresh conversation: {_format_error(exc)}", ephemeral=True)
            return
        await interaction.followup.send(
            f"{bot.profile.name} starts a fresh conversation from your next words.", ephemeral=True
        )

    @group.command(name="status", description="Show this agent's Discord status.")
    async def status(interaction: discord.Interaction):
        if not await _guard_channel(bot, interaction):
            return
        voice = _guild_voice_client(interaction)
        connected = voice.is_connected() if voice else False
        state = (
            f"voice: {voice.channel.name}, connected: {connected}"
            if voice
            else "voice: disconnected"
        )
        session = bot.native_sessions.get(interaction.guild_id)
        live = "live: on" if session is not None else "live: off"
        if voice is not None and session is not None:
            live += ", listening" if voice.is_listening() else ", not hearing (it retries every 30 s)"
        seat = next((s for s in bot._seats() if s.guild_id == interaction.guild_id), None)
        stays = ""
        if seat is not None:
            seat_channel = interaction.guild.get_channel(seat.channel_id) if interaction.guild else None
            stays = f" Stays in {getattr(seat_channel, 'name', 'its voice channel')} until /{bot.profile.slug} leave."
        await interaction.response.send_message(
            f"{bot.profile.name} online. {state}. {live}.{stays}", ephemeral=True
        )

    vad_group = app_commands.Group(
        name="vad",
        description="Toggle and tune the barge-in VAD for side-by-side testing.",
    )

    def _get_turn_session(interaction: discord.Interaction) -> PiDiscordLiveSession | None:
        if not interaction.guild_id:
            return None
        return bot.native_sessions.get(interaction.guild_id)

    @vad_group.command(name="status", description="Show the current VAD configuration.")
    async def vad_status(interaction: discord.Interaction):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session. Use `/vad mode` after joining a call.",
                ephemeral=True,
            )
            return
        snap = session.turns.config_snapshot()
        lines = ["**VAD Configuration**"]
        for key, value in snap.items():
            lines.append(f"- {key}: `{value}`")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    @vad_group.command(name="mode", description="Switch the barge-in VAD mode.")
    @app_commands.describe(
        mode="rms | silero | hybrid | compare (compare runs both in parallel)"
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="rms", value="rms"),
            app_commands.Choice(name="silero", value="silero"),
            app_commands.Choice(name="hybrid", value="hybrid"),
            app_commands.Choice(name="compare", value="compare"),
        ]
    )
    async def vad_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session. Join a call first (`/join`).", ephemeral=True
            )
            return
        snap = session.turns.reconfigure(vad_mode=mode.value)
        await interaction.response.send_message(
            f"VAD mode → **{mode.value}**. Profile: `{snap['vadProfile']}`.",
            ephemeral=True,
        )

    @vad_group.command(name="primary", description="In compare mode, which detector triggers barge-in.")
    @app_commands.describe(detector="rms | silero")
    @app_commands.choices(
        detector=[
            app_commands.Choice(name="rms", value="rms"),
            app_commands.Choice(name="silero", value="silero"),
        ]
    )
    async def vad_primary(interaction: discord.Interaction, detector: app_commands.Choice[str]):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session.", ephemeral=True
            )
            return
        snap = session.turns.reconfigure(compare_primary=detector.value)
        await interaction.response.send_message(
            f"Compare primary → **{detector.value}**", ephemeral=True
        )

    @vad_group.command(name="profile", description="Tag this run for log comparison (e.g. 'aggressive').")
    @app_commands.describe(name="Free-form tag for this Silero settings run.")
    async def vad_profile(interaction: discord.Interaction, name: str):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session.", ephemeral=True
            )
            return
        tag = name.strip()[:60] or "default"
        snap = session.turns.reconfigure(vad_profile=tag)
        await interaction.response.send_message(
            f"VAD profile → **{tag}**", ephemeral=True
        )

    @vad_group.command(name="silero", description="Tune Silero VAD parameters live.")
    @app_commands.describe(
        confidence="Silero confidence threshold (0.0–1.0, default 0.70)",
        start_ms="Min speech duration to trigger (ms, default 220)",
        stop_ms="Min silence duration to stop (ms, default 900)",
        min_volume="Min normalized RMS volume (0.0–1.0, default 0.005)",
        barge_min_ms="Sustained speech needed to interrupt the bot (ms, default 150)",
        barge_min_volume="Echo-gate min volume for barge-in (0.0–1.0, default 0.03)",
        cooldown_ms="Suppress re-triggers after a barge-in (ms, default 1200)",
    )
    async def vad_silero(
        interaction: discord.Interaction,
        confidence: float | None = None,
        start_ms: int | None = None,
        stop_ms: int | None = None,
        min_volume: float | None = None,
        barge_min_ms: int | None = None,
        barge_min_volume: float | None = None,
        cooldown_ms: int | None = None,
    ):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session.", ephemeral=True
            )
            return
        snap = session.turns.reconfigure(
            silero_confidence=confidence,
            silero_start_ms=start_ms,
            silero_stop_ms=stop_ms,
            silero_min_volume=min_volume,
            silero_barge_min_ms=barge_min_ms,
            silero_barge_min_volume=barge_min_volume,
            post_interrupt_cooldown_ms=cooldown_ms,
        )
        lines = ["**Silero reconfigured:**"]
        lines.append(f"- confidence: `{snap['sileroConfidence']}`")
        lines.append(f"- startMs: `{snap['sileroStartMs']}`")
        lines.append(f"- stopMs: `{snap['sileroStopMs']}`")
        lines.append(f"- minVolume: `{snap['sileroMinVolume']}`")
        lines.append(f"- bargeMinMs: `{snap['sileroBargeMinMs']}`")
        lines.append(f"- bargeMinVolume: `{snap['sileroBargeMinVolume']}`")
        lines.append(f"- cooldownMs: `{snap['postInterruptCooldownMs']}`")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    @vad_group.command(name="stats", description="Show compare-mode counters for the current playback.")
    async def vad_stats(interaction: discord.Interaction):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session.", ephemeral=True
            )
            return
        s = session.turns.stats
        lines = [
            "**VAD Compare Stats (current playback)**",
            f"- mode: `{s.last_vad_mode}`",
            f"- pcm frames: `{s.playback_pcm_frames}`",
            f"- rms speech: `{s.rms_speech_frames}`",
            f"- silero speech: `{s.silero_speech_frames}`",
            f"- both: `{s.both_speech_frames}`",
            f"- agree: `{s.agree_frames}` · disagree: `{s.disagree_frames}`",
            f"- triggers → rms-only: `{s.rms_only_triggers}` · silero-only: `{s.silero_only_triggers}` · both: `{s.both_triggers}`",
            f"- interruptions: `{s.interruptions}`",
            f"- max RMS: `{s.max_rms}` · max conf: `{round(s.max_vad_confidence, 4)}`",
        ]
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    @vad_group.command(name="debug", description="Toggle verbose per-frame VAD logging.")
    @app_commands.describe(enabled="true = log every 10th frame; false = off")
    async def vad_debug(interaction: discord.Interaction, enabled: bool):
        if not await _guard_channel(bot, interaction):
            return
        session = _get_turn_session(interaction)
        if not session:
            await interaction.response.send_message(
                "No active voice session.", ephemeral=True
            )
            return
        session.turns.reconfigure(log_debug=enabled)
        await interaction.response.send_message(
            f"VAD debug logging → **{'on' if enabled else 'off'}**", ephemeral=True
        )

    group.add_command(vad_group)
    return group


_build_pi_group = build_slash_group


async def _send_live_voice_link(
    bot: PiDiscordBot, interaction: discord.Interaction
) -> None:
    if not await _guard_channel(bot, interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    rendezvous_url = bot.env.rendezvous_url.strip()
    config_path = bot.env.profile_path.strip()
    if not rendezvous_url or not config_path:
        await interaction.followup.send(
            "Browser calls are not configured. Set SLEEPTALK_RENDEZVOUS_URL and "
            "SLEEPTALK_PROFILE in the bot's local .env file, then restart the bot.",
            ephemeral=True,
        )
        return
    if not Path(config_path).expanduser().is_file():
        await interaction.followup.send(
            "The configured browser-call profile was not found on the laptop.",
            ephemeral=True,
        )
        return

    user_id = getattr(interaction.user, "id", "unknown")
    guild_id = interaction.guild_id or "dm"
    request = DetachedCallRequest(
        rendezvous_url=rendezvous_url,
        config_path=str(Path(config_path).expanduser().resolve()),
        context=CallContext(
            agent_instance=bot.profile.slug,
            conversation_id="",
            platform_user_id=f"discord:{guild_id}:{user_id}",
        ),
        ttl_seconds=5 * 60,
    )
    try:
        url = await asyncio.to_thread(launch_detached_call, request)
    except CallProviderError as exc:
        await interaction.followup.send(
            f"Could not create a live voice link: {_format_error(exc)}",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Your single-use voice link (expires in five minutes):\n{url}",
        ephemeral=True,
        suppress_embeds=True,
    )


# Joining a voice channel can hang when Discord voice can't be reached from this
# computer (outbound UDP blocked); past this the join gives up and says so.
VOICE_CONNECT_SECONDS = 45


async def _start_native_voice(bot: PiDiscordBot, interaction: discord.Interaction) -> None:
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException:
        return
    # Discord shows "is thinking..." until a follow-up arrives: always send one.
    try:
        await _join_after_defer(bot, interaction)
    except Exception as exc:  # noqa: BLE001 — the reason goes to Discord and the log
        if isinstance(exc, TimeoutError):
            reason = (f"Couldn't connect to Discord voice within {VOICE_CONNECT_SECONDS} seconds. The computer "
                      "running the bot must be able to send UDP to Discord's voice servers; check its firewall "
                      "or hosting provider.")
        else:
            reason = f"Could not join voice: {_format_error(exc) or type(exc).__name__}"
        print(f"discord join failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id in bot.native_sessions:
            bot._end_native_session(guild_id)
        voice = _guild_voice_client(interaction)
        if voice is not None:
            with contextlib.suppress(Exception):
                await voice.disconnect(force=True)
        with contextlib.suppress(discord.HTTPException):
            await interaction.followup.send(reason, ephemeral=True)


async def _join_after_defer(bot: PiDiscordBot, interaction: discord.Interaction) -> None:
    if not await _guard_channel(bot, interaction):
        return
    if not bot.profile.discord.native_voice_enabled:
        await interaction.followup.send(
            "Native Discord voice is disabled for this agent.", ephemeral=True
        )
        return
    if not has_native_dave():
        await interaction.followup.send(
            "Native Discord voice needs davey/libdave support in the server environment.",
            ephemeral=True,
        )
        return
    try:
        async with asyncio.timeout(VOICE_CONNECT_SECONDS):
            voice = await _connected_voice_client(bot, interaction, connect_if_needed=True)
    except COMMAND_ERRORS as exc:
        await interaction.followup.send(
            f"Could not join voice: {_format_error(exc)}", ephemeral=True
        )
        return
    if not voice:
        await interaction.followup.send(
            "Join a voice channel first, then call me in.", ephemeral=True
        )
        return

    session = bot.native_sessions.get(voice.guild.id)
    if session and session.voice is not voice:
        bot._end_native_session(voice.guild.id)
        session = None
    if not session:
        session = PiDiscordLiveSession(bot, voice, interaction.channel)
        bot.native_sessions[voice.guild.id] = session
    _kick_warmup(bot)
    try:
        session.start()
    except COMMAND_ERRORS as exc:
        bot._end_native_session(voice.guild.id)
        await interaction.followup.send(
            f"Native listen failed: {_format_error(exc)}", ephemeral=True
        )
        return
    bot._remember_seat(voice.guild.id, voice.channel.id, getattr(interaction.channel, "id", 0) or 0)
    await interaction.followup.send(
        f"Joined {voice.channel.name}. {bot.profile.name} is listening. Just talk in the call. "
        f"It stays in the channel, even when it's empty, until `/{bot.profile.slug} leave`."
    )


async def _end_native_voice(bot: PiDiscordBot, interaction: discord.Interaction) -> None:
    if not await _guard_channel(bot, interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    voice = _guild_voice_client(interaction)
    if getattr(interaction, "guild_id", None):
        bot._forget_seat(interaction.guild_id)
    if not voice:
        await interaction.followup.send(f"No {bot.profile.name} voice session was active here.")
        return
    bot._end_native_session(voice.guild.id)
    await voice.disconnect(force=True)
    await interaction.followup.send("Native Discord voice stopped.")


def _existing(path: Path) -> Path | None:
    return path if path.is_file() else None


def _resolve_sounds_dir(config) -> Path | None:
    candidates: list[Path] = []
    workspace = str(
        getattr(getattr(config, "agent", None), "workspace_dir", "") or ""
    ).strip()
    if workspace:
        root = Path(workspace).expanduser()
        candidates.append(root / "sounds")
    source = str(
        getattr(config, "source_path", "")
        or getattr(getattr(config, "agent", None), "source_path", "")
        or ""
    ).strip()
    if source:
        start = Path(source).expanduser().resolve()
        here = start.parent if start.is_file() else start
        candidates.extend(parent / "sounds" for parent in (here, *here.parents))
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        if (parent / ".git").exists() or (parent / "sleeptalk.toml").exists():
            candidates.append(parent / "sounds")
            break
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path.is_dir():
            return path
    return None


async def _guard_channel(bot: PiDiscordBot, interaction: discord.Interaction) -> bool:
    access = access_for(bot)
    user = getattr(interaction, "user", None)
    guild_id = getattr(interaction, "guild_id", None)
    refusal = ""
    if not access.operator(getattr(user, "id", None)):
        refusal = ("Only the people allowed to use this bot can do this, and your Discord user ID "
                   "isn't one of them. Add it under Who can talk to it in Sleeptalk's settings.")
    elif access.required:
        # The locked voice channel is what matters, not where the command is typed:
        # its chat, or any chat in the server while you sit in that voice channel.
        voice = getattr(getattr(user, "voice", None), "channel", None)
        if not (access.channel(guild_id, interaction.channel_id)
                or access.channel(guild_id, getattr(voice, "id", None))):
            names = ", ".join(f"<#{channel}>" for channel in sorted(access.channels))
            refusal = f"Join {names or 'the voice channel it is locked to'} first, then use this again."
    else:
        allowed = not bot.allowed_channel_ids or interaction.channel_id in bot.allowed_channel_ids
        # Open, but still only in the configured server (never a DM or another server).
        allowed = allowed and (not access.guild_id or guild_id == access.guild_id)
        if not allowed:
            refusal = "This channel is not authorized."
    if not refusal:
        return True
    if interaction.response.is_done():
        await interaction.followup.send(refusal, ephemeral=True)
    else:
        await interaction.response.send_message(refusal, ephemeral=True)
    return False
