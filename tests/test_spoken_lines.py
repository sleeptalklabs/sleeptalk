"""Every spoken-line table has every key cascade reads, and every line is
short enough to play before the answer."""

from __future__ import annotations

import pytest

from sleeptalk import spoken_lines
from sleeptalk.policy import (
    DEFAULT_NARRATION_PHRASES,
    DEFAULT_PREAMBLE_POOLS,
    DEFAULT_REASSURE_TOOL_POOLS,
)


def test_key_sets_match_the_english_defaults() -> None:
    # The English pools are what cascade indexes by literal key; a language
    # table that lacks one would KeyError mid-call after the wholesale swap.
    assert set(spoken_lines.POOL_KEYS) == set(DEFAULT_PREAMBLE_POOLS)
    assert set(spoken_lines.TOOL_KEYS) == set(DEFAULT_REASSURE_TOOL_POOLS)
    assert set(spoken_lines.NARRATION_KEYS) == set(DEFAULT_NARRATION_PHRASES)


@pytest.mark.parametrize("tag", spoken_lines.AVAILABLE)
def test_every_table_is_well_formed(tag: str) -> None:
    lines = spoken_lines.for_language(tag)
    assert lines is not None
    assert spoken_lines.check(lines) == []
    assert set(lines.preamble_pools) == set(DEFAULT_PREAMBLE_POOLS)
    assert set(lines.reassure_tool_pools) == set(DEFAULT_REASSURE_TOOL_POOLS)
    assert set(lines.narration_phrases) == set(DEFAULT_NARRATION_PHRASES)


def test_english_is_the_policy_defaults_and_has_no_table() -> None:
    assert spoken_lines.for_language("en-US") is None
    assert spoken_lines.for_language("en") is None
    english = spoken_lines.english()
    assert english.preamble_pools["ack"] == DEFAULT_PREAMBLE_POOLS["ack"]
    assert spoken_lines.check(english) == []


def test_region_subtags_and_case_resolve_to_the_primary_table() -> None:
    assert spoken_lines.for_language("es-MX").language == "es"
    assert spoken_lines.for_language("PT_br").language == "pt"
    assert spoken_lines.for_language("sw") is None


def test_policy_overrides_are_plain_lists_for_toml() -> None:
    overrides = spoken_lines.for_language("fr").as_policy_overrides()
    assert set(overrides) == {"preamble_pools", "reassure_tool_pools", "narration_phrases"}
    assert all(isinstance(v, list) for v in overrides["preamble_pools"].values())
    assert all(isinstance(v, str) for v in overrides["narration_phrases"].values())


def test_check_reports_a_missing_key_and_a_long_line() -> None:
    broken = spoken_lines.SpokenLines(
        language="xx",
        preamble_pools={"ack": ("x" * 81 + ".",)},
        reassure_tool_pools={},
        narration_phrases={"read": "no mark"},
    )
    problems = spoken_lines.check(broken)
    assert any("preamble pool 'reassure' is missing" in p for p in problems)
    assert any("longer than 80" in p for p in problems)
    assert any("does not end in a sentence mark" in p for p in problems)
