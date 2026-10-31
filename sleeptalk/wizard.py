"""Human setup and settings, plus the legacy answers-file interface."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import Callable

from sleeptalk import discord_check, language_support, setup_core
from sleeptalk.builder import build_session, run_connectivity_checks
from sleeptalk.config import ConfigError, SleeptalkConfig, load_resolved_config
from sleeptalk.credential_store import CredentialError
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS

# Preserve the existing import surface for integrations and terminal callers.
from sleeptalk.setup_core import (
    PROFILES_DIR,
    ENV_PATH,
    COMMON_LANGUAGES,
    SAMPLE_LINES,
    _BCP47,
    SurfaceName,
    SURFACES,
    KNOWN_SURFACES,
    SURFACE_TRANSPORT,
    _AUDIO_PRESETS,
    TTS_WIZARD_PROVIDERS,
    STT_WIZARD_PROVIDERS,
    STT_DEFAULT_MODELS,
    _STT_KEY_ENV,
    TTS_MODES,
    ROUTE_TYPES,
    DEFAULT_REALTIME_MODEL,
    DEFAULT_REALTIME_VOICE,
    REALTIME_PROVIDER,
    DEFAULT_SPEECH_MODEL,
    DEFAULT_TRANSCRIPTION_MODEL,
    _default_voice_id,
    DEFAULT_CARTESIA_VOICE,
    DEFAULT_DEEPGRAM_VOICE,
    PRODUCT_AGENTS,
    AGENT_LABELS,
    CONNECTION_REGISTRATIONS,
    SetupAnswers as WizardAnswers,
    default_language,
    sample_line,
    _primary_language,
    is_english,
    language_label,
    language_notes,
    language_support_tables,
    refresh_voice_for_language,
    slugify,
    _PROFILE_SLUG,
    profile_name_slug,
    detect_agents,
    parse_surface,
    parse_route_type,
    voice_engine_choices,
    transport_for_surface,
    surface_for_transport,
    parse_tts_mode,
    launch_command,
    _accepted_url,
    _url_from_file,
    resolve_rendezvous_url,
    resolved_rendezvous_url,
    required_answer_fields,
    system_dep_names,
    missing_runtime_extra,
    ensure_runtime_extra,
    can_auto_install,
    BYO_AGENT_TYPES,
    OPENCLAW_TOKEN_ENV,
    OPENCLAW_MAIN_SESSION,
    OPENCLAW_CONFIG,
    read_openclaw_gateway_token,
    OWN_AGENT_CHOICE,
    OWN_AGENT_KINDS,
    own_agent_settings,
    _first_validation_message,
    _toml_key,
    _toml_string,
    _toml_value,
    agent_menu_entries,
    resolved_token_env,
    _tts_model_line,
    profile_toml,
    env_entries,
    _TTS_KEY_ENV,
    STT_PROVIDER_LABELS,
    TTS_PROVIDER_LABELS,
    STT_KEY_PAGES,
    _STT_DEGRADED_MODELS,
    DEFAULT_ELEVENLABS_VOICE,
    DEFAULT_LOCAL_VOICE,
    DEFAULT_VOICES,
    SPEECH_SERVICES,
    speech_menu_order,
    speech_service_facts,
    speech_service_label,
    found_speech_keys,
    found_key_choices,
    speech_provider_of,
    apply_speech_provider,
    shares_speech_key,
    capability_providers,
    apply_degraded_stt_model,
    load_existing_secrets,
    _profile_refs,
    _stored_secrets,
    _credential_fields,
    agent_token_fields,
    store_agent_tokens,
    _store_credentials,
    _drop_env_keys,
    hydrate_answers_from_env,
    load_existing_answers,
    existing_profiles,
    allocate_slug,
    apply_create_slug,
    _env_line,
    _merge_env_text,
    merge_env_entries,
    store_profile_secrets,
    write_files,
    auto_pick_voice,
    _catalog_voice_pick,
    _prove_selected_voice,
    REQUIRED_ANSWER_FIELDS,
    answers_from_mapping,
    missing_answer_fields,
    hybrid_connectivity_check,
    validate_answers,
)


def _layout():
    """Where this invocation reads/writes state (SLEEPTALK_HOME tiers 2-4)."""
    from sleeptalk.home import resolve_layout

    return resolve_layout()


async def launch_profile(
    profile_path: str | Path,
    *,
    before_run: Callable[[SleeptalkConfig], None] | None = None,
) -> SleeptalkConfig:
    """Launch one freshly configured profile through the production runtime."""
    config = load_resolved_config(profile_path)
    session = build_session(config)
    await run_connectivity_checks(config)
    if before_run is not None:
        before_run(config)
    await session.run()
    return config


EDIT_DONE = "__done__"
EDIT_ALL = "__all__"
LAUNCH_LATER = "__later__"


# Settings rather than interview questions: offered in the edit menu, skipped
# in the linear first-run walk. Terminal audio has a safe default and is asked
# at boot when it is actually knowable, so setup need not spend a question on
# it — but it stays editable for anyone who wants to pin the answer.
from sleeptalk.policy import (  # noqa: E402
    narration_style_or_default,
    reply_length_or_default,
)

SETTINGS_ONLY_SECTIONS = frozenset({"audio", "narration", "reply", "latency"})


async def ask_tailnet_delivery(answers: WizardAnswers) -> None:
    """Offer the tailnet when Tailscale is ready here; remember the answer.

    Asked only for a browser call. The host cannot know whether the device
    the user will call from is on the same tailnet, so it asks instead of
    switching silently. A profile that already chose the tailnet keeps it
    without being asked again.
    """
    import questionary
    from rich.console import Console

    from sleeptalk import tailnet

    if parse_surface(answers.surface) != "web" or answers.delivery in ("tailnet", "local"):
        return
    found = await tailnet.detect()
    if not found.get("available"):
        if found.get("code") == "tailscale_https_disabled":
            Console().print(f"[dim]Tailscale is connected here, but {found.get('message')}[/]")
        return
    picked = await questionary.select(
        f"Tailscale is connected here ({found.get('name')}). Deliver browser calls over your tailnet?",
        choices=[
            questionary.Choice("Yes — private, no relay; the device I call from is on this tailnet", value="tailnet"),
            questionary.Choice("No — use the hosted call link", value=""),
        ],
        default="tailnet",
    ).ask_async()
    if picked is None:
        raise KeyboardInterrupt
    answers.delivery = picked


def interview_section_keys() -> list[str]:
    """Sections the linear first-run walk actually asks."""
    return ["agent", "speech"]


def edit_section_labels() -> list[tuple[str, str]]:
    """Sections an existing profile can jump to. Order matches first-run."""
    return [
        ("surface", "Where it talks (Discord, Web, Terminal)"),
        ("delivery", "Browser call delivery (tailnet or hosted link)"),
        ("audio", "Headphones or speakers (Terminal)"),
        ("narration", "How tool calls sound"),
        ("reply", "How much it says back"),
        ("latency", "Latency logging"),
        ("voice_engine", "Voice engine"),
        ("system", "System dependencies"),
        ("agent", "Which agent runs"),
        ("name", "Bot display name (Discord)"),
        ("token", "Discord bot token"),
        ("ids", "Discord server"),
        ("access", "Who can talk to it (Discord)"),
        ("language", "Spoken language"),
        ("models", "Speech models (listening and speaking)"),
        ("speed", "Playback speed"),
        ("turns", "Turn taking (how long it waits before answering)"),
        ("speech", "Speech service & key (one service for both directions)"),
        ("voice", "Voice"),
    ]


# The settings main menu already offers these; Advanced shows the rest, grouped.
MAIN_MENU_SECTIONS = ("agent", "surface", "speech", "voice")
ADVANCED_SECTION_ORDER = (
    "language", "models", "speed",          # speech: language, then the model per direction
    "narration", "reply", "turns",          # how it talks
    "delivery", "audio", "name", "token", "ids", "access",  # surface specifics: web, terminal, then Discord
    "voice_engine", "latency", "system",   # runtime
)


def advanced_section_labels() -> list[tuple[str, str]]:
    """Advanced settings: every editable section the main menu does not already show."""
    labels = dict(edit_section_labels())
    ordered = [key for key in ADVANCED_SECTION_ORDER if key in labels]
    rest = [key for key, _label in edit_section_labels() if key not in ordered and key not in MAIN_MENU_SECTIONS]
    return [(key, labels[key]) for key in ordered + rest if key not in MAIN_MENU_SECTIONS]


# ---------------------------------------------------------------------------
# Interactive interview (rich + questionary; thin shell over the above)
# ---------------------------------------------------------------------------


#: Sentinel a prompt yields (and a step returns) to move one step back.
_BACK = "__back__"


_BACK_KEYPRESS = object()
BACK_HINT = "Press Esc at any prompt to go back."


async def _ask_text(prompt: str, *, default: str = "", password: bool = False) -> str:
    """Text/password prompt where Esc retreats one wizard step.

    The hint is printed once when setup starts, not on every prompt. Typing
    'back' still works for terminals that swallow Esc.
    """
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    ask = questionary.password if password else questionary.text
    question = ask(prompt, default=default)
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def _go_back(event) -> None:
        event.app.exit(result=_BACK_KEYPRESS)

    question.application.key_bindings = merge_key_bindings([question.application.key_bindings, bindings])
    value = await question.ask_async()
    if value is None:
        raise KeyboardInterrupt
    if value is _BACK_KEYPRESS:
        return _BACK
    value = value.strip()
    return _BACK if value.lower() == "back" else value


async def choose_model_vendor(answers: WizardAnswers, console, store) -> str | None:
    """Model only: pick a vendor, a model, and enter one key privately.

    The key is verified against the vendor, stored under its endpoint scope,
    and only its reference reaches the profile. No tools, no files, no memory
    past the process: that is said here so the first "why can't it read my
    repo" is answered before it is asked.
    """
    import questionary

    from sleeptalk import model_only_setup as setup

    console.print(
        "[dim]Just a model: it talks, it remembers this conversation, and nothing else. "
        "No tools, no files. Pick an agent later when you want those.[/]"
    )
    vendor_choices = [
        *(questionary.Choice(label, value=name) for name, label in setup.VENDORS),
        questionary.Choice("← Back", value=_BACK),
    ]
    current = setup.normalize_vendor(answers.model_vendor) if answers.model_vendor else setup.DEFAULT_VENDOR
    vendor = await questionary.select(
        "Which model vendor is the key from?",
        choices=vendor_choices,
        default=next((c for c in vendor_choices if c.value == current), None),
    ).ask_async()
    if vendor is None:
        raise KeyboardInterrupt
    if vendor == _BACK:
        return _BACK
    base_url = answers.model_base_url if vendor == setup.OTHER_VENDOR else ""
    while True:
        if vendor == setup.OTHER_VENDOR:
            base_url = await _ask_text(
                "HTTPS base URL of the OpenAI-compatible endpoint (ends in /v1 for most):",
                default=base_url,
            )
            if base_url == _BACK:
                return _BACK
        try:
            choice = setup.auth_choice(vendor, base_url)
            break
        except setup.ModelSetupError as exc:
            console.print(f"[red]{exc}[/]")
    same_endpoint = (
        setup.normalize_vendor(answers.model_vendor or setup.DEFAULT_VENDOR) == vendor
        and (vendor != setup.OTHER_VENDOR or answers.model_base_url == base_url)
    )
    remembered = answers.model if same_endpoint and answers.model else ""
    model = ""
    if vendor == setup.OLLAMA_VENDOR:
        installed = await setup.ollama_models(choice.base_url)
        if installed:
            model_choices = [
                *(questionary.Choice(name, value=name) for name in installed),
                questionary.Choice("Type a model name", value="__type__"),
                questionary.Choice("← Back", value=_BACK),
            ]
            model = await questionary.select(
                "Which Ollama model?",
                choices=model_choices,
                default=next((c for c in model_choices if c.value == remembered), None),
            ).ask_async()
            if model is None:
                raise KeyboardInterrupt
            if model == _BACK:
                return _BACK
            if model == "__type__":
                model = ""
        else:
            console.print(f"[yellow]{setup.unreachable_ollama_message(choice.base_url)}[/]")
    if not model:
        model = await _ask_text("Model name:", default=remembered or setup.default_model(vendor))
        if model == _BACK:
            return _BACK
    optional = setup.key_optional(choice)
    keep = same_endpoint and setup.saved_key_usable(store, answers.model_credential_ref, choice)
    console_url = setup.console_url(vendor)
    if console_url:
        console.print(f"[dim]Keys: {console_url}[/]")
    while True:
        if vendor == setup.OLLAMA_VENDOR:
            key = ""  # Ollama takes no key; nothing to ask.
        else:
            prompt = ("API key (leave empty to keep the saved one):" if keep
                      else "API key (optional for a local server; Enter to skip):" if optional
                      else "API key:")
            key = await _ask_text(prompt, password=True)
            if key == _BACK:
                return _BACK
        if not key and keep:
            reference = answers.model_credential_ref
            login = await setup.verify_key(choice, model, store.get(reference))
        elif not key and not optional:
            console.print("[red]A key is required.[/]")
            continue
        else:
            login = await setup.verify_key(choice, model, key)
            if login == "unavailable" and not optional:
                console.print(f"[red]{setup.rejected_message(vendor)}[/]")
                continue
            reference = setup.store_key(store, choice, key)
        if login == "unavailable" and not optional:
            console.print(f"[red]{setup.rejected_message(vendor)}[/]")
            keep = False
            continue
        break
    if login == "available":
        console.print(f"[green]✓ {setup.vendor_name(vendor)}: {model} answers here.[/]")
    elif vendor == setup.OLLAMA_VENDOR:
        console.print(f"[yellow]{setup.missing_ollama_model_message(model)}[/]")
    else:
        console.print(f"[yellow]{setup.unconfirmed_message(model)}[/]")
    answers.model_vendor = vendor
    answers.model_base_url = choice.base_url if vendor == setup.OTHER_VENDOR else ""
    answers.model = model
    answers.model_key = ""
    answers.model_credential_ref = reference
    return None


async def choose_hermes_connection(answers: WizardAnswers, console) -> str | None:
    """Pick Hermes' richer gateway: join a running dashboard, or start one per call.

    Sets agent_type to hermes_gateway either way. Joining keeps the running
    Hermes' sessions and memory; the local gateway uses the Hermes install's
    home, so its memory and settings still apply, and falls back to ACP.
    """
    import questionary

    from sleeptalk import hermes_connect
    from sleeptalk.hermes_connect import (
        DEFAULT_ORIGIN,
        AuthRequired,
        Ready,
        TokenDead,
        probe_message,
    )

    attach_hermes = hermes_connect.attach_hermes

    def joined(probe) -> None:
        console.print(f"[green]✓ Hermes: joining your running Hermes ({probe.origin}); its sessions and memory carry over.[/]")

    # A local dashboard is found and joined without questions: the default port,
    # any `hermes dashboard --port N` that is running, or the profile's saved URL.
    candidates = ([answers.gateway_url] if answers.gateway_url else []) + hermes_connect.running_dashboard_origins()
    probe = None
    for candidate in candidates:
        found = await attach_hermes(answers, raw_url=candidate, token=answers.gateway_session_token)
        if isinstance(found, Ready):
            joined(found)
            return None
        if probe is None or isinstance(found, (AuthRequired, TokenDead)):
            probe = found
    if isinstance(probe, (AuthRequired, TokenDead)):
        console.print(f"[yellow]Hermes is running at {probe.origin} but needs its session token to join.[/]")
        token = await _ask_text(
            "Session token (from the dashboard URL ?token=), or leave empty to start Hermes per call:",
            password=True,
        )
        if token == _BACK:
            return _BACK
        if token:
            probe = await attach_hermes(answers, raw_url=probe.origin, token=token)
            if isinstance(probe, Ready):
                joined(probe)
                return None
            console.print(f"[red]{probe_message(probe)}[/]")
    else:
        choice = await questionary.select(
            "No running Hermes dashboard found. How should Sleeptalk reach Hermes?",
            choices=[
                questionary.Choice("Start Hermes for each call (recommended)", value="local"),
                questionary.Choice("Join a Hermes dashboard at another address", value="remote"),
                questionary.Choice("← Back", value=_BACK),
            ],
        ).ask_async()
        if choice is None:
            raise KeyboardInterrupt
        if choice == _BACK:
            return _BACK
        if choice == "remote":
            url = await _ask_text("Dashboard URL (for example http://127.0.0.1:8899):", default="")
            if url == _BACK:
                return _BACK
            token = answers.gateway_session_token
            while True:
                probe = await attach_hermes(answers, raw_url=url or DEFAULT_ORIGIN, token=token)
                if isinstance(probe, Ready):
                    joined(probe)
                    return None
                console.print(f"[red]{probe_message(probe)}[/]")
                token = await _ask_text("Session token (from the dashboard URL ?token=):", password=True)
                if token == _BACK:
                    return _BACK
    answers.agent_type = "hermes_gateway"
    answers.gateway_url = ""
    answers.gateway_session_token = ""
    console.print(
        "[green]✓ Hermes: Sleeptalk starts Hermes' gateway for each call from your Hermes install, "
        "so its memory and settings apply. It falls back to `hermes acp` if the gateway can't start.[/]"
    )
    return None


async def choose_openclaw_connection(answers: WizardAnswers, console) -> str | None:
    """Prefer OpenClaw's gateway, and offer to continue its main conversation."""
    import questionary

    token = answers.openclaw_token or os.environ.get(OPENCLAW_TOKEN_ENV, "")
    if not token:
        choices = []
        if setup_core.OPENCLAW_CONFIG.expanduser().is_file():
            choices.append(questionary.Choice(f"Read it from {setup_core.OPENCLAW_CONFIG} (recommended)", value="read"))
        choices += [
            questionary.Choice("Paste the gateway token", value="paste"),
            questionary.Choice("Skip: use `openclaw acp`, a fresh conversation each call", value="skip"),
            questionary.Choice("← Back", value=_BACK),
        ]
        how = await questionary.select(
            "Sleeptalk talks to OpenClaw's gateway, which needs its token. How should it get it?",
            choices=choices,
        ).ask_async()
        if how is None:
            raise KeyboardInterrupt
        if how == _BACK:
            return _BACK
        if how == "read":
            token = setup_core.read_openclaw_gateway_token()
            if not token:
                console.print(f"[yellow]No gateway.auth.token found in {setup_core.OPENCLAW_CONFIG}; paste it instead.[/]")
                how = "paste"
        if how == "paste":
            token = await _ask_text("OpenClaw gateway token (stored in .env):", password=True)
            if token == _BACK:
                return _BACK
    answers.openclaw_token = token
    if not token:
        answers.gateway_session_id = ""
        console.print("[green]✓ OpenClaw: using `openclaw acp`; each call starts a fresh conversation.[/]")
        return None
    memory = await questionary.select(
        "What should a call continue?",
        choices=[
            questionary.Choice("Your main OpenClaw conversation — it remembers what you talked about (recommended)", value="main"),
            questionary.Choice("A fresh conversation each call", value="fresh"),
            questionary.Choice("← Back", value=_BACK),
        ],
    ).ask_async()
    if memory is None:
        raise KeyboardInterrupt
    if memory == _BACK:
        return _BACK
    answers.gateway_session_id = OPENCLAW_MAIN_SESSION if memory == "main" else ""
    console.print(
        "[green]✓ OpenClaw: using its gateway; "
        + ("calls continue your main conversation.[/]" if memory == "main" else "each call starts a fresh conversation.[/]")
    )
    return None


async def ask_own_agent(answers: WizardAnswers, console) -> str | None:
    """Configure a bring-your-own agent and check it without sending a prompt."""
    import questionary

    from sleeptalk.adapter_cli import scaffold
    from sleeptalk.config import AgentConfig
    from sleeptalk.conformance.runner import run_adapter_test

    current = answers.agent_options if answers.agent_type in BYO_AGENT_TYPES else {}
    kind = await questionary.select(
        "What kind of agent is it?",
        choices=[
            *(questionary.Choice(label, value=value) for value, label in OWN_AGENT_KINDS),
            questionary.Choice("← Back", value=_BACK),
        ],
    ).ask_async()
    if kind is None:
        raise KeyboardInterrupt
    if kind == _BACK:
        return _BACK
    permission_mode = ""
    token = ""
    if kind == "starter":
        language = await questionary.select(
            "Which language should the starter agent use?",
            choices=[
                questionary.Choice("Python", value="python"),
                questionary.Choice("Node", value="node"),
                questionary.Choice("← Back", value=_BACK),
            ],
        ).ask_async()
        if language is None:
            raise KeyboardInterrupt
        if language == _BACK:
            return _BACK
        target = (_layout().root / "agents" / answers.slug).resolve()
        if target.exists() and any(target.iterdir()):
            console.print(f"[yellow]{target} already exists; using it as is.[/]")
        else:
            from io import StringIO

            scaffold(answers.slug, kind="command", language=language, directory=str(target), out=StringIO())
            console.print(f"[green]✓ created a starter agent in {target}[/]")
        program = "agent.py" if language == "python" else "agent.mjs"
        runner = "python3" if language == "python" else "node"
        command = f"{runner} {target / program}"
        console.print(
            f"[dim]Edit the marked reply function in {target / program} to call your model. "
            "Check it any time with: sleeptalk adapter test --live --config <this profile>[/]"
        )
        kind, env_names, url, token_env = "command", "", "", ""
    else:
        command = env_names = url = token_env = ""
    while True:
        if kind in {"command", "acp"} and not command:
            default = " ".join(current.get("command", [])) if isinstance(current.get("command"), list) else ""
            command = await _ask_text(
                "Command that starts your agent (e.g. node agent.mjs):", default=default
            )
            if command == _BACK:
                return _BACK
            env_names = await _ask_text(
                "Environment variable names it needs, comma-separated (values stay in your environment or .env):",
                default=", ".join(current.get("env", [])) if isinstance(current.get("env"), list) else "",
            )
            if env_names == _BACK:
                return _BACK
        if kind == "acp" and not permission_mode:
            permission_mode = await questionary.select(
                "When the agent wants to run a tool:",
                choices=[
                    questionary.Choice("Ask the caller (pending)", value="pending"),
                    questionary.Choice("Always refuse (deny)", value="deny"),
                    questionary.Choice("← Back", value=_BACK),
                ],
            ).ask_async()
            if permission_mode is None:
                raise KeyboardInterrupt
            if permission_mode == _BACK:
                return _BACK
        if kind in {"http", "bridge"} and not url:
            example = "https://agent.example.com/sleeptalk/turn" if kind == "http" else "wss://agent.example.com/v1/agent-bridge"
            url = await _ask_text(f"Agent URL (e.g. {example}):", default=str(current.get("url", "")))
            if url == _BACK:
                return _BACK
            token_env = await _ask_text(
                "Name of the environment variable holding its token"
                + (" (required):" if kind == "bridge" else " (empty for none):"),
                default=str(current.get("token_env", "") or ("AGENT_BRIDGE_TOKEN" if kind == "bridge" else "")),
            )
            if token_env == _BACK:
                return _BACK
            if token_env and not os.environ.get(token_env):
                token = await _ask_text(f"{token_env} value (stored in .env):", password=True)
                if token == _BACK:
                    return _BACK
        try:
            agent_type, options = own_agent_settings(
                kind, command=command, env_names=env_names, url=url, token_env=token_env
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            command = url = ""
            continue
        if agent_type == "command":
            from sleeptalk.agents.bridge_lite import KNOWN_ACP_COMMANDS, command_setup_hint

            hint = command_setup_hint(options["command"], options.get("protocol", "lite"))
            if hint:
                console.print(f"[yellow]{hint}[/]")
                fix = await questionary.select(
                    "Use the suggested setup?",
                    choices=[
                        questionary.Choice("Yes, use its ACP command (recommended)", value="fix"),
                        questionary.Choice("Keep what I entered", value="keep"),
                        questionary.Choice("← Back", value=_BACK),
                    ],
                ).ask_async()
                if fix is None:
                    raise KeyboardInterrupt
                if fix == _BACK:
                    return _BACK
                if fix == "fix":
                    import shlex

                    program = options["command"][0]
                    suggested = KNOWN_ACP_COMMANDS[os.path.basename(program)][0]
                    command = shlex.join([program, *suggested[1:]])
                    kind = "acp"
                    continue
        check_env = {**os.environ, **({token_env: token} if token_env and token else {})}
        section = AgentConfig(
            type=agent_type,
            workspace_dir=str(Path(answers.workspace_dir or ".").resolve()),
            permission_mode=permission_mode,
            options=options,
        )
        console.print("[dim]Checking the agent (no prompt is sent)...[/]")
        report = await run_adapter_test(
            lambda: AGENTS.get(agent_type).factory(section.model_copy(deep=True), dict(check_env)),
            adapter=agent_type,
            mode="offline",
        )
        for check in report.checks:
            if check.status == "fail":
                console.print(f"[red]✗ {check.title}: {check.detail}[/]")
        if not report.failed:
            console.print("[green]✓ the agent starts and answers the contract checks[/]")
            break
        retry = await questionary.select(
            "The agent didn't pass. What next?",
            choices=[
                questionary.Choice("Change the settings", value="change"),
                questionary.Choice("Keep these settings anyway", value="keep"),
                questionary.Choice("← Back", value=_BACK),
            ],
        ).ask_async()
        if retry is None:
            raise KeyboardInterrupt
        if retry == _BACK:
            return _BACK
        if retry == "keep":
            break
        command = url = ""
        permission_mode = ""
    answers.agent_type = agent_type
    answers.agent_options = options
    answers.agent_permission_mode = permission_mode
    answers.agent_token = token
    return None


def _shipped_sample(provider: str, voice_id: str, language: str) -> Path | None:
    """The recorded sample Sleeptalk ships for this voice in this language, if any."""
    from sleeptalk.settings_page import _SAMPLES, voice_samples

    item = voice_samples(language).get(provider, {}).get(voice_id)
    if not item:
        return None
    path = _SAMPLES / item["url"].removeprefix("/samples/")
    return path if path.is_file() else None


async def _play_sample(answers: WizardAnswers, voice_id: str, console) -> bool:
    """Play a voice: the shipped recording when there is one, else a live line on the key."""
    import shutil
    import tempfile

    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.registries import TTS_PROVIDERS
    from sleeptalk.voice_sample import play_file

    shipped = _shipped_sample(answers.tts_provider, voice_id, answers.language)
    if shipped is not None:
        # play_file removes what it played; the shipped file stays put.
        handle, copy = tempfile.mkstemp(suffix=shipped.suffix)
        os.close(handle)
        shutil.copyfile(shipped, copy)
        if await play_file(Path(copy)):
            return True
        Path(copy).unlink(missing_ok=True)
    register_defaults()
    spec = tts_spec(TTSConfig(provider=answers.tts_provider, model=answers.tts_model, voice_id="pending",
                              language=answers.language))
    sample_spec = ProviderSpec(provider=spec.provider, model=spec.model, voice_id=voice_id,
                               speed=spec.speed, options=spec.options, language=spec.language)
    tts = TTS_PROVIDERS.build(answers.tts_provider, sample_spec, {_TTS_KEY_ENV[answers.tts_provider]: answers.tts_key})
    try:
        path = await tts.synthesize_wav(sample_line(answers.language))
    except Exception as exc:
        console.print(f"[red]sample synthesis failed: {exc}[/]")
        return False
    finally:
        close = getattr(tts, "close", None)
        if callable(close):
            await close()
    if not await play_file(path):
        console.print(
            f"[yellow]no audio player found on this system (tried afplay, ffplay, paplay, aplay). "
            f"The sample was left at {path} — play it manually to hear this voice.[/]"
        )
    return True


async def _audition_voice(answers: WizardAnswers, console) -> str:
    """The settings page's voice list for this service and language; returns a voice_id (or _BACK).

    Recorded samples play with no key; other voices speak a live line.
    """
    import questionary

    from sleeptalk.voice_catalog import catalog_payload, library_url
    from sleeptalk.voice_list import curate, list_voices

    catalog = catalog_payload((answers.tts_provider,), answers.language).get(answers.tts_provider, {})
    listed = [(voice["voice_id"], voice["name"], voice["description"], voice["default"])
              for voice in catalog.get("voices", [])]
    extra = []
    if answers.tts_provider in ("cartesia", "deepgram", "elevenlabs"):
        # These publish a voice list; a few more in the language join the catalog's picks.
        try:
            picks, warning = curate(await list_voices(answers.tts_provider, answers.tts_key), answers.language)
            if warning:
                console.print(f"[yellow]{warning}[/]")
        except Exception as exc:  # noqa: BLE001 — the catalog's voices still stand
            console.print(f"[dim]couldn't fetch more voices ({exc}).[/]")
            picks = []
        known = {voice_id for voice_id, *_rest in listed}
        extra = [(voice.id, voice.name, voice.description or voice.language or "voice", False)
                 for voice in picks if voice.id not in known]
    library = library_url(answers.tts_provider)
    while True:
        choices = [
            questionary.Choice(
                f"{name}{' (current)' if voice_id == answers.tts_voice_id else ''}"
                f"{' (default)' if default and voice_id != answers.tts_voice_id else ''}"
                + (f" — {description}" if description else ""),
                value=voice_id,
            )
            for voice_id, name, description, default in listed + extra
        ] + [
            questionary.Choice(f"Paste a voice ID (browse {library})" if library else "Paste a voice ID", value="__paste__"),
            questionary.Choice("← Back", value=_BACK),
        ]
        picked = await questionary.select(
            "Pick a voice (each plays a sample):",
            choices=choices,
            default=answers.tts_voice_id if any(c.value == answers.tts_voice_id for c in choices) else None,
        ).ask_async()
        if picked is None:
            raise KeyboardInterrupt
        if picked == _BACK:
            return _BACK
        if picked == "__paste__":
            voice_id = await _ask_text("Voice ID:")
            if voice_id == _BACK or not voice_id:
                continue
            return voice_id
        if not await _play_sample(answers, picked, console):
            continue
        keep = await questionary.confirm("Use this voice?", default=True).ask_async()
        if keep is None:
            raise KeyboardInterrupt
        if keep:
            return picked


async def _confirm_local_download(models, console) -> bool:
    """Disclose the missing model transfer before setup starts any download."""
    import questionary
    from sleeptalk.speech.local_models import download_info, models_dir

    missing = [(model, download_info(model)) for model in models]
    missing = [(model, info) for model, info in missing if not info["ready"]]
    if not missing:
        return True
    for model, info in missing:
        console.print(f"{model.label}: {'~' if info['approximate'] else ''}{info['bytes'] / 1_000_000:.0f} MB", markup=False)
    size = sum(info["bytes"] for _, info in missing)
    amount = f"{size / 1_000_000_000:.2f} GB" if size >= 1_000_000_000 else f"{size / 1_000_000:.0f} MB"
    prefix = "~" if any(info["approximate"] for _, info in missing) else ""
    console.print(f"Models download to {models_dir()} on this computer. Runtime installation is additional.", markup=False)
    choice = await questionary.select(
        "Download these local speech models?",
        choices=[questionary.Choice(f"Download and use ({prefix}{amount})", value="download"),
                 questionary.Choice("← Back", value=_BACK)],
    ).ask_async()
    if choice is None:
        raise KeyboardInterrupt
    return choice == "download"


def run_init(*, live: bool = True, profile: str | None = None, settings: bool = False,
             surface: str | None = None, workspace: str | None = None) -> int:
    import sys

    if not sys.stdin.isatty():
        print("Run sleeptalk in a terminal for setup. Agents should use host input start.", file=sys.stderr)
        return 2
    result = asyncio.run(_configure(live=live, profile=profile, settings=settings,
                                   surface=surface, workspace=workspace))
    if isinstance(result, int):
        return result
    from sleeptalk.launcher import launch

    path, destination = result
    return launch(path, surface=destination)


async def _configure(*, live: bool = True, profile: str | None = None, settings: bool = False,
                     surface: str | None = None, workspace: str | None = None) -> int | tuple[Path, str]:
    import questionary
    from rich.console import Console
    from rich.panel import Panel

    from sleeptalk.builder import stt_spec
    from sleeptalk.config import STTConfig
    from sleeptalk.stt_roundtrip import stt_roundtrip

    console = Console()
    register_defaults()

    try:
        from sleeptalk.interactive_profile import InteractiveProfile
        from sleeptalk.runtime_binding import canonical_workspace

        workspace_path = canonical_workspace(workspace or str(Path.cwd()))
        layout = _layout()
        answers = load_existing_answers(profile, layout.profiles_dir, resolved=True) if profile else None
        editing = answers is not None
        if answers is None:
            # A fresh profile starts with one provider and one key.
            detected = default_language()
            answers = WizardAnswers(workspace_dir=str(workspace_path), tts_provider="deepgram",
                                    language=detected,
                                    tts_voice_id=DEFAULT_DEEPGRAM_VOICE if is_english(detected) else "")
            apply_create_slug(answers, layout.profiles_dir)
            hydrate_answers_from_env(answers, os.environ)
        else:
            load_existing_secrets(answers, layout.env_path, layout.profiles_dir / f"{profile}.toml")
        profile_name = profile if editing else answers.slug
        state = InteractiveProfile(layout.profiles_dir / f"{profile_name}.toml", answers)
        if editing:
            from sleeptalk.config import load_resolved_config
            from sleeptalk.runtime_binding import verify
            from sleeptalk.launcher import _workspace

            loaded = load_resolved_config(state.path, check_env=False, check_binaries=False)
            if _workspace(loaded) != workspace_path:
                raise ValueError("This profile belongs to another workspace. Run sleeptalk there to edit it.")
            verify(loaded)
            answers.workspace_dir = str(workspace_path)
        for field, name, provider in (
            ("stt_key", _STT_KEY_ENV.get(answers.stt_provider, ""), answers.stt_provider),
            ("tts_key", _TTS_KEY_ENV.get(answers.tts_provider, ""), answers.tts_provider),
            ("token", resolved_token_env(answers), "discord"),
        ):
            if name and (not getattr(answers, field) or name in state.refs):
                had_reference = name in state.refs
                restored = state.restore(name, provider)
                if restored or had_reference:
                    setattr(answers, field, restored)
        choose_voice = False
        from sleeptalk.launcher import selected_surface

        launch_surface = surface or (selected_surface(workspace_path) if editing else None) or answers.surface
        console.print(f"\nWorkspace: {workspace_path}", markup=False)
        console.print("Choose your agent and enter one speech key. Voice and other options are in sleeptalk settings."
                      if not editing else f"Editing {answers.name}. Save when you're finished.", markup=False)
        console.print(f"[dim]{BACK_HINT}[/]")

        # -- interview steps --------------------------------------------------
        # Each step returns _BACK to retreat one step; anything else advances.
        # Esc (or typing 'back') at a text prompt or picking '← Back' in a menu goes back.

        async def step_surface() -> str | None:
            nonlocal launch_surface
            picked = await questionary.select(
                "Where should the agent talk?",
                choices=[
                    questionary.Choice("Web — browser call link", value="web"),
                    questionary.Choice("Terminal — this machine's mic and speakers", value="terminal"),
                    questionary.Choice("Discord — voice channel in a server", value="discord"),
                    questionary.Choice("← Back", value=_BACK),
                ],
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.surface = picked
            launch_surface = picked
            await ask_tailnet_delivery(answers)
            return None

        async def step_audio() -> str | None:
            if parse_surface(answers.surface) != "terminal":
                return None
            current = (answers.capture_mode or "speakers").strip().lower()
            picked = await questionary.select(
                "Do you listen on headphones or speakers?",
                choices=[
                    questionary.Choice(
                        "Speakers — the mic can hear the agent",
                        value="speakers",
                    ),
                    questionary.Choice(
                        "Headphones — only you can hear the agent",
                        value="headphones",
                    ),
                    questionary.Choice("← Back", value=_BACK),
                ],
                default=(
                    "headphones" if current == "headphones" else "speakers"
                ),
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.capture_mode = picked
            if picked == "headphones":
                console.print(
                    "  [green]Barge-in will use speech detection — "
                    "room noise won't cut the agent off.[/]"
                )
            else:
                console.print(
                    "  [dim]Barge-in stays loudness-based so the agent does not "
                    "interrupt itself through the speakers.[/]"
                )
            return None

        async def step_narration() -> str | None:
            current = narration_style_or_default(answers.narration_style)
            picked = await questionary.select(
                "How should tool calls sound while the agent works?",
                choices=[
                    questionary.Choice(
                        "Narrated — say what it is doing", value="narrated"
                    ),
                    questionary.Choice("Silent — say nothing", value="silence"),
                    questionary.Choice(
                        "Keyboard sounds — play typing instead", value="keyboard"
                    ),
                    questionary.Choice("← Back", value=_BACK),
                ],
                default=current,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.narration_style = picked
            return None

        async def step_reply() -> str | None:
            current = reply_length_or_default(answers.reply_length)
            picked = await questionary.select(
                "How much of a long answer should it say out loud?",
                choices=[
                    questionary.Choice("Full — say the whole answer", value="full"),
                    questionary.Choice(
                        "Concise speech — keep the complete answer in text", value="concise",
                    ),
                    questionary.Choice(
                        "Summarized — say the opening, then the gist",
                        value="summarized",
                    ),
                    questionary.Choice("← Back", value=_BACK),
                ],
                default=current,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.reply_length = picked
            return None

        async def step_speed() -> str | None:
            from sleeptalk.speech_speed import SPEED_STEPS, snap_speed

            current = snap_speed(answers.tts_speed)
            picked = await questionary.select(
                "Playback speed:",
                choices=[questionary.Choice(f"{step}×", value=step) for step in SPEED_STEPS]
                + [questionary.Choice("← Back", value=_BACK)],
                default=current if current in SPEED_STEPS else None,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.tts_speed = picked
            return None

        async def step_turns() -> str | None:
            current = answers.endpointing or "balanced"
            picked = await questionary.select(
                "Turn taking: how long a pause counts as the end of your turn?",
                choices=[
                    questionary.Choice("Patient — waits longer; best if you pause to think", value="relaxed"),
                    questionary.Choice("Balanced", value="balanced"),
                    questionary.Choice("Snappy — answers sooner; may start while you pause", value="aggressive"),
                    questionary.Choice("← Back", value=_BACK),
                ],
                default=current if current in ("relaxed", "balanced", "aggressive") else None,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.endpointing = picked
            return None

        async def step_delivery() -> str | None:
            if parse_surface(answers.surface) != "web":
                console.print("Delivery applies to browser calls; this profile talks elsewhere.")
                return None
            from sleeptalk import tailnet

            found = await tailnet.detect()
            choices = [questionary.Choice("This computer — calls open in a browser here, no relay", value="local"),
                       questionary.Choice("Hosted call link (relay) — works from anywhere, 30 minutes", value="")]
            if found.get("available") or answers.delivery == "tailnet":
                choices.insert(1, questionary.Choice(
                    "Your tailnet — private, no relay; the device you call from must be on it", value="tailnet"))
            elif found.get("message"):
                console.print(f"[dim]Tailnet delivery isn't available: {found.get('message')}[/]")
            picked = await questionary.select(
                "How should browser calls reach you?",
                choices=choices + [questionary.Choice("← Back", value=_BACK)],
                default=answers.delivery if any(c.value == answers.delivery for c in choices) else None,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.delivery = picked
            return None

        async def step_latency() -> str | None:
            picked = await questionary.select(
                "Record how long each turn takes?",
                choices=[
                    questionary.Choice(
                        "On — write a latency trace for every turn", value="on"
                    ),
                    questionary.Choice("Off — no performance traces; basic error logs remain", value="off"),
                    questionary.Choice("← Back", value=_BACK),
                ],
                default="on" if answers.latency_log else "off",
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.latency_log = picked == "on"
            if answers.latency_log:
                console.print(
                    "  [dim]Every surface of this profile writes to the same "
                    "trace file, so their timings can be compared.[/]"
                )
            return None

        async def step_voice_engine() -> str | None:
            choices = [
                questionary.Choice(label, value=value)
                for value, label in voice_engine_choices()
            ] + [questionary.Choice("← Back", value=_BACK)]
            picked = await questionary.select(
                "Voice engine:", choices=choices, default=answers.route_type
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.route_type = parse_route_type(picked)
            if answers.route_type != "hybrid":
                return None
            return await step_realtime()

        async def step_realtime() -> str | None:
            """OpenAI Realtime: one OpenAI key, proved live, then one of its voices."""
            answers.route_type = "hybrid"
            console.print(Panel(
                "OpenAI Realtime hears you and talks back itself, and hands work to your "
                "agent. It starts answering sooner than the other services but costs "
                "more, and it can answer something your agent should have looked up.",
                title="OpenAI Realtime",
            ))
            if not answers.realtime_key:
                answers.realtime_key = state.restore("OPENAI_API_KEY", REALTIME_PROVIDER)
            while True:
                key = await _ask_text(
                    "OpenAI API key (https://platform.openai.com/api-keys):"
                    + (" (Enter keeps current)" if answers.realtime_key else ""),
                    password=True,
                )
                if key == _BACK:
                    return _BACK
                if key:
                    answers.realtime_key = key
                if not answers.realtime_key:
                    continue
                try:
                    await setup_core.hybrid_connectivity_check(answers)
                except Exception:
                    console.print("[red]OpenAI could not validate this key for Realtime. "
                                  "Check the key, billing, and connection.[/]")
                    continue
                state.save_key("OPENAI_API_KEY", REALTIME_PROVIDER, answers.realtime_key)
                console.print("[green]✓ key works — Realtime heard a test clip and OpenAI spoke a line[/]")
                break
            return await pick_realtime_voice()

        async def pick_realtime_voice() -> str | None:
            from sleeptalk.voice_catalog import curated

            voices = curated(REALTIME_PROVIDER)
            picked = await questionary.select(
                "Voice:",
                choices=[questionary.Choice(f"{v.name} — {v.description}", value=v.voice_id) for v in voices]
                + [questionary.Choice("← Back", value=_BACK)],
                default=answers.realtime_voice if answers.realtime_voice in {v.voice_id for v in voices}
                else DEFAULT_REALTIME_VOICE,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.realtime_voice = picked
            return None

        async def step_system() -> str | None:
            from sleeptalk import sysdeps

            needed = set(system_dep_names(answers.surface))
            missing = [dep for dep in sysdeps.missing_deps() if dep.name in needed]
            extra = ensure_runtime_extra(answers.surface)
            if extra:
                console.print(f"[yellow]{extra}[/]")
            if not missing:
                return None
            console.print("\n[bold]System check[/] — installing what's missing:")
            pm = sysdeps.detect_package_manager()
            for dep in missing:
                console.print(f"  • {dep.name} — {dep.purpose}")
                cmd = sysdeps.install_command(dep.name, pm)
                if not can_auto_install(cmd):
                    console.print(f"[yellow]{dep.name}: {sysdeps.manual_hint(dep.name)}[/]")
                    continue
                ok, detail = sysdeps.run_install(cmd)
                still_missing = any(
                    item.name == dep.name for item in sysdeps.missing_deps()
                )
                if ok and not still_missing:
                    console.print(f"[green]✓ {dep.name} installed[/]")
                else:
                    console.print(
                        f"[red]{dep.name} install didn't take ({detail or 'not found after install'}). "
                        f"{sysdeps.manual_hint(dep.name)}[/]"
                    )
            return None

        async def step_own_agent() -> str | None:
            return await ask_own_agent(answers, console)

        async def step_agent() -> str | None:
            from sleeptalk.agents.readiness import probe

            agent_choices = [
                *(
                    questionary.Choice(label, value=name)
                    for label, name in setup_core.agent_menu_entries(ready_first=True)
                ),
                questionary.Choice(
                    "My own agent (preview) — an ACP agent, or one with an adapter you wrote",
                    value=OWN_AGENT_CHOICE,
                ),
                questionary.Choice("← Back", value=_BACK),
            ]
            while True:
                picked = await questionary.select(
                    "\nWhich agent harness do you want to talk to?",
                    choices=agent_choices,
                ).ask_async()
                if picked is None:
                    raise KeyboardInterrupt
                if picked == _BACK:
                    return _BACK
                if picked != "__pick__":
                    break
                console.print("[yellow]Pick a harness from the list. Ready harnesses are labeled, not selected.[/]")
            if picked == OWN_AGENT_CHOICE:
                return await step_own_agent()
            answers.agent_options = {}
            answers.agent_permission_mode = ""
            answers.agent_token = ""
            if picked == "hermes":
                connection = await choose_hermes_connection(answers, console)
                if connection == _BACK:
                    return _BACK
                if answers.gateway_url:
                    return None  # joined a running Hermes; nothing to install here
                picked = "hermes_gateway"
            else:
                answers.gateway_url = ""
                answers.gateway_session_token = ""
            if picked != "openclaw":
                answers.gateway_session_id = ""
                answers.openclaw_token = ""
            if not answers.gateway_url:
                from sleeptalk.agents.readiness import install_missing

                report = probe(picked)
                while not report.ready:
                    for need in report.missing:
                        console.print(f"[yellow]{need.hint}[/]")
                    choices = [questionary.Choice("← Back", value=_BACK)]
                    installable = report.installable()
                    if installable:
                        # Say what "yes" will run before asking for it. Nothing
                        # is installed until the answer comes back.
                        for need in installable:
                            console.print(f"[dim]  will run: {need.command_text}[/]")
                        manual = [need for need in report.missing if need not in installable]
                        for need in manual:
                            console.print(
                                f"[dim]  you install yourself: {need.hint}[/]"
                            )
                        choices.insert(
                            0,
                            questionary.Choice(
                                "Yes, install it for me", value="install"
                            ),
                        )
                        choices.insert(
                            1, questionary.Choice("Check again", value="recheck")
                        )
                    else:
                        console.print(
                            "[dim]Run the line above, then choose Check again.[/]"
                        )
                        choices.insert(
                            0, questionary.Choice("Check again", value="recheck")
                        )
                    retry = await questionary.select(
                        "Install the missing pieces now?",
                        choices=choices,
                    ).ask_async()
                    if retry is None:
                        raise KeyboardInterrupt
                    if retry == _BACK:
                        return _BACK
                    if retry == "install":
                        for line in install_missing(report):
                            if line.startswith("installed "):
                                console.print(f"[green]✓ {line}[/]")
                            else:
                                console.print(f"[red]{line}[/]")
                    report = probe(picked)
            answers.agent_type = picked
            if answers.agent_type == "openclaw":
                console.print("[dim]OpenClaw is a preview.[/]")
                return await choose_openclaw_connection(answers, console)
            if answers.agent_type == "model":
                return await choose_model_vendor(answers, console, state.store)
            return None

        async def step_name() -> str | None:
            while True:
                name = await _ask_text("Bot's display name:", default=answers.name)
                if name == _BACK:
                    return _BACK
                name = name or answers.name
                if editing or profile_name_slug(name):
                    break
                # The settings page's rule: the name becomes the profile and
                # the Discord command, so it needs a letter or digit.
                console.print("[red]Use up to 64 characters with a letter or digit, and not 'example'.[/]")
            answers.name = name
            apply_create_slug(answers, _layout().profiles_dir, editing=editing)
            console.print(
                f"  profile: [cyan]{_layout().profiles_dir / (answers.slug + '.toml')}[/]"
            )
            if parse_surface(answers.surface) == "discord":
                console.print(f"  Discord command: /{answers.slug} join", markup=False)
            return None

        async def step_token(*, reuse: bool = False) -> str | None:
            if parse_surface(answers.surface) != "discord":
                return None
            # The abandonment pit — full guidance budget.
            console.print(Panel(
                "1. Open [link]https://discord.com/developers/applications[/] -> "
                "New Application -> name it\n"
                "2. Bot -> Reset Token -> copy the token (Discord shows it once)\n"
                "\n"
                "That is all. Voice needs no privileged intents, and Sleeptalk reads\n"
                "the application ID and your servers from the token.",
                title="Discord bot token",
            ))
            if reuse and answers.token:
                # A token from an earlier setup (DISCORD_BOT_TOKEN, or a
                # profile of the same name) used to become this bot without a
                # word. Using it is a choice; a new bot gets its own token.
                try:
                    known = await discord_check.check_token(answers.token)
                except discord_check.DiscordCheckError:
                    known = ""
                picked = "new"
                if known:
                    picked = await questionary.select(
                        f"Use the Discord bot you already set up ({known})?",
                        choices=[
                            questionary.Choice(f"Yes, use {known}", value="reuse"),
                            questionary.Choice("No, set up a new bot", value="new"),
                            questionary.Choice("← Back", value=_BACK),
                        ],
                    ).ask_async()
                if picked is None:
                    raise KeyboardInterrupt
                if picked == _BACK:
                    return _BACK
                if picked == "reuse":
                    state.save_key(resolved_token_env(answers), "discord", answers.token)
                    return None
                answers.token = ""
                reuse = False
            while True:
                token = "" if reuse and answers.token else await _ask_text(
                    "Bot token:" + (" (Enter keeps current)" if answers.token else ""),
                    password=True,
                )
                if token == _BACK:
                    return _BACK
                if token:
                    answers.token = token
                reuse = False
                if not answers.token:
                    continue
                try:
                    bot_name = await discord_check.check_token(answers.token)
                except discord_check.DiscordCheckError as exc:
                    console.print(f"[red]{exc}[/]")
                    continue
                console.print(f"[green]✓ token valid — bot: {bot_name}[/]")
                state.save_key(resolved_token_env(answers), "discord", answers.token)
                return None

        async def step_ids() -> str | None:
            if parse_surface(answers.surface) != "discord":
                return None
            try:
                application = await discord_check.fetch_application(answers.token)
                answers.client_id = application.id
                console.print(f"[green]✓ application: {application.name} ({application.id})[/]")
            except discord_check.DiscordCheckError as exc:
                console.print(f"[yellow]{exc}[/]")
                client_id = await _ask_text(
                    "Application ID (General Information -> Application ID):",
                    default=answers.client_id,
                )
                if client_id == _BACK:
                    return _BACK
                answers.client_id = client_id
            try:
                guilds = await discord_check.list_guilds(answers.token)
            except discord_check.DiscordCheckError as exc:
                console.print(f"[yellow]{exc}[/]")
                guilds = []
            if len(guilds) == 1:
                answers.guild_id = guilds[0].id
                console.print(
                    f"[green]✓ one server: {guilds[0].name} ({guilds[0].id})[/]"
                )
            elif guilds:
                picked = await questionary.select(
                    "Which server should the bot join?",
                    choices=[
                        *(
                            questionary.Choice(f"{guild.name} ({guild.id})", value=guild.id)
                            for guild in guilds
                        ),
                        questionary.Choice("← Back", value=_BACK),
                    ],
                    # A saved server the bot has since left is no longer a choice.
                    default=answers.guild_id if any(g.id == answers.guild_id for g in guilds) else guilds[0].id,
                ).ask_async()
                if picked is None:
                    raise KeyboardInterrupt
                if picked == _BACK:
                    return _BACK
                answers.guild_id = picked
            else:
                guild = await _ask_text(
                    "Server (guild) ID to join — right-click your server -> Copy Server ID "
                    "(needs Developer Mode on):",
                    default=answers.guild_id,
                )
                if guild == _BACK:
                    return _BACK
                answers.guild_id = guild

            if answers.guild_id:
                invite = discord_check.invite_url(answers.client_id)
                console.print(f"\nInvite the bot: [link]{invite}[/]")
                while True:
                    joined = await questionary.confirm(
                        "Opened the link and added the bot to your server?", default=True
                    ).ask_async()
                    if joined is None:
                        raise KeyboardInterrupt
                    if not joined:
                        continue
                    try:
                        await discord_check.check_guild_membership(
                            answers.token, answers.guild_id, answers.client_id
                        )
                        console.print("[green]✓ bot has joined the server[/]")
                        break
                    except discord_check.DiscordCheckError as exc:
                        console.print(f"[red]{exc}[/]")

            while True:
                try:
                    await discord_check.probe_gateway_intents(
                        answers.token, discord_check.requested_intents_value_for()
                    )
                    console.print("[green]✓ gateway accepted the bot (no privileged intents needed)[/]")
                    break
                except discord_check.DisallowedIntentsError as exc:
                    console.print(f"[red]{exc}[/]")
                    retry = await questionary.confirm(
                        "Enabled the intent and saved? Probe again.",
                        default=True,
                    ).ask_async()
                    if retry is None:
                        raise KeyboardInterrupt
                    if not retry:
                        return _BACK
                except discord_check.DiscordCheckError as exc:
                    console.print(f"[yellow]{exc}[/]")
                    break
            return None

        async def step_access() -> str | None:
            """Who may talk to the bot. Default is a closed list: you, in one channel."""
            if parse_surface(answers.surface) != "discord":
                return None
            picked = await questionary.select(
                "Who can talk to this bot?",
                choices=[
                    questionary.Choice("Only me, in one voice channel (recommended)", value="restricted"),
                    questionary.Choice("Anyone who can join its voice channel", value="open"),
                    questionary.Choice("← Back", value=_BACK),
                ],
                default="restricted" if answers.authorization_required or not editing else "open",
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            if picked == "open":
                answers.authorization_required = False
                answers.operator_ids = ""
                answers.allowed_channel_ids = ""
                console.print(
                    "[yellow]Anyone in the server can summon the bot and talk to your agent. "
                    "Change this later under Advanced settings.[/]"
                )
                return None
            console.print(
                "Your Discord user ID: Discord -> Settings -> Advanced -> Developer Mode on, "
                "then right-click your name -> Copy User ID.", markup=False,
            )
            while True:
                user_id = await _ask_text("Your Discord user ID:", default=answers.operator_ids)
                if user_id == _BACK:
                    return _BACK
                if user_id.isdecimal():
                    break
                console.print("[red]A Discord user ID is a number, for example 123456789012345678.[/]")
            answers.operator_ids = user_id
            channels: list = []
            if answers.token and answers.guild_id:
                try:
                    channels = await discord_check.list_voice_channels(answers.token, answers.guild_id)
                except discord_check.DiscordCheckError as exc:
                    console.print(f"[yellow]{exc}[/]")
            if channels:
                picked_channel = await questionary.select(
                    "Which voice channel may it be used in?",
                    choices=[
                        *(
                            questionary.Choice(
                                channel.name if channel.eligible
                                else f"{channel.name} (bot lacks: {', '.join(channel.missing_permissions)})",
                                value=channel.id,
                            )
                            for channel in channels
                        ),
                        questionary.Choice("← Back", value=_BACK),
                    ],
                    # The saved value may name a deleted channel, or several.
                    default=next((c.id for c in channels if c.id == answers.allowed_channel_ids), None) or next(
                        (channel.id for channel in channels if channel.eligible), channels[0].id
                    ),
                ).ask_async()
                if picked_channel is None:
                    raise KeyboardInterrupt
                if picked_channel == _BACK:
                    return _BACK
                answers.allowed_channel_ids = picked_channel
            else:
                channel_id = await _ask_text(
                    "Voice channel ID (right-click the channel -> Copy Channel ID):",
                    default=answers.allowed_channel_ids,
                )
                if channel_id == _BACK:
                    return _BACK
                answers.allowed_channel_ids = channel_id
            answers.authorization_required = True
            console.print(
                "Only you can summon it, and only from that channel's chat. "
                "Add more people in the profile under operator_ids, speaker_ids, and listener_ids.",
                markup=False,
            )
            return None

        async def step_language() -> str | None:
            previous_language = answers.language
            detected_language = default_language()
            language_choices = [
                questionary.Choice(label, value=tag) for tag, label in COMMON_LANGUAGES
            ] + [
                questionary.Choice("Other (type a BCP-47 tag)", value="__other__"),
                questionary.Choice("← Back", value=_BACK),
            ]
            known = dict(COMMON_LANGUAGES)
            default_tag = (answers.language if editing and answers.language in known
                           else detected_language if detected_language in known else "en-US")
            picked = await questionary.select(
                "What language will you speak to it?", choices=language_choices, default=default_tag
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            if picked == "__other__":
                typed = await _ask_text(
                    "BCP-47 tag (e.g. es-ES, ja-JP):", default=detected_language
                )
                if typed == _BACK:
                    return _BACK
                answers.language = typed if _BCP47.match(typed) else "en-US"
            else:
                answers.language = picked
            providers = capability_providers(answers) if answers.route_type == "cascade" else []
            for warning in language_support.warnings_for(answers.language, providers):
                console.print(f"[yellow]⚠ {warning}[/]")
            if answers.route_type == "cascade":
                from sleeptalk import speech_catalog as catalog
                from sleeptalk.speech_choice import SpeechChoice, follow_language

                if catalog.recommended(answers.tts_provider, catalog.TTS, previous_language) is not None:
                    # The settings page's rules: models and voices that followed
                    # the old language move; ones picked by hand stay if they fit.
                    moved, changed = follow_language(SpeechChoice(
                        answers.stt_provider, answers.stt_model, answers.tts_provider,
                        answers.tts_model, answers.tts_voice_id, previous_language), answers.language)
                    answers.stt_model, answers.tts_model, answers.tts_voice_id = moved.stt_model, moved.tts_model, moved.voice
                    if changed:
                        console.print(f"[cyan]Moved for {language_label(answers.language)}: {', '.join(changed)}.[/]")
                else:
                    for note in apply_degraded_stt_model(answers):
                        console.print(f"[yellow]{note}[/]")
                    refresh_voice_for_language(answers, previous_language)
                for note in language_notes(answers):
                    console.print(f"[cyan]{note}[/]")
            return None

        async def step_local_speech(*, prove_stt: bool = True, prove_voice: bool = True) -> str | None:
            """Local speech: install the runtime, fetch the models, prove a turn. No key."""
            from sleeptalk import sysdeps
            from sleeptalk.speech import local_models

            needed = []
            if answers.stt_provider == "local":
                needed.append(local_models.get_model(answers.stt_model or local_models.DEFAULT_STT_MODEL, "stt"))
            if answers.tts_provider == "local":
                needed.append(local_models.resolve_tts_model(answers.tts_model, answers.tts_voice_id))
            if not await _confirm_local_download(needed, console):
                return _BACK
            streaming = (answers.stt_provider == "local" and local_models.get_model(
                answers.stt_model or local_models.DEFAULT_STT_MODEL, "stt").runtime == "moonshine")
            def missing_runtime():
                return sysdeps.missing_local_extra() or (sysdeps.missing_streaming_extra() if streaming else None)
            command = sysdeps.streaming_extra_install_command if streaming else sysdeps.local_extra_install_command
            installer = sysdeps.install_streaming_extra if streaming else sysdeps.install_local_extra
            extra = missing_runtime()
            while extra:
                console.print(f"[yellow]{extra}[/]")
                console.print(f"[dim]  will run: {' '.join(command())}[/]")
                choice = await questionary.select(
                    "Install the local speech runtime now?",
                    choices=[
                        questionary.Choice("Yes, install it for me", value="install"),
                        questionary.Choice("Check again", value="recheck"),
                        questionary.Choice("← Back", value=_BACK),
                    ],
                ).ask_async()
                if choice is None:
                    raise KeyboardInterrupt
                if choice == _BACK:
                    return _BACK
                extra = installer() if choice == "install" else missing_runtime()
                if extra is None:
                    console.print("[green]✓ local speech runtime installed[/]")
            for model in needed:
                if local_models.is_ready(model):
                    continue
                console.print(
                    f"Downloading {model.label} ({model.size_mb} MB, {model.license}) into {local_models.models_dir()}"
                )
                reported = {"decile": -1}

                def progress(done: int, total: int, reported=reported) -> None:
                    decile = min(10, done * 10 // max(total, 1))
                    if decile > reported["decile"]:
                        reported["decile"] = decile
                        console.print(f"[dim]  {decile * 10}%[/]")

                try:
                    await asyncio.to_thread(local_models.ensure_model, model, progress=progress)
                except local_models.LocalModelError as exc:
                    console.print(f"[red]{exc}[/]")
                    return _BACK
                console.print(f"[green]✓ {model.label} ready[/]")
            try:
                if prove_stt and answers.stt_provider == "local":
                    transcript, latency_ms = await stt_roundtrip(
                        "local",
                        stt_spec(STTConfig(provider="local", model=answers.stt_model, language=answers.language)),
                        "",
                    )
                    console.print(f"[green]✓ local listening works — transcribed a test clip in {latency_ms:.0f} ms: {transcript!r}[/]")
                if prove_voice and answers.tts_provider == "local":
                    sample = await setup_core._prove_selected_voice(answers)
                    sample.unlink(missing_ok=True)
                    console.print(f"[green]✓ local voice {answers.tts_voice_id!r} synthesized[/]")
            except Exception as exc:
                console.print(f"[red]Local speech could not run a test turn: {exc}[/]")
                return _BACK
            return None

        async def step_local_voice() -> str | None:
            """Pick one of the local voices; a Kokoro voice pulls Kokoro on first use."""
            from sleeptalk.speech import local_models
            from sleeptalk.voice_list import list_local_voices

            choices = [
                *(questionary.Choice(f"{voice.name} — {voice.description}", value=voice.id) for voice in list_local_voices()),
                questionary.Choice("← Back", value=_BACK),
            ]
            picked = await questionary.select(
                "Which local voice?",
                choices=choices,
                default=next((c for c in choices if c.value == answers.tts_voice_id), None),
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            answers.tts_provider = "local"
            answers.tts_key = ""
            answers.tts_voice_id = picked
            answers.tts_model = local_models.resolve_tts_model("", picked).id
            answers.tts_mode = "known"
            return await step_local_speech(prove_stt=False)

        language_confirmed = editing

        async def confirm_language() -> str | None:
            """First run only: the OS locale is the default, shown once, one keypress to keep."""
            nonlocal language_confirmed
            if language_confirmed:
                return None
            language_confirmed = True
            if is_english(answers.language):
                console.print(
                    f"Speaking {language_label(answers.language)} (from your system). "
                    "Change it under Advanced settings → Spoken language."
                )
                return None
            picked = await questionary.select(
                f"Your system is set to {language_label(answers.language)} ({answers.language}). Speak to it in that language?",
                choices=[
                    questionary.Choice(f"Yes, {language_label(answers.language)}", value="keep"),
                    questionary.Choice("Pick another language", value="pick"),
                ],
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == "pick":
                return await step_language()
            return None

        async def step_speech(*, reuse: bool = False) -> str | None:
            """One service and one key for listening and speaking, as on the settings page."""
            if await confirm_language() == _BACK:
                return _BACK
            realtime = answers.route_type == "hybrid"
            current = "realtime" if realtime else speech_provider_of(answers)
            found = found_speech_keys()
            facts = speech_service_facts()
            choices = [
                *found_key_choices(found, questionary.Choice),
                # Local right under any found key: no key, nothing to sign up for.
                *(questionary.Choice(speech_service_label(name, facts), value=name) for name in speech_menu_order()),
                questionary.Choice("OpenAI Realtime — one OpenAI key, answers fastest, billed per minute", value="realtime"),
                questionary.Choice("← Back", value=_BACK),
            ]
            picked = await questionary.select(
                "Speech service (listening and speaking):",
                choices=choices,
                # A profile with separate listening and speaking services has no single current one.
                default=current if any(choice.value == current for choice in choices) else None,
            ).ask_async()
            if picked is None:
                raise KeyboardInterrupt
            if picked == _BACK:
                return _BACK
            if picked == "realtime":
                return await step_realtime()
            if realtime:
                # Leaving Realtime: listening and speaking go back to a speech service.
                answers.route_type = "cascade"
            found_key = None
            if picked.startswith("found:"):
                found_key = found[picked.removeprefix("found:")]
                picked = found_key.provider
            if picked != current:
                for note in apply_speech_provider(answers, picked):
                    console.print(f"[yellow]{note}[/]")
                answers.stt_key = answers.tts_key = "" if picked == "local" else state.restore(_STT_KEY_ENV[picked], picked)
            if found_key is not None:
                # Chosen by the person: the wizard reads it now, once, validates
                # it like any typed key, and stores a copy in the private store.
                # The original file is left as it is.
                from sleeptalk.key_discovery import read_value

                answers.stt_key = answers.tts_key = read_value(found_key)
                reuse = bool(answers.stt_key)
                if not reuse:
                    console.print(f"[yellow]The {found_key.provider} key at {found_key.source} is empty; paste one instead.[/]")
            answers.speech_provider = picked
            for note in language_notes(answers):
                console.print(f"[cyan]{note}[/]")
            if picked == "local":
                return await step_local_speech()
            label = STT_PROVIDER_LABELS[picked]
            fact = facts.get(picked, {})
            console.print(Panel("\n".join(line for line in (
                str(fact.get("free") or ""), str(fact.get("card") or ""), f"Get a key: {STT_KEY_PAGES[picked]}",
            ) if line), title=f"{label} (one key for listening and speaking)"))
            while True:
                key = "" if reuse and answers.stt_key else await _ask_text(
                    f"{label} API key:" + (" (Enter keeps current)" if answers.stt_key else ""),
                    password=True,
                )
                if key == _BACK:
                    return _BACK
                if key:
                    answers.stt_key = answers.tts_key = key
                reuse = False
                if not answers.stt_key:
                    continue
                try:
                    transcript, latency_ms = await stt_roundtrip(
                        picked,
                        stt_spec(STTConfig(provider=picked, model=answers.stt_model, language=answers.language)),
                        answers.stt_key,
                    )
                    if not answers.tts_voice_id.strip():
                        # A non-English language left the voice to the pick.
                        notes, failure = await auto_pick_voice(answers)
                        if failure:
                            raise RuntimeError(failure)
                        for note in notes:
                            console.print(f"[yellow]{note}[/]")
                    sample = await setup_core._prove_selected_voice(answers)
                except Exception:
                    console.print(f"[red]{label} could not validate this key. Check the key, quota, and connection.[/]")
                    continue
                sample.unlink(missing_ok=True)
                state.save_key(_STT_KEY_ENV[picked], picked, answers.stt_key)
                console.print(
                    f"[green]✓ key works — transcribed a test clip in {latency_ms:.0f} ms: {transcript!r}, "
                    "and the voice synthesized[/]"
                )
                return None

        async def step_stt(*, reuse: bool = False, choose_provider: bool = False) -> str | None:
            """First run keeps the default provider; settings offers the choice."""
            if answers.route_type == "hybrid":
                return None
            provider = answers.stt_provider
            if choose_provider:
                provider = await questionary.select(
                    "Speech-to-text provider:",
                    choices=[
                        questionary.Choice("Deepgram (nova-3)", value="deepgram"),
                        questionary.Choice("Cartesia (ink-2)", value="cartesia"),
                        questionary.Choice("ElevenLabs (Scribe, batch)", value="elevenlabs"),
                        questionary.Choice("Local (Moonshine, no key)", value="local"),
                        questionary.Choice("← Back", value=_BACK),
                    ],
                    default=answers.stt_provider,
                ).ask_async()
                if provider is None:
                    raise KeyboardInterrupt
                if provider == _BACK:
                    return _BACK
            if provider == "local":
                answers.stt_provider = "local"
                answers.stt_model = STT_DEFAULT_MODELS["local"]
                answers.stt_key = ""
                return await step_local_speech(prove_voice=False)
            if provider != answers.stt_provider:
                answers.stt_provider = provider
                answers.stt_model = STT_DEFAULT_MODELS[provider]
                answers.stt_key = state.restore(_STT_KEY_ENV[provider], provider) or (
                    # The voice provider's key already covers this one.
                    answers.tts_key if _TTS_KEY_ENV.get(answers.tts_provider) == _STT_KEY_ENV[provider] else ""
                )
                for note in apply_degraded_stt_model(answers):
                    console.print(f"[yellow]{note}[/]")
            label = STT_PROVIDER_LABELS[answers.stt_provider]
            console.print(Panel(
                f"Get a key: {STT_KEY_PAGES[answers.stt_provider]}",
                title="Speech-to-text",
            ))
            while True:
                key = "" if reuse and answers.stt_key else await _ask_text(
                    f"{label} API key:"
                    + (" (Enter keeps current)" if answers.stt_key else ""),
                    password=True,
                )
                if key == _BACK:
                    return _BACK
                if key:
                    answers.stt_key = key
                reuse = False
                if not answers.stt_key:
                    continue
                try:
                    transcript, latency_ms = await stt_roundtrip(
                        answers.stt_provider,
                        stt_spec(STTConfig(provider=answers.stt_provider, model=answers.stt_model, language=answers.language)),
                        answers.stt_key,
                    )
                except Exception:
                    console.print(f"[red]{label} could not validate this key. Check the key, quota, and connection.[/]")
                    continue
                state.save_key(_STT_KEY_ENV[answers.stt_provider], answers.stt_provider, answers.stt_key)
                console.print(
                    f"[green]✓ key works — transcribed a test clip in {latency_ms:.0f} ms: {transcript!r}[/]"
                )
                return None

        async def step_tts_key(*, reuse: bool = False) -> str | None:
            if answers.route_type == "hybrid":
                return None
            if answers.tts_provider == "local":
                answers.tts_key = ""  # nothing to ask: local voices take no key
                return None
            key_pages = {
                "cartesia": "https://play.cartesia.ai -> API Keys",
                "deepgram": "https://console.deepgram.com -> API Keys",
                "elevenlabs": "https://elevenlabs.io -> Developers -> API Keys",
                "playht": "https://play.ht -> API Access",
                "rime": "the Rime developer dashboard",
                "hume": "https://platform.hume.ai -> API Keys",
            }
            if shares_speech_key(answers) and answers.stt_key:
                # One provider for both directions: the validated STT key is the voice key.
                answers.tts_key = answers.stt_key
                console.print(
                    f"Reusing your {STT_PROVIDER_LABELS[answers.stt_provider]} key for voice "
                    "(change it under Speech service & key)."
                )
                reuse = True
            while True:
                if answers.tts_provider == "playht":
                    user_id = await _ask_text(
                        "PlayHT user ID:"
                        + (" (Enter keeps current)" if answers.tts_user_id else ""),
                        password=True,
                    )
                    if user_id == _BACK:
                        return _BACK
                    if user_id:
                        answers.tts_user_id = user_id
                key = "" if reuse and answers.tts_key else await _ask_text(
                    f"{answers.tts_provider.title()} API key "
                    f"({key_pages.get(answers.tts_provider, 'provider dashboard')})"
                    + (" (Enter keeps current)" if answers.tts_key else "")
                    + ":",
                    password=True,
                )
                if key == _BACK:
                    return _BACK
                if key:
                    answers.tts_key = key
                reuse = False
                if not answers.tts_key:
                    continue
                mode = answers.tts_mode.strip() or ("known" if answers.tts_voice_id else "explore")
                if choose_voice and mode == "explore":
                    voice_id = await _audition_voice(answers, console)
                    if voice_id == _BACK:
                        return _BACK
                    answers.tts_voice_id = voice_id
                    answers.tts_model = ""
                elif choose_voice:
                    voice_id = await _ask_text(
                        "Voice ID:",
                        default=answers.tts_voice_id,
                    )
                    if voice_id == _BACK:
                        return _BACK
                    if voice_id:
                        answers.tts_voice_id = voice_id
                    if not answers.tts_voice_id:
                        continue
                    answers.tts_model = ""
                try:
                    path = await setup_core._prove_selected_voice(answers)
                except Exception:
                    console.print("[red]The voice provider could not validate this key and voice. Check the key, quota, and connection.[/]")
                    if not choose_voice:
                        repair = await questionary.select("Try again or choose another voice?", choices=[
                            questionary.Choice("Try another key", value="key"),
                            questionary.Choice("Choose a voice", value="voice"),
                            questionary.Choice("← Back", value=_BACK),
                        ]).ask_async()
                        if repair is None:
                            raise KeyboardInterrupt
                        if repair == _BACK:
                            return _BACK
                        if repair == "voice":
                            if await step_voice() == _BACK:
                                return _BACK
                    continue
                name = _TTS_KEY_ENV.get(answers.tts_provider)
                if name:
                    state.save_key(name, answers.tts_provider, answers.tts_key)
                path.unlink(missing_ok=True)
                console.print("[green]✓ credentials and selected voice work[/]")
                return None

        async def step_voice() -> str | None:
            """A voice from the service in use, as on the settings page's Voice page.

            The service itself is chosen under Speech; listening and speaking
            share it, so there is no provider to pick here.
            """
            nonlocal choose_voice
            if answers.route_type == "hybrid":
                return await pick_realtime_voice()
            if answers.tts_provider == "local":
                return await step_local_voice()
            env_name = _TTS_KEY_ENV.get(answers.tts_provider, "")
            if not answers.tts_key and env_name:
                answers.tts_key = state.restore(env_name, answers.tts_provider)
            if not answers.tts_key:
                console.print(f"{TTS_PROVIDER_LABELS.get(answers.tts_provider, answers.tts_provider)} has no key "
                              "saved yet; set up the speech service first.")
                return await step_speech()
            from sleeptalk.speech_choice import SpeechChoice, with_voice

            while True:
                voice_id = await _audition_voice(answers, console)
                if voice_id == _BACK:
                    return _BACK
                before = (answers.tts_voice_id, answers.tts_model)
                moved = with_voice(SpeechChoice(answers.stt_provider, answers.stt_model, answers.tts_provider,
                                                answers.tts_model, answers.tts_voice_id, answers.language), voice_id)
                answers.tts_voice_id, answers.tts_model = moved.voice, moved.tts_model
                try:
                    sample = await setup_core._prove_selected_voice(answers)
                except Exception:
                    answers.tts_voice_id, answers.tts_model = before
                    console.print("[red]That voice didn't synthesize with your key. Pick another, or check the voice ID.[/]")
                    continue
                sample.unlink(missing_ok=True)
                choose_voice = True
                answers.tts_mode = "known"
                console.print(f"[green]✓ voice set: {answers.tts_voice_id}[/]")
                return None

        async def step_models() -> str | None:
            """Advanced: the listening and speaking model, from the settings page's catalog.

            The language already resolved a model; this is where a user trades
            it (a cheaper or multilingual one, say). Picking a speaking model
            with its own voices moves the voice to one it can speak.
            """
            if answers.route_type == "hybrid":
                console.print("OpenAI Realtime runs one model; its models live under [route] in the profile.")
                return None
            from sleeptalk import speech_catalog as catalog
            from sleeptalk.builder import stt_model_for_provider, tts_model_for_provider
            from sleeptalk.speech_choice import SpeechChoice, tts_option, with_model

            choice = SpeechChoice(answers.stt_provider, answers.stt_model, answers.tts_provider,
                                  answers.tts_model, answers.tts_voice_id, answers.language)
            for role, provider, title in (("stt", answers.stt_provider, "Listening model"),
                                          ("tts", answers.tts_provider, "Speaking model")):
                models = [m for m in catalog.payload(provider, role, answers.language)["models"] if m.get("usable", True)]
                if not models:
                    continue
                if role == "stt":
                    current = stt_model_for_provider(provider, choice.stt_model)
                else:
                    stored = tts_model_for_provider(provider, choice.tts_model)
                    option = tts_option(provider, stored, choice.voice)
                    current = option.id if option is not None else stored

                def label(model: dict) -> str:
                    facts = [model.get("label") or model["id"]]
                    download = model.get("download")
                    if download:
                        size = download["bytes"] / 1_000_000
                        amount = f"{size / 1000:.2f} GB" if size >= 1000 else f"{size:.0f} MB"
                        facts.append("downloaded" if download["ready"] else
                                     f"{'~' if download['approximate'] else ''}{amount} download")
                    if model.get("recommended"):
                        facts.append(f"recommended for {language_label(answers.language)}")
                    if model.get("covers_language") is False:
                        facts.append(f"doesn't cover {language_label(answers.language)}")
                    if isinstance(model.get("usd_per_hour"), (int, float)):
                        facts.append(f"${model['usd_per_hour']:.2f}/h")
                    return " — ".join(facts)

                picked = await questionary.select(
                    f"{title} ({TTS_PROVIDER_LABELS.get(provider, provider)}):",
                    choices=[questionary.Choice(label(model), value=model["id"]) for model in models]
                    + [questionary.Choice("← Back", value=_BACK)],
                    default=current if any(model["id"] == current for model in models) else None,
                ).ask_async()
                if picked is None:
                    raise KeyboardInterrupt
                if picked == _BACK:
                    return _BACK
                if picked != current:
                    choice = with_model(choice, role, picked)
            moved_voice = choice.voice != answers.tts_voice_id
            answers.stt_model, answers.tts_model, answers.tts_voice_id = choice.stt_model, choice.tts_model, choice.voice
            if moved_voice:
                console.print(f"[yellow]That speaking model can't use your voice; the voice is now {choice.voice}.[/]")
            return None

        step_by_id = {
            "surface": step_surface,
            "audio": step_audio,
            "narration": step_narration,
            "reply": step_reply,
            "latency": step_latency,
            "voice_engine": step_voice_engine,
            "system": step_system,
            "agent": step_agent,
            "name": step_name,
            "token": step_token,
            "ids": step_ids,
            "access": step_access,
            "language": step_language,
            "models": step_models,
            "speech": step_speech,
            "stt": lambda: step_stt(choose_provider=True),
            "voice": step_voice,
            "tts_key": step_tts_key,
            "speed": step_speed,
            "turns": step_turns,
            "delivery": step_delivery,
        }
        async def edit_settings() -> None:
            nonlocal answers, choose_voice, launch_surface
            from copy import deepcopy

            while True:
                picked = await questionary.select(
                    "What do you want to change?",
                    choices=[
                        questionary.Choice("Agent", value="agent"),
                        questionary.Choice("Call destination", value="surface"),
                        questionary.Choice("Speech service & key", value="speech"),
                        questionary.Choice("Voice", value="voice"),
                        questionary.Choice("Advanced settings", value="advanced"),
                        questionary.Choice("Save settings" if editing else "Back to start call", value=EDIT_DONE),
                    ],
                ).ask_async()
                if picked is None:
                    raise KeyboardInterrupt
                if picked == EDIT_DONE:
                    return
                if picked == "advanced":
                    picked = await questionary.select(
                        "Advanced settings",
                        choices=[questionary.Choice(label, value=key) for key, label in advanced_section_labels()]
                        + [questionary.Choice("Back", value=_BACK)],
                    ).ask_async()
                    if picked is None:
                        raise KeyboardInterrupt
                    if picked == _BACK:
                        continue
                before = deepcopy(answers)
                before_refs = dict(state.refs)
                previous_voice_choice, previous_surface = choose_voice, launch_surface
                result = await step_by_id[picked]()
                if result == _BACK:
                    answers, state.refs = before, before_refs
                    choose_voice, launch_surface = previous_voice_choice, previous_surface

        if editing and settings:
            await edit_settings()
        elif not editing:
            # The interview ends at the call action; preferences stay in settings.
            going_back = False
            quick_by_id = {**step_by_id, "speech": lambda: step_speech(reuse=not going_back),
                           "stt": lambda: step_stt(reuse=not going_back), "tts_key": lambda: step_tts_key(reuse=not going_back)}
            quick_steps = [quick_by_id[key] for key in interview_section_keys()]
            index = 0
            while index < len(quick_steps):
                if await quick_steps[index]() == _BACK:
                    index -= 1
                    going_back = True
                    if index < 0:
                        raise KeyboardInterrupt
                else:
                    index += 1
                    going_back = False

        if not settings and surface is None:
            while True:
                console.print("Ready. Using your selected voice." if choose_voice else "Ready. Using the default Sleeptalk voice.")
                picked = await questionary.select("Start a call", choices=[
                    questionary.Choice("Start browser call", value="web"),
                    questionary.Choice("Start terminal call", value="terminal"),
                    questionary.Choice("Set up Discord", value="discord"),
                    questionary.Choice("Change settings", value="settings"),
                ], default=launch_surface).ask_async()
                if picked is None:
                    raise KeyboardInterrupt
                if picked == "settings":
                    await edit_settings()
                    continue
                launch_surface = picked
                answers.surface = picked
                await ask_tailnet_delivery(answers)
                break
        elif surface is not None:
            answers.surface = surface
            launch_surface = surface

        async def set_up_discord() -> None:
            # A new profile's bot is named here: the name is its slash command.
            answers.surface = "discord"
            for step in (*(() if editing else (step_name,)), lambda: step_token(reuse=True), step_ids, step_access):
                if await step() == _BACK:
                    raise KeyboardInterrupt

        if launch_surface == "discord" and not (answers.token and answers.guild_id and answers.client_id):
            await set_up_discord()

        if answers.route_type == "hybrid" and live:
            try:
                await setup_core.hybrid_connectivity_check(answers)
            except Exception:
                console.print("Realtime validation failed. Check your key, model, and connection.")
                return 1
        # Settings can switch back from the research route or replace a lost key.
        if answers.route_type == "cascade":
            if not answers.stt_key and await step_stt() == _BACK:
                raise KeyboardInterrupt
            if not answers.tts_voice_id:
                if answers.tts_provider == "cartesia":
                    answers.tts_voice_id = DEFAULT_CARTESIA_VOICE
                elif answers.tts_provider == "deepgram":
                    answers.tts_voice_id = DEFAULT_DEEPGRAM_VOICE
                elif await step_voice() == _BACK:
                    raise KeyboardInterrupt
            if not answers.tts_key and await step_tts_key() == _BACK:
                raise KeyboardInterrupt
        problems = missing_answer_fields(replace(answers, surface=launch_surface)) + await setup_core.validate_answers(answers, live=False)
        if problems:
            for problem in problems:
                console.print(problem, markup=False)
            return 1
        from sleeptalk.setup import ensure_dependencies
        from sleeptalk.launcher import remember_profile, selected_surface

        problems = ensure_dependencies(launch_surface, repair=True)
        if problems:
            for problem in problems:
                console.print(problem, markup=False)
            return 1
        if not editing:
            state.path = layout.profiles_dir / f"{answers.slug}.toml"
        path = state.write(answers, env_path=layout.env_path)
        preferred = launch_surface
        if editing and surface is not None:
            preferred = selected_surface(workspace_path) or state.effective["transport"]["type"]
        remember_profile(path, workspace_path, surface=preferred)
        console.print("Saved. Run sleeptalk to call, or sleeptalk settings to make changes.")
        if settings:
            discord_ready = bool(answers.token and answers.guild_id and answers.client_id)
            picked = await questionary.select("Start a call now?", choices=[
                questionary.Choice("Start browser call", value="web"),
                questionary.Choice("Start terminal call", value="terminal"),
                questionary.Choice("Join Discord" if discord_ready else "Set up Discord", value="discord"),
                questionary.Choice("Not now", value=LAUNCH_LATER),
            ], default=preferred if preferred in ("web", "terminal", "discord") else LAUNCH_LATER).ask_async()
            if picked is None or picked == LAUNCH_LATER:
                return 0
            if picked == "discord" and not discord_ready:
                saved = path
                await set_up_discord()
                if not editing:
                    state.path = layout.profiles_dir / f"{answers.slug}.toml"
                path = state.write(answers, env_path=layout.env_path)
                if path != saved:
                    # Saved a moment ago under the default name; it moves with the bot's name.
                    saved.unlink(missing_ok=True)
                console.print(f"In your Discord voice channel, use /{answers.slug} join.", markup=False)
            remember_profile(path, workspace_path, surface=picked)
            return path, picked
        if launch_surface == "discord":
            console.print(f"In your Discord voice channel, use /{answers.slug} join.", markup=False)
        return path, launch_surface
    except KeyboardInterrupt:
        console.print("\nSetup cancelled. Validated keys are saved for next time.")
        return 1
    except (ConfigError, CredentialError, ValueError, OSError) as exc:
        console.print(str(exc), markup=False)
        return 1


# ---------------------------------------------------------------------------
# Non-interactive / agent path
# ---------------------------------------------------------------------------

def load_connect_answers(slug: str, profiles_dir: Path, env: dict[str, str]) -> WizardAnswers:
    from sleeptalk.hermes_connect import SESSION_TOKEN_ENV

    answers = load_existing_answers(slug, profiles_dir) or WizardAnswers(
        slug=slug, name=slug
    )
    answers.token = env.get(resolved_token_env(answers), answers.token)
    answers.realtime_key = env.get("OPENAI_API_KEY", answers.realtime_key)
    answers.stt_key = env.get(_STT_KEY_ENV.get(answers.stt_provider, "DEEPGRAM_API_KEY"), answers.stt_key)
    answers.tts_key = (
        env.get(_TTS_KEY_ENV.get(answers.tts_provider, ""), "")
        or env.get("CARTESIA_API_KEY")
        or env.get("ELEVENLABS_API_KEY")
        or env.get("HUME_API_KEY")
        or answers.tts_key
    )
    answers.gateway_session_token = env.get(
        SESSION_TOKEN_ENV, answers.gateway_session_token
    )
    return answers


def pick_connect_slug(profiles_dir: Path, slug: str = "", config_path: str = "") -> str:
    if config_path:
        return Path(config_path).stem
    if slug:
        return slug
    names = existing_profiles(profiles_dir)
    if len(names) == 1:
        return names[0]
    if not names:
        raise ValueError(
            "no profile yet — run `sleeptalk init` first, then `sleeptalk connect hermes`"
        )
    listed = ", ".join(names)
    raise ValueError(f"pass --slug to choose a profile ({listed})")


async def run_connect_hermes(
    *,
    url: str = "",
    token: str = "",
    slug: str = "",
    config_path: str = "",
    live: bool = True,
    handshake=None,
    out=print,
) -> int:
    from sleeptalk.config import _load_env
    from sleeptalk.hermes_connect import (
        DEFAULT_ORIGIN,
        Ready,
        apply_dashboard,
        attach_hermes,
        parse_gateway_target,
        probe_message,
        resolve_session_token,
    )
    from sleeptalk.home import announce, resolve_layout

    layout = resolve_layout()
    out(announce(layout))
    profiles_dir = layout.profiles_dir
    env_path = layout.env_path
    if config_path:
        profile = Path(config_path).resolve()
        profiles_dir = profile.parent
        root = profiles_dir.parent
        if root.name == "config":
            root = root.parent
        env_path = root / ".env"
    try:
        chosen = pick_connect_slug(profiles_dir, slug, config_path)
    except ValueError as exc:
        out(str(exc))
        return 1
    profile_path = profiles_dir / f"{chosen}.toml"
    env = _load_env(profile_path) if profile_path.is_file() else (
        _load_env(env_path) if env_path.is_file() else {}
    )
    answers = load_connect_answers(chosen, profiles_dir, env)
    if live:
        probe = await attach_hermes(
            answers,
            raw_url=url or answers.gateway_url or DEFAULT_ORIGIN,
            token=token,
            env=env,
            handshake=handshake,
        )
        if not isinstance(probe, Ready):
            out(probe_message(probe))
            return 1
        out(probe_message(probe))
    else:
        target = parse_gateway_target(url or answers.gateway_url or DEFAULT_ORIGIN)
        secret = token.strip() or resolve_session_token(url or answers.gateway_url, env)
        apply_dashboard(answers, Ready(origin=target.origin, ws_url=target.ws_url), secret)
    written = write_files(
        answers, profiles_dir=profiles_dir, env_path=env_path
    )
    for line in written:
        out(line)
    out(f"run it: sleeptalk run --config {profiles_dir / (answers.slug + '.toml')}")
    out("if sleeptalk.toml is missing, copy sleeptalk.example.toml to sleeptalk.toml")
    out("for a new bot overlay, copy config/profiles/example.toml to a <slug>.toml")
    return 0


async def run_non_interactive(
    answers: WizardAnswers,
    *,
    live: bool = True,
    out=print,
) -> int:
    """Headless init: validate everything, write files, print the receipt."""
    if live:
        out("validating credentials live...")
    layout = _layout()
    from sleeptalk.setup import ensure_setup

    result = await ensure_setup(
        answers,
        profiles_dir=layout.profiles_dir,
        env_path=layout.env_path,
        live=live,
    )
    if not result.ready:
        prefix = "invalid" if result.phase == "invalid" else "missing"
        for message in result.messages:
            out(f"{prefix}: {message}")
        return 1
    if result.written:
        out("written:")
        for line in result.written:
            out(f"  - {line}")
    else:
        out("unchanged: profile and secrets already matched")
    providers = capability_providers(answers) if answers.route_type == "cascade" else []
    for warning in language_support.warnings_for(answers.language, providers):
        out(f"warning: {warning}")
    profile_path = layout.profiles_dir / f"{answers.slug}.toml"
    out(f"run it: {launch_command(answers, profile_path)}")
    out(f"re-check any time: sleeptalk doctor --config {profile_path}")
    out("if sleeptalk.toml is missing, copy sleeptalk.example.toml to sleeptalk.toml")
    out("for a new bot overlay, copy config/profiles/example.toml to a <slug>.toml")
    return 0


def pick_profile(message: str, options: list[tuple[str, object]], *, default: object = None):
    """One arrow-key select over ``(title, value)`` pairs for the launcher.

    Returns the chosen value, or None when the prompt is dismissed. The
    ``default`` value, when it is one of the options, starts under the pointer.
    """
    import questionary

    choices = [questionary.Choice(title, value=value) for title, value in options]
    preselected = next((choice for choice in choices if choice.value == default), None)
    return questionary.select(message, choices=choices, default=preselected).ask()
