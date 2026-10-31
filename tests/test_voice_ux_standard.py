"""Completeness checks for the voice UX standard and its fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from sleeptalk.config import PolicyConfig, load_config
from sleeptalk.session_events import SessionEventType, SessionState

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "voice_ux"
STANDARD = REPO / "docs" / "voice-ux-standard.md"


class _Missing:
    pass


MISSING = _Missing()


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _walk_policy(
    prefix: str,
    schema_val: Any,
    live_val: Any,
    toml_val: Any,
    rows: list[dict[str, Any]],
) -> None:
    if isinstance(schema_val, dict) and isinstance(live_val, dict):
        present = toml_val if isinstance(toml_val, dict) else {}
        for key in sorted(set(schema_val) | set(live_val)):
            child = f"{prefix}.{key}" if prefix else key
            _walk_policy(
                child,
                schema_val.get(key),
                live_val.get(key),
                present.get(key, MISSING),
                rows,
            )
        return
    in_toml = toml_val is not MISSING
    if in_toml and live_val != schema_val:
        source = "root-tuned"
    elif in_toml:
        source = "root-pinned"
    else:
        source = "schema-live"
    rows.append(
        {
            "path": f"policy.{prefix}",
            "schema": schema_val,
            "effective": live_val,
            "in_root_toml": in_toml,
            "source": source,
        }
    )


def _live_policy_leaves() -> list[dict[str, Any]]:
    import tomllib

    schema = PolicyConfig().model_dump()
    raw = tomllib.loads((REPO / "sleeptalk.example.toml").read_text()).get("policy", {})
    config = load_config(REPO / "sleeptalk.example.toml", check_env=False, check_binaries=False)
    rows: list[dict[str, Any]] = []
    _walk_policy("", schema, config.policy.model_dump(), raw, rows)
    return rows


def _policy_map() -> dict[str, Any]:
    return _load_json("policy_map.json")


def test_manifest_lists_every_scenario_file() -> None:
    manifest = _load_json("manifest.json")
    named = set(manifest["scenarios"])
    on_disk = {path.name for path in FIXTURES.glob("*.json")}
    extra = named - on_disk
    assert extra == set(), extra
    scenario_files = {
        name
        for name in on_disk
        if name
        not in {"manifest.json", "policy_map.json", "baseline-receipt.json"}
    }
    missing = scenario_files - named
    assert missing == set(), missing


def test_every_requirement_has_a_fixture() -> None:
    manifest = _load_json("manifest.json")
    policy_map = _policy_map()
    required = {row["requirement"] for row in policy_map["fields"]}
    required.update(row["requirement"] for row in policy_map["invariants"])
    required.update(
        {
            "VUX-LS-001",
            "VUX-ER-001",
            "VUX-AX-001",
            "VUX-HJ-001",
            "VUX-HJ-002",
            "VUX-HJ-003",
            "VUX-HJ-004",
            "VUX-HJ-005",
            "VUX-HJ-006",
            "VUX-SX-001",
            "VUX-SX-002",
            "VUX-SX-003",
            "VUX-SX-004",
        }
    )
    covered: set[str] = set()
    for name in manifest["scenarios"]:
        fixture = _load_json(name)
        covered.add(fixture["requirement"])
    assert required <= covered, sorted(required - covered)


def test_standard_names_every_requirement() -> None:
    text = STANDARD.read_text()
    policy_map = _policy_map()
    ids = {row["requirement"] for row in policy_map["fields"]}
    ids.update(row["requirement"] for row in policy_map["invariants"])
    ids.update(
        {
            "VUX-LS-001",
            "VUX-ER-001",
            "VUX-AX-001",
            "VUX-HJ-001",
            "VUX-HJ-002",
            "VUX-HJ-003",
            "VUX-HJ-004",
            "VUX-HJ-005",
            "VUX-HJ-006",
            "VUX-SX-001",
            "VUX-SX-002",
            "VUX-SX-003",
            "VUX-SX-004",
        }
    )
    missing = [req_id for req_id in sorted(ids) if req_id not in text]
    assert missing == []


def test_standard_names_every_policy_path() -> None:
    text = STANDARD.read_text()
    missing = [
        row["path"]
        for row in _policy_map()["fields"]
        if f"`{row['path']}`" not in text
    ]
    assert missing == []


def test_policy_map_matches_live_root() -> None:
    mapped = {row["path"]: row for row in _policy_map()["fields"]}
    live = {row["path"]: row for row in _live_policy_leaves()}
    assert set(mapped) == set(live), {
        "only_in_map": sorted(set(mapped) - set(live)),
        "only_in_live": sorted(set(live) - set(mapped)),
    }
    mismatches = []
    for path, expected in mapped.items():
        actual = live[path]
        if (
            expected["effective"] != actual["effective"]
            or expected["schema"] != actual["schema"]
            or expected["source"] != actual["source"]
            or expected["in_root_toml"] != actual["in_root_toml"]
        ):
            mismatches.append({"path": path, "expected": expected, "actual": actual})
    assert mismatches == []


def test_schema_default_substitution_fails_for_tuned_fields() -> None:
    tuned = [row for row in _policy_map()["fields"] if row["source"] == "root-tuned"]
    assert tuned, "the standard must keep the operator-tuned diffs"
    substituted = []
    for row in tuned:
        if row["effective"] == row["schema"]:
            substituted.append(row["path"])
    assert substituted == []


def test_receipt_matches_nonsecret_files() -> None:
    receipt = _load_json("baseline-receipt.json")
    policy_map = _policy_map()
    assert receipt["checksums"] == policy_map["checksums"]
    assert receipt["baseline_commit"] == policy_map["baseline_commit"]
    for relative, expected in receipt["checksums"].items():
        digest = hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
        assert digest == expected, relative


def test_session_vocabulary_matches_contract() -> None:
    policy_map = _policy_map()
    assert policy_map["session_states"] == [state.value for state in SessionState]
    assert policy_map["session_events"] == [event.value for event in SessionEventType]
    text = STANDARD.read_text()
    for state in SessionState:
        assert f"`{state.value}`" in text
    for event in SessionEventType:
        assert f"`{event.value}`" in text


def test_family_fixtures_pin_live_values() -> None:
    mapped = {row["path"]: row["effective"] for row in _policy_map()["fields"]}
    manifest = _load_json("manifest.json")
    for name in manifest["scenarios"]:
        fixture = _load_json(name)
        for path, value in fixture.get("pins", {}).items():
            assert path in mapped, path
            assert mapped[path] == value, path


def test_human_fixtures_are_not_event_traces() -> None:
    manifest = _load_json("manifest.json")
    for name in manifest["scenarios"]:
        if not name.startswith("human-"):
            continue
        fixture = _load_json(name)
        assert fixture["kind"] == "human-judgment"
        assert "question" in fixture
        assert "result_vocabulary" in fixture
        assert "steps" not in fixture
