"""Run the adapter conformance suite without pytest and report two columns.

Modes:

- ``offline`` (default): no prompt is sent. Contract methods, warmup/close,
  idle abort, capability manifest, and voice-session rejection.
- ``live``: also sends a few short real prompts. Proves streaming, abort and
  recovery, advertised events, and measures voice quality. May spend tokens.
- ``fixture``: the in-tree fake prompts (``hello``, ``slow``, ``hang``,
  ``tool``, ``permission``, ``clarify``). Scaffolded echo agents understand
  them, so every capability can be proven deterministically.

Protocol failures mean the adapter broke the contract it advertised. Voice
warnings mean it works but will feel slow or awkward on a call.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AssistantDelta,
    CapabilitySupport,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    UsageReported,
    adapter_capabilities,
    typed_events,
)
from sleeptalk.conformance import cases
from sleeptalk.conformance.harness import (
    MANIFEST_FIELDS,
    ConformanceFailure,
    ConformanceSkip,
    looks_like_preamble,
)

Mode = Literal["offline", "live", "fixture"]
Status = Literal["pass", "fail", "skip", "warn"]
AgentFactory = Callable[[], Any]

LIVE_HELLO = "Say hello in one short sentence."
LIVE_LONG = "Count from one to ten, one number per sentence, like: One. Two. Three."
LIVE_TOOL = "Use one of your tools to look at the current directory, then tell me in one sentence what you found."
LIVE_AFTER_ABORT = "Say okay."

FIRST_SENTENCE_TARGET_S = 1.5
SILENT_GAP_TARGET_S = 8.0
LONG_SENTENCE_CHARS = 300
LIVE_TURN_TIMEOUT_S = 90.0
ABORT_SETTLE_TARGET_S = 3.0
MARKDOWN_MARKERS = ("```", "**", "__", "](", "| ---", "|---")


@dataclass
class CheckResult:
    id: str
    title: str
    column: Literal["protocol", "voice", "availability"]
    status: Status
    detail: str = ""
    metric: float | None = None


@dataclass
class Report:
    adapter: str
    mode: Mode
    checks: list[CheckResult] = field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest: AgentCapabilityManifest | None = field(default=None, repr=False)

    @property
    def failed(self) -> bool:
        return any(check.status == "fail" for check in self.checks)

    def add(self, check: CheckResult) -> None:
        self.checks.append(check)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "operation": "adapter_test",
                "adapter": self.adapter,
                "mode": self.mode,
                "result": "fail" if self.failed else "pass",
                "capabilities": self.capabilities,
                "checks": [asdict(check) for check in self.checks],
            },
            ensure_ascii=False,
        )

    def format_text(self) -> str:
        width = max((len(check.title) for check in self.checks), default=20) + 2
        lines = [f"{self.adapter} ({self.mode})", ""]
        marks = {"pass": "pass", "fail": "FAIL", "skip": "skip", "warn": "warn"}
        for column in ("protocol", "availability", "voice"):
            rows = [check for check in self.checks if check.column == column]
            if not rows:
                continue
            lines.append(column)
            for check in rows:
                metric = f"  {check.metric:.2f}s" if check.metric is not None else ""
                detail = f"  {check.detail}" if check.detail else ""
                lines.append(f"  {check.title:<{width}}{marks[check.status]}{metric}{detail}")
            lines.append("")
        lines.append("capabilities")
        for name, value in self.capabilities.items():
            reason = f" ({value['reason']})" if value.get("reason") else ""
            lines.append(f"  {name:<24}{value['level']}{reason}")
        lines.append("")
        summary = "FAIL" if self.failed else "PASS"
        warnings = sum(1 for check in self.checks if check.status == "warn")
        lines.append(f"{summary}" + (f" with {warnings} warning(s)" if warnings else ""))
        if self.mode == "offline":
            lines.append("No prompt was sent. Run with --live to prove a spoken turn (may spend tokens).")
        return "\n".join(lines)


def manifest_summary(manifest: AgentCapabilityManifest) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(MANIFEST_FIELDS):
        support: CapabilitySupport = getattr(manifest, name)
        out[name] = {"level": support.level.value, "reason": support.reason}
    return out


async def _close_quietly(agent: Any) -> None:
    close = getattr(agent, "close", None)
    if close is None:
        return
    try:
        await asyncio.wait_for(close(), timeout=10.0)
    except Exception:  # noqa: BLE001 - teardown must not hide the result
        pass


async def _run_case(
    report: Report,
    factory: AgentFactory,
    check_id: str,
    title: str,
    body: Callable[[Any], Awaitable[Any]],
    *,
    column: Literal["protocol", "voice", "availability"] = "protocol",
    warmup: bool = False,
    timeout: float = 30.0,
) -> Any:
    try:
        agent = factory()
    except Exception as exc:  # noqa: BLE001
        report.add(CheckResult(check_id, title, "availability", "fail", f"could not build adapter: {exc}"))
        return None
    try:
        if warmup:
            await asyncio.wait_for(agent.warmup(), timeout=timeout)
        value = await asyncio.wait_for(body(agent), timeout=timeout)
    except ConformanceSkip as exc:
        report.add(CheckResult(check_id, title, column, "skip", str(exc)))
        return None
    except ConformanceFailure as exc:
        report.add(CheckResult(check_id, title, column, "fail", str(exc) or "assertion failed"))
        return None
    except TimeoutError:
        report.add(CheckResult(check_id, title, column, "fail", f"timed out after {timeout:.0f}s"))
        return None
    except AssertionError as exc:
        report.add(CheckResult(check_id, title, column, "fail", str(exc) or "assertion failed"))
        return None
    except Exception as exc:  # noqa: BLE001 - an adapter error is a result
        report.add(CheckResult(check_id, title, "availability", "fail", f"{type(exc).__name__}: {exc}"))
        return None
    finally:
        await _close_quietly(agent)
    report.add(value if isinstance(value, CheckResult) else CheckResult(check_id, title, column, "pass"))
    return value


# ---------------------------------------------------------------------------
# Offline checks
# ---------------------------------------------------------------------------


async def _offline(report: Report, factory: AgentFactory) -> None:
    from sleeptalk.agents.construction import _CORE_METHODS

    async def methods(agent: Any) -> CheckResult:
        missing = [name for name in _CORE_METHODS if not callable(getattr(agent, name, None))]
        if missing:
            raise ConformanceFailure(f"missing methods: {', '.join(missing)}")
        if "on_event" not in inspect.signature(agent.respond_streaming).parameters:
            return CheckResult("contract.methods", "contract methods", "protocol", "warn", "respond_streaming has no on_event; tool narration is unavailable")
        return CheckResult("contract.methods", "contract methods", "protocol", "pass")

    await _run_case(report, factory, "contract.methods", "contract methods", methods)

    try:
        probe_agent = factory()
        warnings = tuple(getattr(probe_agent, "setup_warnings", ()) or ())
        await _close_quietly(probe_agent)
    except Exception:  # noqa: BLE001 - construction failures are reported by the checks
        warnings = ()
    for warning in warnings:
        report.add(CheckResult("setup.hint", "configuration", "availability", "warn", warning))

    async def warmup_manifest(agent: Any) -> CheckResult:
        start = time.monotonic()
        await agent.warmup()
        elapsed = time.monotonic() - start
        manifest = adapter_capabilities(agent)
        report.manifest = manifest
        report.capabilities = manifest_summary(manifest)
        if manifest.voice_sessions.level is SupportLevel.UNAVAILABLE and not manifest.voice_sessions.reason:
            raise ConformanceFailure("unavailable voice_sessions must include a reason")
        return CheckResult("lifecycle.warmup", "warmup and capabilities", "availability", "pass", metric=elapsed)

    await _run_case(report, factory, "lifecycle.warmup", "warmup and capabilities", warmup_manifest, column="availability", timeout=60.0)
    await _run_case(report, factory, "lifecycle.close", "warmup then close", cases.case_warmup_then_close, timeout=60.0)
    await _run_case(report, factory, "abort.idle", "abort while idle", cases.case_abort_idle_is_safe)
    await _run_case(report, factory, "voice_sessions.reject", "voice sessions reject when not supported", cases.case_voice_sessions_unavailable, warmup=True, timeout=60.0)


# ---------------------------------------------------------------------------
# Live checks
# ---------------------------------------------------------------------------


@dataclass
class Timeline:
    sentences: list[tuple[float, str]] = field(default_factory=list)
    events: list[tuple[float, object]] = field(default_factory=list)
    finished_at: float = 0.0


async def _timed_turn(agent: Any, prompt: str, *, stop_after: int | None = None) -> Timeline:
    timeline = Timeline()
    start = time.monotonic()
    sink = typed_events(lambda event: timeline.events.append((time.monotonic() - start, event)))
    aborted = False
    try:
        async for sentence in agent.respond_streaming(prompt, on_event=sink):
            timeline.sentences.append((time.monotonic() - start, sentence))
            if stop_after is not None and len(timeline.sentences) >= stop_after:
                aborted = True
                await agent.abort()
    except Exception:
        # Some adapters end an aborted stream by raising; that still settles.
        if not aborted:
            raise
    timeline.finished_at = time.monotonic() - start
    return timeline


def _voice_checks(report: Report, timeline: Timeline) -> None:
    if not timeline.sentences:
        return
    first = timeline.sentences[0][0]
    report.add(
        CheckResult(
            "voice.first_sentence",
            "time to first sentence",
            "voice",
            "pass" if first <= FIRST_SENTENCE_TARGET_S else "warn",
            "" if first <= FIRST_SENTENCE_TARGET_S else f"target under {FIRST_SENTENCE_TARGET_S}s; stream text as it is generated",
            metric=first,
        )
    )
    marks = [text for _t, text in timeline.sentences if any(marker in text for marker in MARKDOWN_MARKERS)]
    report.add(
        CheckResult(
            "voice.plain_speech",
            "plain speech (no markdown)",
            "voice",
            "warn" if marks else "pass",
            f"markdown reaches speech: {marks[0][:60]!r}" if marks else "",
        )
    )
    long = [text for _t, text in timeline.sentences if len(text) > LONG_SENTENCE_CHARS]
    fragments = [text for _t, text in timeline.sentences if len(text.strip()) <= 2]
    detail = ""
    status: Status = "pass"
    if long:
        status, detail = "warn", f"{len(long)} sentence(s) over {LONG_SENTENCE_CHARS} characters delay speech"
    elif len(fragments) > max(3, len(timeline.sentences) // 2):
        status, detail = "warn", "many tiny fragments; send assistant.delta text and let Sleeptalk split sentences"
    report.add(CheckResult("voice.sentence_shape", "sentence shape", "voice", status, detail))
    preambles = [text for _t, text in timeline.sentences if looks_like_preamble(text)]
    report.add(
        CheckResult(
            "voice.no_preamble",
            "no self-made filler",
            "voice",
            "warn" if preambles else "pass",
            f"{preambles[0]!r} duplicates the host's own filler" if preambles else "",
        )
    )
    moments = sorted([t for t, _ in timeline.sentences] + [t for t, _ in timeline.events] + [timeline.finished_at])
    gaps = [later - earlier for earlier, later in zip([0.0, *moments], moments)]
    gap = max(gaps, default=0.0)
    report.add(
        CheckResult(
            "voice.silent_gap",
            "longest silent gap",
            "voice",
            "pass" if gap <= SILENT_GAP_TARGET_S else "warn",
            "" if gap <= SILENT_GAP_TARGET_S else "send tool or status frames during long work so Sleeptalk can narrate",
            metric=gap,
        )
    )


async def _live(report: Report, factory: AgentFactory, manifest: AgentCapabilityManifest | None) -> None:
    timeline_holder: dict[str, Timeline] = {}

    async def stream(agent: Any) -> CheckResult:
        timeline = await _timed_turn(agent, LIVE_HELLO)
        timeline_holder["hello"] = timeline
        if not timeline.sentences:
            raise ConformanceFailure("respond_streaming finished without a sentence")
        thinking = next((t for t, e in timeline.events if isinstance(e, ThinkingStarted)), None)
        text = next((t for t, e in timeline.events if isinstance(e, AssistantDelta)), None)
        if thinking is not None and text is not None and thinking > text:
            raise ConformanceFailure("thinking must precede text when both occur")
        return CheckResult("stream.sentence", "speaks a reply", "protocol", "pass", metric=timeline.finished_at)

    await _run_case(report, factory, "stream.sentence", "speaks a reply", stream, warmup=True, timeout=LIVE_TURN_TIMEOUT_S)
    if "hello" in timeline_holder:
        _voice_checks(report, timeline_holder["hello"])
        if manifest is not None and manifest.usage.level is SupportLevel.SUPPORTED:
            seen = any(isinstance(e, UsageReported) for _t, e in timeline_holder["hello"].events)
            report.add(CheckResult("usage.reported", "usage reported", "protocol", "pass" if seen else "fail", "" if seen else "usage advertised SUPPORTED but no usage.reported"))

    async def abort_recovers(agent: Any) -> CheckResult:
        first = await _timed_turn(agent, LIVE_LONG, stop_after=1)
        if not first.sentences:
            raise ConformanceFailure("abort test needs at least one sentence")
        if len(first.sentences) > 1:
            raise ConformanceFailure(f"{len(first.sentences) - 1} sentence(s) spoken after abort")
        second = await asyncio.wait_for(_timed_turn(agent, LIVE_AFTER_ABORT), timeout=LIVE_TURN_TIMEOUT_S)
        if not second.sentences:
            raise ConformanceFailure("the turn after an abort produced no sentence")
        recovery = second.sentences[0][0]
        status: Status = "pass" if recovery <= ABORT_SETTLE_TARGET_S + FIRST_SENTENCE_TARGET_S else "warn"
        return CheckResult(
            "abort.recovers",
            "abort mid-reply, then answer again",
            "protocol",
            status,
            "" if status == "pass" else "the next turn waited on the aborted one; acknowledge turn.abort promptly",
            metric=recovery,
        )

    await _run_case(report, factory, "abort.recovers", "abort mid-reply, then answer again", abort_recovers, warmup=True, timeout=LIVE_TURN_TIMEOUT_S * 2)

    if manifest is not None and manifest.tool_lifecycle.level is SupportLevel.SUPPORTED:
        async def tools(agent: Any) -> CheckResult:
            timeline = await _timed_turn(agent, LIVE_TOOL)
            seen = [e for _t, e in timeline.events if isinstance(e, ToolEvent)]
            if not seen:
                return CheckResult("tool.lifecycle", "tool events", "protocol", "warn", "tool_lifecycle is advertised but no tool frame was observed for this prompt")
            return CheckResult("tool.lifecycle", "tool events", "protocol", "pass")

        await _run_case(report, factory, "tool.lifecycle", "tool events", tools, warmup=True, timeout=LIVE_TURN_TIMEOUT_S)
    for name in ("permissions", "clarification", "steering"):
        if manifest is not None and getattr(manifest, name).level is SupportLevel.SUPPORTED:
            report.add(CheckResult(f"{name}.live", name, "protocol", "skip", "not exercised live because it would act on your workspace; prove it with --fixture"))


# ---------------------------------------------------------------------------
# Fixture checks
# ---------------------------------------------------------------------------


async def _fixture(report: Report, factory: AgentFactory) -> None:
    pairs = [
        ("stream.sentence", "speaks a reply", cases.case_stream_yields_sentence),
        ("abort.mid_stream", "abort stops further sentences", cases.case_abort_mid_stream_stops),
        ("abort.settles", "abort settles a hung turn", cases.case_timeout_cancel_settles),
        ("order.thinking", "thinking before text", cases.case_thinking_before_text),
        ("tool.lifecycle", "tool events", cases.case_tool_lifecycle),
        ("permissions", "permission request and decision", cases.case_permissions),
        ("clarification", "clarification request and answer", cases.case_clarification),
        ("steering", "steering", cases.case_steering),
        ("usage.reported", "usage reported", cases.case_usage),
    ]
    for check_id, title, case in pairs:
        await _run_case(report, factory, check_id, title, case, warmup=True, timeout=30.0)


async def run_adapter_test(
    factory: AgentFactory,
    *,
    adapter: str,
    mode: Mode = "offline",
) -> Report:
    report = Report(adapter=adapter, mode=mode)
    await _offline(report, factory)
    if mode == "live":
        await _live(report, factory, report.manifest)
    elif mode == "fixture":
        await _fixture(report, factory)
    return report


__all__ = ["CheckResult", "Report", "run_adapter_test"]
