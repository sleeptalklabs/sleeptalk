"""A local proxy in front of a speech provider that fails on request.

    uv run python scripts/synthetic_call/fault_proxy.py <port> <control.json> [upstream]

Profiles point [stt] base_url at http://127.0.0.1:<port>/stt and [tts]
base_url at http://127.0.0.1:<port>/tts; both forward to `upstream`
(OpenRouter by default). POST /_fault {"stt": "429", "tts": "pass",
"agent": "crash"} sets what happens next, and is written to control.json,
which fault_agent.py reads too.

Faults: pass, 401, 429, 500, stall (no answer for 90 s), cut (half the
body, then the connection drops), garble (200 with a body that isn't one).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

# aiohttp decompresses upstream bodies, so their encoding headers must not pass on.
HOP = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding", "content-encoding"}


def main() -> None:
    port, control = int(sys.argv[1]), Path(sys.argv[2])
    upstream = (sys.argv[3] if len(sys.argv) > 3 else "https://openrouter.ai/api/v1").rstrip("/")
    state = {"stt": "pass", "tts": "pass", "agent": "ok"}
    control.write_text(json.dumps(state))
    seen: list[dict] = []

    async def set_fault(request: web.Request) -> web.Response:
        state.update(await request.json())
        control.write_text(json.dumps(state))
        return web.json_response(state)

    async def log(request: web.Request) -> web.Response:
        return web.json_response(seen)

    async def forward(request: web.Request) -> web.StreamResponse:
        side = request.match_info["side"]
        fault = state.get(side, "pass")
        seen.append({"side": side, "path": request.match_info["path"], "fault": fault})
        body = await request.read()
        if fault in {"401", "429", "500"}:
            message = {"401": "No auth credentials found", "429": "Rate limit exceeded",
                       "500": "Internal server error"}[fault]
            return web.json_response({"error": {"message": message, "code": int(fault)}}, status=int(fault))
        if fault == "garble":
            return web.Response(status=200, body=b"<html>upstream maintenance</html>", content_type="text/html")
        if fault == "stall":
            await asyncio.sleep(90)
            return web.json_response({"error": {"message": "stalled"}}, status=504)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        async with aiohttp.ClientSession() as session:
            async with session.request(request.method, f"{upstream}/{request.match_info['path']}",
                                       headers=headers, data=body) as upstream_response:
                response = web.StreamResponse(status=upstream_response.status, headers={
                    k: v for k, v in upstream_response.headers.items() if k.lower() not in HOP})
                await response.prepare(request)
                sent = 0
                async for chunk in upstream_response.content.iter_chunked(4096):
                    if fault == "cut" and sent > 0 and sent + len(chunk) > 12_000:
                        # Half a reply, then the connection is gone.
                        request.transport.close()
                        return response
                    await response.write(chunk)
                    sent += len(chunk)
                await response.write_eof()
                return response

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/_fault", set_fault)
    app.router.add_get("/_log", log)
    app.router.add_route("*", "/{side:stt|tts}/{path:.*}", forward)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
