"""CLI boundary for remote pairing, setup, and relay processes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def default_remote_root() -> Path:
    return Path.home() / ".local" / "share" / "sleeptalk" / "remote"


def _add_spec(parser: argparse.ArgumentParser) -> None:
    from sleeptalk.agent_setup_options import add_arguments
    add_arguments(parser)
    parser.add_argument("--collect-agent-auth", action="store_true", help="Collect the Hermes model credential in the paired browser; omit --agent-credential-ref.")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--surface", choices=("web", "terminal", "discord"), required=True)
    parser.add_argument("--runtime", choices=("native", "gateway", "acp"), default="")
    parser.add_argument("--permission-mode", default="")
    parser.add_argument("--gateway-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--pi-bin", default="")
    parser.add_argument("--capture-mode", choices=("headphones", "speakers"), default="")
    parser.add_argument("--user-terminal-local", action="store_true")
    from sleeptalk.agent_setup_options import add_speech_provider_arguments
    add_speech_provider_arguments(parser)
    parser.add_argument("--language", default="en-US")
    parser.add_argument("--guild-id", default="")
    parser.add_argument("--channel-id", default="")
    parser.add_argument("--discord-operator-id", default="")
    parser.add_argument("--replace-reference", default="")
    parser.add_argument("--affected-profile", action="append", default=[])


def add_remote_parser(host_sub) -> argparse.ArgumentParser:
    remote = host_sub.add_parser("remote", help="Pair a private device and operate the remote setup relay.")
    sub = remote.add_subparsers(dest="remote_command", required=True)
    sub.add_parser("identity", help="Print this host's public remote setup identity.")

    serve = sub.add_parser("serve", help="Run the host's outbound relay connection in the foreground.")
    serve.add_argument("--origin", required=True, help="Exact HTTPS setup service origin.")

    relay = sub.add_parser("relay", help="Run the separate volatile setup relay service.")
    relay_sub = relay.add_subparsers(dest="relay_command", required=True)
    relay_serve = relay_sub.add_parser("serve")
    relay_serve.add_argument("--origin", required=True, help="Exact public HTTPS origin served by the TLS frontend.")
    relay_serve.add_argument("--host-map", type=Path, required=True, help="Private JSON map of host IDs to public keys.")
    relay_serve.add_argument("--bind", default="127.0.0.1")
    relay_serve.add_argument("--port", type=int, default=8789)
    relay_serve.add_argument("--assets", type=Path, default=None)

    pair = sub.add_parser("pair", help="Open a trusted loopback page for passkey pairing confirmation.")
    pair.add_argument("--origin", required=True)
    pair.add_argument("--display-host", default="Sleeptalk host")
    pair.add_argument("--user-browser-local", action="store_true")
    _add_spec(pair)

    setup = sub.add_parser("setup", help="Create a private remote credential-entry invitation.")
    setup.add_argument("--origin", required=True)
    setup.add_argument("--display-host", default="Sleeptalk host")
    setup.add_argument("--operator-id", required=True, help="Paired remote operator ID.")
    setup.add_argument("--workspace-id", required=True, help="Workspace ID returned by pairing.")
    _add_spec(setup)

    status = sub.add_parser("status", help="Read value-free durable remote progress.")
    status.add_argument("--request-id", required=True)
    status.add_argument("--kind", choices=("setup", "pairing"), default="setup")

    cancel = sub.add_parser("cancel", help="Cancel a setup request from this trusted host.")
    cancel.add_argument("--request-id", required=True)

    revoke = sub.add_parser("revoke", help="Revoke a paired operator on this host.")
    revoke.add_argument("--operator-id", required=True)
    return remote


def load_host_map(path: Path) -> dict[str, str]:
    from sleeptalk.remote_crypto import decode

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o022:
        os.close(fd)
        raise ValueError("The trusted host map must be an owner-controlled regular file.")
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("The trusted host map is too large.")
    value = json.loads(raw)
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError("The trusted host map must be a bounded JSON object.")
    result = {}
    for host_id, public_key in value.items():
        if not isinstance(host_id, str) or not isinstance(public_key, str):
            raise ValueError("The trusted host map has an invalid entry.")
        key = decode(public_key)
        if len(key) != 32 or hashlib.sha256(key).hexdigest() != host_id:
            raise ValueError("The trusted host map has an invalid entry.")
        result[host_id] = public_key
    return result


def _store():
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.remote_store import RemoteStore

    return RemoteStore(default_remote_root(), CredentialStore())


def _spec(args):
    from sleeptalk.agent_setup_options import speech_provider_choices
    from sleeptalk.private_input import InputSpec

    from sleeptalk.agent_setup_options import choices
    return InputSpec(
        **choices(args),
        request_id=args.request_id, profile=args.profile, workspace=args.workspace,
        agent=args.agent, surface=args.surface, runtime=args.runtime,
        permission_mode=args.permission_mode, gateway_url=args.gateway_url,
        model=args.model, provider=args.provider, pi_bin=args.pi_bin,
        capture_mode=args.capture_mode, user_terminal_local=args.user_terminal_local,
        **speech_provider_choices(args), language=args.language,
        guild_id=args.guild_id, channel_id=args.channel_id,
        operator_id=args.discord_operator_id, replace_reference=args.replace_reference,
        affected_profiles=args.affected_profile,
    )


def _print(value: dict) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)


def _public_receipt(invitation: dict) -> dict:
    from sleeptalk.remote_local import remote_invitation_url

    descriptor = invitation["descriptor"]
    request_id = descriptor.get("request_id", descriptor.get("enrollment_id"))
    return {
        "schema_version": 1, "operation": "remote_setup", "status": "needs_input",
        "request_id": request_id, "expires_at": descriptor["expires_at"],
        "setup_url": remote_invitation_url(invitation),
        "next_action": "Open this invitation in the paired user browser. Do not send credentials through chat.",
    }


async def _pair(store, args) -> int:
    from sleeptalk.agent_setup_options import speech_provider_choices
    from sleeptalk.private_input import InputSpec
    from sleeptalk.remote_host import prepare_pairing, runtime_binding_digest
    from sleeptalk.remote_local import serve_pairing

    spec = _spec(args)
    invitation = await prepare_pairing(store, args.origin, spec, display_host=args.display_host)
    enrollment_id = invitation["descriptor"]["enrollment_id"]

    def current_binding() -> str:
        with store.transaction() as db:
            state = store._get(db, "enrollment", enrollment_id)
        if not state:
            raise ValueError("The pairing request is unavailable.")
        return runtime_binding_digest(InputSpec.model_validate(state["workspace_spec"]))

    result = await serve_pairing(store, invitation, current_binding=current_binding, on_ready=_print)
    if result.get("status") != "needs_input":
        _print(result)
    return 0 if result.get("status") == "confirmed" else 2


async def _setup(store, args) -> int:
    from sleeptalk.remote_host import start_setup

    invitation = await start_setup(store, args.origin, args.operator_id, args.workspace_id,
                                   _spec(args), display_host=args.display_host)
    _print(_public_receipt(invitation) if 'descriptor' in invitation else
           {'schema_version':1,'operation':'remote_setup',**invitation})
    return 0


async def _serve_host(store, origin: str) -> int:
    from sleeptalk.remote_host import RemoteHost

    await RemoteHost(store, origin).run()
    return 0


def _serve_relay(args) -> int:
    from aiohttp import web
    from sleeptalk.remote_relay import SetupRelay

    assets = args.assets or Path(__file__).parent / "_remote_entry"
    if not (assets / "index.html").is_file():
        raise ValueError("The packaged remote entry page is missing.")
    relay = SetupRelay(args.origin, load_host_map(args.host_map))
    web.run_app(relay.app(assets=assets), host=args.bind, port=args.port, print=None)
    return 0


def run_remote(args) -> int | None:
    if getattr(args, "host_command", None) != "remote":
        return None
    if args.remote_command == "pair" and not args.user_browser_local:
        print("Remote pairing requires a user-controlled browser on this runtime host.", file=sys.stderr)
        return 2
    try:
        if args.remote_command == "relay":
            return _serve_relay(args)
        store = _store()
        if args.remote_command == "identity":
            _print({"schema_version": 1, "operation": "remote_identity", "status": "ready",
                    "host_id": store.host_id, "host_signing_public_key": store.public_key})
            return 0
        if args.remote_command == "serve":
            return asyncio.run(_serve_host(store, args.origin))
        if args.remote_command == "pair":
            return asyncio.run(_pair(store, args))
        if args.remote_command == "setup":
            return asyncio.run(_setup(store, args))
        if args.remote_command == "status":
            receipt = (store.enrollment_status(args.request_id) if args.kind == "pairing"
                       else store.status(args.request_id))
            _print({"schema_version": 1, "operation": "remote_status", **receipt})
            return 0
        if args.remote_command == "cancel":
            store.cancel(args.request_id)
            _print({"schema_version": 1, "operation": "remote_cancel", "status": store.status(args.request_id)['payload']['status'],
                    "request_id": args.request_id})
            return 0
        if args.remote_command == "revoke":
            store.revoke(args.operator_id)
            _print({"schema_version": 1, "operation": "remote_revoke", "status": "revoked",
                    "operator_id": args.operator_id})
            return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Remote setup could not complete. Inspect the selected origin, workspace, pairing, and host status.", file=sys.stderr)
        return 2
    return 2
