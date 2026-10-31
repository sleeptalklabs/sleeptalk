"""VOICE_PREAMBLES_PLAN.md coverage: smart ack classification, agent
preamble dedupe matching, the non-committal pool-line lint, and the
smart-ack session flow (pool selection + blind fallback)."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleeptalk.policy import (
    DEFAULT_FILLER_GENERAL,
    DEFAULT_FILLER_WEB_SEARCH,
    DEFAULT_NARRATION_PHRASES,
    DEFAULT_PREAMBLE_POOLS,
    DEFAULT_REASSURE_TOOL_POOLS,
)
from sleeptalk.streaming import is_generic_preamble, select_ack_pool

from sleeptalk.session import RealtimeSession
from sleeptalk.voice_policy_runtime import TranscriptionResult
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace
from sleeptalk.discord_voice import PiDiscordLiveSession
from sleeptalk.discord.access import DiscordAccess

OPEN_ACCESS = DiscordAccess(required=False)

# ---------------------------------------------------------------------------
# select_ack_pool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "What's the latest on the repo?",
        "Can you check the logs?",
        "Did the deploy finish?",
        "How does the search work?",
        "Tell me about the roadmap.",
        "Look up the Cartesia docs.",
    ],
)
def test_select_ack_pool_lookup_turns_get_combined_clip(transcript):
    assert select_ack_pool(transcript) == "ack_action"


@pytest.mark.parametrize(
    "transcript",
    [
        "Fix the failing test.",
        "Run the test suite.",
        "Please add a logging line there.",
        "Deploy the server.",
    ],
)
def test_select_ack_pool_task_turns(transcript):
    assert select_ack_pool(transcript) == "action:task"


@pytest.mark.parametrize(
    "transcript",
    [
        "My dog died yesterday.",
        "I'm really frustrated with this bug.",
        "The hospital called about mom.",
    ],
)
def test_select_ack_pool_emotional_turns_never_get_cheerful_ack(transcript):
    assert select_ack_pool(transcript) == "ack:empathy"


@pytest.mark.parametrize(
    "transcript",
    [
        "yeah",
        "okay",
        "thanks",
        "sounds good",
        "hello there",
        "I was thinking about the design",
        "",
    ],
)
def test_select_ack_pool_fast_turns_stay_silent(transcript):
    assert select_ack_pool(transcript) == "none"


def test_select_ack_pool_long_ambiguous_utterance_gets_generic_ack():
    transcript = "So I was walking through the code and noticing that " * 3
    assert len(transcript) >= 120
    assert select_ack_pool(transcript) == "ack"


def test_select_ack_pool_empathy_outranks_lookup():
    # "worried" + a question shape: tone safety wins over the lookup match.
    assert select_ack_pool("I'm worried — did the deploy finish?") == "ack:empathy"


# ---------------------------------------------------------------------------
# is_generic_preamble
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Let me check that.",
        "let me check that",
        "Let me check that for you.",
        "Okay, let me check that.",
        "On it.",
        "Got it.",
        "One second, checking.",
    ],
)
def test_generic_preamble_lines_match(text):
    assert is_generic_preamble(text)


@pytest.mark.parametrize(
    "text",
    [
        "The tests are failing because of an import error.",
        "I found three issues in the runner.",
        "Sure, the deploy finished at noon.",
        "Okay, done — the file is updated.",
    ],
)
def test_real_content_never_matches(text):
    assert not is_generic_preamble(text)


def test_generic_preamble_uses_policy_pools_when_given():
    policy = SimpleNamespace(
        preamble_pools={"ack": ("Custom ack line.",)},
        filler_general=(),
        filler_web_search=(),
    )
    assert is_generic_preamble("Custom ack line.", policy)
    # Default lines are replaced, not merged, when a policy supplies pools.
    assert not is_generic_preamble("Let me check that.", policy)


# ---------------------------------------------------------------------------
# Non-committal lint: a preamble must never promise an outcome before the
# tool resolves (research: premature commitment breaks trust).
# ---------------------------------------------------------------------------

_COMMITMENT_RE = re.compile(
    r"\b(i'?ll (book|order|schedule|send|buy|reserve|cancel|delete|ship)"
    r"|i'?ve (booked|ordered|scheduled|sent|bought|reserved|cancelled|deleted|shipped)"
    r"|all done|all set|done deal)\b",
    re.IGNORECASE,
)


def _all_spoken_lines() -> list[str]:
    lines: list[str] = []
    for pool_lines in DEFAULT_PREAMBLE_POOLS.values():
        lines.extend(pool_lines)
    for pool_lines in DEFAULT_REASSURE_TOOL_POOLS.values():
        lines.extend(pool_lines)
    lines.extend(DEFAULT_NARRATION_PHRASES.values())
    lines.extend(DEFAULT_FILLER_GENERAL)
    lines.extend(DEFAULT_FILLER_WEB_SEARCH)
    return lines


def test_pool_lines_never_commit_to_outcomes():
    offenders = [line for line in _all_spoken_lines() if _COMMITMENT_RE.search(line)]
    assert not offenders, f"pool lines promising outcomes: {offenders}"


# ---------------------------------------------------------------------------
# Smart-ack session flow (mirrors test_turn_correlation's blind-mode test)
# ---------------------------------------------------------------------------

LOUD = (3000).to_bytes(2, "little", signed=True) * 960  # same as test_turn_correlation


def _smart_session(played, speak_kwargs, *, budget_ms=900):
    async def fake_play_file(clip):
        played.append(clip)
        return SimpleNamespace(cancelled=False)

    async def fake_stream_and_speak(prompt, trace, *, suppress_preamble=False, ack_already_played=False):
        speak_kwargs.append({"ack_already_played": ack_already_played})
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
        playback=SimpleNamespace(active=False, play_file=fake_play_file),
        streaming_stt=None,
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            realtime_session=RealtimeSession(SimpleNamespace()),
            stt=None,
            stt_rest_timeout_seconds=8.0,
            settings=settings,
            surface="test",
            persona="test",
            profile=SimpleNamespace(slug="test", name="Test"),
            preamble_clips={
                "ack": [Path("ack-1.wav")],
                "ack_action": [Path("ack-action-1.wav")],
                "action": [Path("action-1.wav")],
            },
            _preamble_rotation={},
            policy=SimpleNamespace(
                resume_grace_ms=750,
                fragment_hold_ms=0,
                instant_ack=True,
                instant_ack_mode="smart",
                smart_ack_budget_ms=budget_ms,
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
    )
    session._handle_utterance = (
        lambda user, pcm: PiDiscordLiveSession._handle_utterance(
            session, user, pcm
        )
    )
    return session


@pytest.mark.asyncio
async def test_discord_defers_question_ack_to_shared_policy(monkeypatch):
    """Discord leaves opening acknowledgements to the shared policy."""
    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0, result=TranscriptionResult("what's the latest on the repo?", "streaming", {})),
    )
    played: list[object] = []
    speak_kwargs: list[dict] = []
    session = _smart_session(played, speak_kwargs)
    user = SimpleNamespace(id=7, display_name="U", bot=False)

    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    await asyncio.sleep(0)

    assert played == []
    assert speak_kwargs == [{"ack_already_played": False}]


@pytest.mark.asyncio
async def test_smart_ack_fast_turn_stays_silent(monkeypatch):
    """Predicted-fast turn: no instant ack at all; the runner's tiered
    coverage still applies (ack_already_played stays False)."""
    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0, result=TranscriptionResult("hello there", "streaming", {})),
    )
    played: list[object] = []
    speak_kwargs: list[dict] = []
    session = _smart_session(played, speak_kwargs)
    user = SimpleNamespace(id=7, display_name="U", bot=False)

    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    await asyncio.sleep(0)

    assert played == []
    assert speak_kwargs == [{"ack_already_played": False}]


@pytest.mark.asyncio
async def test_discord_does_not_start_parallel_ack_during_slow_stt(monkeypatch):
    """Slow transcription must not start a second acknowledgement timer."""
    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0.1, result=TranscriptionResult("hello there", "streaming", {})),
    )
    played: list[object] = []
    speak_kwargs: list[dict] = []
    session = _smart_session(played, speak_kwargs, budget_ms=10)
    user = SimpleNamespace(id=7, display_name="U", bot=False)

    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    await asyncio.sleep(0)

    assert played == []
    assert speak_kwargs == [{"ack_already_played": False}]
