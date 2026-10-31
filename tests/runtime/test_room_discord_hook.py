from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.session import RealtimeSession
from sleeptalk.voice_policy_runtime import TranscriptionResult
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor
from sleeptalk.discord.session_helpers import _format_voice_turn
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace
from sleeptalk.discord_voice import PiDiscordLiveSession
from sleeptalk.room import ParticipationMode, Room, identity_from_agent, SpeakerId
from sleeptalk.discord.access import DiscordAccess

OPEN_ACCESS = DiscordAccess(required=False)


LOUD = b"p" * 32


def _session(*, mode: str = "conversationalist", name: str = "Test", slug: str = "test"):
    spoken: list[str] = []

    async def fake_stream_and_speak(prompt, trace, *, suppress_preamble=False, ack_already_played=False):
        spoken.append(prompt)
        return "answer"

    settings = SimpleNamespace(
        discord_transcript_debug=False,
        discord_voice_text_echo=False,
        voice_chat_log_enabled=False,
        voice_chat_log_path="",
    )
    session = SimpleNamespace(
        _lifecycle=LifecycleSupervisor(),
        _barge_listen=set(),
        _resume_offer_task=None,
        turn_lock=asyncio.Lock(),
        _pending_fragments={},
        _in_flight_prompts={},
        _last_utterance_committed_at={},
        _live_turn=None,
        _soft_barge_pending=False,
        _speak_tasks=[],
        _interrupted_context=None,
        _resume_offered=False,
        _pending_cron_offer=False,
        _turn_generation=0,
        playback=SimpleNamespace(active=False),
        streaming_stt=None,
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            realtime_session=RealtimeSession(SimpleNamespace()),
            stt=None,
            stt_rest_timeout_seconds=8.0,
            settings=settings,
            surface="test",
            persona="test",
            user=SimpleNamespace(id=99),
            profile=SimpleNamespace(
                slug=slug,
                name=name,
                participation_mode=mode,
                addressable_name="",
                proactive_triggers=[],
            ),
            policy=SimpleNamespace(
                resume_grace_ms=750,
                fragment_hold_ms=0,
                instant_ack=False,
                instant_ack_mode="smart",
                capture=SimpleNamespace(silence_ms=450),
            ),
        ),
        text_channel=None,
        voice=None,
        bot_speaking=False,
        _new_trace=lambda u, pcm: VoiceLatencyTrace(
            surface="test", transport="test", persona="test"
        ),
        _maybe_hold_fragment=lambda u, t, tr, ack=None: asyncio.sleep(0, result=t),
        _stream_and_speak=fake_stream_and_speak,
        _maybe_offer_resume=lambda tr: asyncio.sleep(0),
        spoken=spoken,
    )
    session._handle_utterance = (
        lambda user, pcm: PiDiscordLiveSession._handle_utterance(session, user, pcm)
    )
    return session


@pytest.mark.asyncio
async def test_conversationalist_hook_sends_raw_transcript(monkeypatch):
    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0, result=TranscriptionResult("What changed today?", "streaming", {})),
    )
    session = _session()
    user = SimpleNamespace(id=7, display_name="Alice", bot=False)
    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    assert session.spoken == ["What changed today?"]


@pytest.mark.asyncio
async def test_attendee_hook_stays_silent_until_named(monkeypatch):
    texts = iter(["Ship it Friday.", "Works for me.", "Notes, capture that."])
    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0, result=TranscriptionResult(next(texts), "streaming", {})),
    )
    session = _session(mode="attendee", name="Notes", slug="notes")
    alice = SimpleNamespace(id=7, display_name="Alice", bot=False)
    bob = SimpleNamespace(id=8, display_name="Bob", bot=False)
    await PiDiscordLiveSession.handle_utterance(session, alice, LOUD)
    await PiDiscordLiveSession.handle_utterance(session, bob, LOUD)
    assert session.spoken == []
    await PiDiscordLiveSession.handle_utterance(session, alice, LOUD)
    assert len(session.spoken) == 1
    assert "Ship it Friday." in session.spoken[0]


@pytest.mark.asyncio
async def test_conversationalist_hook_stays_silent_when_peer_is_named(monkeypatch):
    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0, result=TranscriptionResult("Notes, capture that", "streaming", {})),
    )
    session = _session()
    notes = SimpleNamespace(id=55, display_name="Notes", name="Notes", bot=True)
    alice = SimpleNamespace(id=7, display_name="Alice", bot=False)
    session.voice = SimpleNamespace(channel=SimpleNamespace(members=[alice, notes]))
    await PiDiscordLiveSession.handle_utterance(session, alice, LOUD)
    assert session.spoken == []


def test_format_voice_turn_names_the_speaker():
    text = _format_voice_turn("Campbell", "hello", "hi", speaker="Alice")
    assert text == "sleeptalk-voice/1\n**Alice:** hello\n**Campbell:** hi"


def test_ingest_room_echo_is_reading_not_a_trigger():
    session = _session(mode="attendee", name="Bonds", slug="bonds")
    identity = identity_from_agent(session.bot.profile, SpeakerId("99"))
    session._room = Room(identity)
    other = SimpleNamespace(id=55, display_name="Crypto", name="Crypto")
    content = _format_voice_turn(
        "Crypto", "what happened", "crypto rallied because of bonds", speaker="Alice"
    )
    PiDiscordLiveSession.ingest_room_echo(session, other, content, event_id="m1")
    PiDiscordLiveSession.ingest_room_echo(session, other, content, event_id="m1")
    claims = [line.text for line in session._room.lines if line.text == "crypto rallied because of bonds"]
    assert claims == ["crypto rallied because of bonds"]
    assert identity.mode is ParticipationMode.ATTENDEE
    PiDiscordLiveSession.ingest_room_echo(
        session, other, "**Alice:** what happened\n**Crypto:** unmarked should not land"
    )
    assert not any(line.text == "unmarked should not land" for line in session._room.lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("statistics", [
    {"transcript_audio_ms": 20},  # Moonshine omits cloud finalization fields.
    {"finalize_wait_ms": 12, "finalize_exit": "from_finalize",
     "sent_audio_ms": 30, "transcript_audio_ms": 20},
    {},
], ids=["moonshine", "cloud", "no-stats"])
async def test_production_discord_transcription_uses_shared_session_policy(statistics):
    from sleeptalk.discord.bot import DiscordSessionKernel
    from sleeptalk.session import RealtimeSession

    captured = []

    class Route:
        async def prepare(self, session):
            pass

        async def transcribe(self, pcm, *, participant_id=0):
            captured.append((pcm, participant_id))
            return TranscriptionResult('Shared policy transcript', 'streaming', statistics)

        def active_turn(self):
            return None

        async def close(self):
            pass

    live = _session()
    # The Discord surface must use the normalized result above; rereading
    # provider-specific stats used to crash on Moonshine's dictionary.
    live.streaming_stt = SimpleNamespace(last_turn_stats=lambda _user: statistics)
    marks = []
    live._new_trace = lambda *_args: SimpleNamespace(
        trace_id="test",
        mark=lambda name, **fields: marks.append((name, fields)),
        finish=lambda *_args, **_kwargs: None,
    )
    session = RealtimeSession(SimpleNamespace(route=Route()))
    live.bot.realtime_session = session
    live.bot.kernel = DiscordSessionKernel(session)
    user = SimpleNamespace(id=7, display_name='Alice', bot=False)
    stereo = b'\x01\x00\x03\x00' * 24

    await PiDiscordLiveSession.handle_utterance(live, user, stereo)

    assert captured == [(b'\x02\x00' * 24, 7)]
    assert live.spoken == ['Shared policy transcript']
    completed = dict(marks)['stt_completed']
    assert completed['streamingFinalizeWaitMs'] == statistics.get('finalize_wait_ms')
    assert completed['streamingFinalizeExit'] == statistics.get('finalize_exit')
    assert completed['streamingSentAudioMs'] == statistics.get('sent_audio_ms')
    assert completed['streamingTranscriptAudioMs'] == statistics.get('transcript_audio_ms')
    await session.aclose()
