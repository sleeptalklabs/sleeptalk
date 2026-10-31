"""sleeptalk doctor — re-run the full validation ladder, forever after init.

Ladder: file parses -> schema -> component choices -> env vars present ->
binaries on PATH -> runtime extras -> provider connectivity -> Discord token/guild/intents ->
STT transcription round-trip -> language capability + data freshness.

Every rung reports all of its problems in one pass, fix-stating, reusing the
loader's messages where a rung already exists. Exit non-zero on any failure;
warnings (language capability, stale data) never fail the run.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sleeptalk import language_support
from sleeptalk.config import (
    ConfigError,
    SleeptalkConfig,
    _format_validation_errors,
    _load_env,
    _validate_component_choices,
    collect_missing_binaries,
    collect_missing_env,
    collect_missing_runtime_extras,
    collect_missing_system_dependencies,
    resolve_config_document,
    resolve_credential_env,
    transport_config_for_surface,
)
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import STT_PROVIDERS, TTS_PROVIDERS

OK = "ok"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"

_RUNG_IDS = {
    "file parses": "config_file",
    "schema valid": "config_schema",
    "component choices": "components",
    "env vars present": "env_vars",
    "agent runtime": "agent_runtime",
    "binaries on PATH": "binaries",
    "runtime extras": "runtime_extras",
    "system dependencies": "system_dependencies",
    "provider connectivity": "provider_connectivity",
    "hermes dashboard": "hermes_dashboard",
    "discord": "discord",
    "stt round-trip": "stt_key",
    "language support": "language_support",
}


@dataclass
class Rung:
    name: str
    status: str = OK
    lines: list[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _RUNG_IDS.get(
                self.name,
                re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_") or "rung",
            )


@dataclass
class DoctorReport:
    rungs: list[Rung] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(rung.status == FAIL for rung in self.rungs)

    def add(self, name: str, status: str = OK, lines: list[str] | None = None) -> Rung:
        rung = Rung(name, status, list(lines or []))
        self.rungs.append(rung)
        return rung

    def to_json(self) -> str:
        payload = {
            "schema_version": 1,
            "status": "failed" if self.failed else "ok",
            "rungs": [
                {
                    "id": rung.id,
                    "name": rung.name,
                    "status": rung.status,
                    "lines": list(rung.lines),
                }
                for rung in self.rungs
            ],
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _static_ladder(
    path: Path, env: dict[str, str], *, surface: str | None = None
) -> tuple[SleeptalkConfig | None, DoctorReport]:
    """Rungs 1-5: everything the loader already knows how to check."""
    report = DoctorReport()
    try:
        document = resolve_config_document(path)
    except Exception as exc:  # ConfigError: problems already collected
        report.add("file parses", FAIL, _problem_lines(exc))
        return None, report
    report.add("file parses")

    try:
        config = SleeptalkConfig.model_validate(document.raw)
    except Exception as exc:  # pydantic ValidationError
        report.add("schema valid", FAIL, _format_validation_errors(exc))
        return None, report
    config.source_path = str(document.source_path)
    config.agent.source_path = config.source_path
    if surface is not None:
        config = config.model_copy(
            update={
                "transport": transport_config_for_surface(config.transport, surface)
            }
        )
    report.add("schema valid")

    try:
        resolved = resolve_credential_env(config, env)
    except ConfigError as exc:
        report.add("env vars present", FAIL, exc.problems)
        return config, report
    env.update(resolved)
    register_defaults()
    choice_problems = _validate_component_choices(config, env=env)
    report.add("component choices", FAIL if choice_problems else OK, choice_problems)

    env_problems = collect_missing_env(config, env)
    report.add("env vars present", FAIL if env_problems else OK, env_problems)
    report.rungs.append(_agent_runtime_rung(config, env))

    binary_problems = collect_missing_binaries(config, env=env)
    report.add("binaries on PATH", FAIL if binary_problems else OK, binary_problems)
    extra_problems = collect_missing_runtime_extras(config, env=env)
    report.add("runtime extras", FAIL if extra_problems else OK, extra_problems)
    system_problems = collect_missing_system_dependencies(config)
    report.add(
        "system dependencies",
        FAIL if system_problems else OK,
        system_problems,
    )
    return config, report


async def _provider_connectivity(
    config: SleeptalkConfig, env: dict[str, str]
) -> Rung:
    """Rung 6: each configured STT/TTS provider's declared auth check."""
    if config.route.type == "hybrid":
        from sleeptalk.builder import run_connectivity_checks

        try:
            await run_connectivity_checks(config, env=env)
        except Exception as exc:
            return Rung("provider connectivity", FAIL, _problem_lines(exc))
        return Rung(
            "provider connectivity",
            OK,
            ["openai-realtime: OK", "openai-speech: OK"],
        )
    checks = []
    for registry, name, section in (
        (STT_PROVIDERS, config.stt.provider, config.stt),
        (TTS_PROVIDERS, config.tts.provider, config.tts),
    ):
        registration = registry.get_optional(name)
        if registration and registration.check:
            checks.append((name, registration.check, section))
    for spec in config.tts.fallback:
        registration = TTS_PROVIDERS.get_optional(spec.provider)
        if registration and registration.check:
            checks.append((spec.provider, registration.check, spec))
    if not checks:
        return Rung("provider connectivity", SKIP, ["no provider declares a check"])
    results = await asyncio.gather(
        *(check(section, env) for _name, check, section in checks),
        return_exceptions=True,
    )
    problems = [
        f"{name}: {result}" for (name, _c, _s), result in zip(checks, results)
        if isinstance(result, Exception)
    ]
    oks = [f"{name}: OK" for (name, _c, _s), result in zip(checks, results)
           if not isinstance(result, Exception)]
    return Rung("provider connectivity", FAIL if problems else OK, oks + problems)


async def _discord_rung(config: SleeptalkConfig, env: dict[str, str]) -> Rung:
    """Rung 7: Discord token -> guild membership -> gateway intent probe."""
    if config.transport.type != "discord":
        return Rung("discord", SKIP, [f"transport={config.transport.type}"])
    from sleeptalk.discord_check import run_discord_ladder

    if not env.get(config.transport.token_env, "").strip():
        return Rung(
            "discord", FAIL,
            [f"{config.transport.token_env} missing — see 'env vars present' above."],
        )
    oks, problems = await run_discord_ladder(config, env)
    return Rung("discord", FAIL if problems else OK, oks + problems)


async def _stt_roundtrip_rung(
    config: SleeptalkConfig,
    env: dict[str, str],
    connectivity: Rung,
) -> Rung:
    """Rung 8: transcribe the bundled speech clip; report connectivity+latency."""
    if config.route.type == "hybrid":
        return Rung(
            "stt round-trip",
            OK if connectivity.status == OK else SKIP,
            [
                "OpenAI Realtime transcription validated in provider connectivity"
                if connectivity.status == OK
                else "OpenAI Realtime connectivity failed above"
            ],
        )
    if config.stt.provider != "deepgram":
        return Rung(
            "stt round-trip", SKIP,
            [f"no round-trip implemented for stt provider {config.stt.provider!r}"],
        )
    if not env.get("DEEPGRAM_API_KEY", "").strip():
        return Rung("stt round-trip", SKIP, ["DEEPGRAM_API_KEY missing"])
    if connectivity.status == FAIL:
        return Rung("stt round-trip", SKIP, ["provider connectivity failed above"])
    from sleeptalk.builder import stt_spec
    from sleeptalk.stt_roundtrip import deepgram_roundtrip

    try:
        transcript, latency_ms = await deepgram_roundtrip(
            stt_spec(config.stt), env["DEEPGRAM_API_KEY"]
        )
    except Exception as exc:
        return Rung("stt round-trip", FAIL, [str(exc)])
    return Rung(
        "stt round-trip", OK,
        [f"transcribed bundled clip ({latency_ms:.0f} ms): {transcript!r}"],
    )


def _agent_runtime_rung(config: SleeptalkConfig, env: dict[str, str]) -> Rung:
    from sleeptalk.agents.construction import resolve_agent
    from sleeptalk.agents.selection import doctor_lines

    try:
        construction = resolve_agent(config.agent, env)
    except ValueError as exc:
        return Rung("agent runtime", FAIL, [str(exc)])
    lines = (
        doctor_lines(construction.selection)
        if construction.selection is not None
        else [f"selected registered agent {construction.registration.name}"]
    )
    return Rung("agent runtime", OK, lines)


def _language_rung(config: SleeptalkConfig) -> Rung:
    """Rung 9: capability warnings for stt.language + table freshness nudge."""
    if config.route.type == "hybrid":
        return Rung(
            "language support",
            OK,
            [
                f"OpenAI Realtime language is '{config.route.language}'; "
                "live validation checks the selected transcription model"
            ],
        )
    providers = [config.stt.provider, config.tts.provider]
    providers += [spec.provider for spec in config.tts.fallback]
    seen: set[str] = set()
    unique = [p for p in providers if not (p in seen or seen.add(p))]
    warnings = language_support.warnings_for(config.stt.language, unique)
    nudge = language_support.staleness_nudge()
    if nudge and nudge not in warnings:
        warnings.append(nudge)
    resolved = _resolved_speech_lines(config)
    if warnings:
        return Rung("language support", WARN, warnings + resolved)
    return Rung(
        "language support", OK,
        [f"all configured providers fully support '{config.stt.language}' "
         f"(data last verified {language_support.LAST_VERIFIED})", *resolved],
    )


def _resolved_speech_lines(config: SleeptalkConfig) -> list[str]:
    """What the language resolved to: the STT model at call time, and
    whether the first thing spoken is in the language or in English."""
    from sleeptalk.spoken_lines import for_language

    language = config.stt.language
    stt_model = language_support.resolve_stt_model(config.stt.provider, language, config.stt.model)
    lines = [f"stt: {config.stt.provider} {stt_model}" + (
        f" (resolved from {config.stt.model} for '{language}')"
        if config.stt.model and stt_model != config.stt.model else ""
    )]
    tts_model = config.tts.voice_id if config.tts.provider == "deepgram" and config.tts.voice_id else (
        language_support.resolve_tts_model(config.tts.provider, language, config.tts.model)
    )
    lines.append(f"tts: {config.tts.provider} {tts_model}")
    if language_support.primary_tag(language) == "en":
        lines.append("spoken lines: English (policy defaults)")
    elif config.policy.preamble_pools:
        lines.append(f"spoken lines: localized in the profile ({len(config.policy.preamble_pools)} pools)")
    elif for_language(language) is not None:
        lines.append(
            f"spoken lines: a table exists for '{language}' but the profile carries no [policy.preamble_pools] — "
            "re-run `sleeptalk init` or copy the pools in; acks play in English until then"
        )
    else:
        lines.append(f"spoken lines: no table for '{language}'; acks and narration play in English")
    return lines


async def _hermes_gateway_rung(config: SleeptalkConfig, env: dict[str, str]) -> Rung:
    if config.agent.type not in {"hermes_gateway", "hermes-gateway"}:
        return Rung("hermes dashboard", SKIP, [f"agent={config.agent.type}"])
    from sleeptalk.hermes_connect import (
        Ready,
        probe_dashboard,
        probe_message,
        resolve_session_token,
    )

    url = (config.agent.gateway_url or "").strip()
    if not url:
        return Rung(
            "hermes dashboard",
            FAIL,
            ["gateway_url missing — run `sleeptalk connect hermes`"],
        )
    probe = await probe_dashboard(url, token=resolve_session_token(url, env))
    if isinstance(probe, Ready):
        return Rung("hermes dashboard", OK, [probe_message(probe)])
    return Rung("hermes dashboard", FAIL, [probe_message(probe)])


async def run_doctor(
    path: str | Path,
    *,
    env: dict[str, str] | None = None,
    live: bool = True,
    surface: str | None = None,
) -> DoctorReport:
    """Run the ladder for one invocation surface.

    ``live=False`` skips network rungs. ``surface`` changes only the effective
    transport being checked; it never rewrites the saved profile.
    """
    register_defaults()
    path = Path(path)
    env = dict(env) if env is not None else _load_env(path)
    config, report = _static_ladder(path, env, surface=surface)
    if config is None:
        return report
    if live and not any(rung.name == "env vars present" and rung.status == FAIL for rung in report.rungs):
        connectivity = await _provider_connectivity(config, env)
        report.rungs.append(connectivity)
        report.rungs.append(await _hermes_gateway_rung(config, env))
        report.rungs.append(await _discord_rung(config, env))
        report.rungs.append(await _stt_roundtrip_rung(config, env, connectivity))
    report.rungs.append(_language_rung(config))
    return report


def format_report(report: DoctorReport) -> str:
    symbols = {OK: "✓", FAIL: "✗", WARN: "⚠", SKIP: "-"}
    lines: list[str] = []
    for rung in report.rungs:
        lines.append(f"{symbols[rung.status]} {rung.name}")
        lines.extend(f"    {line}" for line in rung.lines)
    verdict = "doctor: FAILED" if report.failed else "doctor: OK"
    lines.append(verdict)
    return "\n".join(lines)


def _problem_lines(exc: Exception) -> list[str]:
    problems = getattr(exc, "problems", None)
    if problems:
        return [str(p) for p in problems]
    return [str(exc)]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="sleeptalk doctor")
    parser.add_argument(
        "--config",
        default="sleeptalk.toml",
        help="Config path. If missing, run sleeptalk to set up.",
    )
    parser.add_argument(
        "--offline", action="store_true", help="Skip live network rungs."
    )
    parser.add_argument(
        "--json", action="store_true", help="Print one machine-readable JSON line."
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run_doctor(args.config, live=not args.offline))
    print(report.to_json() if args.json else format_report(report))
    return 1 if report.failed else 0
