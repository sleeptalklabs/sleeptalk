"""sleeptalk run [--config PATH] — load, validate, assemble, run.

sleeptalk doctor [--config PATH] — re-run the full validation ladder.

Exit-code contract for `run` (documented in docs/deployment.md; supervisors
and Restart=on-failure depend on it):

  0  clean shutdown — SIGTERM/SIGINT was handled and close() completed
  1  runtime crash — a FATAL log line (with traceback) is written first;
     safe for a supervisor to restart
  2  config/startup error — bad TOML, missing env/binary, failed startup
     check; a fix-stating message is printed. Never auto-restart: the
     config will not heal itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from sleeptalk.builder import build_session, run_connectivity_checks
from sleeptalk.config import ConfigError, load_resolved_config, load_runtime_config
from sleeptalk.discord_check import DiscordCheckError
from sleeptalk.transports.terminal_audio import AudioDeviceError, audio_recovery

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_CONFIG = 2

_log = logging.getLogger("sleeptalk")


async def _run_session(session, *, log_path=None) -> int:
    """Run the session under SIGTERM/SIGINT supervision.

    SIGTERM/SIGINT cancel the session task; cancellation unwinds through the
    transport's context managers (DiscordTransport's `async with bot:` calls
    PiDiscordBot.close(), which closes the agent subprocess, stream TTS, and
    voice sessions) — the bot leaves the channel instead of ghosting.
    """
    task = asyncio.ensure_future(session.run())
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:
        if not task.done():
            _log.info("received %s — shutting down gracefully", sig.name)
            task.cancel()

    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
            installed.append(sig)
        except NotImplementedError:  # Windows: no loop signal handlers
            pass
    try:
        await task
        return EXIT_OK
    except asyncio.CancelledError:
        print("sleeptalk: shutdown complete")
        return EXIT_OK
    except DiscordCheckError as exc:
        print(f"sleeptalk: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AudioDeviceError as exc:
        # The terminal UI is gone by now; the log alone left a silent exit.
        _log.error("audio unavailable: %s", exc)
        print(f"sleeptalk: audio unavailable: {exc}\n{audio_recovery(exc)}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 — the supervisor contract logs every runtime crash.
        # Never disappear silently: the fatal line lands before exit.
        _log.critical("FATAL: voice session crashed", exc_info=True)
        where = f" Details: {log_path}" if log_path else ""
        print(f"sleeptalk: the call stopped: {type(exc).__name__}: {exc}.{where}", file=sys.stderr)
        return EXIT_CRASH
    except KeyboardInterrupt:  # Windows fallback (no loop handlers)
        task.cancel()
        return EXIT_OK
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _package_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("sleeptalk")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _redact_logs() -> None:
    from sleeptalk.logging_setup import install_redaction

    install_redaction()


def _run_local_profile(*, profile: str, config: str | None) -> int:
    """Run one attached Terminal session without rewriting the saved profile."""
    from sleeptalk.host import resolve_profile_path

    logging.basicConfig(
        level=logging.INFO, format="sleeptalk %(levelname)s: %(message)s"
    )
    _redact_logs()
    path = resolve_profile_path(profile=profile, config=config)
    try:
        loaded = load_runtime_config(path, surface="terminal")
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG
    return _run_loaded_config(path, loaded, skip_checks=False)


def _run_loaded_config(config_path, config, *, skip_checks: bool) -> int:
    """Run an already resolved profile through the canonical CLI lifecycle."""
    from pathlib import Path

    from sleeptalk.call_registry import (
        CallBusyError,
        ProfileIdentity,
        acquire_profile_lease,
        registry_root_for_config,
    )

    identity = ProfileIdentity.from_config(
        config_path=str(config_path),
        profile=config.agent.slug or Path(config_path).stem,
    )
    try:
        lease = acquire_profile_lease(
            root=registry_root_for_config(config_path), identity=identity
        )
    except CallBusyError as exc:
        print(f"sleeptalk: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return _run_owned_config(config_path, config, skip_checks=skip_checks)
    finally:
        lease.close()


def _run_owned_config(config_path, config, *, skip_checks: bool) -> int:
    """Run a profile after its caller has claimed the process lock."""
    from sleeptalk.logging_setup import setup_profile_logging

    log_path, _ = setup_profile_logging(config_path, config)
    try:
        session = build_session(config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG
    if not skip_checks:
        try:
            asyncio.run(run_connectivity_checks(config))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return EXIT_CONFIG
    print(
        f"sleeptalk: transport={config.transport.type} agent={config.agent.type} "
        f"stt={config.stt.provider} tts={config.tts.provider} "
        f"policy(endpointing={config.policy.endpointing}, "
        f"barge_in={config.policy.barge_in}, fillers={config.policy.fillers})"
    )
    return asyncio.run(_run_session(session, log_path=log_path))


#: init flags that carry a key or token.
_SECRET_INIT_FIELDS = frozenset({
    "realtime_key", "gateway_api_key", "gateway_session_token", "agent_token",
    "openclaw_token", "token", "speech_key", "stt_key", "tts_key",
})


def _warn_url_token(flag: str, url: str) -> None:
    """A dashboard link pasted with its ?token= is a secret on the command line too."""
    from urllib.parse import parse_qs, urlsplit

    if url and "token" in parse_qs(urlsplit(url).query):
        print(
            f"warning: the token in {flag} shows in ps and shell history; pass the address without "
            "?token= and give the token with --token - (or an --answers file) instead.",
            file=sys.stderr,
        )


def _read_secret_flags(overrides: dict[str, str]) -> None:
    """Resolve ``--<secret> -`` from a hidden prompt, or stdin when piped.

    A key given literally still works, with a warning: it is visible to other
    processes (ps) and kept in shell history.
    """
    import getpass

    wanted = [name for name in overrides if name in _SECRET_INIT_FIELDS and overrides[name] == "-"]
    literal = [name for name in overrides if name in _SECRET_INIT_FIELDS and overrides[name] != "-"]
    for name in literal:
        flag = "--" + name.replace("_", "-")
        print(
            f"warning: {flag} on the command line shows in ps and shell history; "
            f"use {flag} - to enter it privately, or an --answers file.",
            file=sys.stderr,
        )
    if not wanted:
        return
    if not sys.stdin.isatty():
        if len(wanted) > 1:
            raise ValueError("Only one key can be piped on stdin; use an --answers file for several.")
        value = sys.stdin.readline().strip()
        if not value:
            raise ValueError(f"No value on stdin for --{wanted[0].replace('_', '-')}.")
        overrides[wanted[0]] = value
        return
    for name in wanted:
        value = getpass.getpass(f"{name.replace('_', ' ')}: ").strip()
        if not value:
            raise ValueError(f"No value entered for --{name.replace('_', '-')}.")
        overrides[name] = value


def main(argv: list[str] | None = None) -> int:
    code = _main(argv)
    # launchd (KeepAlive on failure) restarts every non-zero exit; a setup
    # problem won't fix itself, so it ends the job instead of looping.
    if code == EXIT_CONFIG and os.environ.get("SLEEPTALK_SERVICE") == "launchd":
        return EXIT_OK
    return code


def _main(argv: list[str] | None = None) -> int:
    from sleeptalk.logging_setup import redact_default_output

    redact_default_output()
    program = os.path.basename(sys.argv[0]) if argv is None else "sleeptalk"
    parser = argparse.ArgumentParser(prog=program, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.description = (
        "Run sleeptalk to open the settings page in your browser: set up, pick a profile, and start a call.\n"
        "Without a browser here (over SSH, or no display) it runs the setup in the terminal instead."
    )
    parser.epilog = (
        "Agents: use `sleeptalk host` (ensure, input link, start-voice). Never ask the user\n"
        "for provider keys in chat or write them to a file; `sleeptalk host input link`\n"
        "sends the user a private setup page instead. Never read or copy files under the\n"
        "state directory's credentials/ folder; the JSON receipts carry everything you\n"
        "need. See `sleeptalk host --help`."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    destinations = parser.add_mutually_exclusive_group()
    for surface in ("web", "terminal", "discord"):
        destinations.add_argument(
            f"--{surface}", dest="launch_surface", action="store_const", const=surface,
            help=f"Use {surface} for this call without changing your default.",
        )
    sub = parser.add_subparsers(dest="command")
    settings_parser = sub.add_parser("settings", help="Change this workspace's agent, voice, keys, or call destination.")
    settings_parser.add_argument("--open", action="store_true", dest="settings_open",
                                 help="Open the settings page in this machine's browser (the default when there is one).")
    settings_parser.add_argument("--terminal", action="store_true", dest="settings_terminal",
                                 help="Use the terminal setup instead of the settings page.")
    settings_parser.add_argument("--phone", action="store_true", dest="settings_phone",
                                 help="Print a single-use link that opens the settings page on your phone (or any device), "
                                      "through an encrypted channel the call service can't read. Keep this command running, "
                                      "or add --json to return a link from a detached server and exit.")
    settings_parser.add_argument("--json", action="store_true", dest="settings_json",
                                 help="With --phone, return a JSON link receipt and exit while a detached server handles settings.")
    settings_parser.add_argument("--profile", default="", dest="settings_profile", help="Profile slug to edit (default: the workspace's default).")
    trust_parser = sub.add_parser(
        "trust",
        help="Use the Sleeptalk config in this repo (config/profiles/). Its profiles choose the agent, endpoints, and .env.",
    )
    trust_parser.add_argument("path", nargs="?", default=".", help="Repo to trust (default: the one containing the current directory).")
    trust_parser.add_argument("--remove", action="store_true", help="Stop trusting it.")
    link_parser = sub.add_parser(
        "link",
        help="Mint a single-use live-call link for an already configured profile (agents: use `host ensure` and `host start-voice`).",
    )
    from sleeptalk.call_provider import LOCAL_RENDEZVOUS_URL

    link_parser.add_argument(
        "--rendezvous",
        default=None,
        help=(
            "Custom rendezvous origin (default: a package-owned localhost service)."
        ),
    )
    link_parser.add_argument(
        "--agent-instance",
        default=os.getenv("SLEEPTALK_AGENT_INSTANCE", "terminal"),
        help="Agent instance identifier stored with the ticket.",
    )
    link_parser.add_argument(
        "--conversation-id",
        default=os.getenv("SLEEPTALK_CONVERSATION_ID", ""),
        help="Originating agent session/conversation identifier.",
    )
    link_parser.add_argument(
        "--platform-user-id",
        default=os.getenv("SLEEPTALK_PLATFORM_USER_ID", "terminal-user"),
        help="Originating platform user identifier stored with the ticket.",
    )
    link_parser.add_argument(
        "--config",
        default=os.getenv("SLEEPTALK_PROFILE", ""),
        help="Runtime profile whose agent/STT/TTS host the call.",
    )
    link_parser.add_argument(
        "--tailnet",
        action="store_true",
        help="Publish the call on this host's tailnet with `tailscale serve`; no hosted relay or TURN.",
    )
    link_parser.add_argument(
        "--echo",
        action="store_true",
        help="Use the M1 echo fixture instead of an agent runtime.",
    )
    link_parser.add_argument(
        "--foreground",
        action="store_true",
        help="Stay attached to the call host (development only).",
    )
    link_parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=300,
        help="Unused-link lifetime from 1 to 300 seconds (default: 300).",
    )
    dev_call_parser = sub.add_parser(
        "dev-call",
        help="M0 WebRTC walking skeleton: browser microphone to host echo.",
    )
    dev_call_parser.add_argument(
        "--rendezvous",
        default=os.getenv("SLEEPTALK_RENDEZVOUS_URL", LOCAL_RENDEZVOUS_URL),
        help=(
            "Public staging origin (default: SLEEPTALK_RENDEZVOUS_URL or "
            "localhost:8788 for the M0 echo)."
        ),
    )
    dev_call_parser.add_argument(
        "--room",
        default="",
        help="Fixed development room ID for debugging; random by default.",
    )
    run_parser = sub.add_parser("run", help="Run a voice session from sleeptalk.toml.")
    run_parser.add_argument(
        "--config",
        default=None,
        help="Explicit config path (tier 1; orthogonal to SLEEPTALK_HOME). "
        "Default: resolved via SLEEPTALK_HOME -> repo context -> ~/.sleeptalk.",
    )
    run_parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip provider connectivity checks at startup.",
    )
    doctor_parser = sub.add_parser(
        "doctor", help="Re-run the full validation ladder against a config."
    )
    doctor_parser.add_argument(
        "--config",
        default=None,
        help="Explicit config path (tier 1; orthogonal to SLEEPTALK_HOME). "
        "Default: resolved via SLEEPTALK_HOME -> repo context -> ~/.sleeptalk.",
    )
    doctor_parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip live network rungs (connectivity, Discord, STT round-trip). Live rungs send each key "
             "only to its own provider; some (an ElevenLabs transcription and one spoken word) use a little credit.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print one machine-readable JSON line with stable rung ids.",
    )
    connect_parser = sub.add_parser(
        "connect", help="Attach a running agent dashboard to an existing profile."
    )
    connect_sub = connect_parser.add_subparsers(dest="target", required=True)
    hermes_connect = connect_sub.add_parser(
        "hermes",
        help="Find a Hermes dashboard and write type=hermes_gateway.",
    )
    hermes_connect.add_argument(
        "--url",
        default="",
        help="Dashboard URL (default: http://127.0.0.1:9119).",
    )
    hermes_connect.add_argument(
        "--token", default="",
        help="Dashboard session token. Use - to type it at a hidden prompt (or pipe it on stdin).",
    )
    hermes_connect.add_argument("--slug", default="", help="Existing profile slug.")
    hermes_connect.add_argument("--config", default=None, help="Existing profile path.")
    hermes_connect.add_argument(
        "--offline",
        action="store_true",
        help="Write the profile without handshaking the dashboard.",
    )
    init_parser = sub.add_parser(
        "init", help="Set up a new profile; use settings to edit the current workspace."
    )
    init_parser.add_argument("--answers", help="TOML answers file (headless setup).")
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; answers come from --answers and/or flags.",
    )
    init_parser.add_argument(
        "--offline", action="store_true", help="Skip live credential validation."
    )
    for field_name in (
        "surface",
        "route_type", "realtime_key", "realtime_model", "realtime_voice",
        "speech_model", "transcription_model",
        "agent_type", "workspace_dir", "gateway_url", "gateway_api_key",
        "gateway_session_token", "agent_permission_mode", "agent_token", "gateway_session_id", "openclaw_token",
        "name", "slug",
        "guild_id", "client_id", "token_env", "token", "language",
        "speech_provider", "speech_key",
        "stt_provider", "stt_model", "stt_key", "tts_provider",
        "tts_model", "tts_voice_id", "tts_key", "tts_user_id", "tts_mode",
    ):
        init_parser.add_argument(
            f"--{field_name.replace('_', '-')}", default="",
            help=(
                "Use - to type it at a hidden prompt (or pipe it on stdin); a key "
                "written here shows in ps and shell history."
                if field_name in _SECRET_INIT_FIELDS else None
            ),
        )
    service_parser = sub.add_parser(
        "service", help="Opt-in OS persistence: install/uninstall/status."
    )
    service_parser.add_argument("action", choices=("install", "uninstall", "status", "preflight"))
    service_parser.add_argument("slug", help="Profile slug.")
    service_parser.add_argument(
        "--config",
        default=None,
        help="Config path for install (default: resolved layout's profile).",
    )
    service_parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    from sleeptalk.adapter_cli import add_parser as add_adapter_parser

    add_adapter_parser(sub)
    eval_parser = sub.add_parser(
        "eval",
        help="Run eval tiers: contract (pytest) or health (trace invariants).",
    )
    eval_sub = eval_parser.add_subparsers(dest="eval_tier", required=True)
    eval_sub.add_parser("contract", help="Run public contract pytest tier.")
    health_parser = eval_sub.add_parser(
        "health", help="Check session health invariants on JSONL traces."
    )
    health_parser.add_argument(
        "--log",
        required=True,
        help="Path to voice_latency.jsonl (or compatible trace log).",
    )
    health_parser.add_argument(
        "--events",
        default="",
        help="Optional session_events.jsonl for tool/interruption events.",
    )
    health_parser.add_argument(
        "--minimum-turns",
        type=int,
        default=1,
        help="Minimum completed turns required (default: 1).",
    )
    host_parser = sub.add_parser(
        "host",
        help="Context-aware setup and voice start for agents and local terminals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Setup and voice start for an agent acting on the user's behalf.\n"
            "\n"
            "  1. sleeptalk host ensure --profile default --agent <yours> --surface web --json\n"
            "     Reports needs_input with the missing pieces; never starts a call. If its\n"
            "     `tailnet.available` is true, ask the user whether the device they will call\n"
            "     from is on the same Tailscale network; if yes, rerun with --tailnet once and\n"
            "     every later call uses the tailnet (private, no relay) without a flag.\n"
            "     It also checks for a key already on this host that can listen and speak\n"
            "     (OpenRouter, Groq, Together, Venice: from Sleeptalk's store, the environment,\n"
            "     or your own config and .env). If it finds one, it only offers it: the receipt\n"
            "     carries `reusable_keys` and an `ask` line. Put that question to the user and\n"
            "     wait. On yes, rerun ensure with --reuse-key <provider>; it comes back ready with\n"
            "     a `say` line to relay, then go to step 3. Never reuse a key without that yes.\n"
            "     On no, or when nothing was found, send a link:\n"
            "  2. sleeptalk host input link --request-id <id> --profile default --workspace <dir>\n"
            "       --agent <yours>\n"
            "     Returns a single-use setup link. Send it to the user: the page offers every\n"
            "     speech provider with its free usage and whether a card is needed, and the user\n"
            "     picks one and enters its key there (one key listens and speaks). Don't ask them\n"
            "     to choose in chat; --speech-provider only puts one first. Never ask for keys\n"
            "     in chat, never write them to a file, never open the link yourself.\n"
            "     Poll with `host input wait --request-id <id>`; its receipt names the provider.\n"
            "  3. sleeptalk host start-voice --profile default --json\n"
            "     Starts one call and prints the join link for the user. Add --tailnet when the\n"
            "     host and the user's device share a Tailscale network: no relay is used. The\n"
            "     receipt's `delivery` says which way the call travels (a tailnet link contains\n"
            "     .ts.net); tell the user that, and never call a relay link a tailnet call.\n"
            "     A profile that stored --tailnet uses it for every call. When the user asks\n"
            "     for a normal web link instead, add --no-tailnet to this one start-voice; to\n"
            "     stop using the tailnet for good, run `host ensure --no-tailnet`.\n"
            "  Settings, any time: `sleeptalk host settings-link --json` returns a single-use\n"
            "     link to the full settings page for the user's phone (speech service, keys,\n"
            "     voice, agent, Discord). Send it privately; never open it yourself.\n"
            "  4. Discord, optional. Once the first browser call has worked, mention once that\n"
            "     the user can also talk to you in a Discord voice channel and offer to set it\n"
            "     up; they can ask for it at any time. Then:\n"
            "       python -m pip install 'sleeptalk[discord]'   (the python that runs sleeptalk)\n"
            "       sleeptalk host input link --request-id <id> --profile discord --surface discord\n"
            "         --speech-from-profile default --workspace <dir> --agent <yours>\n"
            "     Always pass --speech-from-profile <the working profile> and no provider flag:\n"
            "     it reuses the speech key already saved, so the page asks only for the bot token.\n"
            "     The page walks them through creating the bot, adding it to their server,\n"
            "     picking the voice channel, and locking the bot to their own account. It also\n"
            "     shows them the invite link and how to find their user ID. When `host input\n"
            "     wait` reports\n"
            "     profile_ready, run `sleeptalk service install discord --yes` and start the\n"
            "     service with the command it prints. In Discord they join that voice channel\n"
            "     and type /discord join in any chat in that server; the bot joins and greets them.\n"
            "\n"
            "Never read or copy files under the state directory's credentials/ folder (provider\n"
            "keys and the hosted-service grant live there). The receipts from these commands\n"
            "carry every value an agent needs. Manage calls with `sleeptalk host list` (live\n"
            "calls and their callId) and `sleeptalk host stop <callId>`."
        ),
    )
    host_sub = host_parser.add_subparsers(dest="host_command", required=True)
    from sleeptalk.remote_cli import add_remote_parser
    add_remote_parser(host_sub)
    from sleeptalk.agent_auth_input import add_parser as add_auth_parser
    add_auth_parser(host_sub)
    host_sub.add_parser("inventory", help="Report non-secret bootstrap inventory as JSON.")
    bootstrap_parser = host_sub.add_parser("bootstrap", help="Install a verified wheel into a separate per-user runtime.")
    bootstrap_parser.add_argument("--artifact", required=True)
    bootstrap_parser.add_argument("--sha256", required=True)
    bootstrap_parser.add_argument("--surface", required=True, choices=("web", "terminal", "discord"))
    bootstrap_parser.add_argument("--agent", required=True)
    probe_parser = host_sub.add_parser("probe-runtime", help="Verify an adapter without starting user work.")
    probe_parser.add_argument("--workspace", required=True)
    probe_parser.add_argument("--agent", required=True)
    probe_parser.add_argument("--runtime", choices=("native", "gateway", "acp"), default="")
    from sleeptalk.agent_setup_options import (
        add_arguments as add_agent_setup_arguments,
        add_speech_provider_arguments,
        choices as agent_setup_choices,
        speech_provider_choices,
    )
    add_agent_setup_arguments(probe_parser)
    probe_parser.add_argument("--permission-mode", default="")
    probe_parser.add_argument("--model", default="")
    probe_parser.add_argument("--provider", default="")
    probe_parser.add_argument("--pi-bin", default="")
    probe_parser.add_argument("--gateway-url", default="", help="Non-secret gateway address; configure authentication privately on the runtime host.")
    discover_parser = host_sub.add_parser(
        "discover", help="Read-only capability discovery; does not start setup or audio.",
    )
    discover_parser.add_argument("--workspace", help="Intended directory on this execution host.")
    discover_parser.add_argument("--agent", help="Explicit voice adapter choice; no automatic selection.")
    discover_parser.add_argument("--surface", choices=("web", "terminal", "discord"))
    discover_parser.add_argument("--preferred-surface", choices=("web", "terminal", "discord"))
    discover_parser.add_argument("--session", choices=("new", "resume"), default="new")
    discover_parser.add_argument("--persist", action="store_true", help="Request a persistence feasibility check.")
    discover_parser.add_argument("--context", help="Non-secret integration claims in JSON, at most 32 KiB.")
    discover_parser.add_argument("--schema", action="store_true", help="Print the installed discovery JSON schemas without probing.")
    discover_parser.add_argument("--json", action="store_true", default=True)
    ensure_parser = host_sub.add_parser(
        "ensure",
        help="Convergent readiness check; never mints a call.",
    )
    ensure_parser.add_argument(
        "--profile",
        default="default",
        help="Profile slug under SLEEPTALK_HOME/profiles (default: default).",
    )
    ensure_parser.add_argument("--workspace", help="Bind new profiles to this explicit workspace; verify existing bindings.")
    ensure_parser.add_argument(
        "--tailnet",
        action="store_true",
        help="Store delivery = \"tailnet\" in the profile: browser calls publish on this host's tailnet, no hosted relay. Ask the user first whether the device they call from is on it.",
    )
    ensure_parser.add_argument(
        "--no-tailnet",
        action="store_true",
        help="Remove a stored delivery = \"tailnet\": later browser calls go through the hosted relay again.",
    )
    ensure_parser.add_argument(
        "--config",
        default=None,
        help="Explicit profile path (overrides --profile).",
    )
    ensure_parser.add_argument(
        "--reuse-key",
        choices=("openrouter", "groq", "together", "venice"),
        default="",
        help="Set voice up from this key already on this host, after the user said yes to the receipt's `ask`. "
             "Without it, ensure only offers the keys it finds. The key is validated with its provider "
             "(a small paid request), copied into the private store, and never shown.",
    )
    ensure_parser.add_argument(
        "--no-key-reuse",
        action="store_true",
        help="Do not look for keys already on this host; report what is missing instead.",
    )
    ensure_parser.add_argument(
        "--answers",
        default=None,
        help="Local TOML answers file used to create or repair the profile.",
    )
    ensure_parser.add_argument(
        "--surface",
        choices=("auto", "web", "terminal", "discord"),
        default="auto",
        help="Interface override (default: infer from caller context).",
    )
    ensure_parser.add_argument(
        "--caller-context",
        metavar="CONTEXT",
        default=None,
        help=(
            "Integration-supplied context; local/terminal/desktop run locally, "
            "any chat integration name returns a hosted link "
            "(default: SLEEPTALK_CALLER_CONTEXT or Web)."
        ),
    )
    ensure_parser.add_argument(
        "--route",
        choices=("cascade", "hybrid"),
        default="cascade",
        help=(
            "Requested route for the receipt (default: cascade; "
            "hybrid is an unsupported research preview)."
        ),
    )
    ensure_parser.add_argument(
        "--agent",
        default="auto",
        help="Requested agent type, or auto from the profile (default: auto).",
    )
    ensure_parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Print a versioned JSON receipt on stdout (default).",
    )
    ensure_parser.add_argument(
        "--live",
        action="store_true",
        help="Include live doctor rungs (default: offline static checks only).",
    )
    input_parser = host_sub.add_parser("input", help="Bounded private credential entry in the user's local browser.")
    input_sub = input_parser.add_subparsers(dest="input_command", required=True)
    input_start = input_sub.add_parser("start")
    input_link = input_sub.add_parser(
        "link",
        help="Web setup for a host without the user's browser: returns a single-use phone link through the hosted call service. Send it to the user; never open it yourself.",
    )
    for entry_parser in (input_start, input_link):
        entry_parser.add_argument("--request-id", required=True)
        entry_parser.add_argument("--profile", required=True)
        entry_parser.add_argument("--workspace", required=True)
        entry_parser.add_argument("--agent", required=True)
        entry_parser.add_argument("--runtime", choices=("native", "gateway", "acp"), default="")
        add_agent_setup_arguments(entry_parser)
        entry_parser.add_argument("--permission-mode", default="")
        entry_parser.add_argument("--model", default="")
        entry_parser.add_argument("--provider", default="")
        entry_parser.add_argument("--pi-bin", default="")
        entry_parser.add_argument("--gateway-url", default="", help="Gateway address without embedded credentials or query parameters.")
        add_speech_provider_arguments(entry_parser)
        entry_parser.add_argument("--language", default="en-US")
        entry_parser.add_argument("--replace-reference", default="", help="Replace this stored credential through the private page.")
        entry_parser.add_argument("--affected-profile", action="append", default=[])
    input_link.add_argument("--surface", choices=("web", "discord"), default="web",
                            help="What the profile talks through: a browser call (default) or a Discord bot, which the page also sets up.")
    input_link.set_defaults(capture_mode="", user_terminal_local=False, user_browser_local=False)
    input_start.add_argument("--surface", choices=("web", "terminal", "discord"), required=True)
    input_start.add_argument("--capture-mode", choices=("headphones", "speakers"), default="")
    input_start.add_argument("--user-terminal-local", action="store_true", help="The user controls a terminal on the audio host and will start audio there.")
    input_start.add_argument("--user-browser-local", action="store_true", help="The user controls a browser on the runtime host; never set for an agent browser or remote phone.")
    for action in ("status", "cancel"):
        input_sub.add_parser(action).add_argument("--request-id", required=True)
    input_wait = input_sub.add_parser("wait", help="Block until the setup request completes, ends, or the timeout passes.")
    input_wait.add_argument("--request-id", required=True)
    input_wait.add_argument("--timeout", type=float, default=900, help="Seconds to wait (default: 900).")

    settings_link_parser = host_sub.add_parser(
        "settings-link",
        help="A single-use link to the full settings page for the user's phone: speech service, keys, voice, agent, "
             "Discord, and the rest. Send it privately; never open it yourself.")
    settings_link_parser.add_argument("--profile", default="", help="Profile slug to open (default: the workspace's default).")
    settings_link_parser.add_argument("--json", action="store_true", help="Accepted for symmetry; the receipt is always JSON.")
    credentials_parser = host_sub.add_parser("credentials", help="List, inspect, prune, move, or remove stored credentials; never print a value.")
    credentials_sub = credentials_parser.add_subparsers(dest="credential_command", required=True)
    credentials_sub.add_parser("status").add_argument("reference")
    credentials_sub.add_parser("list", help="Every stored key: reference, provider, and the profiles using it. Never a value.")
    prune_parser = credentials_sub.add_parser(
        "prune", help="Remove stored keys no known profile uses (left over from replaced keys or deleted profiles). "
                      "Never revokes them at the provider.")
    prune_parser.add_argument("--confirm-local-deletion", action="store_true", required=True)
    credentials_sub.add_parser(
        "move-to-keychain",
        help="Move every stored key from the private file into the macOS Keychain or Linux Secret Service "
             "(needs sleeptalk[credentials] and a desktop session). Calls then need that session unlocked.")
    forget_parser = credentials_sub.add_parser("remove")
    forget_parser.add_argument("reference")
    forget_parser.add_argument("--affected-profile", action="append", default=[])
    forget_parser.add_argument("--confirm-local-deletion", action="store_true", required=True)

    start_voice_parser = host_sub.add_parser(
        "start-voice",
        help=(
            "Run Terminal, start a localhost Web call, or return a hosted "
            "agent call. Use --request-id to recover an existing attempt."
        ),
    )
    start_voice_parser.add_argument(
        "surface",
        nargs="?",
        choices=("web", "terminal"),
        default=None,
        help="Interface override (default: infer from caller context).",
    )
    start_voice_parser.add_argument(
        "--profile",
        default="default",
        help=(
            "Profile slug or .toml filename under SLEEPTALK_HOME/profiles "
            "(default: default)."
        ),
    )
    start_voice_parser.add_argument("--request-id", default="", help="Stable browser start ID, scoped to this profile; prints JSON and replays the same attempt.")
    start_voice_parser.add_argument("--workspace", help="Verify the profile's intended workspace before starting.")
    start_voice_parser.add_argument(
        "--caller-context",
        metavar="CONTEXT",
        default=None,
        help=(
            "Integration-supplied context; local/terminal/desktop run locally, "
            "any chat integration name returns a hosted link "
            "(default: SLEEPTALK_CALLER_CONTEXT or Web)."
        ),
    )
    start_voice_parser.add_argument(
        "--config",
        default=None,
        help="Explicit profile path (overrides --profile).",
    )
    start_voice_parser.add_argument(
        "--rendezvous",
        default=None,
        help=(
            "Custom rendezvous origin. Explicit Web otherwise uses localhost; "
            "agent/chat starts use the hosted service."
        ),
    )
    start_voice_parser.add_argument(
        "--agent-instance",
        default=os.getenv("SLEEPTALK_AGENT_INSTANCE", ""),
        help="Agent instance identifier stored with the ticket.",
    )
    start_voice_parser.add_argument(
        "--conversation-id",
        default=os.getenv("SLEEPTALK_CONVERSATION_ID", ""),
        help="Originating agent session/conversation identifier.",
    )
    start_voice_parser.add_argument(
        "--platform-user-id",
        default=os.getenv("SLEEPTALK_PLATFORM_USER_ID", "terminal-user"),
        help="Originating platform user identifier stored with the ticket.",
    )
    start_voice_parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=300,
        help="Unused-link lifetime from 1 to 300 seconds (default: 300).",
    )
    start_voice_parser.add_argument(
        "--echo",
        action="store_true",
        help="Use the M1 echo fixture instead of an agent runtime.",
    )
    start_voice_parser.add_argument(
        "--tailnet",
        action="store_true",
        help="Publish the call on this host's tailnet with `tailscale serve` (https://<name>:8443); no hosted relay or TURN. Only tailnet members can open the link.",
    )
    start_voice_parser.add_argument(
        "--no-tailnet",
        action="store_true",
        help="This call only: skip the profile's stored tailnet delivery and use the usual link (the hosted relay for an agent). `host ensure --no-tailnet` removes the stored choice.",
    )
    start_voice_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a versioned JSON receipt instead of a plain local URL.",
    )
    list_parser = host_sub.add_parser(
        "list",
        help="List live detached calls as one JSON receipt.",
    )
    list_parser.add_argument("--json", action="store_true", help="Accepted for symmetry; the receipt is always JSON.")
    status_parser = host_sub.add_parser("status", help="Read a start attempt as JSON without returning its join link.")
    status_parser.add_argument("--request-id", required=True)
    status_parser.add_argument("--profile", default="default")
    status_parser.add_argument("--config", default=None)
    status_parser.add_argument("--echo", action="store_true")
    status_parser.add_argument("--agent-instance", default="terminal")
    stop_parser = host_sub.add_parser(
        "stop",
        help="Stop a detached call by callId and print one JSON receipt.",
    )
    stop_parser.add_argument("call_id", nargs="?", metavar="callId", help="Opaque call id, from `host list`.")
    # Agents reach for a flag as often as a positional; both mean the same call.
    stop_parser.add_argument("--call-id", "--callId", dest="call_id_flag", metavar="CALL_ID", help="The same call id, as a flag.")
    reset_access_parser = host_sub.add_parser(
        "reset-access",
        help="Replace this machine's anonymous hosted-service credential.",
    )
    reset_access_parser.add_argument(
        "--rendezvous",
        default=None,
        help="Hosted rendezvous origin (default: the Sleeptalk production service).",
    )
    args = parser.parse_args(argv)

    if args.launch_surface and args.command is not None:
        parser.error("Use --web, --terminal, or --discord with sleeptalk alone.")
    if args.command == "settings" and args.settings_json and not args.settings_phone:
        parser.error("settings --json requires --phone.")
    if args.command == "settings" and getattr(args, "settings_phone", False):
        if args.settings_json:
            from sleeptalk.host import print_json_receipt
            from sleeptalk.remote_settings import start_detached

            receipt = start_detached(profile=args.settings_profile)
            print_json_receipt(receipt)
            return EXIT_OK if receipt["status"] == "ready" else EXIT_CONFIG
        from sleeptalk.remote_settings import run_link

        return run_link(profile=args.settings_profile)
    if args.command in (None, "settings") and not args.launch_surface and not getattr(args, "settings_terminal", False):
        from sleeptalk.launcher import settings_page_first

        # The settings page is the front door; `--open` asks for it even where it can't be detected.
        if getattr(args, "settings_open", False) or settings_page_first():
            from sleeptalk.settings_page import run as run_settings_page

            return run_settings_page(profile=getattr(args, "settings_profile", ""))
        if sys.stdin.isatty() and sys.stdout.isatty():
            print("No browser on this computer, so setup runs here in the terminal. "
                  "For the settings page on your phone instead: sleeptalk settings --phone")
    if args.command is None or args.command == "settings":
        from sleeptalk.launcher import run

        return run(settings=args.command == "settings", surface=args.launch_surface)

    if args.command == "host":
        from sleeptalk.agent_auth_input import run as run_auth
        auth_result = run_auth(args)
        if auth_result is not None:
            return auth_result
        from sleeptalk.remote_cli import run_remote
        remote_result = run_remote(args)
        if remote_result is not None:
            return remote_result
        if args.host_command in {"inventory", "bootstrap", "probe-runtime"}:
            from pathlib import Path
            from sleeptalk.host import print_json_receipt
            from sleeptalk.receipts import error_receipt
            from sleeptalk.bootstrap import BootstrapError, inventory, install_artifact
            try:
                if args.host_command == "inventory":
                    receipt = inventory()
                elif args.host_command == "bootstrap":
                    receipt = install_artifact(Path(args.artifact), args.sha256, surface=args.surface, agent=args.agent)
                else:
                    from sleeptalk.config import AgentConfig
                    from sleeptalk.runtime_probe import probe_runtime
                    receipt = asyncio.run(probe_runtime(AgentConfig(type=args.agent, workspace_dir=args.workspace,
                        on_incompatible="fail", **agent_setup_choices(args), **{name: getattr(args, name)
                        for name in ("runtime", "permission_mode", "gateway_url", "model", "provider", "pi_bin") if getattr(args, name)})))
            except (BootstrapError, OSError, ValueError):
                receipt = error_receipt(operation=args.host_command.replace("-", "_"), code="config", message="The requested bootstrap operation could not complete. Verify the artifact, selected adapter, workspace, and host permissions.")
            print_json_receipt(receipt)
            return EXIT_OK if receipt.get("status") == "ready" else EXIT_CONFIG
        if args.host_command == "input":
            from sleeptalk import private_input
            from sleeptalk.host import print_json_receipt
            try:
                if args.input_command in {"start", "link"}:
                    spec = private_input.InputSpec(request_id=args.request_id, profile=args.profile,
                        workspace=args.workspace, agent=args.agent, surface=args.surface, **agent_setup_choices(args),
                        runtime=args.runtime, permission_mode=args.permission_mode,
                        gateway_url=args.gateway_url, model=args.model, provider=args.provider, pi_bin=args.pi_bin,
                        capture_mode=args.capture_mode, user_terminal_local=args.user_terminal_local,
                        **speech_provider_choices(args), language=args.language,
                        replace_reference=args.replace_reference, affected_profiles=args.affected_profile)
                    entry = {"entry": "link"} if args.input_command == "link" else {}
                    receipt = asyncio.run(private_input.start(spec, user_browser_local=args.user_browser_local, **entry))
                elif args.input_command == "wait":
                    receipt = asyncio.run(private_input.wait(args.request_id, timeout=args.timeout))
                elif args.input_command == "cancel":
                    receipt = private_input.cancel(args.request_id)
                else:
                    receipt = private_input.status(args.request_id)
            except private_input.InputError as exc:
                receipt = {"schema_version": 1, "operation": "private_input", "status": "failed", "next_action": str(exc)}
            except (OSError, ValueError, private_input.CredentialError):
                receipt = {"schema_version": 1, "operation": "private_input", "status": "failed",
                    "next_action": "Check the non-secret choices, runtime login, local user browser, and setup status. Without the user's browser on this host, use host input link."}
            print_json_receipt(receipt)
            return EXIT_CONFIG if receipt.get("status") in {"failed", "needs_input"} else EXIT_OK
        if args.host_command == "settings-link":
            from sleeptalk.host import print_json_receipt
            from sleeptalk.remote_settings import start_detached

            receipt = start_detached(profile=args.profile)
            print_json_receipt(receipt)
            return EXIT_OK if receipt["status"] == "ready" else EXIT_CONFIG
        if args.host_command == "credentials":
            from dataclasses import asdict
            from sleeptalk.credential_store import CredentialError, CredentialImpactError, CredentialStore
            from sleeptalk.host import print_json_receipt
            try:
                store = CredentialStore()
                if args.credential_command == "status":
                    receipt = {"status": "ready", **asdict(store.status(args.reference))}
                elif args.credential_command == "list":
                    from sleeptalk.credential_store import storage_kind
                    from sleeptalk.key_inventory import inventory
                    receipt = {"status": "ready", "backend": storage_kind(store.root), "credentials": inventory(store=store)}
                elif args.credential_command == "move-to-keychain":
                    from sleeptalk.credential_store import storage_kind
                    receipt = {"status": "ready", "moved": store.move_to_keyring(), "backend": storage_kind(store.root)}
                elif args.credential_command == "prune":
                    from sleeptalk.key_inventory import forget_unused, inventory
                    unused = [item["reference"] for item in inventory(store=store) if item["unused"]]
                    receipt = {"status": "removed", "removed": forget_unused(unused, store=store), "provider_revoked": False}
                else:
                    store.remove(args.reference, affected_profiles=args.affected_profile)
                    receipt = {"status": "removed", "provider_revoked": False}
            except CredentialImpactError as exc:
                receipt = {"status": "needs_input", "affected_profiles": list(exc.profiles), "next_action": "Confirm every affected profile before local deletion. Revoke the key separately at its provider."}
            except CredentialError as exc:
                receipt = {"status": "failed", "next_action": str(exc) if args.credential_command == "move-to-keychain"
                           else "Check the reference and access to the private store."}
            print_json_receipt({"schema_version": 1, "operation": "credentials", **receipt})
            return EXIT_OK if receipt["status"] in {"ready", "removed"} else EXIT_CONFIG
        if args.host_command == "discover":
            from pathlib import Path

            from sleeptalk.host import print_json_receipt
            from sleeptalk.onboarding import Context, Intent, contract_schema, discover
            from sleeptalk.receipts import error_receipt

            if args.schema:
                print_json_receipt(contract_schema())
                return EXIT_OK
            try:
                context = None
                if args.context:
                    with Path(args.context).open("rb") as stream:
                        raw = stream.read(32769)
                    if len(raw) > 32768:
                        raise ValueError("context too large")
                    context = Context.model_validate_json(raw)
                intent = Intent(
                    workspace=args.workspace, agent=args.agent, surface=args.surface,
                    preferred_surface=args.preferred_surface, session=args.session,
                    persistence=args.persist,
                )
            except (OSError, ValueError):
                print_json_receipt(error_receipt(
                    operation="discover", code="config",
                    message="Invalid discovery input. Use host discover --schema; context must be non-secret JSON of at most 32 KiB.",
                ))
                return EXIT_CONFIG
            receipt = discover(intent, context)
            print_json_receipt(receipt.model_dump(mode="json"))
            return EXIT_OK if receipt.status == "ready" else EXIT_CONFIG

        from sleeptalk.call_provider import CallProviderError
        from sleeptalk.host import (
            ensure,
            list_voice_calls,
            print_json_receipt,
            start_web_voice,
            start_voice_web,
            stop_voice_call,
            voice_call_status,
        )
        from sleeptalk.caller_context import (
            VoiceDelivery,
            launch_plan_for,
            resolve_caller_context,
            surface_for,
        )
        from sleeptalk.receipts import STATUS_ERROR, STATUS_NEEDS_INPUT, STATUS_READY

        caller = resolve_caller_context(
            explicit=getattr(args, "caller_context", None),
            env=os.environ,
        )

        if args.host_command == "list":
            receipt = list_voice_calls()
            print_json_receipt(receipt)
            return EXIT_OK if receipt.get("status") != STATUS_ERROR else EXIT_CONFIG
        if args.host_command == "status":
            receipt = voice_call_status(
                request_id=args.request_id, profile=args.profile, config=args.config,
                echo=args.echo, agent_instance=args.agent_instance,
            )
            print_json_receipt(receipt)
            return EXIT_OK if receipt.get("status") != STATUS_ERROR else EXIT_CONFIG
        if args.host_command == "stop":
            call_id = args.call_id or args.call_id_flag
            if not call_id or (args.call_id and args.call_id_flag and args.call_id != args.call_id_flag):
                print("Name one call: `sleeptalk host stop <callId>` (ids come from `sleeptalk host list`).", file=sys.stderr)
                return EXIT_CONFIG
            receipt = stop_voice_call(call_id)
            print_json_receipt(receipt)
            return EXIT_OK if receipt.get("status") != STATUS_ERROR else EXIT_CONFIG
        if args.host_command == "reset-access":
            from sleeptalk.call_provider import HOSTED_RENDEZVOUS_URL
            from sleeptalk.service_grant import ServiceGrantError, ServiceGrantStore

            origin = args.rendezvous or HOSTED_RENDEZVOUS_URL
            try:
                grant = ServiceGrantStore().reset(origin)
            except ServiceGrantError as exc:
                print(f"Hosted access reset failed: {exc}", file=sys.stderr)
                return EXIT_CONFIG
            print(
                f"Hosted access reset for {grant.origin}. "
                "A new anonymous installation will enroll on the next hosted call."
            )
            return EXIT_OK

        if args.host_command in {"ensure", "start-voice"} and args.tailnet and args.no_tailnet:
            print("Choose --tailnet or --no-tailnet, not both.", file=sys.stderr)
            return EXIT_CONFIG
        if args.host_command == "ensure":
            requested_surface = (
                surface_for(caller) if args.surface == "auto" else args.surface
            )
            receipt = ensure(
                profile=args.profile,
                surface=requested_surface,
                route=args.route,
                agent=args.agent,
                config=args.config,
                answers=args.answers,
                live=args.live,
                tailnet=args.tailnet,
                no_tailnet=args.no_tailnet,
                reuse_key=args.reuse_key,
                key_reuse=not args.no_key_reuse,
                **({"workspace": args.workspace} if args.workspace is not None else {}),
            )
            print_json_receipt(receipt)
            if receipt.get("status") == STATUS_READY:
                return EXIT_OK
            if receipt.get("status") == STATUS_NEEDS_INPUT:
                return EXIT_CONFIG
            return EXIT_CONFIG
        if args.host_command == "start-voice":
            plan = launch_plan_for(
                surface=args.surface,
                caller=caller,
                rendezvous_override=args.rendezvous,
                json_requested=args.json or bool(args.request_id),
            )
            if plan.delivery is VoiceDelivery.TERMINAL:
                if args.request_id:
                    from sleeptalk.receipts import error_receipt
                    print_json_receipt(error_receipt(operation="start_voice", code="config", message="Request recovery is supported for browser calls. Choose Web delivery."))
                    return EXIT_CONFIG
                if args.workspace is not None:
                    from sleeptalk.host import _read_profile_config, resolve_profile_path
                    from sleeptalk.runtime_binding import BindingError, verify
                    try:
                        verify(_read_profile_config(resolve_profile_path(profile=args.profile, config=args.config)), workspace=args.workspace)
                    except (BindingError, OSError, ValueError):
                        print("The profile workspace binding could not be verified.", file=sys.stderr)
                        return EXIT_CONFIG
                return _run_local_profile(profile=args.profile, config=args.config)
            local = plan.delivery is VoiceDelivery.LOCAL_WEB
            start_args = {
                "profile": args.profile,
                "config": args.config,
                "rendezvous_url": args.rendezvous,
                "agent_instance": args.agent_instance or None,
                "conversation_id": args.conversation_id,
                "platform_user_id": args.platform_user_id,
                "ttl_seconds": args.ttl_seconds,
                "echo": args.echo,
                "local": local and not args.tailnet,
                "tailnet": args.tailnet,
            }
            if args.no_tailnet:
                start_args["no_tailnet"] = True
            if args.workspace is not None:
                start_args["workspace"] = args.workspace
            if args.request_id:
                start_args["request_id"] = args.request_id
            if plan.structured:
                receipt = start_voice_web(**start_args)
                print_json_receipt(receipt)
                return (
                    EXIT_OK
                    if receipt.get("status") not in {STATUS_ERROR, "conflict"}
                    else EXIT_CONFIG
                )
            try:
                started = start_web_voice(**start_args)
            except CallProviderError as exc:
                print(f"Live voice link unavailable: {exc}", file=sys.stderr)
                return EXIT_CONFIG
            print(started.join_url, flush=True)
            return EXIT_OK
        print(f"unknown host command {args.host_command!r}", file=sys.stderr)
        return EXIT_CONFIG

    if args.command == "link":
        from sleeptalk.call_provider import (
            CallContext,
            CallProviderError,
            run_ticket_echo_call,
        )
        from sleeptalk.host import start_web_voice

        conversation_id = args.conversation_id or (
            "terminal-demo" if args.echo else ""
        )
        context = CallContext(
            agent_instance=args.agent_instance,
            conversation_id=conversation_id,
            platform_user_id=args.platform_user_id,
        )
        try:
            if not 1 <= args.ttl_seconds <= 300:
                raise CallProviderError(
                    "Call links must expire between 1 and 300 seconds."
                )
            if not args.echo and not args.config:
                raise CallProviderError(
                    "An agent call needs a runtime profile. Pass --config PATH; "
                    "use --echo only for the development fixture."
                )
            if args.foreground:
                if not args.echo:
                    from sleeptalk.webcalls import resolve_web_call

                    asyncio.run(
                        resolve_web_call()(
                            config_path=args.config,
                            rendezvous_url=args.rendezvous,
                            context=context,
                            ttl_seconds=args.ttl_seconds,
                            on_ready=lambda url: print(url, flush=True),
                            local=args.rendezvous is None and not args.tailnet,
                            tailnet=args.tailnet,
                        )
                    )
                else:
                    if args.rendezvous is None or args.tailnet:
                        from sleeptalk.local_rendezvous import run_local_echo_call

                        asyncio.run(
                            run_local_echo_call(
                                context,
                                ttl_seconds=args.ttl_seconds,
                                on_ready=lambda url: print(url, flush=True),
                                tailnet=args.tailnet,
                            )
                        )
                    else:
                        asyncio.run(
                            run_ticket_echo_call(
                                args.rendezvous,
                                context,
                                ttl_seconds=args.ttl_seconds,
                                on_ready=lambda url: print(url, flush=True),
                            )
                        )
            else:
                started = start_web_voice(
                    config=args.config or None,
                    rendezvous_url=args.rendezvous,
                    agent_instance=args.agent_instance,
                    conversation_id=conversation_id,
                    platform_user_id=args.platform_user_id,
                    ttl_seconds=args.ttl_seconds,
                    echo=args.echo,
                    local=args.rendezvous is None and not args.tailnet,
                    tailnet=args.tailnet,
                )
                print(started.join_url, flush=True)
        except KeyboardInterrupt:
            print("sleeptalk: live call ended")
            return EXIT_OK
        except CallProviderError as exc:
            print(f"Live voice link unavailable: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        return EXIT_OK

    if args.command == "dev-call":
        from sleeptalk.dev_call import DevCallError, run_dev_echo_call

        try:
            asyncio.run(
                run_dev_echo_call(
                    args.rendezvous,
                    room_id=args.room,
                    on_ready=lambda url: print(url, flush=True),
                )
            )
        except KeyboardInterrupt:
            print("sleeptalk: development call ended")
            return EXIT_OK
        except DevCallError as exc:
            print(f"Live voice link unavailable: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        return EXIT_OK

    if args.command == "service":
        from sleeptalk.service import install, status, uninstall, preflight

        if args.action == "status":
            return status(args.slug)
        if args.action == "uninstall":
            return uninstall(args.slug, yes=args.yes)
        if args.config:
            from pathlib import Path

            config_path = Path(args.config)
        else:
            from sleeptalk.home import resolve_layout

            config_path = resolve_layout().profiles_dir / f"{args.slug}.toml"
        if not config_path.is_file():
            print(f"{config_path} not found — pass --config PATH.", file=sys.stderr)
            return EXIT_CONFIG
        if args.action == "preflight":
            from sleeptalk.host import print_json_receipt
            receipt = preflight(args.slug, config_path)
            print_json_receipt(receipt)
            return EXIT_OK if receipt["status"] == "ready" else EXIT_CONFIG
        return install(args.slug, config_path, yes=args.yes)

    if args.command == "adapter":
        from sleeptalk.adapter_cli import run as run_adapter

        return run_adapter(args)

    if args.command == "connect":
        from sleeptalk.wizard import run_connect_hermes

        if args.target != "hermes":
            print(f"unknown connect target {args.target!r}", file=sys.stderr)
            return EXIT_CONFIG
        secret = {"token": args.token} if args.token else {}
        _warn_url_token("--url", args.url)
        try:
            _read_secret_flags(secret)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        return asyncio.run(
            run_connect_hermes(
                url=args.url,
                token=secret.get("token", ""),
                slug=args.slug,
                config_path=args.config or "",
                live=not args.offline,
            )
        )

    if args.command == "init":
        import tomllib
        from pathlib import Path

        from sleeptalk.setup_core import answers_from_mapping
        from sleeptalk.wizard import run_init, run_non_interactive

        flag_overrides = {
            field_name: getattr(args, field_name)
            for field_name in (
                "surface",
                "route_type", "realtime_key", "realtime_model", "realtime_voice",
                "speech_model", "transcription_model",
                "agent_type", "workspace_dir", "gateway_url", "gateway_api_key",
                "gateway_session_token", "agent_permission_mode", "agent_token", "gateway_session_id", "openclaw_token",
                "name", "slug",
                "guild_id", "client_id", "token_env", "token", "language",
                "speech_provider", "speech_key",
        "stt_provider", "stt_model", "stt_key", "tts_provider",
                "tts_model", "tts_voice_id", "tts_key", "tts_user_id", "tts_mode",
            )
            if getattr(args, field_name)
        }
        _warn_url_token("--gateway-url", flag_overrides.get("gateway_url", ""))
        try:
            _read_secret_flags(flag_overrides)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.answers or args.non_interactive or flag_overrides:
            raw = {}
            if args.answers:
                raw = tomllib.loads(Path(args.answers).read_text(encoding="utf-8"))
            raw.update(flag_overrides)
            try:
                answers = answers_from_mapping(raw)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            return asyncio.run(run_non_interactive(answers, live=not args.offline))
        return run_init(live=not args.offline)

    if args.command == "eval":
        from pathlib import Path

        from sleeptalk.evals.cli import run_contract, run_health

        if args.eval_tier == "contract":
            return run_contract()
        if args.eval_tier == "health":
            events = Path(args.events) if args.events else None
            return run_health(
                latency_log=Path(args.log),
                events_log=events,
                minimum_turns=args.minimum_turns,
            )
        print(f"unknown eval tier {args.eval_tier!r}", file=sys.stderr)
        return EXIT_CONFIG

    if args.command == "trust":
        from pathlib import Path

        from sleeptalk.home import detect_repo_context, set_trusted

        detected = detect_repo_context(Path(args.path))
        if detected is None and not args.remove:
            print(f"No Sleeptalk repo config (config/profiles/*.toml) at or above {args.path}.", file=sys.stderr)
            return EXIT_CONFIG
        root = set_trusted(detected[0] if detected else Path(args.path), not args.remove)
        print(f"{'No longer trusted' if args.remove else 'Trusted'}: {root}")
        return 0

    if args.command == "doctor":
        from sleeptalk.doctor import format_report, run_doctor

        if args.config and not args.json:
            print(f"config: {args.config} (tier 1: explicit --config)")
        config_path = _resolve_config_path(
            args.config, announce_resolution=not args.json
        )
        report = asyncio.run(run_doctor(config_path, live=not args.offline))
        print(report.to_json() if args.json else format_report(report))
        return 1 if report.failed else 0

    if args.command == "run":
        logging.basicConfig(
            level=logging.INFO, format="sleeptalk %(levelname)s: %(message)s"
        )
        _redact_logs()
        try:
            config_path = _resolve_config_path(args.config)
            config = load_resolved_config(config_path)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return EXIT_CONFIG
        return _run_loaded_config(config_path, config, skip_checks=args.skip_checks)
    return EXIT_CONFIG


def _resolve_config_path(
    explicit: str | None, *, announce_resolution: bool = True
):
    """Tier 1 (--config) wins; else tiers 2-4 via SLEEPTALK_HOME resolution.

    Tier 3 (repo context) is never silent: one line names what was detected
    and that SLEEPTALK_HOME overrides it.
    """
    from pathlib import Path

    from sleeptalk.home import announce, resolve_layout

    if explicit:
        return Path(explicit)
    layout = resolve_layout()
    if announce_resolution:
        print(announce(layout))
    path = layout.root_config
    if not path.is_file():
        print(
            f"{path} not found. Run sleeptalk to set up (it writes this file).",
            file=sys.stderr,
        )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
