"""Run the reference Agent Bridge remote agent on loopback."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sleeptalk"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import EchoRemoteAgent  # noqa: E402
from sleeptalk.agent_bridge import AgentBridgeServer  # noqa: E402


async def _run(args: argparse.Namespace) -> None:
    server = AgentBridgeServer(token=args.token, agent=EchoRemoteAgent())
    if args.transport == "ws":
        host, port = await server.start_ws(args.host, args.port)
        printed = f"ws://{host}:{port}/v1/agent-bridge"
    else:
        host, port = await server.start_tcp(args.host, args.port)
        printed = f"tcp://{host}:{port}"
    print(f"agent-bridge listening on {printed}", flush=True)
    try:
        await server.wait_closed()
    finally:
        await server.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reference Agent Bridge remote agent")
    parser.add_argument("--token", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--transport", choices=("tcp", "ws"), default="tcp")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
