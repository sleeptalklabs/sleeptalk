import inspect

import pytest

from sleeptalk.speech import cartesia, elevenlabs, hume, playht, rime
from sleeptalk.speech.lexicon import apply_pronunciation


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("LS", "el es"),
        ("ls", "el es"),
        ("example.com", "example dot com"),
        ("api.example.com", "api dot example dot com"),
        ("www.example.co.uk", "www dot example dot co dot uk"),
        ("AGENTS.md", "agents dot markdown"),
        ("ROADMAP.md", "roadmap dot markdown"),
        ("Node.js", "node"),
        ("server/app/main.py", "server/app/main dot py"),
        ("3.14", "3.14"),
        ("1.2.3", "1.2.3"),
        ("v1.2.3", "v1.2.3"),
        ("192.168.1.1", "192.168.1.1"),
        ("cd", "see dee"),
        ("pwd", "pee double-you dee"),
        ("gh", "gee aitch"),
        ("rg", "rip grep"),
    ],
)
def test_pronunciation_table(raw, spoken):
    assert apply_pronunciation(raw) == spoken


def test_hostname_does_not_spell_api():
    assert apply_pronunciation("api.example.com") == "api dot example dot com"
    assert "ay pee eye" not in apply_pronunciation("api.example.com")


def test_standalone_acronym_still_spells_api():
    assert "ay pee eye" in apply_pronunciation("Check the API.")


@pytest.mark.parametrize("raw", ["api.example.com", "ls"])
def test_pronunciation_is_idempotent(raw):
    once = apply_pronunciation(raw)
    assert apply_pronunciation(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        "api.example.com",
        "www.example.co.uk",
        "AGENTS.md",
        "ls cd",
        "Check the API.",
        "server/app/main.py",
    ],
)
def test_no_doubled_spaces(raw):
    assert "  " not in apply_pronunciation(raw)


def test_no_lexicon_hits_only_splits_underscores():
    plain = "hello world"
    assert apply_pronunciation(plain) == plain
    assert apply_pronunciation("foo_bar") == "foo bar"


def test_empty_input_is_unchanged():
    assert apply_pronunciation("") == ""


def test_adapters_still_call_apply_pronunciation():
    for module in (cartesia, elevenlabs, hume, playht, rime):
        assert "apply_pronunciation" in inspect.getsource(module), module.__name__
