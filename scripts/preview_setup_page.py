"""Walk an agent's key request on the settings page in your own browser, with no relay and no VPS.

Serves the real pages against a throwaway host: keys are "validated" by a
fake that accepts any value, Discord is answered by a fake with one server
and two channels, and everything lands in a temporary state directory that
is deleted on exit. Nothing you type reaches a provider.

    uv run python scripts/preview_setup_page.py                 # open provider choice
    uv run python scripts/preview_setup_page.py --mode preferred  # Deepgram named by the agent
    uv run python scripts/preview_setup_page.py --mode fixed      # a requested voice locks Cartesia
    uv run python scripts/preview_setup_page.py --mode reuse      # speech key reused from another profile
    uv run python scripts/preview_setup_page.py --mode discord    # Discord bot: token, server, channel, owner

The phone shows this same page through the sealed channel; narrow the window
to see its layout.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import time
import functools
import webbrowser
from pathlib import Path

print = functools.partial(print, flush=True)  # the URL must show even when piped

MODES = ("open", "preferred", "fixed", "reuse", "discord")


def spec_for(mode: str, root: Path, profile_path: Path):
    from sleeptalk.private_input import InputSpec

    base = dict(request_id=f"preview-{mode}-{int(time.time())}", profile="discord" if mode == "discord" else "voice",
                workspace=str(root), agent="hermes", surface="discord" if mode == "discord" else "web",
                profile_path=str(profile_path))
    extra = {
        "open": {},
        "preferred": {"preferred_provider": "deepgram"},
        "fixed": {"stt_provider": "cartesia", "tts_provider": "cartesia", "tts_voice_id": "62ae83ad-4f6a-430b-af41-a9bede9286ca"},
        "reuse": {"stt_provider": "cartesia", "tts_provider": "cartesia", "speech_from_profile": "default"},
        "discord": {"stt_provider": "cartesia", "tts_provider": "cartesia", "speech_from_profile": "default"},
    }[mode]
    return InputSpec(**base, **extra)


def install_fakes():
    """Providers and Discord answer instantly and never see a real key."""
    from sleeptalk import discord_check

    joined = {"ready": False}

    async def list_guilds(token):
        return [discord_check.Guild(id="456", name="Preview server")]

    async def list_voice_channels(token, guild_id):
        return [discord_check.VoiceChannel(id="789", name="lounge", eligible=True, missing_permissions=()),
                discord_check.VoiceChannel(id="790", name="stage", eligible=False, missing_permissions=("connect",))]

    discord_check.list_guilds = list_guilds
    discord_check.list_voice_channels = list_voice_channels

    async def validate(provider, value, spec, voice):
        await asyncio.sleep(0.8)  # long enough to see "Checking…"
        if provider == "discord":
            return {"application": {"id": "123", "name": "Preview Bot", "invite_url": "https://discord.com/oauth2/authorize?client_id=123"},
                    "guilds": []}
        return {"stt_model": "preview", "voice_id": voice or "preview-voice"}

    from sleeptalk import runtime_probe, setup

    async def ready(*args, **kwargs):
        return {"status": "ready"}

    runtime_probe.probe_runtime = ready
    setup.ensure_dependencies = lambda *args, **kwargs: ()
    return validate, joined


def make_session(mode: str, root: Path):
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.private_input import InputSession, atomic_json

    validate, _ = install_fakes()
    store = CredentialStore(root / "store", backend="file")
    profiles = root / ".sleeptalk" / "profiles"
    profiles.mkdir(parents=True)
    spec = spec_for(mode, root, profiles / f"{'discord' if mode == 'discord' else 'voice'}.toml")
    refs, details = {}, {}
    if spec.speech_from_profile:
        ref = store.put_validated("cartesia", "preview-key", request_id="preview:cartesia")
        refs = {"cartesia": ref}
        details = {"cartesia": {"stt_model": "ink-2", "voice_id": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"}}
    directory = root / "setup" / spec.request_id
    atomic_json(directory / "request.json", spec.model_dump())
    atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(), "expires_at": time.time() + 15 * 60,
                                           "refs": refs, "details": details})
    return InputSession(directory, store=store, validator=validate)


async def serve(session, open_browser: bool) -> None:
    """The settings page, opened on this request, on this Mac's loopback.

    The phone gets the same page (the same markup, styles, and script)
    through the sealed channel, so this shows what the phone shows; narrow
    the browser window to see the phone layout.
    """
    from sleeptalk.settings_page import SettingsPage

    page = SettingsPage(session.profile_path, profiles_dir=session.profile_path.parent, environ={},
                        state_dir=session.directory.parent / "page-state", request=session)

    def ready(url: str) -> None:
        print(f"Key request page: {url}")
        if open_browser:
            webbrowser.open(url)
        print("Ctrl-C to stop.")

    await page.serve(open_browser=False, ready=ready)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="open")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL only.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sleeptalk-preview-") as temporary:
        root = Path(temporary)
        os.environ["HOME"] = str(root)
        os.environ["SLEEPTALK_HOME"] = str(root / ".sleeptalk")
        session = make_session(args.mode, root)
        print(f"Mode: {args.mode}. Throwaway state in {root}; any key is accepted; nothing reaches a provider.")
        try:
            asyncio.run(serve(session, not args.no_browser))
        except KeyboardInterrupt:
            pass
        if session.profile_path.exists():
            print(f"\nThe page wrote this profile (deleted with the temp dir):\n{session.profile_path.read_text()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
