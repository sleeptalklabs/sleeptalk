"""Native Discord live voice session (join/speak/turn transport)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from array import array
from collections import deque
from typing import Any

import discord
from discord.ext import voice_recv

from sleeptalk.discord.helpers import discord_pcm_bytes as _discord_pcm_bytes
from sleeptalk.discord.helpers import actor_id as discord_actor_id
from sleeptalk.discord.helpers import format_error as _format_error
from sleeptalk.discord.observe import _log_native_endpoint_decision
from sleeptalk.streaming import elapsed_suffix as _elapsed_suffix
from sleeptalk.discord.session_helpers import (
    _cancel_session_turn,
    _dynamic_clip,
    _format_voice_turn,
    _is_status_query,
    _next_preamble_clip,
    _outcome_text,
    _pick_ack_clip,
    _prepare_kernel_for_voice,
    _user_speaking,
    parse_voice_echo,
)
from sleeptalk.discord_native import NativeDiscordOpusSink
from sleeptalk.pause_ahead import configured_pause_ms, start_from_pause, warm_on_speech
from sleeptalk.discord.access import access_for, audience_allowed, speaker_allowed
from sleeptalk.discord_native.playback import DiscordPlaybackController
from sleeptalk.discord_native.timeline import VoiceTimeline
from sleeptalk.discord_native.turns import DiscordTurnCoordinator, interrupt_from_voice_policy
from sleeptalk.discord_voice.constants import (
    COMMAND_ERRORS,
    PREROLL_GAP_SECONDS,
)
from sleeptalk.discord_voice.route_audio import DiscordRouteAudioOutput
from sleeptalk.discord_voice.reply_text import ReplyFeed
from sleeptalk.discord_voice.transcripts import LiveTranscripts
from sleeptalk.input_transcripts import input_endpointing_config, input_partial, input_source
from sleeptalk.discord_voice.speak import _play_clip
from sleeptalk.discord_voice.utterance_patterns import (
    _BACKCHANNEL_RE,
    _CONTINUE_RE,
    _CONTROL_WORD_RE,
    _RESUME_NO_RE,
    _RESUME_YES_RE,
    _SHORT_CONFIRMATION_RE,
)
from sleeptalk.room import (
    ParticipantKind,
    Room,
    RouteAction,
    Speaker,
    SpeakerId,
    Utterance,
    identity_from_agent,
)
from sleeptalk.routes.base import RouteType
from sleeptalk.session_events import SessionEvent, SessionEventType
from sleeptalk.streaming import select_ack_pool
from sleeptalk.text import is_hearable
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor
from sleeptalk.voice_loop.endpointing import estimate_completion_score
from sleeptalk.voice_loop.telemetry import (
    VoiceLatencyTrace,
    cascade_turn_identity,
    log_chat_turn,
    log_latency_event,
)
from sleeptalk.voice_loop.vad import pcm_duration_ms, pcm_rms


def _session_room(session) -> Room:
    room = getattr(session, "_room", None)
    if isinstance(room, Room):
        return room
    profile = session.bot.profile
    bot_user = getattr(session.bot, "user", None)
    user_id = getattr(bot_user, "id", None)
    self_id = SpeakerId(str(user_id if user_id is not None else profile.slug))
    room = Room(identity_from_agent(profile, self_id))
    session._room = room
    return room


def _speaker_for(user) -> Speaker:
    display = (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(getattr(user, "id", "user"))
    )
    kind = ParticipantKind.BOT if getattr(user, "bot", False) else ParticipantKind.HUMAN
    return Speaker(SpeakerId(str(user.id)), str(display), kind)


def _notice_voice_members(session) -> None:
    room = _session_room(session)
    voice = getattr(session, "voice", None)
    channel = getattr(voice, "channel", None)
    for member in tuple(getattr(channel, "members", None) or ()):
        room.notice(_speaker_for(member))


def _utterance_for(user, text: str) -> Utterance:
    return Utterance(_speaker_for(user), text)


def _apology_owed(exc: BaseException) -> bool:
    """Whether Discord should speak its own apology for a failed turn.

    Not when the route already told the caller, and not when the voice is
    what failed: it can't say the apology either, and the route has played
    the voice-unavailable tone (trying held a stalled turn 6 s more).
    """
    return not getattr(exc, "caller_told", False) and not getattr(exc, "voice_failed", False)


def _stereo_to_mono_s16(pcm: bytes) -> bytes:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 4])
    mono = array("h")
    mono.extend(
        (int(samples[index]) + int(samples[index + 1])) // 2
        for index in range(0, len(samples), 2)
    )
    return mono.tobytes()


def _trace_session_id(session: Any) -> str:
    kernel = getattr(getattr(session, "bot", None), "kernel", None)
    if kernel is None:
        return ""
    bound = getattr(kernel, "session", None)
    if bound is not None:
        return str(getattr(bound, "session_id", "") or "")
    return str(getattr(kernel, "session_id", "") or "")


def _configured(bot, section: str) -> str:
    """The configured `type` of a bot's agent or route, or empty."""
    return str(getattr(getattr(getattr(bot, "config", None), section, None), "type", "") or "")


_call_log = logging.getLogger(__name__)


def _voice_chat(voice: Any) -> Any:
    """The voice channel's own integrated text chat, when Discord offers one."""
    channel = getattr(voice, "channel", None)
    return channel if callable(getattr(channel, "send", None)) else None


def _attribute_written_turn(session: Any, user: Any, transcript: str) -> None:
    """Name the caller of the turn about to start, for the written call.

    Discord serializes turns on ``turn_lock``, so the next turn the route
    opens is this one. The route's own user transcript is not used: what
    Discord hands the route is a prompt, which can carry room context or a
    system note the channel should never show.
    """
    feed = getattr(session, "_reply_feed", None)
    if feed is not None:
        feed.speaking(getattr(user, "display_name", None) or "You", transcript)


def _written_name(bot: Any, voice: Any, *, parseable: bool) -> str:
    """Who the written call says answered: the name the server shows for
    the bot (its nickname there, else its account name), not the profile's
    file name. An open profile keeps the profile name, which peer bots
    match when they parse its echo back."""
    profile_name = getattr(getattr(bot, "profile", None), "name", "") or "Agent"
    if parseable:
        return profile_name
    guild = getattr(getattr(voice, "channel", None), "guild", None) or getattr(voice, "guild", None)
    shown = getattr(getattr(guild, "me", None), "display_name", None) or getattr(getattr(bot, "user", None), "name", None)
    return str(shown or profile_name)


def _log_call_text(session: Any) -> None:
    """Say in the log whether this call will be written, and where."""
    feed = getattr(session, "_reply_feed", None)
    channel = getattr(session, "text_channel", None)
    if feed is not None:
        _call_log.info("Call text: writing replies to #%s", getattr(channel, "name", "") or "the voice chat")
    else:
        _call_log.info("Call text: off for this call%s", "" if channel is not None else " (no text channel)")


def _surface_flag(bot: Any, name: str, default: bool) -> bool:
    surface = getattr(getattr(bot, "profile", None), "discord", None)
    value = getattr(surface, name, None)
    return default if value is None else bool(value)


class PiDiscordLiveSession:
    def __init__(
        self,
        bot: Any,
        voice: voice_recv.VoiceRecvClient,
        text_channel: discord.abc.Messageable | None,
    ) -> None:
        self.bot = bot
        self.voice = voice
        access = access_for(bot)
        # An authorized profile writes only into the voice channel's own chat.
        # audience_allowed() already proves every member of that channel is a
        # permitted listener, so the written call reaches exactly the people
        # the spoken one does. A server-wide text channel has the wider
        # readership, so a /join typed in one falls back to the voice chat
        # instead of publishing the call there.
        self.text_channel = _voice_chat(voice) if access.required else text_channel
        cascade = (
            getattr(bot.realtime_session, "route_type", RouteType.CASCADE)
            is RouteType.CASCADE
        )
        self._live_transcripts = (
            LiveTranscripts(self.text_channel)
            if self.text_channel and getattr(bot.settings, "discord_transcript_debug", False)
            and cascade
            else None
        )
        # The call in writing: one message per finished agent turn, from the
        # same events the web call page's transcript renders.
        self._reply_feed = (
            ReplyFeed(
                self.text_channel,
                name=_written_name(bot, voice, parseable=not access.required),
                caller_speech=_surface_flag(bot, "post_caller_speech", False),
                # Only an open profile reads peer bots' posts back in, so only
                # it needs the parseable echo marker.
                echo_marker=not access.required,
                authorized=lambda: audience_allowed(bot, voice),
            )
            if self.text_channel
            and cascade
            and _surface_flag(bot, "post_replies", True)
            and getattr(bot.settings, "discord_voice_text_echo", True)
            else None
        )
        self._caption_watch: asyncio.Task | None = None
        self.turn_lock = asyncio.Lock()
        self.bot_speaking = False
        self.loop = asyncio.get_running_loop()
        self._speak_tasks: list[asyncio.Task] = []
        self._interrupted_context: dict | None = None
        # Live in-flight turn state, for soft status queries mid-turn.
        self._live_turn: dict | None = None
        self._soft_barge_pending = False
        # Words that never settle a soft barge (a cough, a dropped clip) must
        # not leave the answer paused for good: the watchdog resumes it.
        self._soft_barge_watchdog: asyncio.TimerHandle | None = None
        self._soft_barge_settling = False
        self._pending_cron_offer = False
        self._lifecycle = LifecycleSupervisor()
        # P0 turn correlation: monotonic counter bumped every time a new user
        # turn commits (and once more at teardown). The response pipeline
        # stamps its run with the generation at start; a mismatch on audio
        # arrival means the response is stale and must not play.
        self._turn_generation = 0
        # Cut-off metric: when the user starts speaking again within ~750ms
        # of their previous utterance committing, the previous endpoint
        # likely fired mid-thought. Logged per turn as a suspect flag.
        self._last_utterance_committed_at: dict[int, float] = {}
        # Created at local speech onset (not after endpointing), then popped
        # by handle_utterance so STT, agent, and TTS share one trace ID and
        # one mic-origin clock.
        self._speech_latency_traces: dict[int, VoiceLatencyTrace] = {}
        self._pending_endpoint_timings: dict[int, deque[dict[str, int]]] = {}
        self._task_endpoint_timings: dict[asyncio.Task, dict[str, int]] = {}
        # Record when a continuation actually starts. Measuring grace only
        # when that continuation later flushes makes the merge path impossible
        # for any resumed phrase longer than resume_grace_ms.
        self._grace_continuation_started_at: dict[int, float] = {}
        # Grace-period abort support: the prompt text of each user's
        # in-flight turn, so a resumed-within-grace utterance can cancel it
        # and re-ask with the merged transcript instead of letting the bot
        # answer a fragment.
        self._in_flight_prompts: dict[int, str] = {}
        self._room: Room | None = None
        # Audio-native turn scorer (Smart Turn v3): built once per session,
        # ONNX session loads lazily on first ambiguous endpoint. None when
        # the profile disables it.
        self._smart_turn_scorer = None
        if bot.policy.capture.smart_turn:
            from sleeptalk.voice_loop.smart_turn import SmartTurnScorer

            self._smart_turn_scorer = SmartTurnScorer(
                threshold=bot.policy.capture.smart_turn_threshold
            )
        # Barge-in capture: rolling pre-roll of each user's mic (~1.5s of
        # 20ms frames) so the sentence that CAUSES an interruption — spoken
        # while the bot was still playing — is heard, not clipped off.
        self._preroll: dict[int, deque[bytes]] = {}
        self._preroll_heard_at: dict[int, float] = {}
        # Users a barge-in just fired for: their audio keeps being collected
        # even while the old turn still holds the ignore gate.
        self._barge_listen: set[int] = set()
        # Last time any user was heard above the RMS floor — lets the resume
        # offer wait for a genuine gap instead of talking over the user.
        self._last_user_voice_at = 0.0
        # Post-barge-in fragment holding: user_id -> {"text", "event",
        # "continuation"}. Set when an incomplete utterance arrives right
        # after a barge-in; the in-flight turn waits for the continuation
        # instead of answering the fragment.
        self._pending_fragments: dict[int, dict] = {}
        self._last_barge_at = 0.0
        self.streaming_stt = bot.streaming_stt
        self._hybrid_route = (
            getattr(getattr(bot, "realtime_session", None), "route_type", None)
            is RouteType.HYBRID
        )
        self._hybrid_unsubscribe = None
        self._job_unsubscribe = None
        self._hybrid_posts: deque[str] = deque()
        self._hybrid_post_task: asyncio.Task | None = None
        self._hybrid_authors: dict[int, str] = {}
        self._hybrid_traces: dict[int, VoiceLatencyTrace] = {}
        self._hybrid_audio_turns: set[int] = set()
        # A batch-only listener (OpenRouter, Groq, ...) has no stream: each turn
        # is transcribed whole when it ends, through the session's REST path.
        if self.streaming_stt is None and getattr(bot, "stt", None) is None and not self._hybrid_route:
            raise RuntimeError(
                "Discord STT must come from the registry-built RealtimeSession."
            )
        self.playback = DiscordPlaybackController(
            voice,
            VoiceTimeline(),
            authorized=lambda: audience_allowed(bot, voice),
            on_started=self._playback_started,
            on_stopped=self._playback_stopped,
        )
        self.route_output = DiscordRouteAudioOutput(
            self.playback,
            on_write=self._route_audio_started,
        )
        self.turns = DiscordTurnCoordinator(
            config=bot.policy.turn_config(
                surface=bot.surface,
                persona=bot.persona,
                agent=_configured(bot, "agent"),
                route=_configured(bot, "route"),
            ),
            playback=self.playback,
            timeline=VoiceTimeline(),
            on_interrupt=self._handle_barge_in,
            interrupt=interrupt_from_voice_policy(bot.policy),
        )
        self.sink = NativeDiscordOpusSink(
            authorized=lambda user: speaker_allowed(bot, voice, user),
            loop=self.loop,
            min_bytes=_discord_pcm_bytes(bot.policy.capture.min_utterance_ms),
            silence_seconds=bot.policy.capture.silence_ms / 1000,
            on_utterance=self.handle_utterance,
            should_ignore_audio=lambda: self.turn_lock.locked(),
            collect_during_ignore=self._collect_during_turn,
            on_pcm_frame=self._handle_pcm_frame,
            on_speech_started=self._speech_started,
            speech_rms_threshold=bot.policy.capture.rms_threshold,
            vad_mode=bot.policy.capture.vad_mode,
            compare_primary=bot.policy.capture.compare_primary,
            smart_turn_scorer=self._smart_turn_scorer,
            on_smart_turn_event=lambda user_id, event: log_latency_event(
                surface=bot.surface,
                transport="discord-native",
                persona=bot.persona,
                stage="smart_turn",
                userId=user_id,
                **event,
            ),
            on_vad_compare_event=lambda user_id, event: log_latency_event(
                surface=bot.surface,
                transport="discord-native",
                persona=bot.persona,
                stage="capture_vad_compare",
                userId=user_id,
                **event,
            ),
            silero_vad_config=bot.policy.silero_config(),
            endpointing_config=input_endpointing_config(
                bot.realtime_session, bot.policy.endpointing_config(),
            ),
            on_capture_event=self._on_capture_event,
            turn_completion_provider=(
                bot.realtime_session.turn_completion_signal
            ),
            on_capture_gap=lambda user_id, event: log_latency_event(
                surface=bot.surface,
                transport="discord-native",
                persona=bot.persona,
                stage="capture_frame_starvation",
                userId=user_id,
                **event,
            ),
            on_decrypt_failure=lambda user_id, event: log_latency_event(
                surface=bot.surface,
                transport="discord-native",
                persona=bot.persona,
                stage="capture_decrypt_failure",
                userId=user_id,
                **event,
            ),
            on_endpoint_decision=lambda user_id, decision: _log_native_endpoint_decision(
                surface=bot.surface,
                persona=bot.persona,
                user_id=user_id,
                decision=decision,
            ),
            # Judge "is the caller done?" by words, as web and terminal do: a
            # batch listener's early transcript at the pause (without it every
            # short utterance ended on 120 ms of quiet), or a streaming
            # listener asked to finalize what it holds.
            on_pause=self._on_speech_pause,
            pause_ms=configured_pause_ms(bot.realtime_session),
            early_transcripts=lambda: bool(
                callable(getattr(bot.realtime_session, "listens_ahead", None))
                and bot.realtime_session.listens_ahead()
            ),
        )

    def start(self) -> None:
        if not audience_allowed(self.bot, self.voice):
            raise PermissionError("Discord voice participants are not authorized.")
        self._subscribe_hybrid_events()
        self._subscribe_job_events()
        _log_call_text(self)
        self._start_listen_watch()
        if self.voice.is_listening():
            return
        self.voice.listen(self.sink, after=self._after_listen)
        if self._live_transcripts is not None and (
            self._caption_watch is None or self._caption_watch.done()
        ):
            self._caption_watch = asyncio.create_task(self._watch_live_transcripts())
        asyncio.create_task(_prepare_kernel_for_voice(self.bot))

    async def _watch_live_transcripts(self) -> None:
        # Discord stops transmitting at a pause. Provider text can still arrive
        # while endpointing owns a capture, without another PCM callback.
        while not self._lifecycle.is_shutting_down:
            if not self.bot_speaking and not self.turn_lock.locked():
                for user_id, user in tuple(self.sink.users.items()):
                    if user_id in self.sink.buffers and speaker_allowed(self.bot, self.voice, user):
                        self._live_transcripts.update(
                            user, input_partial(self.bot.realtime_session, user_id),
                        )
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        """Idempotent teardown: the first caller runs the sequence, later
        callers (simultaneous disconnect + timeout + /leave) are no-ops.

        Reconnect = fresh floor: invalidate and cancel any in-flight response
        (generation bump + speak-task cancel + agent abort), flush pending
        capture state (utterance buffers, fragments, preroll, barge listens),
        and cancel a pending resume offer. Nothing from the dead session may
        produce audio or agent output after this returns."""
        if not self._lifecycle.begin_shutdown():
            return
        captions = getattr(self, "_live_transcripts", None)
        if captions is not None:
            captions.close()
        feed = getattr(self, "_reply_feed", None)
        if feed is not None:
            feed.close()
        watch = getattr(self, "_caption_watch", None)
        if watch is not None:
            watch.cancel()
        self._unsubscribe_hybrid_events()
        unsubscribe_jobs, self._job_unsubscribe = getattr(self, "_job_unsubscribe", None), None
        if unsubscribe_jobs is not None:
            unsubscribe_jobs()
        self._turn_generation += 1  # stale-mark any response still in flight
        self._cancel_pending_utterances()
        self._pending_fragments.clear()
        self._barge_listen.clear()
        self._preroll.clear()
        self._in_flight_prompts.clear()
        getattr(self, "_speech_latency_traces", {}).clear()
        getattr(self, "_pending_endpoint_timings", {}).clear()
        getattr(self, "_task_endpoint_timings", {}).clear()
        getattr(self, "_grace_continuation_started_at", {}).clear()
        self._interrupted_context = None
        self.sink.drop_all()
        watch = getattr(self, "_listen_watch", None)
        if watch is not None:
            watch.cancel()
        if self.voice.is_listening():
            self.voice.stop_listening()
        asyncio.create_task(PiDiscordLiveSession._shutdown_async(self))

    async def _shutdown_async(self) -> None:
        """Async half of stop(): cancel the route turn and close capture."""
        captions = getattr(self, "_live_transcripts", None)
        if captions is not None:
            await captions.wait_closed()
        feed = getattr(self, "_reply_feed", None)
        if feed is not None:
            await feed.wait_closed()
        watch = getattr(self, "_caption_watch", None)
        if watch is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        post_task = getattr(self, "_hybrid_post_task", None)
        if post_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await post_task
        with contextlib.suppress(COMMAND_ERRORS):
            await _cancel_session_turn(self.bot, owner=self)
        await self.bot.realtime_session.reset_capture()
        route_output = getattr(self, "route_output", None)
        if route_output is not None:
            await route_output.close()

    def _cancel_pending_utterances(self, *, preserve_current: bool = False) -> None:
        current = asyncio.current_task() if preserve_current else None
        survivors = []
        for task in self._speak_tasks:
            if task is current:
                survivors.append(task)
            else:
                task.cancel()
        self._speak_tasks = survivors

    def ingest_room_echo(
        self, author, content: str, *, event_id: str | None = None
    ) -> None:
        if access_for(self.bot).required:
            return
        echo = parse_voice_echo(content)
        if echo is None:
            return
        if event_id:
            seen = getattr(self, "_echo_event_ids", None)
            if seen is None:
                seen = deque(maxlen=64)
                self._echo_event_ids = seen
            if event_id in seen:
                return
            seen.append(event_id)
        room = _session_room(self)
        room.ingest_echo(
            Speaker(SpeakerId(f"echo-human-{echo.human_name}"), echo.human_name, ParticipantKind.HUMAN),
            echo.human_text,
        )
        room.ingest_echo(
            Speaker(SpeakerId(str(author.id)), echo.bot_name, ParticipantKind.BOT),
            echo.bot_text,
        )

    async def handle_utterance(self, user: discord.User, pcm: bytes) -> None:
        if not speaker_allowed(self.bot, self.voice, user):
            return
        pending_timings = getattr(self, "_pending_endpoint_timings", {})
        pending = pending_timings.get(user.id)
        endpoint_fields = pending.popleft() if pending else {}
        if not pending:
            pending_timings.pop(user.id, None)
        if self._lifecycle.is_shutting_down:
            return
        task = asyncio.current_task()
        task_timings = getattr(self, "_task_endpoint_timings", {})
        if task is not None:
            task_timings[task] = endpoint_fields
        if task is not None and task not in self._speak_tasks:
            self._speak_tasks.append(task)
        captions = getattr(self, "_live_transcripts", None)
        caption = captions.seal(user) if captions is not None else None
        try:
            if caption is None:
                await self._handle_utterance(user, pcm)
            else:
                await self._handle_utterance(user, pcm, caption=caption)
        finally:
            if caption is not None and caption.text and not caption.final:
                captions.finish(caption, "(no final transcript)")
            task_timings.pop(task, None)
            if task in self._speak_tasks:
                self._speak_tasks.remove(task)

    async def _handle_utterance(self, user: discord.User, pcm: bytes, *, caption=None) -> None:
        if not speaker_allowed(self.bot, self.voice, user):
            return
        preset: tuple[str, str] | None = None
        self._barge_listen.discard(user.id)
        if getattr(self, "_hybrid_route", False):
            await self._handle_hybrid_utterance(user, pcm)
            return
        grace_started_at = getattr(
            self, "_grace_continuation_started_at", {}
        ).pop(user.id, None)
        if self.turn_lock.locked():
            # A turn is in flight. Transcribe instead of dropping: a status
            # question gets answered from live turn state WITHOUT aborting
            # the agent; anything else during a soft barge-in is a real
            # interruption — tear down now (abort was deferred at onset)
            # and reprocess as a fresh turn.
            fragment = self._pending_fragments.get(user.id)
            if fragment is not None:
                # This utterance is the continuation of a held post-barge-in
                # fragment — hand it to the waiting turn, don't drop it.
                continuation = await self._transcribe_rest(pcm)
                if caption is not None and continuation:
                    self._live_transcripts.finish(caption, continuation)
                if continuation:
                    fragment["continuation"] = continuation
                fragment["event"].set()
                return
            self._soft_barge_settling = True
            try:
                transcript = await self._transcribe_for_status(pcm)
            finally:
                self._soft_barge_settling = False
            if caption is not None and transcript:
                self._live_transcripts.finish(caption, transcript)
            if transcript and _is_status_query(transcript):
                if self._soft_barge_pending:
                    self._soft_barge_pending = False
                    await self.turns.resume_after_soft_barge("status-query")
                await self._answer_status_query()
                return
            in_flight_prompt = self._in_flight_prompts.get(user.id)
            committed_at = self._last_utterance_committed_at.get(user.id)
            grace_ms = self.bot.policy.resume_grace_ms
            grace_reference = grace_started_at or time.monotonic()
            gap_ms = (
                (grace_reference - committed_at) * 1000
                if committed_at is not None
                else float("inf")
            )
            live = self._live_turn
            answer_playing = live is not None and live["answer_queued"].is_set()
            if (
                in_flight_prompt
                and transcript
                and grace_ms > 0
                and gap_ms <= grace_ms
                and (not answer_playing or grace_started_at is not None)
            ):
                # Grace-period abort: the endpoint fired mid-thought and the
                # in-flight turn is answering a fragment. Cancel it and
                # reprocess the merged transcript as one turn. A continuation
                # that began before answer audio was queued remains eligible
                # even if it takes longer than the grace window to flush.
                was_soft_barge = self._soft_barge_pending
                self._soft_barge_pending = False
                if was_soft_barge:
                    self.turns.commit_soft_barge("grace-continuation")
                await _cancel_session_turn(self.bot, owner=self)
                preset = (f"{in_flight_prompt} {transcript}", "grace_continuation")
            elif self._soft_barge_pending:
                self._soft_barge_pending = False
                if not transcript or _BACKCHANNEL_RE.match(transcript.strip()):
                    await self.turns.resume_after_soft_barge(
                        "empty" if not transcript else "backchannel"
                    )
                    return
                self.turns.commit_soft_barge("transcript")
                self._remember_interrupted_context(user.id)
                await _cancel_session_turn(self.bot, owner=self)
                preset = (transcript, "rest_soft_barge")
            else:
                return
        trace = self._new_trace(user, pcm)
        # Cut-off diagnostic: utterance landing within ~750ms of the previous
        # one committing means the last endpoint probably fired mid-thought.
        now_mono = time.monotonic()
        prev_committed_at = self._last_utterance_committed_at.get(user.id)
        self._last_utterance_committed_at[user.id] = now_mono
        if prev_committed_at is not None:
            gap_ms = round((now_mono - prev_committed_at) * 1000)
            if gap_ms < 750:
                trace.mark("endpoint_resume_suspect", gapMs=gap_ms)
        instant_acked = False
        ack_state: dict[str, bool] = {"stt_done": False, "played": False}
        async with self.turn_lock:
            # A new user turn commits: bump the generation so any response
            # still arriving for the previous turn is recognized as stale.
            self._turn_generation += 1
            turn_id = self._turn_generation
            trace.mark("turn_committed", turnId=turn_id)
            try:
                stt_stats: dict = {}
                if preset is not None:
                    transcript, stt_source = preset
                else:
                    result = await self.bot.realtime_session.transcribe(
                        _stereo_to_mono_s16(pcm), participant_id=user.id
                    )
                    transcript, stt_source = result.text, result.source
                    # The session normalizes each provider's stats into a dict.
                    # Re-reading the provider's own object crashed on Moonshine
                    # (local listening), whose stats are a dict, on every turn.
                    stt_stats = result.statistics or {}
                trace.mark(
                    "stt_completed",
                    transcriptChars=len(transcript),
                    sttSource=stt_source,
                    streamingFinalizeWaitMs=stt_stats.get("finalize_wait_ms"),
                    streamingFinalizeExit=stt_stats.get("finalize_exit"),
                    streamingSentAudioMs=stt_stats.get("sent_audio_ms"),
                    streamingTranscriptAudioMs=stt_stats.get("transcript_audio_ms"),
                )
                if transcript:
                    # Capture-hole guard: a decrypt-failure burst inside this
                    # utterance's window (join-window DAVE key establishment
                    # destroys ~1s of speech) means the transcript is missing
                    # audio. The frames are unrecoverable — no key, no
                    # plaintext — so tell the agent to ask for a repeat
                    # instead of answering a headless/garbled transcript.
                    window_s = pcm_duration_ms(pcm) / 1000 + 1.5
                    sink = getattr(self, "sink", None)
                    burst = (
                        sink.decrypt_failures_in_window(user.id, window_s)
                        if sink is not None
                        else 0
                    )
                    if burst >= 3:
                        trace.mark("capture_decrypt_burst", failures=burst)
                        transcript = (
                            "[system note — not a literal user utterance] Part of this "
                            "message was lost to a voice-encryption handshake hiccup on "
                            "our side (the user's mic is fine). If the transcript below "
                            "starts mid-thought or seems to be missing words, briefly ask "
                            "the user to repeat the missing part instead of guessing; "
                            "otherwise answer normally.\n"
                            + transcript
                        )
                if not transcript:
                    trace.finish("empty_transcript")
                    return
                transcript = await self._maybe_hold_fragment(
                    user, transcript, trace, ack_state
                )
                if caption is not None:
                    self._live_transcripts.finish(caption, transcript)
                elif self.bot.settings.discord_transcript_debug and self.text_channel:
                    await self.text_channel.send(f"heard {user.display_name}: {transcript}")
                if self._pending_cron_offer:
                    # The canned join offer was just played; this utterance is
                    # its answer, whatever else it might look like.
                    self._pending_cron_offer = False
                    stripped_offer = transcript.strip()
                    if _RESUME_YES_RE.match(stripped_offer):
                        trace.mark("cron_offer_accepted")
                        transcript = (
                            "[system note — not a literal user utterance] The user just "
                            "joined voice and said yes to hearing the latest from their "
                            "scheduled (cron) jobs. IMPORTANT: cron run outputs do not "
                            "appear in this conversation's history — use your cron/job "
                            "tools to look up the most recent completed run outputs right "
                            "now; never answer from memory. Then read them briefly, spoken "
                            "style, most recent first. If the latest run failed or nothing "
                            "new exists, say so in one line and offer to pull live data."
                        )
                    elif _RESUME_NO_RE.match(stripped_offer):
                        trace.mark("cron_offer_declined")
                        clip = _next_preamble_clip(self.bot, "control")
                        if clip is not None:
                            await _play_clip(self.playback, clip)
                        trace.finish("turn_complete")
                        return
                    # anything else: fall through as a normal turn
                if _CONTROL_WORD_RE.match(transcript.strip()):
                    # Bare control utterance ("stop", "never mind"): abort any
                    # in-flight work, acknowledge briefly, never reach the agent.
                    trace.mark("control_word_intercepted", transcriptChars=len(transcript))
                    self._interrupted_context = None
                    await _cancel_session_turn(self.bot, owner=self)
                    clip = _next_preamble_clip(self.bot, "control")
                    if clip is not None:
                        trace.mark("preamble_played", pool="control")
                        await _play_clip(self.playback, clip)
                    log_chat_turn(
                        surface=self.bot.surface,
                        transport="discord-native",
                        persona=self.bot.persona,
                        channel_id=getattr(self.text_channel, "id", 0) or 0,
                        trace_id=trace.trace_id,
                        user_text=transcript,
                        assistant_text="Stopped.",
                        session_id=f"{self.bot.profile.slug}-discord-{user.id}",
                        user_id=user.id,
                        actor_id=discord_actor_id(user.id),
                        enabled=self.bot.settings.voice_chat_log_enabled,
                        log_path=self.bot.settings.voice_chat_log_path,
                    )
                    trace.finish("control_word")
                    return
                if self._interrupted_context and _CONTINUE_RE.match(transcript.strip()):
                    # An earlier answer was interrupted and the user is
                    # explicitly asking for the rest. No proactive offer —
                    # the user drives the resume, the agent gets the
                    # interrupted context plus the last spoken sentence so
                    # it can pick up exactly where the audio stopped.
                    trace.mark("resume_requested")
                    ctx = self._interrupted_context
                    self._interrupted_context = None
                    trace.mark("agent_started", turnId=turn_id, kind="resume")
                    if ctx.get("phase") == "working":
                        # Interrupted before any answer: re-run the
                        # original request — there is no partial answer
                        # to pick up from.
                        prompt = ctx["transcript"]
                    else:
                        spoken_tail = (ctx.get("spoken") or [""])[-1].strip()
                        if spoken_tail:
                            prompt = (
                                "You were interrupted mid-answer and the listener wants the "
                                f"rest. You had already said, up to: \"{spoken_tail}\" "
                                "Pick up right after that, keep it brief, and don't repeat "
                                "what you already said."
                            )
                        else:
                            prompt = (
                                "You were interrupted mid-answer and the listener wants the "
                                "rest. Pick up where you left off, keep it brief, and don't "
                                "repeat what you already said."
                            )
                    _attribute_written_turn(self, user, transcript)
                    spoken = await self._stream_and_speak(prompt, trace)
                    if spoken.strip():
                        _session_room(self).record_reply(spoken)
                    trace.finish("turn_complete")
                    return
                utterance = _utterance_for(user, transcript)
                _notice_voice_members(self)
                decision = _session_room(self).route(utterance)
                if decision.action is RouteAction.IGNORE:
                    trace.finish("room_ignore")
                    return
                prompt = _session_room(self).prompt_for(utterance, decision)
                # A substantive new prompt means the user has moved on — the
                # interrupted answer is stale, so a later "continue" should
                # not resurrect it. Bare acks/backchannels ("yeah", "ok")
                # keep the context: the user didn't mean to redirect.
                is_short_confirmation = bool(
                    _SHORT_CONFIRMATION_RE.match(transcript.strip())
                )
                if not is_short_confirmation:
                    self._interrupted_context = None
                trace.mark("agent_started", turnId=turn_id)
                suppress_preamble = is_short_confirmation
                if suppress_preamble:
                    trace.mark("preamble_suppressed", reason="short_confirmation")
                if _user_speaking(self, user.id) and not ack_state["played"]:
                    trace.mark("ack_skipped", reason="user_speaking")
                if ack_state["played"] and not instant_acked:
                    # The blind fallback fired while we were waiting on STT.
                    instant_acked = True
                self._in_flight_prompts[user.id] = transcript
                _attribute_written_turn(self, user, transcript)
                try:
                    spoken = await self._stream_and_speak(
                        prompt,
                        trace,
                        suppress_preamble=suppress_preamble,
                        ack_already_played=instant_acked,
                    )
                finally:
                    self._in_flight_prompts.pop(user.id, None)
                if spoken.strip():
                    _session_room(self).record_reply(spoken)
                log_chat_turn(
                    surface=self.bot.surface,
                    transport="discord-native",
                    persona=self.bot.persona,
                    channel_id=getattr(self.text_channel, "id", 0) or 0,
                    trace_id=trace.trace_id,
                    user_text=transcript,
                    assistant_text=spoken,
                    session_id=f"{self.bot.profile.slug}-discord-{user.id}",
                    user_id=user.id,
                    actor_id=discord_actor_id(user.id),
                    enabled=self.bot.settings.voice_chat_log_enabled,
                    log_path=self.bot.settings.voice_chat_log_path,
                )
            except COMMAND_ERRORS as exc:
                trace.mark("turn_error", error=_format_error(exc))
                if self.text_channel:
                    await self.text_channel.send(
                        f"Voice loop failed: {_format_error(exc)}"
                    )
                # Never fail silently on voice — say it. Capped: an apology
                # that hangs is worse than none. The route may already have.
                clip = None
                if _apology_owed(exc):
                    try:
                        clip = await asyncio.wait_for(
                            _dynamic_clip(
                                self.bot, "That one died on me mid-work. Want me to try it again?"
                            ),
                            timeout=6.0,
                        )
                    except (TimeoutError, COMMAND_ERRORS):
                        clip = None
                if clip is not None:
                    with contextlib.suppress(COMMAND_ERRORS, TimeoutError):
                        await asyncio.wait_for(_play_clip(self.playback, clip), timeout=10.0)
            finally:
                self.bot_speaking = False
                trace.finish("turn_complete")

    def _subscribe_hybrid_events(self) -> None:
        if (
            not self._hybrid_route
            or self._hybrid_unsubscribe is not None
            or self._lifecycle.is_shutting_down
        ):
            return
        self._hybrid_unsubscribe = self.bot.realtime_session.subscribe(
            self._on_hybrid_event
        )

    def _subscribe_job_events(self) -> None:
        """Post a background job's start and result in the text channel, where
        its links can be clicked (sleeptalk/jobs.py), and the written call."""
        session = getattr(self.bot, "realtime_session", None)
        if (getattr(self, "_job_unsubscribe", None) is not None or self._hybrid_route
                or not callable(getattr(session, "subscribe", None))):
            return
        self._job_unsubscribe = session.subscribe(self._on_channel_event)

    def _on_channel_event(self, event: SessionEvent) -> None:
        """One subscriber for everything the text channel shows. Both halves
        only queue work, so neither can hold up the event dispatch."""
        self._on_job_event(event)
        feed = getattr(self, "_reply_feed", None)
        if feed is not None:
            feed.handle(event)

    def _on_job_event(self, event: SessionEvent) -> None:
        if event.type is not SessionEventType.AGENT_EVENT or self.text_channel is None:
            return
        payload = event.payload
        name = str(payload.get("name") or "")
        title = str(payload.get("title") or "background job")
        if name == "job.started":
            content = f"Working in the background: **{title}**"
        elif name == "job.completed":
            content = f"**{title}** is done.\n{payload.get('result') or payload.get('summary') or ''}".strip()
        elif name == "job.failed":
            content = f"**{title}** stopped: {payload.get('error') or 'it failed'}"
        else:
            return
        if len(content) > 1900:
            content = content[:1899].rstrip() + "…"
        self._hybrid_posts.append(content)
        if self._hybrid_post_task is None or self._hybrid_post_task.done():
            self._hybrid_post_task = asyncio.create_task(self._drain_hybrid_posts())

    def _unsubscribe_hybrid_events(self) -> None:
        unsubscribe = getattr(self, "_hybrid_unsubscribe", None)
        if unsubscribe is None:
            return
        unsubscribe()
        self._hybrid_unsubscribe = None
        self._hybrid_posts.clear()
        if self._hybrid_post_task is not None:
            self._hybrid_post_task.cancel()
        self._hybrid_authors.clear()
        self._hybrid_traces.clear()
        self._hybrid_audio_turns.clear()

    def _on_hybrid_event(self, event: SessionEvent) -> None:
        payload = event.payload
        source_turn = payload.get("sourceTurnId", event.turn_id)
        trace = self._hybrid_traces.get(source_turn)
        if event.type is SessionEventType.AGENT_EVENT:
            name = payload.get("name")
            if trace is not None and name in {
                "hybrid.intent.committed", "hybrid.task.started", "hybrid.task.completed",
                "hybrid.task.failed", "hybrid.delivery",
            }:
                fields = {key: value for key, value in payload.items() if key != "name"}
                fields.setdefault("sourceTurnId", source_turn)
                if name == "hybrid.delivery":
                    fields.setdefault("deliveryTurnId", event.turn_id)
                if "elapsedMs" in fields:
                    fields["routeElapsedMs"] = fields.pop("elapsedMs")
                trace.mark(name, **fields)
            return
        if event.type is not SessionEventType.TRANSCRIPT_FINAL:
            return
        text = " ".join(str(payload.get("text", "")).split())
        if not text or self.text_channel is None:
            return
        speaker = payload.get("speaker")
        author = self._hybrid_authors.get(source_turn, "You")
        if speaker == "user":
            content = f"**{author}:** {text}"
            if len(content) > 1900:
                content = content[:1899].rstrip() + "…"
        elif speaker == "assistant" and payload.get("delivered") is True:
            spoken = text
            if payload.get("interrupted"):
                spoken = f"[Interrupted; partially played] {spoken}"
            content = _format_voice_turn(
                self.bot.profile.name, "", spoken, speaker=author
            )
        else:
            return
        self._hybrid_posts.append(content)
        if self._hybrid_post_task is None or self._hybrid_post_task.done():
            self._hybrid_post_task = asyncio.create_task(self._drain_hybrid_posts())

    async def _drain_hybrid_posts(self) -> None:
        while self._hybrid_posts:
            content = self._hybrid_posts.popleft()
            with contextlib.suppress(COMMAND_ERRORS):
                await self.text_channel.send(content)

    async def _handle_hybrid_utterance(
        self, user: discord.User, pcm: bytes
    ) -> None:
        trace = self._new_trace(user, pcm)
        self._subscribe_hybrid_events()
        async with self.turn_lock:
            self._turn_generation += 1
            turn_id = self._turn_generation
            trace.mark("turn_committed", transportGeneration=self._turn_generation)

            def turn_started(semantic_id: int) -> None:
                nonlocal turn_id
                turn_id = semantic_id
                self._hybrid_authors[semantic_id] = getattr(user, "display_name", None) or "You"
                self._hybrid_traces[semantic_id] = trace
                trace.mark("route_started", turnId=semantic_id)
            self._live_turn = {
                "started": time.perf_counter(),
                "detail": [],
                "tool": [],
                "answer_queued": asyncio.Event(),
            }
            def route_audio_ready(written_turn_id: int) -> None:
                delivery_trace = self._hybrid_traces.get(written_turn_id)
                if delivery_trace is not None and written_turn_id not in self._hybrid_audio_turns:
                    self._hybrid_audio_turns.add(written_turn_id)
                    delivery_trace.mark("route_first_audio_write", turnId=written_turn_id)
                self._route_audio_started(written_turn_id)

            self.route_output.on_write = route_audio_ready
            try:
                outcome = await self.bot.kernel.run_turn(
                    pcm=_stereo_to_mono_s16(pcm),
                    output=self.route_output,
                    owner=self,
                    extras={
                        "participant_id": user.id,
                        "permission_authorized": access_for(self.bot).operator(user.id),
                    },
                    on_started=turn_started,
                )
                trace.mark(
                    "route_completed",
                    turnId=turn_id,
                    status=outcome.status,
                    # Splits a slow hybrid turn into the transcription wait and
                    # everything after it. They are fixed by different things.
                    heardMs=getattr(outcome, "heard_ms", None),
                )
                trace.finish(
                    "turn_complete",
                    turnId=turn_id,
                    status=outcome.status,
                )
            except COMMAND_ERRORS as exc:
                trace.finish("turn_error", error=_format_error(exc))
                if self.text_channel:
                    await self.text_channel.send(
                        f"Voice loop failed: {_format_error(exc)}"
                    )
            finally:
                self._live_turn = None

    async def _stream_and_speak(
        self,
        transcript: str,
        trace: VoiceLatencyTrace,
        *,
        suppress_preamble: bool = False,
        ack_already_played: bool = False,
    ) -> str:
        """Commit one Discord transcript through the configured route.

        Capture/endpointing and playback remain Discord-owned. STT/agent/TTS
        assembly and cancellation belong to the shared session.
        """
        answer_queued = asyncio.Event()
        self._live_turn = {
            "started": time.perf_counter(),
            "detail": [],
            "tool": [],
            "answer_queued": answer_queued,
        }
        def route_audio_ready(turn_id: int) -> None:
            if not answer_queued.is_set():
                trace.mark("route_first_audio_write", turnId=turn_id)
            answer_queued.set()

        self.route_output.on_write = route_audio_ready
        try:
            outcome = await self.bot.kernel.run_turn(
                text=transcript,
                output=self.route_output,
                owner=self,
                extras={"suppress_preamble": suppress_preamble, "latency_trace": trace},
                on_started=lambda turn_id: trace.mark("route_started", turnId=turn_id),
            )
            spoken = _outcome_text(outcome)
            trace.mark(
                "route_completed",
                turnId=outcome.turn_id,
                status=outcome.status,
                outputChars=len(spoken),
            )
            return spoken
        finally:
            answer_queued.set()
            self._live_turn = None


    async def _transcribe_rest(self, pcm: bytes) -> str:
        """REST STT for out-of-turn audio (status queries, fragment joins)."""
        try:
            text = await self.bot.stt.transcribe_pcm(_stereo_to_mono_s16(pcm))
        except COMMAND_ERRORS:
            return ""
        return (text or "").strip()

    async def _maybe_hold_fragment(
        self,
        user: discord.User,
        transcript: str,
        trace: VoiceLatencyTrace,
        ack_state: dict[str, bool] | None = None,
    ) -> str:
        """Hold an incomplete utterance for its continuation.

        Fragments ("can you tell me about the...", "And, also,") are not
        turns — the speaker paused mid-thought and the endpointing fired
        early. Post-barge-in this is obvious (the bot was still talking),
        but hesitant mid-monologue pauses fragment the same way, so the
        hold applies to any incomplete transcript, not just recent
        barge-ins. If a continuation arrives within policy.fragment_hold_ms,
        merge and answer the whole sentence; otherwise answer the fragment
        as-is.
        """
        policy = self.bot.policy
        if policy.fragment_hold_ms <= 0:
            return transcript
        if estimate_completion_score(transcript) >= 0.55:
            return transcript
        since_barge_ms = (time.monotonic() - self._last_barge_at) * 1000
        post_barge = (
            self._last_barge_at > 0
            and since_barge_ms <= policy.fragment_barge_window_ms
        )
        fragment = {
            "text": transcript,
            "event": asyncio.Event(),
            "continuation": None,
        }
        self._pending_fragments[user.id] = fragment
        trace.mark(
            "fragment_held",
            fragmentChars=len(transcript),
            postBarge=post_barge,
        )
        # A held fragment is never a fast turn: claim the floor NOW so the
        # hold isn't dead air (2026-08-14 regression: 4s silent holds,
        # median ttfa 403ms -> 2762ms). The smart-ack block in the turn
        # path skips because ack_state["played"] is set.
        if (
            ack_state is not None
            and not ack_state["played"]
            and policy.instant_ack
            and getattr(policy, "instant_ack_mode", "smart") != "blind"
            and not self.playback.active
            and not _user_speaking(self, user.id)
        ):
            pool = select_ack_pool(transcript)
            if pool == "none":
                pool = "ack"
            clip = _pick_ack_clip(self.bot, pool)
            if clip is not None:
                ack_state["played"] = True
                trace.mark("ack_selected", pool=pool, during="fragment_hold")
                asyncio.create_task(_play_clip(self.playback, clip))
        released_early = False
        try:
            await asyncio.wait_for(
                fragment["event"].wait(), timeout=policy.fragment_hold_ms / 1000
            )
            released_early = True
        except asyncio.TimeoutError:
            pass
        finally:
            self._pending_fragments.pop(user.id, None)
        continuation = fragment.get("continuation")
        if continuation:
            merged = f"{transcript.rstrip(' .,!?')} {continuation}"
            trace.mark(
                "fragment_merged",
                fragmentChars=len(transcript),
                continuationChars=len(continuation),
            )
            return merged
        trace.mark(
            "fragment_released",
            reason="continuation_empty" if released_early else "timeout",
        )
        return transcript

    async def _transcribe_for_status(self, pcm: bytes) -> str:
        if self._live_turn is None:
            return ""
        try:
            text = await self.bot.stt.transcribe_pcm(_stereo_to_mono_s16(pcm))
        except COMMAND_ERRORS:
            return ""
        return (text or "").strip()

    async def _answer_status_query(self) -> None:
        live = self._live_turn
        if live is None:
            return
        line = self._status_line(live)
        if not is_hearable(line):
            line = "Still working on it. I'll speak the answer as soon as it's ready."
        clip = await _dynamic_clip(self.bot, line)
        if (
            clip is None
            or self._live_turn is not live
            or live["answer_queued"].is_set()
            or self._lifecycle.is_shutting_down
        ):
            return
        await _play_clip(self.playback, clip)

    def _status_line(self, live: dict) -> str:
        elapsed = time.perf_counter() - live["started"]
        detail = live["detail"][0] if live["detail"] else ""
        if detail:
            core = f"I'm still {detail}"
        elif live["tool"]:
            phrase = self.bot.policy.narration_phrases.get(live["tool"][0], "Working on it.").rstrip(".")
            core = f"I'm still {phrase[0].lower()}{phrase[1:]}"
        else:
            core = "I'm still thinking it over"
        return f"{core}{_elapsed_suffix(elapsed)}. I'll speak the answer as soon as it's ready."

    async def _handle_barge_in(self, user: discord.User) -> None:
        """User interrupted bot playback. Feed the moment-of-interruption
        speech (captured in the pre-roll while the bot was still playing)
        into both transcript paths, and keep collecting this user's audio
        through the old turn's ignore gate — the interruption sentence must
        be heard in full, from its first syllable. During the working phase
        (preambles/knock, no answer audio yet) defer the abort: pause the
        knock and let the transcript decide — status check or real
        interruption."""
        preroll = b"".join(self._preroll.pop(user.id, ()))
        self._barge_listen.add(user.id)
        self._last_barge_at = time.monotonic()
        if preroll:
            self.sink.inject_preroll(user, preroll)
            with contextlib.suppress(COMMAND_ERRORS):
                await PiDiscordLiveSession._append_capture_pcm(
                    self, user.id, preroll
                )
        if self.bot.policy.barge_in == "soft" and not getattr(
            self, "_hybrid_route", False
        ):
            self._soft_barge_pending = True
            PiDiscordLiveSession._arm_soft_barge_watchdog(self, user.id)
            return
        self._remember_interrupted_context(user.id)
        await _cancel_session_turn(self.bot, owner=self)

    SOFT_BARGE_TIMEOUT_SECONDS = 4.0

    def _arm_soft_barge_watchdog(self, user_id: int) -> None:
        watchdog = getattr(self, "_soft_barge_watchdog", None)
        if watchdog is not None:
            watchdog.cancel()
        self._soft_barge_watchdog = asyncio.get_running_loop().call_later(
            PiDiscordLiveSession.SOFT_BARGE_TIMEOUT_SECONDS,
            PiDiscordLiveSession._soft_barge_timed_out, self, user_id,
        )

    def _soft_barge_timed_out(self, user_id: int) -> None:
        """No words settled the barge: put the answer back on, unless the
        caller is still talking or their words are being heard."""
        self._soft_barge_watchdog = None
        lifecycle = getattr(self, "_lifecycle", None)
        if not self._soft_barge_pending or (lifecycle is not None and lifecycle.is_shutting_down):
            return
        sink = getattr(self, "sink", None)
        if getattr(self, "_soft_barge_settling", False) or (sink is not None and user_id in sink.buffers):
            PiDiscordLiveSession._arm_soft_barge_watchdog(self, user_id)
            return
        self._soft_barge_pending = False
        self._barge_listen.discard(user_id)
        asyncio.ensure_future(self.turns.resume_after_soft_barge("no words"))

    def _remember_interrupted_context(self, user_id: int) -> None:
        prompt = self._in_flight_prompts.get(user_id)
        if not prompt:
            return
        live = self._live_turn
        answering = bool(live is not None and live["answer_queued"].is_set())
        self._interrupted_context = {
            "transcript": prompt,
            "spoken": [],
            "phase": "answer" if answering else "working",
        }

    def _route_audio_started(self, _turn_id: int) -> None:
        live = self._live_turn
        if live is not None:
            live["answer_queued"].set()

    async def _handle_pcm_frame(self, user: discord.User, pcm: bytes) -> None:
        if not speaker_allowed(self.bot, self.voice, user):
            return
        preroll = self._preroll.setdefault(user.id, deque(maxlen=75))
        # Discord sends frames only while someone transmits: a gap means
        # they stopped. Frames from before it are the last question's tail,
        # not the barge ("Just explain it. Mm-hmm." became a real turn).
        now = time.monotonic()
        heard_at = getattr(self, "_preroll_heard_at", None)
        if heard_at is None:
            heard_at = self._preroll_heard_at = {}
        if now - heard_at.get(user.id, now) > PREROLL_GAP_SECONDS:
            preroll.clear()
        heard_at[user.id] = now
        preroll.append(pcm)
        if pcm_rms(pcm) >= self.bot.policy.turns.vad_threshold:
            self._last_user_voice_at = time.monotonic()
        await self.turns.observe_pcm(user, pcm)
        if self.bot_speaking:
            # Bot audio (ack mp3, knock loop, answers) can echo from the
            # user's speakers into the mic; keep it out of the STT stream.
            # Barge-in detection above still sees every frame.
            return
        try:
            await PiDiscordLiveSession._append_capture_pcm(self, user.id, pcm)
            lock = getattr(self, "turn_lock", None)
            if lock is not None and not lock.locked() and user.id in self.sink.buffers:
                partial = input_partial(self.bot.realtime_session, user.id)
                captions = getattr(self, "_live_transcripts", None)
                if captions is not None:
                    captions.update(user, partial)
                self._note_input_partial(user.id, partial)
        except COMMAND_ERRORS as exc:
            if self.text_channel:
                await self.text_channel.send(f"Streaming STT failed: {_format_error(exc)}")

    def _note_input_partial(self, user_id: int, text: str) -> None:
        """Log the live transcript endpointing is judging, and re-check on it.

        Discord logged no `input_partial` at all, so a turn that sat in the
        1.8 s "uncertain" bucket was indistinguishable in the latency log
        from one whose listener simply never answered. Web and terminal both
        emit this; without it the Discord endpointing path is unreadable.
        """
        seen = getattr(self, "_input_partials", None)
        if seen is None:
            seen = self._input_partials = {}
        if seen.get(user_id) == text:
            return
        seen[user_id] = text
        log_latency_event(
            surface=self.bot.surface,
            transport="discord-native",
            persona=self.bot.persona,
            stage="input_partial",
            userId=user_id,
            textChars=len(text),
            source=input_source(self.bot.realtime_session),
        )
        # Fresh streaming text can settle a pause before the next poll.
        self.sink.recheck_streaming_endpoint(user_id)

    async def _append_capture_pcm(self, user_id: int, pcm: bytes) -> None:
        await self.bot.realtime_session.append_audio(
            _stereo_to_mono_s16(pcm), participant_id=user_id
        )

    def _trace_metadata(self, user: discord.User) -> dict:
        """Fields stamped on every stage of a turn.

        `agent` names the brain behind the same bot: one persona on one
        surface can be driven by pi, claude_code, codex, cursor or omp, and
        without it those runs are indistinguishable in the latency log.
        """
        return {
            **cascade_turn_identity(
                getattr(self.bot, "config", None),
                session_id=_trace_session_id(self),
            ),
            "guildId": getattr(getattr(self.voice, "guild", None), "id", ""),
            "userId": getattr(user, "id", ""),
        }

    def _on_capture_event(self, user_id: int, event: dict) -> None:
        stage = "capture_vad_dropout"
        if event.get("kind") == "utterance_committed":
            # The next turn starts with no partial, so an identical phrase
            # twice running still logs twice.
            getattr(self, "_input_partials", {}).pop(user_id, None)
            if self._lifecycle.is_shutting_down:
                return
            fields = {
                "endpointSilenceMs": event["endpointSilenceMs"]
            } if "endpointSilenceMs" in event else {}
            self._pending_endpoint_timings.setdefault(user_id, deque()).append(fields)
            stage = "utterance_committed"
        log_latency_event(
            surface=self.bot.surface,
            transport="discord-native",
            persona=self.bot.persona,
            stage=stage,
            userId=user_id,
            **event,
        )

    def _new_trace(self, user: discord.User, pcm: bytes) -> VoiceLatencyTrace:
        trace = self._speech_latency_traces.pop(user.id, None)
        if trace is None:
            trace = VoiceLatencyTrace(
                surface=self.bot.surface,
                transport="discord-native",
                persona=self.bot.persona,
                metadata=self._trace_metadata(user),
            )
            trace.mark("speech_trace_started_late", reason="speech_callback_missing")
        task_timings = getattr(self, "_task_endpoint_timings", {})
        endpoint_fields = task_timings.get(asyncio.current_task(), {}) if task_timings else {}
        trace.mark(
            "utterance_ready",
            audioMs=pcm_duration_ms(pcm),
            audioBytes=len(pcm),
            configuredSilenceMs=self.bot.policy.capture.silence_ms,
            **endpoint_fields,
        )
        return trace

    def _speech_started(self, user: discord.User) -> None:
        heard = getattr(getattr(self.bot, "realtime_session", None), "note_caller_speech", None)
        if callable(heard):
            heard()  # a background job's announcement waits for the caller
        # The bot sits in its channel for days, so its speech sockets idle
        # shut between turns. Reopen them while the caller is still talking,
        # as web and terminal do: a listener that reconnects *after* the
        # pause hands endpointing no transcript, which costs the turn the
        # 650 ms "complete" bucket and drops it into the 1.8 s one.
        warm_on_speech(getattr(self.bot, "realtime_session", None))
        if not self.turn_lock.locked():
            trace = VoiceLatencyTrace(
                surface=self.bot.surface,
                transport="discord-native",
                persona=self.bot.persona,
                metadata=self._trace_metadata(user),
            )
            self._speech_latency_traces[user.id] = trace
            trace.mark(
                "speech_detected",
                measurementPoint="local_vad",
            )
            begin_attempt = getattr(self.streaming_stt, "begin_latency_attempt", None)
            if callable(begin_attempt):
                begin_attempt(
                    user.id,
                    attempt_id=f"{trace.trace_id}:stt:ws:1",
                    on_event=lambda stage, fields: trace.mark(stage, **fields),
                )
            return
        committed_at = self._last_utterance_committed_at.get(user.id)
        grace_ms = self.bot.policy.resume_grace_ms
        if committed_at is None or grace_ms <= 0:
            return
        now = time.monotonic()
        gap_ms = (now - committed_at) * 1000
        live = self._live_turn
        answer_playing = live is not None and live["answer_queued"].is_set()
        if gap_ms <= grace_ms and not answer_playing:
            self._grace_continuation_started_at[user.id] = now
            log_latency_event(
                surface=self.bot.surface,
                transport="discord-native",
                persona=self.bot.persona,
                stage="endpoint_resume_started",
                userId=user.id,
                gapMs=round(gap_ms),
            )

    def _on_speech_pause(self, user_id: int, pcm: bytes) -> None:
        if self._lifecycle.is_shutting_down:
            return
        # Same conversion and participant as the committed turn, so the early
        # transcript is matched against what endpointing finally sends.
        task = start_from_pause(self.bot.realtime_session, _stereo_to_mono_s16(pcm), user_id)
        if task is not None:
            self.sink.watch_early_transcript(user_id, task)

    def _collect_during_turn(self, user_id: int) -> bool:
        """Keep possible post-endpoint continuations capturable.

        The global turn lock used to discard ordinary speech while the prior
        prompt was in flight, so resume_grace_ms could not observe the speech
        it was meant to rescue. Admit frames during grace; once they open a
        VAD buffer, keep the buffer alive until the continuation flushes.
        """
        if user_id in self._barge_listen or user_id in self._pending_fragments:
            return True
        sink = getattr(self, "sink", None)
        if sink is not None and user_id in sink.buffers:
            return True
        committed_at = self._last_utterance_committed_at.get(user_id)
        grace_ms = self.bot.policy.resume_grace_ms
        if committed_at is None or grace_ms <= 0:
            return False
        live = self._live_turn
        if live is not None and live["answer_queued"].is_set():
            return False
        return (time.monotonic() - committed_at) * 1000 <= grace_ms

    def _playback_started(self) -> None:
        self.bot_speaking = True
        self.turns.bot_playback_started()

    def _playback_stopped(self) -> None:
        self.bot_speaking = False
        self.turns.bot_playback_stopped()

    # The bot can sit in a channel for days. A Discord voice reset can stop
    # the receiver while the bot stays in the channel, so it looks present
    # but hears nothing; the receiver is restarted when it stops, and this
    # often in case nothing said it stopped.
    LISTEN_CHECK_SECONDS = 30.0
    # Failed restarts (about 90 s) before the room is told.
    LISTEN_FAILURES_BEFORE_NOTE = 3

    def _start_listen_watch(self) -> None:
        watch = getattr(self, "_listen_watch", None)
        if watch is None or watch.done():
            self._listen_watch = asyncio.create_task(self._watch_listening(), name="discord-listen-watch")

    async def _watch_listening(self) -> None:
        while not self._lifecycle.is_shutting_down:
            await asyncio.sleep(self.LISTEN_CHECK_SECONDS)
            await self._ensure_listening()

    def _after_listen(self, error: Exception | None = None) -> None:
        """The receiver stopped. Called by voice_recv, possibly from its own thread."""
        if self._lifecycle.is_shutting_down:
            return
        with contextlib.suppress(RuntimeError):  # the loop is closing
            self.loop.call_soon_threadsafe(self._listen_stopped, error)

    def _listen_stopped(self, error: Exception | None) -> None:
        if self._lifecycle.is_shutting_down:
            return
        reason = _format_error(error) if error is not None else "no error given"
        print(f"voice: listening stopped ({reason}); starting it again")
        asyncio.create_task(self._ensure_listening())

    async def _ensure_listening(self) -> bool:
        """Listen again if the bot is in voice but its receiver stopped.

        While discord.py is still reconnecting there is nothing to listen to
        yet; the next check tries again. True when listening.
        """
        if self._lifecycle.is_shutting_down:
            return False
        if self.voice.is_listening():
            self._listen_failures = 0
            return True
        restarted = False
        if self.voice.is_connected():
            try:
                self.voice.listen(self.sink, after=self._after_listen)
                restarted = True
            except (discord.ClientException, OSError) as exc:
                print(f"voice: could not listen again: {_format_error(exc)}")
        if restarted:
            self._listen_failures = 0
            self.listen_restarts = getattr(self, "listen_restarts", 0) + 1
            print("voice: listening again")
            return True
        self._listen_failures = getattr(self, "_listen_failures", 0) + 1
        if self._listen_failures == self.LISTEN_FAILURES_BEFORE_NOTE and self.text_channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.text_channel.send(
                    "I can't hear this voice channel right now. "
                    f"`/{self.bot.profile.slug} join` brings me back."
                )
        return False
