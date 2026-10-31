"""A throwaway Sleeptalk home, one test profile, and a local web call.

Prints the call's receipt as JSON. Run from the repo:

    uv run python scripts/synthetic_call/start_call.py <run_dir> [--agent omp] [--speech openrouter]
        [--language en-US] [--voice ara] [--conversation-id ID] [--openclaw-read]

The profile references the saved OpenRouter key named by
SYNTH_OPENROUTER_PROFILE (see qa_env.py); no key value is copied. The agent
gets an empty workspace inside <run_dir>, so it can't touch the repo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_env import load_speech_keys, openrouter_ref  # noqa: E402


def speech_tables(args) -> tuple[str, str]:
    """[stt]/[tts] (or the Realtime route) for one service, as a new profile gets them."""
    lang = args.language
    if args.speech == "live":
        return "", 'type = "hybrid"\nmodel = "gpt-live-1"'
    if args.speech == "realtime":
        # OpenAI Realtime hears and speaks; the agent answers (docs/realtime-hybrid.md).
        return "", 'type = "hybrid"\naudio_first = true'
    if args.speech == "local":
        # On this computer: Moonshine listens, Kokoro speaks (models already downloaded).
        return ('[stt]\nprovider = "local"\nmodel = "moonshine-small-streaming-en"\nlanguage = "en-US"\n\n'
                '[tts]\nprovider = "local"\nmodel = "kokoro-en"\nvoice_id = "af_heart"\nlanguage = "en-US"\nspeed = 1.0\n'), 'type = "cascade"'
    from sleeptalk import speech_catalog
    from sleeptalk.speech_choice import for_new_profile

    choice = for_new_profile(args.speech, lang)
    voice = args.voice if args.speech == "openrouter" and args.voice else speech_catalog.recommended_voice(args.speech, lang)
    tts_model = choice.tts_model or voice  # Deepgram: the voice is the model, as setup writes it
    stt_base = tts_base = ""
    if args.fault_proxy:
        # fault_proxy.py in front of the provider: /stt and /tts fail on request.
        base = args.fault_proxy.rstrip("/")
        stt_base, tts_base = f'base_url = "{base}/stt"\n', f'base_url = "{base}/tts"\n'
    return (f'[stt]\nprovider = "{args.speech}"\nmodel = "{choice.stt_model}"\nlanguage = "{lang}"\n{stt_base}\n'
            f'[tts]\nprovider = "{args.speech}"\nmodel = "{tts_model}"\nvoice_id = "{voice}"\nlanguage = "{lang}"\n{tts_base}'), 'type = "cascade"'


SEED = {
    "README.md": "# Lantern\n\nLantern is a tiny lighthouse simulator.\n",
    "lamp.py": "def brightness(level):\n    # TODO: add dimming at dawn\n    return level * 10\n",
    "beam.py": "def rotate(angle):\n    # TODO: rotate faster in fog\n    return (angle + 15) % 360\n",
    "tide.py": "def height(hour):\n    # TODO: handle storm surges\n    return 2 if hour % 12 < 6 else 1\n",
    "settings.json": "{\"lamps\": 3}\n",
}


def seed_workspace(workspace: Path) -> None:
    """What the tool scenarios ask about: the project is Lantern, three
    Python files, three TODOs (analyze_tools.py knows these answers)."""
    for name, text in SEED.items():
        (workspace / name).write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument("--voice", default="", help="a voice ID; default: the language's recommended voice")
    parser.add_argument("--language", default="en-US")
    parser.add_argument("--barge-in", default="", help="policy.barge_in override (soft|immediate)")
    parser.add_argument("--agent", default="omp", help="a setup agent id: omp pi hermes claude_code codex cursor openclaw model")
    parser.add_argument("--speech", default="openrouter", help="openrouter | deepgram | cartesia | realtime | live | local")
    parser.add_argument("--key-profile", default="", help="read an existing profile’s OpenAI credential into this process only")
    parser.add_argument("--conversation-id", default="", help="continue this agent conversation (Hermes session ID)")
    parser.add_argument("--openclaw-read", action="store_true",
                        help="OpenClaw with the gateway token read from its config (the page's 'read' choice), fresh conversation")
    parser.add_argument("--fault-proxy", default="", help="fault_proxy.py's address: speech goes through it")
    parser.add_argument("--fault-control", default="", help="with --agent fault: the control.json fault_agent.py reads")
    parser.add_argument("--seed", action="store_true",
                        help="seed the workspace for the tool scenarios (a README, three Python files with TODOs)")
    parser.add_argument("--permission-mode", default="", help="the agent's permission mode (e.g. ask)")
    args = parser.parse_args()

    # Deepgram, Cartesia and OpenAI keys go into this process's environment
    # only; the detached call host inherits them. OpenRouter uses the saved key's reference.
    load_speech_keys()
    if args.key_profile:
        from sleeptalk.builder import _load_env_from_config
        from sleeptalk.config import HybridRouteConfig, load_resolved_config, resolve_credential_env
        key_config = load_resolved_config(args.key_profile, check_env=False, check_binaries=False, verify_binding=False)
        key_config = key_config.model_copy(update={"route": HybridRouteConfig(model="gpt-live-1")})
        key_env = resolve_credential_env(key_config, _load_env_from_config(key_config))
        if not key_env.get("OPENAI_API_KEY"):
            raise SystemExit("The selected profile has no available OpenAI key")
        os.environ["OPENAI_API_KEY"] = key_env["OPENAI_API_KEY"]
        key_env.clear()

    run = Path(args.run_dir).resolve()
    home = run / "home"
    workspace = run / "workspace"
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    if args.seed:
        seed_workspace(workspace)
    os.environ["SLEEPTALK_HOME"] = str(home)
    os.chdir(workspace)

    ref = openrouter_ref()
    policy = '[policy]\nnarration_style = "keyboard"\n'
    if args.barge_in:
        policy += f'\n[policy.interrupt]\nmode = "{args.barge_in}"\n'
    # The agent tables exactly as setup writes them (wizard.profile_toml), with
    # the choices the settings page makes: Hermes starts per call, OpenClaw
    # without a gateway token (`openclaw acp`), Just a model on OpenRouter
    # reusing the speech key's reference (nothing new is stored).
    from sleeptalk.profile_edit import toml_tables
    from sleeptalk.wizard import answers_from_mapping, profile_toml

    agent_type = {"hermes": "hermes_gateway", "fault": "omp"}.get(args.agent, args.agent)
    answers = answers_from_mapping({
        "name": "synthetic-qa", "slug": "synthetic-qa", "surface": "web", "route_type": "cascade",
        "agent_type": agent_type, "workspace_dir": str(workspace), "language": args.language,
        "speech_provider": "openrouter",
    })
    if args.permission_mode:
        answers.agent_permission_mode = args.permission_mode
    if args.agent == "openclaw" and args.openclaw_read:
        from sleeptalk.wizard import OPENCLAW_TOKEN_ENV, read_openclaw_gateway_token

        # In this process's environment only, as the profile's .env would hold it.
        token = read_openclaw_gateway_token()
        if not token:
            raise SystemExit("no OpenClaw gateway token in its config")
        os.environ[OPENCLAW_TOKEN_ENV] = token
        answers.openclaw_token = ""  # never written into the profile
        answers.gateway_session_id = ""  # fresh conversation, not the user's main one
    if args.agent == "model":
        from sleeptalk import model_only_setup

        from sleeptalk.credential_store import CredentialStore

        # As setup does: the key stored once more under the model endpoint's
        # scope (the speech entry can't be used for it). run.sh deletes this
        # entry after the run (remove_model_key.py); its reference is kept in the run dir.
        store = CredentialStore()
        auth = model_only_setup.auth_choice("openrouter", "")
        model_ref = model_only_setup.store_key(store, auth, store.get(ref))
        (run / "model_ref.txt").write_text(model_ref + "\n")
        answers.model_vendor = "openrouter"
        answers.model = model_only_setup.default_model("openrouter")
        answers.model_credential_ref = model_ref
    agent_tables = "".join(piece for name, piece in toml_tables(profile_toml(answers))
                           if name == "agent" or name.startswith("agent."))
    if args.agent == "fault":
        # A command agent that fails on request (fault_agent.py).
        script = Path(__file__).resolve().parent / "fault_agent.py"
        agent_tables = (f'[agent]\ntype = "command"\nworkspace_dir = {json.dumps(str(workspace))}\n\n'
                        f'[agent.options]\ncommand = {json.dumps([sys.executable, str(script), args.fault_control])}\n')
    speech, route = speech_tables(args)
    text = f'''{agent_tables.rstrip()}

[route]
{route}

[transport]
type = "web"

{policy}
{speech}
[log]
latency = true
level = {json.dumps(os.environ.get("SYNTH_LOG_LEVEL", "INFO"))}

[credentials.refs]
OPENROUTER_API_KEY = {json.dumps(ref)}
'''
    from sleeptalk.config import AgentConfig, packaged_example_root
    from sleeptalk.runtime_binding import bind, toml

    # The shared base every profile composes with, as setup writes it.
    root = home / "sleeptalk.toml"
    if not root.exists():
        root.write_bytes(packaged_example_root().read_bytes())

    agent = AgentConfig.model_validate(tomllib.loads(text)["agent"])
    text = text.replace("\n[credentials.refs]", toml(bind(agent)) + "\n[credentials.refs]")
    if args.speech != "openrouter" and args.agent != "model":
        # Keys come from the environment; the profile references none.
        text = text.replace("\n[credentials.refs]\nOPENROUTER_API_KEY = " + json.dumps(ref) + "\n", "\n")
    profile = home / "profiles" / "synthetic-qa.toml"
    profile.write_text(text)
    profile.chmod(0o600)

    from sleeptalk.host import start_voice_web

    receipt = start_voice_web(profile="synthetic-qa", config=str(profile), local=True,
                              platform_user_id="synthetic-qa", workspace=str(workspace),
                              conversation_id=args.conversation_id)
    print(json.dumps({"home": str(home), "profile": str(profile), **receipt}))
    return 0 if receipt.get("join_url") else 1


if __name__ == "__main__":
    sys.exit(main())
