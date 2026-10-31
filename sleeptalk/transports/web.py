"""Browser transport using the same assembled session as other surfaces."""

from __future__ import annotations

from typing import Literal, TYPE_CHECKING

from pydantic import Field

from sleeptalk.registries import ComponentOptions

if TYPE_CHECKING:
    from sleeptalk.config import SleeptalkConfig
    from sleeptalk.session import RealtimeSession


class WebOptions(ComponentOptions):
    local: bool = True
    rendezvous_url: str = ""
    # "tailnet" publishes the loopback call server on this host's tailnet with
    # `tailscale serve` for every call; "local" opens every call on this
    # computer (localhost), links the agent sends included; empty keeps the
    # launch's own choice.
    delivery: Literal["", "tailnet", "local"] = ""
    ttl_seconds: int = Field(default=300, gt=0, le=3600)


class WebTransport:
    def __init__(self, config: SleeptalkConfig, env: dict[str, str]) -> None:
        self.config = config
        self.env = env
        self.options = WebOptions.model_validate(config.transport.options)

    async def run(self, session: RealtimeSession) -> None:
        from sleeptalk.call_provider import (
            CallContext,
            DEFAULT_RENDEZVOUS_URL,
            P2PCallProvider,
            RENDEZVOUS_URL_ENV,
        )
        from sleeptalk.web_call import _context_for_profile, run_web_session

        context = _context_for_profile(
            self.config,
            CallContext(agent_instance=self.config.agent.slug, conversation_id="", platform_user_id=""),
            env=self.env,
        )

        async def run(provider) -> None:
            await run_web_session(
                runtime=session,
                config=self.config,
                context=context,
                ttl_seconds=self.options.ttl_seconds,
                on_ready=lambda url: print(url, flush=True),
                provider=provider,
            )

        if self.options.local:
            from sleeptalk.local_rendezvous import LocalCallProvider

            async with LocalCallProvider() as provider:
                await run(provider)
        else:
            url = self.options.rendezvous_url or self.env.get(
                RENDEZVOUS_URL_ENV, DEFAULT_RENDEZVOUS_URL
            )
            await run(P2PCallProvider(url))
