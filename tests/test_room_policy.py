from __future__ import annotations

import pytest

from sleeptalk.room import (
    Directed,
    Elsewhere,
    Identity,
    Miss,
    Named,
    ParticipantKind,
    ParticipationMode,
    Room,
    RouteAction,
    ScriptedResolver,
    Speaker,
    SpeakerId,
    Triggered,
    Utterance,
    identity_from_agent,
)


def _human(name: str, ident: str | None = None) -> Speaker:
    return Speaker(SpeakerId(ident or name.lower()), name, ParticipantKind.HUMAN)


def _bot(name: str, ident: str | None = None) -> Speaker:
    return Speaker(SpeakerId(ident or name.lower()), name, ParticipantKind.BOT)


def _identity(
    *,
    mode: ParticipationMode = ParticipationMode.CONVERSATIONALIST,
    name: str = "Campbell",
    slug: str = "campbell",
    triggers: tuple[str, ...] = (),
) -> Identity:
    return Identity(
        id=SpeakerId("self"),
        display_name=name,
        mode=mode,
        addressable_names=(name.casefold(), slug.casefold()),
        proactive_triggers=triggers,
    )


def _said(speaker: Speaker, text: str) -> Utterance:
    return Utterance(speaker, text)


def test_conversationalist_dyad_answers_and_returns_raw_text() -> None:
    room = Room(_identity())
    alice = _human("Alice")
    utterance = _said(alice, "What changed today?")
    decision = room.route(utterance)
    assert decision.action is RouteAction.ANSWER
    assert isinstance(decision.hit, Miss)
    assert room.prompt_for(utterance, decision) == "What changed today?"


def test_attendee_stays_silent_on_human_human_until_named() -> None:
    room = Room(_identity(mode=ParticipationMode.ATTENDEE, name="Notes", slug="notes"))
    alice = _human("Alice")
    bob = _human("Bob")
    first = room.route(_said(alice, "Ship it Friday."))
    second = room.route(_said(bob, "Works for me."))
    assert first.action is RouteAction.IGNORE
    assert second.action is RouteAction.IGNORE
    assert [line.text for line in room.lines] == ["Ship it Friday.", "Works for me."]

    named = _said(alice, "Notes, capture the deadline.")
    decision = room.route(named)
    assert decision.action is RouteAction.ANSWER
    assert isinstance(decision.hit, Named)
    prompt = room.prompt_for(named, decision)
    assert "Ship it Friday." in prompt
    assert "Works for me." in prompt
    assert "Notes, capture the deadline." in prompt


def test_scripted_indirect_reference_answers_attendee() -> None:
    room = Room(
        _identity(mode=ParticipationMode.ATTENDEE, name="Implementer", slug="implementer"),
        resolver=ScriptedResolver(
            {"can the implementer also grab that follow-up": Directed()},
        ),
    )
    alice = _human("Alice")
    assert room.route(_said(alice, "The build is red.")).action is RouteAction.IGNORE
    utterance = _said(alice, "can the implementer also grab that follow-up")
    decision = room.route(utterance)
    assert decision.action is RouteAction.ANSWER
    assert isinstance(decision.hit, Directed)


def test_bot_bot_soak_never_answers_unaddressed_bot_speech() -> None:
    room = Room(_identity(mode=ParticipationMode.CONVERSATIONALIST))
    other = _bot("Researcher")
    for i in range(20):
        decision = room.route(_said(other, f"crypto rallied because of bonds {i}"))
        assert decision.action is RouteAction.IGNORE
        assert isinstance(decision.hit, Miss)
    named = room.route(_said(other, "Campbell, is that true?"))
    assert named.action is RouteAction.ANSWER
    assert isinstance(named.hit, Named)


def test_cross_bot_claim_lands_in_prompt_via_echo_ingest() -> None:
    room = Room(_identity(mode=ParticipationMode.ATTENDEE, name="Bonds", slug="bonds"))
    researcher = _bot("Crypto")
    room.ingest_echo(researcher, "crypto rallied because of bonds")
    alice = _human("Alice")
    utterance = _said(alice, "Bonds, is that true?")
    decision = room.route(utterance)
    assert decision.action is RouteAction.ANSWER
    prompt = room.prompt_for(utterance, decision)
    assert "crypto rallied because of bonds" in prompt


def test_proactive_speaks_only_on_configured_trigger() -> None:
    room = Room(
        _identity(
            mode=ParticipationMode.PROACTIVE,
            name="Watcher",
            slug="watcher",
            triggers=("funding rate flip",),
        )
    )
    alice = _human("Alice")
    assert room.route(_said(alice, "nice weather today")).action is RouteAction.IGNORE
    utterance = _said(alice, "heads up, funding rate flip on BTC")
    decision = room.route(utterance)
    assert decision.action is RouteAction.ANSWER
    assert isinstance(decision.hit, Triggered)


def test_conversationalist_stays_silent_when_someone_else_is_named() -> None:
    room = Room(_identity())
    room.notice(_bot("Notes"))
    decision = room.route(_said(_human("Alice"), "Notes, capture that"))
    assert decision.action is RouteAction.IGNORE
    assert isinstance(decision.hit, Elsewhere)


def test_lurkers_do_not_wrap_one_on_one() -> None:
    room = Room(_identity())
    utterance = _said(_human("Alice"), "just us")
    decision = room.route(utterance)
    assert room.prompt_for(utterance, decision) == "just us"


def test_second_human_wraps_conversationalist_prompt() -> None:
    room = Room(_identity())
    room.route(_said(_human("Alice"), "first take"))
    utterance = _said(_human("Bob"), "second take")
    decision = room.route(utterance)
    prompt = room.prompt_for(utterance, decision)
    assert prompt != "second take"
    assert "Alice: first take" in prompt
    assert "Bob: second take" in prompt


def test_ingest_echo_is_not_a_trigger_and_is_idempotent() -> None:
    room = Room(_identity(mode=ParticipationMode.ATTENDEE))
    other = _bot("Crypto")
    room.ingest_echo(other, "the sky is green")
    room.ingest_echo(other, "the sky is green")
    assert [line.text for line in room.lines] == ["the sky is green"]
    room.ingest_echo(_bot("self", "self"), "own echo")
    assert [line.text for line in room.lines] == ["the sky is green"]


def test_prompt_for_rejects_ignore() -> None:
    room = Room(_identity(mode=ParticipationMode.ATTENDEE))
    utterance = _said(_human("Alice"), "chit chat")
    decision = room.route(utterance)
    with pytest.raises(ValueError, match="IGNORE"):
        room.prompt_for(utterance, decision)


def test_empty_text_is_ignored_and_not_recorded() -> None:
    room = Room(_identity())
    decision = room.route(_said(_human("Alice"), "   "))
    assert decision.action is RouteAction.IGNORE
    assert room.lines == ()


def test_identity_from_agent_parses_mode_and_names() -> None:
    class Agent:
        name = "Implementer"
        slug = "impl"
        participation_mode = "attendee"
        addressable_name = ""
        proactive_triggers = ["funding rate flip"]

    identity = identity_from_agent(Agent(), SpeakerId("99"))
    assert identity.mode is ParticipationMode.ATTENDEE
    assert "implementer" in identity.addressable_names
    assert "impl" in identity.addressable_names
    assert identity.proactive_triggers == ("funding rate flip",)


def test_identity_from_agent_rejects_unknown_mode() -> None:
    class Agent:
        name = "X"
        slug = "x"
        participation_mode = "heckler"
        addressable_name = ""
        proactive_triggers: list[str] = []

    with pytest.raises(ValueError, match="heckler"):
        identity_from_agent(Agent(), SpeakerId("1"))
