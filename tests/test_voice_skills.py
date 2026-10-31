from __future__ import annotations

from pathlib import Path

from sleeptalk.voice_skills import (
    discover_skills,
    match_skill,
    parse_spoken_slash,
    rewrite_spoken_skill,
)


def test_parse_spoken_slash_cases() -> None:
    how = parse_spoken_slash("SLASH how")
    assert how is not None
    assert how.token == "how"
    assert how.rest == ""
    assert how.raw == "SLASH how"

    named = parse_spoken_slash("slash ship notes")
    assert named is not None
    assert named.token == "ship"
    assert named.rest == "notes"

    path = parse_spoken_slash("/review fix this file")
    assert path is not None
    assert path.token == "review"
    assert path.rest == "fix this file"

    assert parse_spoken_slash("just talk") is None


def test_rewrite_matches_any_catalog_slug() -> None:
    catalog = frozenset({"review", "ship-notes", "night-shift", "goal"})
    assert rewrite_spoken_skill("SLASH review", catalog) == "/review"
    assert rewrite_spoken_skill("slash ship notes", catalog) == "/ship-notes"
    assert rewrite_spoken_skill("slash night shift", catalog) == "/night-shift"
    assert rewrite_spoken_skill("slash goal", catalog) == "/goal"
    assert rewrite_spoken_skill("/ship-notes the voice path", catalog) == (
        "/ship-notes the voice path"
    )
    assert rewrite_spoken_skill("hello there", catalog) == "hello there"


def test_rewrite_spoken_skill_natural_language() -> None:
    catalog = frozenset({"review", "night-shift"})
    assert rewrite_spoken_skill(
        "can you use the skill review", catalog, invoke="prompt"
    ) == "Use the review skill."
    assert rewrite_spoken_skill(
        "use the review skill to fix this", catalog, invoke="prompt"
    ) == "Use the review skill to fix this"
    assert rewrite_spoken_skill(
        "use the night shift skill", catalog, invoke="slash"
    ) == "/night-shift"


def test_unknown_slash_still_rewrites() -> None:
    text = "slash not-a-real-skill-xyz please"
    assert rewrite_spoken_skill(text, frozenset({"review"})) == (
        "/not-a-real-skill-xyz please"
    )


def test_match_skill_treats_spaces_hyphens_underscores_as_equal() -> None:
    catalog = frozenset({"night-shift"})
    assert match_skill("night shift", catalog) == "night-shift"
    assert match_skill("night_shift", catalog) == "night-shift"
    assert match_skill("missing", catalog) is None


def test_discover_skills_reads_frontmatter_name(tmp_path: Path) -> None:
    skill = tmp_path / ".cursor" / "skills" / "folder" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\n---\nFix slop.\n", encoding="utf-8")
    names = discover_skills(tmp_path)
    assert "review" in names


def test_discover_skills_slugifies_display_names_and_plugin_trees(
    tmp_path: Path,
) -> None:
    hosted = tmp_path / ".agents" / "skills" / "night-shift" / "SKILL.md"
    hosted.parent.mkdir(parents=True)
    hosted.write_text("---\nname: Night Shift\n---\nStyle.\n", encoding="utf-8")
    plugin = (
        tmp_path
        / "plugins"
        / "cache"
        / "vendor"
        / "1.0.0"
        / "skills"
        / "ship-notes"
        / "SKILL.md"
    )
    plugin.parent.mkdir(parents=True)
    plugin.write_text("---\nname: ship-notes\n---\nPlan.\n", encoding="utf-8")
    names = discover_skills(tmp_path, extra_roots=[tmp_path / "plugins" / "cache"])
    assert "night-shift" in names
    assert "Night Shift" not in names
    assert "ship-notes" in names
    assert rewrite_spoken_skill("slash night shift", names) == "/night-shift"
    assert rewrite_spoken_skill("slash ship notes", names) == "/ship-notes"
