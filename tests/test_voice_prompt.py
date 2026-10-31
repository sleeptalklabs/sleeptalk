from pathlib import Path

from sleeptalk.voice_prompt import (
    BRIEF_SPEECH_CONTRACT,
    VOICE_SPEECH_CONTRACT,
    prepare_voice_prompt,
)


def test_prepare_voice_prompt_rewrites_skill_to_slash() -> None:
    assert prepare_voice_prompt("use the plan skill") == "/plan"
    assert prepare_voice_prompt("/plan") == "/plan"
    assert prepare_voice_prompt("slash plan to fix this") == "/plan to fix this"


def test_prepare_voice_prompt_can_still_use_prose_invoke() -> None:
    assert prepare_voice_prompt(
        "use the plan skill", invoke="prompt"
    ) == "Use the plan skill."


def test_prepare_voice_prompt_can_prefix_contract() -> None:
    out = prepare_voice_prompt("use the plan skill", contract=True)
    assert out.startswith(VOICE_SPEECH_CONTRACT)
    assert "/plan" in out


def test_prepare_voice_prompt_uses_discovered_catalog(tmp_path: Path) -> None:
    skill = tmp_path / ".agents" / "skills" / "night-shift" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: Night Shift\n---\nStyle.\n", encoding="utf-8")
    assert prepare_voice_prompt("slash night shift", cwd=str(tmp_path)) == "/night-shift"
    assert prepare_voice_prompt("use the night shift skill", cwd=str(tmp_path)) == (
        "/night-shift"
    )


def test_brief_asks_for_a_short_answer_and_wins_over_the_general_contract() -> None:
    """Summarized is the stronger ask, so it replaces the contract rather than
    stacking a second set of instructions on top of it."""
    out = prepare_voice_prompt("how did it go", brief=True, contract=True)
    assert out.startswith(BRIEF_SPEECH_CONTRACT)
    assert VOICE_SPEECH_CONTRACT not in out
    assert out.endswith("how did it go")


def test_concise_requests_a_spoken_opening_and_a_complete_written_result():
    from sleeptalk.voice_prompt import CONCISE_SPEECH_CONTRACT

    prompt = prepare_voice_prompt('check the repo', concise=True)
    assert prompt.startswith(CONCISE_SPEECH_CONTRACT)
    assert BRIEF_SPEECH_CONTRACT not in prompt
    assert prompt.endswith('check the repo')
