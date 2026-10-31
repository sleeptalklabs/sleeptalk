import argparse
import hashlib
import json
from pathlib import Path

import pytest

from sleeptalk import remote_cli


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    host = sub.add_parser("host")
    host_sub = host.add_subparsers(dest="host_command", required=True)
    remote_cli.add_remote_parser(host_sub)
    return root


def test_remote_parser_keeps_paired_and_discord_operator_ids_distinct(tmp_path):
    args = parser().parse_args(["host", "remote", "setup", "--origin", "https://setup.example.com",
        "--operator-id", "paired", "--workspace-id", "workspace", "--request-id", "request-123",
        "--profile", "remote", "--workspace", str(tmp_path), "--agent", "pi", "--surface", "discord",
        "--tts-provider", "cartesia", "--guild-id", "1", "--channel-id", "2", "--discord-operator-id", "3"])
    assert args.operator_id == "paired"
    assert args.discord_operator_id == "3"
    assert args.remote_command == "setup"


@pytest.fixture
def remote_dependencies():
    pytest.importorskip('fido2')
    pytest.importorskip('rfc8785')
    pytest.importorskip('cryptography.hazmat.primitives.hpke')


def test_host_map_is_bounded_and_exact(tmp_path, remote_dependencies):
    from sleeptalk.remote_crypto import encode

    path = tmp_path / "hosts.json"
    host_id = hashlib.sha256(b"k" * 32).hexdigest()
    public_key = encode(b"k" * 32)
    path.write_text(json.dumps({host_id: public_key}))
    assert remote_cli.load_host_map(path) == {host_id: public_key}
    path.write_text("[]")
    with pytest.raises(ValueError):
        remote_cli.load_host_map(path)


def test_host_map_rejects_symlink(tmp_path, remote_dependencies):
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "hosts.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        remote_cli.load_host_map(link)


def test_pair_requires_explicit_user_owned_browser(tmp_path, monkeypatch, capsys):
    args = parser().parse_args(["host", "remote", "pair", "--origin", "https://setup.example.com",
        "--request-id", "request-123", "--profile", "remote", "--workspace", str(tmp_path),
        "--agent", "pi", "--surface", "web", "--tts-provider", "cartesia"])
    assert remote_cli.run_remote(args) == 2
    assert "user-controlled browser" in capsys.readouterr().err


def test_main_dispatches_remote_identity_without_loading_voice_runtime(monkeypatch, capsys):
    class Store:
        host_id = "a" * 64
        public_key = "public"

    monkeypatch.setattr(remote_cli, "_store", Store)
    from sleeptalk.cli import main
    assert main(["host", "remote", "identity"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {"host_id": "a" * 64, "host_signing_public_key": "public",
                       "operation": "remote_identity", "schema_version": 1, "status": "ready"}


def test_remote_model_collection_requires_complete_scope_and_no_existing_reference(tmp_path):
    from sleeptalk.agent_setup_options import choices
    arguments = ['host', 'remote', 'pair', '--origin', 'https://setup.example.com',
        '--request-id', 'request-123', '--profile', 'hermes', '--workspace', str(tmp_path),
        '--agent', 'hermes', '--runtime', 'acp', '--surface', 'web', '--tts-provider', 'cartesia',
        '--collect-agent-auth', '--agent-auth-mode', 'api_key',
        '--agent-auth-protocol', 'openai-completions', '--agent-auth-url', 'https://provider.test/v1']
    selected = choices(parser().parse_args(arguments))
    assert selected['auth_request'].base_url == 'https://provider.test/v1'
    assert 'auth' not in selected
    with pytest.raises(ValueError, match='either'):
        choices(parser().parse_args(arguments + ['--agent-credential-ref', 'credential:' + 'a' * 32]))
