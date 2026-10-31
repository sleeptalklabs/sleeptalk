"""Bounded GPT-Live account check; uses a saved profile's key without changing it.

Usage: uv run python scripts/probe_openai_live.py --config PATH
This proves startup/finalization only. It does not open a microphone or restart services.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleeptalk.builder import _load_env_from_config
from sleeptalk.config import HybridRouteConfig, load_resolved_config, resolve_credential_env
from sleeptalk.openai_live import OpenAILiveClient
from sleeptalk.openai_realtime import OpenAIRealtimeConfig


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_resolved_config(args.config, check_env=False, check_binaries=False, verify_binding=False)
    config = config.model_copy(update={"route": HybridRouteConfig(model="gpt-live-1")})
    env = resolve_credential_env(config, _load_env_from_config(config))
    key = env.get("OPENAI_API_KEY", "")
    if not key:
        print(json.dumps({"status": "unavailable", "reason": "No OpenAI key referenced by this profile"}))
        return 2
    live = OpenAILiveClient(OpenAIRealtimeConfig(api_key=key, model="gpt-live-1"),
                            instructions="Wait quietly for the caller.")
    env.clear()
    try:
        await live.connect()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    finally:
        await live.close()
    print(json.dumps({"status": "ready" if live.finalized else "incomplete", "model": "gpt-live-1",
                      "finalized": live.finalized, "usage": live.usage}))
    return 0 if live.finalized else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
