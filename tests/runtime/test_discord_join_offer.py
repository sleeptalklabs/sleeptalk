"""The canned join offer's answers: yes runs the cron recap, no ends the turn."""
from __future__ import annotations

import pytest

from sleeptalk.discord_voice import live_session
from sleeptalk.discord_voice.utterance_patterns import _RESUME_NO_RE, _RESUME_YES_RE


def test_live_session_has_the_join_offer_patterns():
    # They were used but never defined: the first answer to the offer crashed.
    assert live_session._RESUME_YES_RE is _RESUME_YES_RE
    assert live_session._RESUME_NO_RE is _RESUME_NO_RE


@pytest.mark.parametrize("said", ["Yes.", "yeah", "Sure, go ahead", "okay please", "Go for it!", "um, yes"])
def test_yes_to_the_join_offer(said):
    assert _RESUME_YES_RE.match(said) and not _RESUME_NO_RE.match(said)


@pytest.mark.parametrize("said", ["No.", "nah", "not now", "No thanks", "maybe later", "skip it"])
def test_no_to_the_join_offer(said):
    assert _RESUME_NO_RE.match(said) and not _RESUME_YES_RE.match(said)


@pytest.mark.parametrize("said", ["what's the weather", "yes but first tell me about the build", "no I want the logs"])
def test_anything_else_is_a_normal_turn(said):
    assert not _RESUME_YES_RE.match(said) and not _RESUME_NO_RE.match(said)
