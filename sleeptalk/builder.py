"""Assemble the public RealtimeSession through component registries."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sleeptalk.config import (
    ConfigError,
    SleeptalkConfig,
    _load_env,
    _validate_component_choices,
    resolve_agent_token_env,
    resolve_credential_env,
)
from sleeptalk.agent_environment import agent_environment
from sleeptalk.defaults import register_defaults
from sleeptalk.metering import UsageMeter, attach_usage_meter
from sleeptalk.policy import VoicePolicy
from sleeptalk.provider_factory import build_stt_components, build_tts, build_tts_chain
from sleeptalk.registries import STT_PROVIDERS, TRANSPORTS, TTS_PROVIDERS

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sleeptalk.adapter import AgentAdapter
    from sleeptalk.routes.base import Route
    from sleeptalk.speech.contracts import RESTSTT, StreamingSTT, TTS
    from sleeptalk.transports import Transport
    from sleeptalk.session import RealtimeSession


@dataclass
class _SessionAssembly:
    """Components owned by one RealtimeSession."""

    config: SleeptalkConfig
    policy: VoicePolicy
    transport: Transport
    agent: AgentAdapter
    stt: RESTSTT | None
    streaming_stt: StreamingSTT | None
    tts: TTS | None
    route: Route
    tts_fallbacks: list[TTS] = field(default_factory=list)
    usage_meter: UsageMeter | None = None
    # Builds a fresh instance of the call's agent for a background job.
    job_agent_factory: Callable[[], AgentAdapter] | None = None


def build_session(
    config: SleeptalkConfig,
    *,
    env: dict[str, str] | None = None,
) -> RealtimeSession:
    """Build the session through provider, agent, and transport registries.

    Provider factories validate configuration and attach usage metering.
    Agents and transports come from their registries."""
    register_defaults()
    base_env = env if env is not None else _load_env_from_config(config)
    base_env = resolve_agent_token_env(config, base_env)
    agent_env = agent_environment(base_env)
    agent_env.pop(config.transport.token_env, None)
    env = resolve_credential_env(config, base_env)
    problems = _validate_component_choices(config, env=env)
    if problems:
        raise ConfigError(problems)

    policy = VoicePolicy.from_config(config.policy)
    from sleeptalk.policy import local_responsiveness_enabled, max_background_jobs_for

    policy.local_responsiveness = local_responsiveness_enabled(config)
    policy.max_background_jobs = max_background_jobs_for(config)
    if config.transport.type == "discord":
        policy.input_previews = (
            env.get("DISCORD_TRANSCRIPT_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
            and not config.transport.authorization_required
        )
    usage_meter = UsageMeter.for_config(config)
    from sleeptalk.agents.construction import build_agent
    from sleeptalk.agents.selection import SelectionError, doctor_lines

    try:
        agent, selection = build_agent(config.agent, agent_env)
    except SelectionError as exc:
        raise ConfigError([str(exc)]) from exc
    if selection is not None:
        for line in doctor_lines(selection):
            logger.info("agent runtime: %s", line)
    if hasattr(agent, "keepalive_interval_s"):
        agent.keepalive_interval_s = policy.keepalive_interval_s
    transport = TRANSPORTS.build(config.transport.type, config, env)
    if not callable(getattr(transport, "run", None)):
        raise TypeError(
            f"transport registration {config.transport.type!r} returned "
            f"{type(transport).__name__}, which has no callable run method"
        )
    attach_usage_meter(agent, usage_meter)
    route_type = getattr(getattr(config, "route", None), "type", "cascade")
    if route_type == "cascade":
        stt_components = build_stt_components(
            stt_spec(config.stt), env, meter=usage_meter
        )
        assert stt_components is not None
        if stt_components.streaming is None:
            policy.tune_for_batch_listener()
        tts = build_tts(tts_spec(config.tts), env, meter=usage_meter)
        tts_fallbacks = build_tts_chain(
            [tts_spec(spec) for spec in config.tts.fallback],
            env,
            meter=usage_meter,
        )
        from sleeptalk.speech.fallback import FallbackTTS

        route_tts = FallbackTTS(tts, fallbacks=tts_fallbacks)
        from sleeptalk.routes.cascade import CascadeRoute

        route = CascadeRoute(
            agent=agent,
            tts=route_tts,
            stt=stt_components.rest,
            stt_streaming=stt_components.streaming,
            warm_opening_lines=True,
        )
        stt = stt_components.rest
        streaming_stt = stt_components.streaming
        job_agent_factory = _job_agent_factory(config, agent_env, usage_meter)
    elif route_type == "hybrid" and config.route.model == "gpt-live-1":
        from sleeptalk.routes.live import LiveRoute

        route = LiveRoute(config=openai_realtime_config(config, env), agent=agent,
                          display_name=config.agent.name or config.agent.slug or "Sleeptalk", usage_meter=usage_meter)
        job_agent_factory = _job_agent_factory(config, agent_env, usage_meter)
        stt = streaming_stt = tts = None
        tts_fallbacks = []
    elif route_type == "hybrid":
        from sleeptalk.openai_realtime import (
            OpenAIHybridRealtimeProvider,
        )
        from sleeptalk.routes.hybrid import HybridRoute
        from sleeptalk.routes.hybrid_types import HybridRoleContract

        provider_config = openai_realtime_config(config, env)
        display_name = (
            config.agent.name.strip()
            or config.agent.slug.strip()
            or "Sleeptalk"
        )
        role_contract = HybridRoleContract(display_name)
        # The shared policy object is mutated live by the terminal hotkey and
        # the web control, so both halves read it rather than a boot snapshot.
        def reply_length() -> str:
            return policy.reply_length

        def narration_style() -> str:
            return policy.narration_style

        realtime = OpenAIHybridRealtimeProvider(
            provider_config,
            role_contract=role_contract,
            reply_length=reply_length,
        )
        realtime.set_tts_speed(config.tts.speed)
        tts = None
        tts_fallbacks = []
        stt = None
        streaming_stt = None
        route = HybridRoute(
            role_contract=role_contract,
            agent=agent,
            realtime=realtime,
            keepalive_interval_s=policy.keepalive_interval_s,
            first_token_budget_ms=policy.first_token_budget_ms,
            speculative_audio=config.route.speculative_audio,
            audio_first=config.route.audio_first,
            reply_length=reply_length,
            narration_style=narration_style,
        )
    else:
        raise ConfigError([f"route.type {route_type!r} is not supported"])
    if route_type != "cascade" and config.route.model != "gpt-live-1":
        job_agent_factory = None
    assembly = _SessionAssembly(
        config=config,
        policy=policy,
        transport=transport,
        agent=agent,
        stt=stt,
        streaming_stt=streaming_stt,
        tts=tts,
        tts_fallbacks=tts_fallbacks,
        usage_meter=usage_meter,
        route=route,
        job_agent_factory=job_agent_factory,
    )
    from sleeptalk.session import RealtimeSession

    return RealtimeSession(assembly)


# Agents that only talk (no tools) have nothing to hand off.
_NO_JOB_AGENT_TYPES = frozenset({"model", "command"})


def _job_agent_factory(
    config: SleeptalkConfig, agent_env: dict[str, str], usage_meter: UsageMeter | None,
) -> Callable[[], AgentAdapter] | None:
    """A fresh instance of the call's agent: same profile and folder, no saved
    conversation, so a background job never continues or overwrites the
    foreground's session."""
    if str(config.agent.type or "").strip().lower() in _NO_JOB_AGENT_TYPES:
        return None
    section = config.agent.model_copy(update={"gateway_session_id": ""})

    def build() -> AgentAdapter:
        from sleeptalk.agents.construction import build_agent

        agent, _selection = build_agent(section, dict(agent_env))
        attach_usage_meter(agent, usage_meter)
        return agent

    return build


async def run_connectivity_checks(
    config: SleeptalkConfig, *, env: dict[str, str] | None = None
) -> None:
    """Cheap connectivity check per configured provider. All failures are
    collected and raised as one ConfigError."""
    register_defaults()
    if env is None:
        env = _load_env_from_config(config)
    env = resolve_credential_env(config, env)
    if config.route.type == "hybrid":
        from sleeptalk.openai_realtime import check_openai_connectivity

        try:
            if config.route.model == "gpt-live-1":
                from sleeptalk.openai_live import check_openai_live_connectivity
                await check_openai_live_connectivity(openai_realtime_config(config, env))
            else:
                await check_openai_connectivity(openai_realtime_config(config, env))
        except Exception as exc:
            raise ConfigError(
                [
                    "OpenAI hybrid connectivity check failed: "
                    f"{exc} — verify OPENAI_API_KEY and the configured Realtime, "
                    "transcription, voice, and Speech model access."
                ]
            ) from exc
        return
    checks = []
    for registry, name, section in (
        (STT_PROVIDERS, config.stt.provider, config.stt),
        (TTS_PROVIDERS, config.tts.provider, config.tts),
    ):
        registration = registry.get(name)
        if registration.check:
            checks.append((name, registration.check, section))
    for spec in config.tts.fallback:
        registration = TTS_PROVIDERS.get(spec.provider)
        if registration.check:
            checks.append((spec.provider, registration.check, spec))

    problems: list[str] = []
    results = await asyncio.gather(
        *(check(section, env) for _name, check, section in checks),
        return_exceptions=True,
    )
    for (name, _check, _section), result in zip(checks, results):
        if isinstance(result, Exception):
            problems.append(
                f"{name} connectivity check failed: {result} — verify the API key "
                "in .env and that the provider is reachable."
            )
    if problems:
        raise ConfigError(problems)


def openai_realtime_config(config: SleeptalkConfig, env: dict[str, str]):
    """Parse the hybrid provider boundary from validated config and secrets."""
    from sleeptalk.openai_realtime import OpenAIRealtimeConfig

    route = config.route
    if route.type != "hybrid":
        raise ConfigError(["OpenAI Realtime config requires route.type='hybrid'"])
    return OpenAIRealtimeConfig(
        api_key=env.get("OPENAI_API_KEY", ""),
        model=route.model,
        voice=route.voice,
        transcription_model=route.transcription_model,
        speech_model=route.speech_model,
        language=route.language,
        base_url=route.base_url,
        specialist_tool=route.specialist_tool,
    )


def stt_model_for_provider(provider: str, model: str = "") -> str:
    """A model from another provider's family (nova-3 on Cartesia) falls back to the default."""
    register_defaults()
    registration = STT_PROVIDERS.get(provider)
    selected = (model or "").strip()
    if registration.normalize_model is not None:
        return registration.normalize_model(selected)
    return selected or registration.provider_defaults.get("model", "")


def stt_spec(stt):
    """STTConfig -> ProviderSpec (the shape STT factories consume)."""
    from sleeptalk.providers import ProviderSpec

    from sleeptalk.language_support import resolve_stt_model

    keyterms = [t.strip() for t in stt.keyterms.split(",") if t.strip()]
    # The language picks the model: a launch default that does not cover
    # stt.language swaps to the provider's wider model here, at build time,
    # so hand-edited profiles get the same swap the wizard applies.
    resolved = resolve_stt_model(stt.provider, stt.language, stt.model)
    return ProviderSpec(
        provider=stt.provider,
        model=stt_model_for_provider(stt.provider, resolved),
        language=stt.language,
        options=_merge_provider_options(
            stt,
            {
                "base_url": stt.base_url,
                "keyterms": keyterms,
                "rest_timeout_seconds": stt.rest_timeout_seconds,
                "streaming_timeout_seconds": stt.streaming_timeout_seconds,
                "endpointing_ms": stt.endpointing_ms,
            },
        ),
    )


def tts_model_for_provider(provider: str, model: str = "") -> str:
    register_defaults()
    registration = TTS_PROVIDERS.get(provider)
    selected = (model or "").strip()
    if registration.normalize_model is not None:
        return registration.normalize_model(selected)
    return selected or registration.provider_defaults.get("model", "")


def tts_spec(tts):
    """TTSConfig/TTSSpecConfig -> ProviderSpec with provider defaults filled."""
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech_speed import snap_speed

    provider = tts.provider.strip().lower()
    register_defaults()
    registration = TTS_PROVIDERS.get(provider)
    model, voice_id = _language_tts_choice(provider, tts)
    return ProviderSpec(
        provider=provider,
        model=model,
        voice_id=voice_id,
        language=getattr(tts, "language", ""),
        speed=snap_speed(tts.speed),
        options=_merge_provider_options(
            tts,
            {
                "base_url": tts.base_url,
                "output_format": (
                    tts.output_format
                    or registration.provider_defaults.get("output_format", "")
                ),
            },
        ),
    )


def _language_tts_choice(provider: str, tts) -> tuple[str, str]:
    """Model and voice for this language. On the OpenAI-compatible services a
    launch-default model follows the language (speech_catalog.recommended);
    when that changes the model, a voice that isn't the new model's becomes
    its default voice, or the voice family would route it straight back."""
    from sleeptalk.language_support import _AGGREGATORS, resolve_tts_model

    model = tts_model_for_provider(provider, tts.model)
    voice_id = tts.voice_id
    if provider == "cartesia":
        # Sonic 3 for the few languages Sonic 3.6 doesn't list; voices fit both.
        return resolve_tts_model(provider, getattr(tts, "language", "") or "", model), voice_id
    if provider not in _AGGREGATORS:
        return model, voice_id
    from sleeptalk import speech_catalog

    resolved = resolve_tts_model(provider, getattr(tts, "language", "") or "", tts.model)
    if resolved == model:
        return model, voice_id
    option = speech_catalog.find(provider, speech_catalog.TTS, resolved, cached=False)
    if option is not None and option.owns_voice(voice_id) is False:
        voice_id = option.default_voice
    return resolved, voice_id


def _merge_provider_options(section, common: dict[str, Any]) -> dict[str, Any]:
    private = dict(getattr(section, "options", {}) or {})
    model_fields = set(getattr(type(section), "model_fields", {})) - {"options"}
    duplicates = sorted(set(private) & model_fields)
    if duplicates:
        raise ValueError(
            f"component options duplicate common fields {duplicates}; "
            "set them directly in the component section"
        )
    return {**common, **private}


def _load_env_from_config(config: SleeptalkConfig) -> dict[str, str]:
    if config.source_path:
        from pathlib import Path

        return _load_env(Path(config.source_path))
    return dict(os.environ)
