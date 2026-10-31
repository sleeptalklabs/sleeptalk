"""Thin WebRTC transport around the canonical :class:`RealtimeSession`.

Browser code owns capture, VAD, WebRTC playout, UI projection, and call
delivery. STT, agent work, TTS, voice policy, cancellation epochs, and turn
accounting remain behind the public session boundary.

Room/playout live in :mod:`sleeptalk.web_voice`; this module keeps the profile call
entrypoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Callable

from aiortc import MediaStreamError
from sleeptalk.builder import _load_env_from_config, build_session
from sleeptalk.call_provider import (
    CallContext,
    CallEnded,
    CallProvider,
    CallProviderError,
    P2PCallProvider,
)
from sleeptalk.config import ConfigError, SleeptalkConfig, load_runtime_config
from sleeptalk.transports.terminal_visual import agent_display_name, speaker_tag, stack_from_config

from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace
from sleeptalk.web_voice.playback import WebRTCPlaybackController
from sleeptalk.web_voice.session import BrowserTransportState, WebCallSession

LOCAL_PLATFORM_USER_ID = "local-user"


def _context_for_profile(
    config: SleeptalkConfig, context: CallContext, *, env: dict[str, str] | None = None
) -> CallContext:
    from sleeptalk.agents.construction import resolve_agent

    agent = config.agent
    registration = resolve_agent(
        agent, _load_env_from_config(config) if env is None else env
    ).registration
    if registration.conversation_context is not None:
        conversation_id = registration.conversation_context(agent, context.conversation_id)
    else:
        conversation_id = context.conversation_id.strip() or (
            f"{agent.type}:{agent.slug}:new-web-call"
        )
    return CallContext(
        agent_instance=context.agent_instance,
        conversation_id=conversation_id,
        # Local browser calls have no chat platform behind them; the ticket
        # still requires an identifier, and this one never leaves the machine's
        # own call.
        platform_user_id=context.platform_user_id.strip() or LOCAL_PLATFORM_USER_ID,
    )


async def _record_browser_telemetry(
    stream, trace: VoiceLatencyTrace, state: BrowserTransportState, on_progress=None
) -> None:
    receive = getattr(stream, "recv_telemetry", None)
    if not callable(receive):
        return
    while True:
        event = await receive()
        if event is None:
            return
        state.update(event)
        stage = str(event.get("stage", "unknown"))
        observed = {
            "ticket_redeemed": "link_consumed",
            "talk_tapped": "waiting_for_permission",
            "microphone_ready": "microphone_ready",
            "peer_connected": "browser_connected",
            "remote_audio_playing": "media_playing",
        }.get(stage)
        if observed is not None and on_progress is not None:
            on_progress(observed)
        fields = {key: value for key, value in event.items() if key != "stage"}
        trace.transport = state.transport
        trace.mark(f"browser_{stage}", **fields)


async def run_profile_web_call(
    *,
    config_path: str,
    rendezvous_url: str,
    context: CallContext,
    ttl_seconds: int,
    on_ready: Callable[[str], None],
    local: bool = False,
    on_progress: Callable[[str], None] | None = None,
    tailnet: bool = False,
) -> None:
    """Host one bounded browser call on the profile's canonical session."""
    if tailnet:
        from sleeptalk.tailnet import published_provider

        async with published_provider() as provider:
            await _run_profile_web_call(
                config_path=config_path,
                context=context,
                ttl_seconds=ttl_seconds,
                on_ready=on_ready,
                provider=provider,
                on_progress=on_progress,
            )
        return
    if local:
        from sleeptalk.local_rendezvous import LocalCallProvider

        async with LocalCallProvider() as provider:
            await _run_profile_web_call(
                config_path=config_path,
                context=context,
                ttl_seconds=ttl_seconds,
                on_ready=on_ready,
                provider=provider,
                on_progress=on_progress,
            )
        return
    await _run_profile_web_call(
        config_path=config_path,
        context=context,
        ttl_seconds=ttl_seconds,
        on_ready=on_ready,
        provider=P2PCallProvider(rendezvous_url),
        on_progress=on_progress,
    )


logger = logging.getLogger("sleeptalk.web_call")

# When this process's call ended (monotonic): the call host logs how long it
# took from there to exit, the window in which the profile reads as busy.
call_ended_at: float | None = None


@contextlib.contextmanager
def _quiet_relay_teardown():
    """Keep the relay's end-of-call refusals out of the host log.

    The hosted relay revokes a call's TURN credential the moment the call
    ends. aioice may still be sending through it and fails that send with a
    401 in a task nobody awaits, which Python prints as a traceback. That
    one case becomes a log line; every other error is handled as before.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        future = context.get("future")
        coro = future.get_coro() if isinstance(future, asyncio.Task) else None
        name = getattr(coro, "__qualname__", "")
        if type(exc).__name__ == "TransactionFailed" and name.endswith("send_data") \
                and type(exc).__module__.startswith("aioice"):
            logger.info("TURN relay refused a send (its credential is revoked when the call ends): %s", exc)
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(previous)


async def _run_profile_web_call(
    *,
    config_path: str,
    context: CallContext,
    ttl_seconds: int,
    on_ready: Callable[[str], None],
    provider: CallProvider,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    try:
        config = load_runtime_config(config_path, surface="web")
        effective_context = _context_for_profile(config, context)
    except ConfigError as exc:
        raise CallProviderError(str(exc)) from exc

    # Keep construction as an observable configuration boundary for callers
    # and acceptance probes; readiness failures receive the human-facing link
    # error below.
    runtime = build_session(config)
    with _quiet_relay_teardown():
        await run_web_session(
            runtime=runtime,
            config=config,
            context=effective_context,
            ttl_seconds=ttl_seconds,
            on_ready=on_ready,
            provider=provider,
            on_progress=on_progress,
        )


async def run_web_session(
    *,
    runtime,
    config: SleeptalkConfig,
    context: CallContext,
    ttl_seconds: int,
    on_ready: Callable[[str], None],
    provider: CallProvider,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Deliver a browser link and run the already assembled session."""
    browser_session = None
    try:
        await runtime.prepare()
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await runtime.aclose()
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await runtime.aclose()
        raise CallProviderError(
            "The configured session runtime is unavailable, so no call link was sent: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if on_progress is not None:
        on_progress("runtime_ready")
    try:
        ticket = await provider.mint(context, ttl_seconds=ttl_seconds)
    except BaseException:
        with contextlib.suppress(Exception):
            await runtime.aclose()
        raise
    if on_progress is not None and "ticket.redeemed" not in getattr(ticket, "signal_types", ()):
        on_progress("redemption_notifications_unavailable")
    call_trace = VoiceLatencyTrace(
        surface=f"{config.agent.slug}-web",
        transport="web-webrtc",
        persona=config.agent.slug,
        metadata={
            "callProvider": "webrtc",
            "callId": hashlib.sha256(ticket.token.encode()).hexdigest()[:12],
        },
    )
    call_trace.mark("ticket_minted")
    try:
        on_ready(ticket.join_url)
    except BaseException:
        with contextlib.suppress(Exception):
            await runtime.aclose()
        raise
    try:
        async with provider.connect(ticket) as stream:
            call_trace.mark("agent_signal_connected")
            if on_progress is not None:
                on_progress("host_signaling_connected")
            transport_state = BrowserTransportState()
            end_reason = "unknown"
            telemetry_task = asyncio.create_task(
                _record_browser_telemetry(stream, call_trace, transport_state, on_progress),
                name="browser-transport-telemetry",
            )
            try:
                browser_session = WebCallSession(
                    session=runtime,
                    stream=stream,
                    call_sid=ticket.token,
                    surface=f"{config.agent.slug}-web",
                    persona=config.agent.slug,
                    transport_state=transport_state,
                    stack=stack_from_config(config),
                    speaker=speaker_tag(agent_display_name(config.agent)),
                    profile_path=str(getattr(config, "source_path", "") or ""),
                    **({"on_progress": on_progress} if on_progress is not None else {}),
                )
                while True:
                    await browser_session.feed_audio_frame(await stream.recv())
            except (CallEnded, MediaStreamError) as exc:
                global call_ended_at
                call_ended_at = time.monotonic()
                # Why a call ended is the first question a "it dropped on its
                # own" report asks. Without it every unexpected hangup and
                # every clean goodbye land in the log identically.
                end_reason = getattr(exc, "reason", "") or "media_stream_error"
                if browser_session is not None:
                    # The peer has already ended the media stream. Cancel kernel
                    # work without erasing frames that were successfully handed
                    # to the transport; barge-in still clears queued playback.
                    await browser_session.interrupt_presentation(clear_output=False)
            finally:
                close_started = time.monotonic()
                try:
                    if browser_session is not None:
                        await browser_session.close()
                finally:
                    telemetry_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await telemetry_task
                    call_trace.transport = transport_state.transport
                    call_trace.finish(
                        "call_complete",
                        endReason=end_reason,
                        endDetail=getattr(stream, "end_detail", ""),
                        signalReconnects=getattr(stream, "signal_reconnects", 0),
                        # The session's own shutdown (agent, speech, playback).
                        closeMs=round((time.monotonic() - close_started) * 1000),
                        **transport_state.fields(),
                    )
    finally:
        if browser_session is None and runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.aclose()


# Prefer package registration for standalone installs.
try:
    from sleeptalk.webcalls import register_web_call, web_call_registered

    if not web_call_registered():
        register_web_call(run_profile_web_call)
except Exception:
    pass
