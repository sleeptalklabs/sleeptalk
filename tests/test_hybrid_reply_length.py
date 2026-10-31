"""reply_length on the hybrid route.

The setting used to reach only cascade, so a profile asking for
summarized still got hybrid's full-length answers.
"""

from __future__ import annotations

import pytest
from sleeptalk.openai_realtime import (
    OpenAIRealtimeConfig,
    OpenAIHybridRealtimeProvider,
    _brief_response_instructions,
)
from sleeptalk.policy import wants_brief_speech
from sleeptalk.routes.base import RouteInput, TurnContext
from sleeptalk.routes.hybrid import HybridRoute
from sleeptalk.routes.hybrid_types import (
    GroundedBrief,
    HybridRoleContract,
    TaskId,
)
from sleeptalk.voice_prompt import BRIEF_SPEECH_CONTRACT

from tests.test_hybrid_route import FakeAgent, FakeOutput, FakeRealtime, _context

BRIEF = GroundedBrief(TaskId("t1"), "what changed in the repo", ("A finding.",))


def _route(reply_length: object) -> HybridRoute:
    calls: list = []
    return HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=FakeRealtime(calls),
        audio_output=FakeOutput(calls),
        reply_length=reply_length,
    )


@pytest.mark.parametrize(
    ("value", "brief"),
    [("full", False), ("summarized", True), ("nonsense", False), ("", False)],
)
def test_an_unknown_reply_length_keeps_every_word(value: str, brief: bool) -> None:
    """A typo must cost the listener time, never information."""
    assert wants_brief_speech(value) is brief


def test_the_setting_is_read_when_it_is_used_not_at_boot() -> None:
    """The terminal hotkey and the web control mutate the live policy."""
    policy = {"reply_length": "full"}
    reader = lambda: policy["reply_length"]  # noqa: E731

    assert wants_brief_speech(reader) is False
    policy["reply_length"] = "summarized"
    assert wants_brief_speech(reader) is True


@pytest.mark.asyncio
async def test_summarized_asks_the_specialist_for_a_short_answer() -> None:
    """The ask is the half that shortens the turn.

    The mouth reads the findings back, so a specialist that wrote four
    paragraphs is four paragraphs to listen to however briefly the mouth
    is told to put it.
    """
    calls: list = []
    route = _route("summarized")
    await route.prepare(
        type("S", (), {"session_id": "s1", "emit": staticmethod(lambda e: None)})
    )
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    prompt = next(iter(route._task_prompts.values()))
    assert prompt.startswith(BRIEF_SPEECH_CONTRACT)
    assert "Research current prices" in prompt


@pytest.mark.asyncio
async def test_full_length_leaves_the_specialist_prompt_alone() -> None:
    calls: list = []
    route = _route("full")
    await route.prepare(
        type("S", (), {"session_id": "s1", "emit": staticmethod(lambda e: None)})
    )
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    prompt = next(iter(route._task_prompts.values()))
    assert BRIEF_SPEECH_CONTRACT not in prompt


def test_the_mouth_is_told_the_length_it_was_asked_for() -> None:
    spoken = _brief_response_instructions(BRIEF, "what changed", terse=False)
    terse = _brief_response_instructions(BRIEF, "what changed", terse=True)

    assert "conversational length" in spoken
    assert "three short sentences" in terse
    assert "conversational length" not in terse
    # Both stay honest about the findings they were handed.
    for text in (spoken, terse):
        assert "Do not invent facts beyond the findings." in text
        assert "A finding." in text


def test_the_session_persona_carries_the_same_length() -> None:
    contract = HybridRoleContract("m4rin")
    provider = OpenAIHybridRealtimeProvider(
        OpenAIRealtimeConfig(api_key="k"),
        role_contract=contract,
        reply_length="summarized",
    )
    assert "three short sentences" in provider.role_contract.realtime_instructions_for(
        brief=wants_brief_speech(provider.reply_length)
    )
    assert "conversational length" in contract.realtime_instructions


@pytest.mark.asyncio
async def test_direct_realtime_turn_reads_the_live_reply_length() -> None:
    class Client:
        def __init__(self, calls):
            self.calls = calls

        async def append_text(self, _text):
            return None

        async def set_tts_speed(self, _speed):
            return None

        async def respond(self, *, instructions=None, allow_tools=True):
            self.calls.append((instructions, allow_tools))
            return 1

    async def instructions_after_toggle(initial: str, selected: str) -> str:
        policy = {"reply_length": initial}
        provider = OpenAIHybridRealtimeProvider(
            OpenAIRealtimeConfig(api_key="k"),
            reply_length=lambda: policy["reply_length"],
        )
        calls: list[tuple[str | None, bool]] = []
        provider._client = Client(calls)  # type: ignore[assignment]
        provider._stream_created_response = lambda _generation: _empty_stream()  # type: ignore[method-assign]
        policy["reply_length"] = selected
        async for _ in provider.respond("hello", turn_id=1):
            pass
        return calls[0][0] or ""

    summarized = await instructions_after_toggle("full", "summarized")
    full = await instructions_after_toggle("summarized", "full")
    assert "three short sentences" in summarized
    assert "conversational length" in full


async def _empty_stream():
    if False:
        yield None


async def test_concise_keeps_the_specialist_findings_complete():
    assert wants_brief_speech('concise')
    route = _route('concise')
    assert BRIEF_SPEECH_CONTRACT not in route._specialist_prompt('Research current prices')
