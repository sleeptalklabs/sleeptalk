"""Host operations for attached, local-browser, and remote-browser voice.

``sleeptalk host ensure`` never mints a call. For chat callers,
``sleeptalk host start-voice`` starts one bounded detached child and returns a
chat-safe receipt. The CLI sends explicit human Web calls through a package-
owned loopback rendezvous and prints a plain URL.
"""

from __future__ import annotations

import asyncio
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sleeptalk.call_provider import (
    DEFAULT_TICKET_TTL_SECONDS,
    CallContext,
    CallProviderError,
)
from sleeptalk.call_registry import (
    CallNotFoundError,
    CallRegistryError,
    CallRequestConflictError,
    ProfileIdentity,
    get_start_request,
    registry_root_for_config,
    default_registry_root,
    CallStopTimeoutError,
    list_live_calls,
    stop_call,
)
from sleeptalk.config import _load_env
from sleeptalk.detached_call import (
    DetachedCallBusyError,
    DetachedCallRequest,
    DetachedCallReplay,
    launch_detached_call,
    profile_snapshot,
)
from sleeptalk.doctor import FAIL, OK, run_doctor
from sleeptalk.home import resolve_layout
from sleeptalk.receipts import (
    OPERATION_ENSURE,
    OPERATION_START_VOICE,
    OPERATION_CALL_STATUS,
    call_progress_receipt,
    start_request_conflict_receipt,
    ensure_conflict_receipt,
    ensure_failed_receipt,
    ensure_needs_input_receipt,
    ensure_ready_receipt,
    error_receipt,
    list_calls_receipt,
    missing_names_from_problems,
    start_voice_receipt,
    stop_call_receipt,
    STATUS_NEEDS_INPUT,
    STATUS_READY,
)
from sleeptalk.operator_errors import ErrorCode, error_code_for
from sleeptalk.setup_core import resolved_rendezvous_url

_PROFILE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class StartedWebVoice:
    """Result of starting one bounded web voice child."""

    join_url: str
    expires_at: str
    agent_type: str
    agent_slug: str
    agent_name: str
    continuity: str
    profile: str
    call_id: str
    progress: dict[str, Any] | None = None


class HostCallError(CallProviderError):
    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code


def _read_profile_config(path: Path):
    """Load a profile for identity checks without requiring live env secrets."""
    from sleeptalk.config import ConfigError, SleeptalkConfig, resolve_config_document

    try:
        document = resolve_config_document(path)
        return SleeptalkConfig.model_validate(document.raw)
    except (ConfigError, Exception):
        # Self-contained fixture/profile documents (no shared root) still expose identity.
        import tomllib

        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return SleeptalkConfig.model_validate(raw)


def resolve_profile_path(
    *,
    profile: str = "",
    config: str | None = None,
) -> Path:
    """Resolve a profile TOML from --config or --profile against the layout."""
    if config:
        return Path(config).expanduser()
    slug = _profile_slug(profile)
    layout = resolve_layout()
    return layout.profiles_dir / f"{slug}.toml"


def _profile_slug(profile: str) -> str:
    slug = (profile or "default").strip() or "default"
    return slug[:-5] if slug.lower().endswith(".toml") else slug


def _target_layout(path: Path, *, explicit_config: bool) -> tuple[Path, Path]:
    """Return the profile directory and secret file for one target profile."""
    if not explicit_config:
        layout = resolve_layout()
        return layout.profiles_dir, layout.env_path
    profiles_dir = path.parent
    root = profiles_dir
    if profiles_dir.name == "profiles":
        root = profiles_dir.parent
        if root.name == "config":
            root = root.parent
    return profiles_dir, root / ".env"


def _answer_field_names(messages: tuple[str, ...] | list[str]) -> list[str]:
    """Reduce private validation messages to stable, value-free field names."""
    names: list[str] = []
    for message in messages:
        head = str(message).split(" missing", 1)[0].strip(" '\"")
        if head and " " not in head and head not in names:
            names.append(head)
    return names


def _auto_agent(
    raw_answers: dict[str, Any], environment: dict[str, str]
) -> tuple[str | None, list[str]]:
    """Select one local agent or explain why the caller must choose."""
    from sleeptalk.setup_core import detect_agents

    from_answers = str(raw_answers.get("agent_type", "")).strip()
    if from_answers and from_answers != "auto":
        return from_answers, []
    calling_agent = (
        environment.get("SLEEPTALK_CALLING_AGENT", "").strip()
        or environment.get("SLEEPTALK_AGENT_TYPE", "").strip()
    )
    if calling_agent:
        return calling_agent, []
    detected = sorted(detect_agents())
    if len(detected) == 1:
        return detected[0], []
    return None, detected


def _existing_conflicts(
    cfg: Any,
    *,
    surface: str,
    route: str,
    agent: str,
    raw_answers: dict[str, Any],
    workspace: str | None = None,
) -> list[str]:
    """Name requested identity axes that disagree with a present profile."""
    conflicts: list[str] = []
    if workspace is not None or cfg.binding is not None:
        from sleeptalk.runtime_binding import BindingError, verify
        try:
            verify(cfg, workspace=workspace)
        except BindingError:
            conflicts.append("workspace_binding")
    if cfg.route.type != route:
        conflicts.append("route")
    requested_agent = agent
    if requested_agent in {"", "auto"}:
        requested_agent = str(raw_answers.get("agent_type", "")).strip()
    if requested_agent not in {"", "auto"} and cfg.agent.type != requested_agent:
        conflicts.append("agent")
    for field, configured in (
        ("stt_provider", getattr(cfg.stt, "provider", "")),
        ("tts_provider", getattr(cfg.tts, "provider", "")),
    ):
        requested = str(raw_answers.get(field, "")).strip()
        if requested and requested != configured:
            conflicts.append(field)
    return conflicts


def ensure(
    *,
    profile: str = "default",
    surface: str = "web",
    route: str = "cascade",
    agent: str = "auto",
    config: str | None = None,
    answers: str | None = None,
    live: bool = False,
    env: dict[str, str] | None = None,
    workspace: str | None = None,
    tailnet: bool = False,
    reuse_key: str = "",
    key_reuse: bool = True,
    no_tailnet: bool = False,
) -> dict[str, Any]:
    """Converge local setup, then return a chat-safe readiness receipt.

    ``tailnet`` stores ``delivery = "tailnet"`` in the profile so later calls
    use the host's tailnet without a flag; ``no_tailnet`` removes it, so
    calls go through the hosted relay again. Web receipts carry a ``tailnet``
    block saying whether that is available here, so the agent can ask the
    user once whether their device is on the same tailnet.

    When a profile still needs speech keys, ensure looks for a key the user
    already has on this host (OpenRouter, Groq, Together, Venice: Sleeptalk's
    store, the environment, or the agent's own files) and offers it: the
    receipt's ``reusable_keys`` and ``ask`` name each one by source, and
    nothing is read, sent, or copied. Only when the user says yes does the
    agent rerun with ``reuse_key``, which validates that key for voice
    (a small paid request to its provider), copies it into the private
    store, and writes the profile. ``key_reuse=False`` skips the lookup.
    Values never appear anywhere an agent reads.
    """
    reused: dict[str, Any] | None = None
    if reuse_key:
        outcome = _reuse_speech_key(profile=profile, surface=surface, route=route, agent=agent, config=config,
                                    env=env, workspace=workspace, provider=reuse_key)
        if outcome.get("status") != "reused":
            return outcome
        reused = outcome["reused"]
    receipt = _ensure(profile=profile, surface=surface, route=route, agent=agent, config=config,
                      answers=answers, live=live, env=env, workspace=workspace, tailnet=tailnet)
    if reused is None and key_reuse and receipt.get("status") == STATUS_NEEDS_INPUT \
            and _speech_keys_missing(receipt.get("missing", [])) and surface == "web":
        found = _reusable_keys(env)
        if found:
            # Offer, never take: the user decides whether a key another tool
            # holds is checked (a small paid request) and copied for voice.
            receipt["reusable_keys"] = found
            receipt["ask"] = _reuse_question(found)
            import shlex

            flag = " ".join(part for part in (
                f"--config {shlex.quote(config)}" if config else f"--profile {shlex.quote(profile)}",
                f"--agent {shlex.quote(agent)}" if agent not in {"", "auto"} else "",
                f"--workspace {shlex.quote(workspace)}" if workspace else "",
            ) if part)
            receipt["remediation"] = (
                "Ask the user the `ask` line and wait for their answer. If they say yes, run "
                f"`sleeptalk host ensure {flag} --surface web --reuse-key PROVIDER --json` with the service they chose. "
                "If they say no, or name another service, send the private key page instead: "
            ) + str(receipt.get("remediation", ""))
    if receipt.get("surface") in {"web", "auto"} and receipt.get("operation") == OPERATION_ENSURE:
        _describe_tailnet(receipt, profile=profile, config=config, env=env, tailnet=tailnet,
                          no_tailnet=no_tailnet)
    if reused is not None:
        receipt["reused_key"] = reused["reused_key"]
        receipt["speech"] = reused["speech"]
        speech = reused["speech"]
        receipt["say"] = (
            (f"Voice is already set up with your {speech['label']} key: "
             if reused["reused_key"]["source"] == "already set up" else
             f"Voice is set up from your {speech['label']} key ({reused['reused_key']['source']}), now copied into Sleeptalk's private store: ")
            + f"{speech['listening_model']} listens and the {speech['voice']} voice on {speech['speaking_model']} speaks"
            f"{'; ' + speech['free'][0].lower() + speech['free'][1:].rstrip('.') if speech.get('free') else ''}. "
            "Say 'use a different service' any time to switch."
        )
        if receipt.get("status") == STATUS_READY:
            ask = receipt.get("tailnet", {}).get("ask", "")
            receipt["remediation"] = ("Tell the user the `say` line in your own words. "
                                      + (ask + " " if ask else "")
                                      + "Then run `sleeptalk host start-voice`. Never print or ask for the key itself.")
    return receipt


def _describe_tailnet(receipt: dict[str, Any], *, profile: str, config: str | None,
                      env: dict[str, str] | None, tailnet: bool, no_tailnet: bool = False) -> None:
    """Say whether this host can publish calls on its tailnet, whether the
    profile has chosen to, and, when it could but hasn't, what to ask."""
    from sleeptalk import tailnet as tailnet_driver

    summary = asyncio.run(tailnet_driver.detect(environ=env))
    selected = tailnet
    path = resolve_profile_path(profile=profile, config=config)
    if path.is_file():
        try:
            options = tomllib.loads(path.read_text(encoding="utf-8")).get("transport", {}).get("options", {})
            stored = str(options.get("delivery", "")).strip() == "tailnet" if isinstance(options, dict) else False
        except (OSError, tomllib.TOMLDecodeError):
            stored = False
        if tailnet and not stored and receipt.get("status") == STATUS_READY:
            # Asked for on an existing profile: store it, as a new profile would.
            from sleeptalk.profile_edit import set_delivery

            try:
                set_delivery(path, "tailnet")
                stored = True
            except (OSError, ValueError):
                stored = False
        if no_tailnet and stored:
            from sleeptalk.profile_edit import set_delivery

            try:
                set_delivery(path, "")
                stored = False
            except (OSError, ValueError):
                pass
        selected = stored
    block: dict[str, Any] = {"available": summary.get("available", False), "selected": selected,
                             **({"name": summary["name"]} if summary.get("name") else {}),
                             **({"reason": summary["code"]} if summary.get("code") else {})}
    if block["available"] and not selected and not no_tailnet:
        block["ask"] = (
            f"Tailscale is on this host ({summary.get('name', 'this machine')}), but calls still go through the hosted relay. "
            "Ask the user once whether the device they will call from is on the same tailnet; if yes, rerun this command "
            "with --tailnet and every call is private with no time limit. Don't call a relay link a tailnet call: a tailnet "
            "join link contains .ts.net."
        )
    receipt["tailnet"] = block


# Adapters whose setup probe requires tool approvals to stay with the caller.
_CALLER_APPROVAL_AGENTS = frozenset({"hermes", "acp", "hermes_gateway", "hermes-gateway", "pi"})


def _speech_keys_missing(missing: list[str]) -> bool:
    return any(item in {"provider_credentials", "env_vars", "stt_key", "tts_key"} or "key" in item for item in missing)


def _reusable_keys(env: dict[str, str] | None) -> list[dict[str, Any]]:
    """Keys already on this host that could give the user voice: sources and models, never values."""
    from sleeptalk.key_discovery import discover
    from sleeptalk.key_reuse import profile_summary

    try:
        keys = discover(environment=env)
    except Exception:  # noqa: BLE001 — discovery is a courtesy, never a failure
        return []
    return [{**key.public(), **profile_summary(key.provider)} for key in keys]


def _reuse_question(found: list[dict[str, Any]]) -> str:
    """One plain question for the agent to put to the user before any key is used."""
    names = [f"your {key['label']} key ({key['source']})" for key in found]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return (f"I found {listed} on this computer. Want me to use it for voice? "
            "Sleeptalk would check it with that service (a tiny paid request) and keep its own copy in its private store. "
            "Or I can send you a page to enter a different key.")


def _reuse_speech_key(*, profile: str, surface: str, route: str, agent: str, config: str | None,
                      env: dict[str, str] | None, workspace: str | None, provider: str) -> dict[str, Any]:
    """Validate a key found on this host for voice and write the profile from it."""
    import uuid

    from sleeptalk.key_discovery import discover
    from sleeptalk.key_reuse import ReuseError, pick, profile_summary, reuse_key, spec_for_reuse
    from sleeptalk.runtime_binding import BindingError, canonical_workspace
    from sleeptalk.speech.openai_audio import PRESETS

    profile_label = Path(config).stem if config else _profile_slug(profile)

    def failed(message: str) -> dict[str, Any]:
        return ensure_needs_input_receipt(profile=profile_label, surface=surface, route=route, agent=agent,
                                          missing=["provider_credentials"], remediation=message)

    if provider not in PRESETS:
        return failed(f"{provider!r} is not a service whose key can listen and speak.")
    if surface != "web" or route != "cascade":
        return failed("Reusing a key sets up browser calls (surface web, cascade route); set up other surfaces from that profile afterwards.")
    path = resolve_profile_path(profile=profile, config=config)
    if path.is_file():
        # Already done on an earlier run: the normal ensure below reports it,
        # but only as this service if the profile really speaks through it.
        try:
            stt = tomllib.loads(path.read_text(encoding="utf-8")).get("stt", {})
            in_use = str(stt.get("provider", "")) if isinstance(stt, dict) else ""
        except (OSError, tomllib.TOMLDecodeError):
            in_use = ""
        if in_use != provider:
            return failed(f"Profile {profile_label!r} already exists and uses {in_use or 'another service'} for voice; "
                          "change its speech service on the settings page instead.")
        return {"status": "reused", "reused": {"reused_key": {"provider": provider, "source": "already set up"},
                                               "speech": profile_summary(provider)}}
    environment = dict(os.environ) if env is None else dict(env)
    selected_agent = agent
    if selected_agent in {"", "auto"}:
        selected_agent, detected = _auto_agent({}, environment)
        if selected_agent is None:
            return failed("Multiple local agents are available; rerun with --agent NAME." if detected
                          else "No local agent was detected; install one or rerun with --agent NAME.")
    try:
        workspace_dir = str(canonical_workspace(workspace) if workspace is not None else Path.cwd().resolve())
    except BindingError as exc:
        return failed(str(exc))
    try:
        key = pick(provider, discover(environment=environment))
        # Agent-driven setups keep tool approvals with the caller, the same
        # choice the link recipe makes with --permission-mode pending; the
        # ACP-style adapters' setup probes refuse "auto".
        agent_fields = {"permission_mode": "pending"} if selected_agent in _CALLER_APPROVAL_AGENTS else {}
        spec = spec_for_reuse(request_id=f"reuse-{uuid.uuid4().hex[:16]}", profile=profile_label, workspace=workspace_dir,
                              agent=selected_agent, surface="web", profile_path=str(path.resolve()), **agent_fields)
        result = asyncio.run(reuse_key(spec, key, environment=environment))
    except ReuseError as exc:
        return failed(f"{exc} (code: {exc.code}). Fall back to `sleeptalk host input link` so the user enters a key privately.")
    except Exception as exc:  # noqa: BLE001 — a fixed reason, never a provider body
        # aiohttp HTTP errors from prepare_profile carry Sleeptalk's own fixed
        # sentences; anything else is named by class only.
        reason = str(getattr(exc, "text", "") or "").strip() if exc.__class__.__module__.startswith("aiohttp") else ""
        return failed(f"The key could not be set up for voice ({reason or type(exc).__name__}). "
                      "Fall back to `sleeptalk host input link` so the user enters a key privately.")
    return {"status": "reused", "reused": result}


def _ensure(
    *,
    profile: str = "default",
    surface: str = "web",
    route: str = "cascade",
    agent: str = "auto",
    config: str | None = None,
    answers: str | None = None,
    live: bool = False,
    env: dict[str, str] | None = None,
    workspace: str | None = None,
    tailnet: bool = False,
) -> dict[str, Any]:
    profile_label = (
        Path(config).stem
        if config
        else _profile_slug(profile)
    )
    if not config and (
        profile_label in {".", ".."} or _PROFILE_SLUG.fullmatch(profile_label) is None
    ):
        return ensure_failed_receipt(
            profile="invalid",
            surface=surface,
            route=route,
            agent=agent,
            message="The profile name must be a local slug.",
            code="config",
        )
    path = resolve_profile_path(profile=profile, config=config)
    profiles_dir, env_path = _target_layout(path, explicit_config=bool(config))
    environment = dict(os.environ) if env is None else dict(env)
    effective_agent = agent
    raw_answers: dict[str, Any] = {}
    if answers:
        try:
            raw_answers = tomllib.loads(
                Path(answers).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, tomllib.TOMLDecodeError):
            return ensure_failed_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent=agent,
                message="The local answers file could not be read as TOML.",
                code="config",
            )

    if path.is_file():
        try:
            cfg = _read_profile_config(path)
        except Exception:
            return ensure_failed_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent=agent,
                message="The existing profile is not valid.",
                code="config",
            )
        effective_agent = cfg.agent.type
        conflicts = _existing_conflicts(
            cfg,
            surface=surface,
            route=route,
            agent=agent,
            raw_answers=raw_answers,
            workspace=workspace,
        )
        if conflicts:
            return ensure_conflict_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent=agent,
                conflicts=conflicts,
            )
        if raw_answers:
            from sleeptalk.setup_core import (
                answers_from_mapping,
                env_entries,
                hydrate_answers_from_env,
                load_existing_answers,
                load_existing_secrets,
                store_profile_secrets,
            )

            try:
                supplied = answers_from_mapping(raw_answers)
                current = load_existing_answers(path.stem, profiles_dir)
                if current is None:
                    current = supplied
                current.agent_type = cfg.agent.type
                current.surface = cfg.transport.type
                current.route_type = cfg.route.type
                current.stt_provider = cfg.stt.provider
                current.tts_provider = cfg.tts.provider
                current.token_env = cfg.transport.token_env
                load_existing_secrets(current, env_path, path)
                hydrate_answers_from_env(current, environment)
                for field in (
                    "token",
                    "realtime_key",
                    "stt_key",
                    "tts_key",
                    "tts_user_id",
                    "gateway_api_key",
                    "gateway_session_token",
                    "agent_token",
                    "openclaw_token",
                ):
                    value = str(raw_answers.get(field, ""))
                    if value:
                        setattr(current, field, value)
                if env_entries(current):
                    environment.update(store_profile_secrets(current, path, env_path))
            except Exception:
                return ensure_failed_receipt(
                    profile=profile_label,
                    surface=surface,
                    route=route,
                    agent=cfg.agent.type,
                    code="config",
                )
    else:
        from sleeptalk.setup import ensure_setup
        from sleeptalk.setup_core import (
            answers_from_mapping,
            env_entries,
            hydrate_answers_from_env,
            load_existing_secrets,
        )

        selected_agent = agent
        detected: list[str] = []
        if selected_agent in {"", "auto"}:
            selected_agent, detected = _auto_agent(raw_answers, environment)
        if selected_agent is None:
            detail = (
                "Multiple local agents are available; rerun with --agent NAME."
                if detected
                else "No local agent was detected; install one or rerun with --agent NAME."
            )
            return ensure_needs_input_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent="auto",
                missing=["agent"],
                remediation=detail,
            )
        effective_agent = selected_agent
        requested = dict(raw_answers)
        if tailnet and surface == "web":
            requested["delivery"] = "tailnet"
        requested.update(
            {
                "surface": surface,
                "route_type": route,
                "agent_type": selected_agent,
                "slug": profile_label,
            }
        )
        requested.setdefault("name", profile_label)
        if workspace is not None:
            from sleeptalk.runtime_binding import BindingError, canonical_workspace
            try:
                requested["workspace_dir"] = str(canonical_workspace(workspace))
            except BindingError as exc:
                return ensure_failed_receipt(
                    profile=profile_label, surface=surface, route=route,
                    agent=selected_agent, message=str(exc), code="config",
                )
        else:
            requested.setdefault("workspace_dir", str(Path.cwd().resolve()))
        try:
            setup_answers = answers_from_mapping(requested)
        except (TypeError, ValueError):
            return ensure_failed_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent=selected_agent,
                message="The local answers file contains unsupported setup values.",
                code="config",
            )
        load_existing_secrets(setup_answers, env_path, profiles_dir / f"{profile_label}.toml")
        hydrate_answers_from_env(setup_answers, environment)
        result = asyncio.run(
            ensure_setup(
                setup_answers,
                profiles_dir=profiles_dir,
                env_path=env_path,
                live=live,
                bind_runtime=workspace is not None,
            )
        )
        if result.status == "needs_input":
            missing = _answer_field_names(result.messages)
            if not missing:
                missing = missing_names_from_problems(list(result.messages))
            if not missing:
                missing = (
                    ["agent"]
                    if any("install" in message.lower() for message in result.messages)
                    else ["provider_credentials"]
                )
            return ensure_needs_input_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent=selected_agent,
                missing=missing,
                remediation=(
                    (
                        "Never ask the user for provider keys in chat or write them to a file. "
                        "Run `sleeptalk host input link` and send the user the returned setup link "
                        "(web calls), or `sleeptalk host input start --user-browser-local` when the "
                        "user has a browser on this host; then rerun host ensure. "
                        if "provider_credentials" in missing or any("key" in item for item in missing)
                        else ""
                    )
                    + (
                        "stt_key and tts_key are roles, not two providers: one provider covers both with "
                        "one key. The setup link's page offers every provider with its free usage; the "
                        "user picks one there, so don't ask them to choose in chat. "
                        if "stt_key" in missing and "tts_key" in missing
                        else ""
                    )
                    + "An answers file (--answers FILE) is only for non-secret choices."
                ),
            )
        if result.status == "failed":
            return ensure_failed_receipt(
                profile=profile_label,
                surface=surface,
                route=route,
                agent=selected_agent,
            )
        environment.update(env_entries(setup_answers))

    from sleeptalk.setup import ensure_dependencies

    dependency_problems = ensure_dependencies(surface, repair=live)
    if dependency_problems:
        return ensure_needs_input_receipt(
            profile=profile_label,
            surface=surface,
            route=route,
            agent=effective_agent,
            missing=["dependencies"],
            remediation="; ".join(dependency_problems),
        )

    # Doctor offline by default for agent ensure: live provider probes are
    # optional and must not mint tickets. Callers that want live can pass live=True.
    report = asyncio.run(
        run_doctor(
            path,
            env=environment if env is not None else None,
            live=live,
            surface=surface,
        )
    )
    if report.failed:
        missing = [rung.id for rung in report.rungs if rung.status == FAIL]
        return ensure_needs_input_receipt(
            profile=profile_label,
            surface=surface,
            route=route,
            agent=effective_agent,
            missing=missing,
            remediation=(
                "Never ask the user for provider keys in chat. Run `sleeptalk host input link` and "
                "send the user the returned setup link (web calls), or `sleeptalk host input start "
                "--user-browser-local` when the user has a browser on this host; then re-run host ensure."
            ),
        )

    # Prefer identity from the loaded config when available.
    agent_type = agent
    agent_name = ""
    try:
        cfg = _read_profile_config(path)
        route = cfg.route.type or route
        agent_type = cfg.agent.type
        agent_name = cfg.agent.name or cfg.agent.slug
    except Exception:  # noqa: BLE001 — receipt still reports ready from doctor.
        agent_type = agent if agent not in {"", "auto"} else "auto"

    # Sanity: a successful ensure receipt must not look like a mint.
    receipt = ensure_ready_receipt(
        profile=profile_label,
        surface=surface,
        route=route,
        agent=agent_type,
        agent_name=agent_name,
    )
    assert "join_url" not in receipt
    assert "url" not in receipt
    # Touch OK rungs so a second ensure is a pure re-check (convergent).
    _ = [rung.name for rung in report.rungs if rung.status == OK]
    return receipt


def start_web_voice(
    *,
    profile: str = "default",
    config: str | None = None,
    rendezvous_url: str | None = None,
    agent_instance: str | None = None,
    conversation_id: str = "",
    platform_user_id: str = "terminal-user",
    ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
    echo: bool = False,
    local: bool = False,
    workspace: str | None = None,
    request_id: str = "",
    tailnet: bool = False,
    no_tailnet: bool = False,
) -> StartedWebVoice:
    """Start one bounded detached web voice host and return join metadata.

    ``tailnet`` publishes the loopback call server on the host's tailnet
    instead of using a hosted relay; a profile can select it with
    ``[transport.options] delivery = "tailnet"``. ``no_tailnet`` skips that
    stored choice for this one call. ``delivery = "local"`` opens every call
    on this computer (localhost), as ``local`` does for one call.
    """
    if tailnet and no_tailnet:
        raise HostCallError("Choose --tailnet or --no-tailnet, not both.", code="config")
    if not 1 <= ttl_seconds <= 300:
        raise HostCallError(
            "Call links must expire between 1 and 300 seconds.", code="config"
        )

    path = resolve_profile_path(profile=profile, config=config)
    profile_label = Path(config).stem if config else _profile_slug(profile)
    if not echo and not path.is_file():
        raise HostCallError(
            f"Profile not found: {path}. Run `sleeptalk host ensure` or pass --config.",
            code="profile_not_found",
        )

    config_path = "" if echo else str(path.resolve())
    agent_type = "echo"
    agent_slug = profile_label
    agent_name = profile_label
    continuity = "new"

    if not echo:
        from sleeptalk.agents.selection import (
            SelectionError,
            request_from_config,
            select_runtime,
        )

        # Identity only — do not re-validate env here; the child host does that.
        try:
            cfg = _read_profile_config(path)
        except Exception as exc:
            raise HostCallError(
                "The profile config is not valid.", code="config"
            ) from exc
        agent_type = cfg.agent.type
        from sleeptalk.runtime_binding import BindingError, verify
        try:
            verify(cfg, workspace=workspace)
        except BindingError as exc:
            raise HostCallError(str(exc), code="config") from None
        agent_slug = cfg.agent.slug or profile_label
        agent_name = cfg.agent.name or agent_slug
        env = _load_env(path) if path.is_file() else dict(os.environ)
        try:
            selection = select_runtime(request_from_config(cfg.agent, env))
            continuity = continuity_label(
                conversation_id=conversation_id,
                session_continuity=selection.session_continuity,
                agent_type=agent_type,
                agent_slug=agent_slug,
            )
        except SelectionError:
            continuity = continuity_label(
                conversation_id=conversation_id,
                session_continuity=False,
                agent_type=agent_type,
                agent_slug=agent_slug,
            )
        if agent_instance is None:
            agent_instance = agent_slug
    elif agent_instance is None:
        agent_instance = "terminal"

    effective_conversation = conversation_id
    if not effective_conversation and not echo:
        effective_conversation = ""
    elif not effective_conversation and echo:
        effective_conversation = "terminal-demo"

    context = CallContext(
        agent_instance=agent_instance or "terminal",
        conversation_id=effective_conversation,
        platform_user_id=platform_user_id,
    )
    stored = "" if echo else str(getattr(getattr(cfg, "transport", None), "options", {}).get("delivery", "")).strip()
    if not tailnet and not no_tailnet:
        tailnet = stored == "tailnet"
    if tailnet:
        local = False
    elif stored == "local":
        # "This computer": every link opens here, on localhost, with no relay.
        local = True
    request = DetachedCallRequest(
        rendezvous_url=rendezvous_url or resolved_rendezvous_url(),
        context=context,
        ttl_seconds=ttl_seconds,
        config_path=config_path,
        echo=echo,
        profile=profile_label,
        local_rendezvous=local,
        tailnet=tailnet,
        request_id=request_id,
        profile_snapshot=profile_snapshot(cfg) if request_id and not echo else "",
    )
    url = launch_detached_call(request)
    progress = None
    if request_id:
        progress = get_start_request(
            identity=request.identity(), request_id=request_id,
            root=registry_root_for_config(config_path) if config_path else default_registry_root(),
            fingerprint=request.fingerprint(),
        )
        if progress is None:
            raise HostCallError("The start request was not recorded.", code="internal")
        if progress["state"] in {"ended", "failed", "expired"} or {"link_consumed", "browser_connected"}.intersection(progress["observations"]):
            raise DetachedCallReplay(progress)
        expires_at = datetime.fromtimestamp(progress["expires_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return StartedWebVoice(
        join_url=url,
        expires_at=expires_at,
        agent_type=agent_type,
        agent_slug=agent_slug,
        agent_name=agent_name,
        continuity=continuity,
        profile=profile_label,
        call_id=request.call_id,
        progress=progress,
    )


def _delivery_of(join_url: str) -> dict[str, str]:
    """Which way the call travels, read from the link itself, plus the line to say."""
    from urllib.parse import urlsplit

    host = (urlsplit(join_url).hostname or "").lower()
    if host.endswith(".ts.net"):
        return {"delivery": "tailnet", "say": "This call runs over your tailnet: private, no relay, no time limit."}
    if host in {"127.0.0.1", "localhost", "::1"}:
        return {"delivery": "local", "say": "This call runs on this machine only."}
    return {"delivery": "hosted_relay",
            "say": "This call goes through the hosted relay and ends after 30 minutes."}


def start_voice_web(
    *,
    profile: str = "default",
    config: str | None = None,
    rendezvous_url: str | None = None,
    agent_instance: str | None = None,
    conversation_id: str = "",
    platform_user_id: str = "terminal-user",
    ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
    echo: bool = False,
    local: bool = False,
    workspace: str | None = None,
    request_id: str = "",
    tailnet: bool = False,
    no_tailnet: bool = False,
) -> dict[str, Any]:
    """Agent JSON path: start one web voice child and return a chat-safe receipt."""
    try:
        started = start_web_voice(
            profile=profile,
            config=config,
            rendezvous_url=rendezvous_url,
            agent_instance=agent_instance,
            conversation_id=conversation_id,
            platform_user_id=platform_user_id,
            ttl_seconds=ttl_seconds,
            echo=echo,
            local=local,
            tailnet=tailnet,
            no_tailnet=no_tailnet,
            **({"workspace": workspace} if workspace is not None else {}),
            **({"request_id": request_id} if request_id else {}),
        )
    except DetachedCallReplay as exc:
        return call_progress_receipt(exc.progress, operation=OPERATION_START_VOICE)
    except CallRequestConflictError:
        return start_request_conflict_receipt()
    except (CallRegistryError, OSError):
        return error_receipt(
            operation=OPERATION_START_VOICE, code="internal",
            message="Local call recovery state is unavailable. Inspect the profile state before retrying.",
        )
    except ValueError:
        return error_receipt(
            operation=OPERATION_START_VOICE, code="config",
            message="The request ID must contain 8 to 128 URL-safe characters.",
        )
    except CallProviderError as exc:
        return error_receipt(
            operation=OPERATION_START_VOICE,
            code=error_code_for(exc, fallback="provider"),
            message=str(exc),
        )
    receipt = start_voice_receipt(
        surface="web",
        join_url=started.join_url,
        expires_at=started.expires_at,
        agent_type=started.agent_type,
        agent_slug=started.agent_slug,
        agent_name=started.agent_name,
        continuity=started.continuity,
        profile=started.profile,
        call_id=started.call_id,
    )
    receipt.update(_delivery_of(started.join_url))

    if started.progress is not None:
        progress_receipt = call_progress_receipt(started.progress)
        for key in ("call_state", "observations", "audio_acceptance"):
            receipt[key] = progress_receipt[key]
    return receipt


def voice_call_status(
    *, request_id: str, profile: str = "default", config: str | None = None,
    echo: bool = False, agent_instance: str = "terminal",
) -> dict[str, Any]:
    """Read one explicit start attempt without recovering its bearer link."""
    try:
        path = resolve_profile_path(profile=profile, config=config)
        profile_label = _profile_slug(profile) if config is None else path.stem
        identity = (
            ProfileIdentity.for_echo(agent_instance=agent_instance, profile=profile_label)
            if echo else ProfileIdentity.from_config(config_path=str(path), profile=profile_label)
        )
        progress = get_start_request(
            identity=identity, request_id=request_id,
            root=default_registry_root() if echo else registry_root_for_config(path),
        )
        if progress is None:
            return error_receipt(operation=OPERATION_CALL_STATUS, code="not_found", message="No start request was found for this profile and request ID.")
        return call_progress_receipt(progress)
    except ValueError:
        return error_receipt(operation=OPERATION_CALL_STATUS, code="config", message="The request ID must contain 8 to 128 URL-safe characters.")
    except (CallRegistryError, OSError):
        return error_receipt(operation=OPERATION_CALL_STATUS, code="internal", message="Local call recovery state is unavailable.")


def list_voice_calls() -> dict[str, Any]:
    """Return a chat-safe snapshot of every live detached call."""
    try:
        calls = [
            {
                "callId": record.call_id,
                "profile": record.profile,
                "started_at": record.started_at,
            }
            for record in list_live_calls()
        ]
    except (CallRegistryError, OSError) as exc:
        return error_receipt(operation="list", code="internal", message=str(exc))
    return list_calls_receipt(calls)


def stop_voice_call(call_id: str) -> dict[str, Any]:
    """Stop one detached call by its public id."""
    try:
        outcome = stop_call(call_id)
    except (CallNotFoundError, CallRegistryError, OSError) as exc:
        return error_receipt(
            operation="stop",
            code=error_code_for(exc, fallback="internal"),
            message=str(exc),
        )
    return stop_call_receipt(
        call_id=outcome.record.call_id,
        profile=outcome.record.profile,
        forced=outcome.forced,
    )


def continuity_label(
    *,
    conversation_id: str,
    session_continuity: bool,
    agent_type: str,
    agent_slug: str,
) -> str:
    """Evidence-derived continuity label for chat-safe receipts."""
    cid = (conversation_id or "").strip()
    if not cid or cid.endswith(":new-web-call") or cid == f"{agent_type}:{agent_slug}:new-web-call":
        return "new"
    if session_continuity:
        return "continues"
    return "reset"


def print_json_receipt(receipt: dict[str, Any]) -> None:
    import json
    import sys

    # Diagnostics stay on stderr; stdout is only the receipt.
    sys.stdout.write(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
