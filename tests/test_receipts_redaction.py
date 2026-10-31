"""Public receipts must never carry secrets, paths, or process IDs."""

from __future__ import annotations

import json

import pytest

from sleeptalk import receipts


FORBIDDEN_SAMPLES = [
    "sk-provider-key-ABC123",
    "discord-bot-token-XYZ",
    "install-secret-999",
    "voice-pin-4242",
    "turn-credential-secret",
    "/home/ubuntu/.sleeptalk/.env",
    "/Users/op/project/config/profiles/x.toml",
    "pid",
]


def test_redact_strips_secret_keys_and_paths():
    dirty = {
        "schema_version": 1,
        "status": "started",
        "join_url": "https://rendezvous.example/call/abc#frag",
        "api_key": "sk-provider-key-ABC123",
        "token": "discord-bot-token-XYZ",
        "install_secret": "install-secret-999",
        "pin": "voice-pin-4242",
        "voice_pin": "voice-pin-4242",
        "ice_servers": [{"urls": ["turn:example"], "credential": "turn-credential-secret"}],
        "turn_credential": "turn-credential-secret",
        "config_path": "/home/ubuntu/.sleeptalk/profiles/default.toml",
        "path": "/Users/op/project/config/profiles/x.toml",
        "pid": 12345,
        "process_id": 99,
        "nested": {
            "Authorization": "Bearer sk-provider-key-ABC123",
            "ok": True,
        },
        "note": "see /home/ubuntu/.sleeptalk/.env",
    }
    clean = receipts.redact_public_receipt(dirty)
    blob = json.dumps(clean)
    for sample in FORBIDDEN_SAMPLES:
        if sample == "pid":
            continue
        assert sample not in blob
    assert "pid" not in clean
    assert "process_id" not in clean
    assert "api_key" not in clean
    assert "token" not in clean
    assert "pin" not in clean
    assert "ice_servers" not in clean
    assert "config_path" not in clean
    assert "path" not in clean
    assert clean["join_url"].startswith("https://")
    assert clean["nested"] == {"ok": True}
    assert clean["note"] == "<redacted-path>"


def test_ensure_receipts_have_schema_and_no_urls():
    ready = receipts.ensure_ready_receipt(
        profile="default",
        surface="web",
        route="hybrid",
        agent="pi",
        agent_name="Bot",
    )
    needs = receipts.ensure_needs_input_receipt(
        profile="default",
        surface="web",
        route="hybrid",
        agent="auto",
        missing=["OPENAI_API_KEY", "DEEPGRAM_API_KEY"],
        remediation="Set the named keys in .env, then re-run host ensure.",
    )
    assert ready["schema_version"] == 1
    assert ready["status"] == "ready"
    assert "join_url" not in ready
    assert needs["status"] == "needs_input"
    assert needs["missing"] == ["OPENAI_API_KEY", "DEEPGRAM_API_KEY"]
    assert "sk-" not in json.dumps(needs)


def test_ensure_conflict_and_failure_are_explicit_and_safe():
    conflict = receipts.ensure_conflict_receipt(
        profile="default",
        surface="web",
        route="cascade",
        agent="codex",
        conflicts=["agent"],
    )
    failed = receipts.ensure_failed_receipt(
        profile="default",
        surface="web",
        route="cascade",
        agent="codex",
        message="Could not read /Users/operator/private/answers.toml",
    )

    assert conflict["status"] == receipts.STATUS_CONFLICT
    assert conflict["conflicts"] == ["agent"]
    assert failed["status"] == receipts.STATUS_FAILED
    assert failed["code"] == "internal"
    assert "/Users/operator" not in json.dumps(failed)


def test_start_voice_receipt_shape():
    receipt = receipts.start_voice_receipt(
        surface="web",
        join_url="https://example.test/j#x",
        expires_at="2026-09-02T21:00:00Z",
        agent_type="pi",
        agent_slug="default",
        agent_name="Default",
        continuity="new",
        profile="default",
        call_id="safe-call-id",
    )
    assert set(receipt.keys()) >= {
        "schema_version",
        "status",
        "operation",
        "join_url",
        "expires_at",
        "agent",
        "continuity",
    }
    assert receipt["agent"]["slug"] == "default"
    assert receipt["callId"] == "safe-call-id"


def test_list_and_stop_receipts_never_expose_process_metadata():
    listed = receipts.list_calls_receipt(
        [
            {
                "callId": "safe-call-id",
                "profile": "default",
                "started_at": "2026-09-10T00:00:00Z",
                "pid": 123,
                "config_path": "/Users/operator/private.toml",
            }
        ]
    )
    stopped = receipts.stop_call_receipt(
        call_id="safe-call-id", profile="default", forced=False
    )

    assert listed["status"] == "listed"
    assert listed["calls"] == [
        {
            "callId": "safe-call-id",
            "profile": "default",
            "started_at": "2026-09-10T00:00:00Z",
        }
    ]
    assert stopped["status"] == "stopped"
    assert stopped["callId"] == "safe-call-id"


def test_missing_names_from_problems_ignores_values():
    names = receipts.missing_names_from_problems(
        [
            "OPENAI_API_KEY missing — set it to sk-should-not-be-a-name",
            "'pi' binary not found on PATH — install pi",
        ]
    )
    assert "OPENAI_API_KEY" in names
    assert "pi" in names
    assert "sk-should-not-be-a-name" not in names


@pytest.mark.parametrize(
    "code",
    [
        "unconfigured",
        "busy",
        "profile_not_found",
        "config",
        "provider",
        "timeout",
        "cancelled",
        "not_found",
        "internal",
    ],
)
def test_error_receipt_accepts_only_host_error_vocabulary(code):
    receipt = receipts.error_receipt(
        operation="start_voice", code=code, message="safe message"
    )
    assert receipt["code"] == code


def test_error_receipt_rejects_unknown_wire_code():
    with pytest.raises(ValueError):
        receipts.error_receipt(
            operation="start_voice", code="INVALID_REQUEST", message="no"
        )
