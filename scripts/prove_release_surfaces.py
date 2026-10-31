#!/usr/bin/env python3
"""Prove the built release artifacts outside the checkout (plan 07).

Runs after prove_release_wheel.sh has built and audited one wheel and one
source archive. Every check installs the distributed bytes into a blank
environment under --workdir and never imports from the repository.

Checks:
  artifacts   rebuild reproducibility, a wheel built from the sdist alone,
              metadata/README validation, BUILD_RECORD.json
  surfaces    blank install per surface extra, first-time setup, offline
              doctor, bundled assets through their real loaders
  web call    a bounded local Web call started, served, listed, and stopped
  terminal    session construction for a Terminal profile without audio I/O
  discord     no-connect Discord startup from a blank home
  rollback    two immutable artifacts installed side by side; the previous
              runtime still runs and reports its own version

Speech providers use placeholder keys and are never contacted. The agent is
the bring-your-own-agent echo scaffold, so no model account is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_ANSWERS = (
    'name = "Release Proof"\n'
    'stt_key = "placeholder-deepgram"\n'
    'tts_key = "placeholder-cartesia"\n'
    'tts_voice_id = "placeholder-voice"\n'
)
SECRETS = ("placeholder-deepgram", "placeholder-cartesia", "placeholder-discord-token")


def log(message: str) -> None:
    print(message, flush=True)


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = True, capture: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(part) for part in args], cwd=cwd, env=env, text=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None,
    )
    if check and result.returncode != 0:
        output = result.stdout or ""
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(map(str, args))}\n{output[-4000:]}")
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_STUB_BIN: Path | None = None


def install_stub_binaries(work: Path) -> None:
    """Put stand-in `pi` and `ffmpeg` binaries first on PATH for every proof subprocess.

    The proof exercises setup, the doctor, and session construction, not the
    agent or ffmpeg themselves, so a blank runner must not need either installed.
    """
    global _STUB_BIN
    bin_dir = work / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("pi", "ffmpeg"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    _STUB_BIN = bin_dir


def outside_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment without the checkout on any import path."""
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}}
    if _STUB_BIN is not None:
        env["PATH"] = f"{_STUB_BIN}{os.pathsep}{env.get('PATH', os.defpath)}"
    env.update(extra or {})
    return env


class Venv:
    def __init__(self, path: Path, python: str) -> None:
        self.path = path
        run(["uv", "venv", "-q", "--python", python, path])
        self.python = path / "bin" / "python"
        self.sleeptalk = path / "bin" / "sleeptalk"

    def install(self, requirement: str) -> None:
        run(["uv", "pip", "install", "-q", "--python", self.python, requirement], env=outside_env())

    def freeze(self) -> list[str]:
        # Distribution metadata gives exact installed versions without uv's
        # diagnostics or direct-file URLs that reveal the build machine's paths.
        return json.loads(self.py(
            "import importlib.metadata as m, json; "
            "print(json.dumps(sorted(f'{d.name}=={d.version}' for d in m.distributions())))",
            cwd=self.path,
        ))

    def py(self, code: str, *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 120) -> str:
        result = run([self.python, "-c", code], cwd=cwd, env=outside_env(env), timeout=timeout)
        return result.stdout


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def prove_artifacts(work: Path, wheel: Path, sdist: Path, record: dict) -> Path:
    rebuild = work / "rebuild"
    run(["uv", "build", "-q", "--out-dir", rebuild], cwd=ROOT, env=outside_env())
    rebuilt = {path.suffix: sha256(path) for path in rebuild.iterdir() if path.suffix in {".whl", ".gz"}}
    reproducible = rebuilt.get(".whl") == sha256(wheel) and rebuilt.get(".gz") == sha256(sdist)
    record["reproducible_rebuild"] = reproducible
    if not reproducible:
        record["reproducible_rebuild_note"] = "a second build produced different bytes; investigate before claiming byte-for-byte reproducibility"
    log(f"artifacts: second build {'reproduced identical bytes' if reproducible else 'DIFFERED (recorded, not claimed)'}")

    extracted = work / "sdist-only"
    extracted.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter="data")
    (source,) = [path for path in extracted.iterdir() if path.is_dir()]
    from_sdist = work / "from-sdist"
    run(["uv", "build", "-q", "--wheel", "--out-dir", from_sdist, source], cwd=work, env=outside_env())
    (sdist_wheel,) = from_sdist.glob("*.whl")
    run([sys.executable, ROOT / "scripts" / "audit_release_contents.py", sdist_wheel], cwd=ROOT)
    record["wheel_from_sdist_sha256"] = sha256(sdist_wheel)
    log("artifacts: wheel built from the source archive alone matches tracked inputs: ok")

    run(["uvx", "--from", "twine>=5", "twine", "check", "--strict", wheel, sdist], cwd=work, env=outside_env())
    log("artifacts: metadata and README render (twine check --strict): ok")
    return source


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


def ensure(venv: Venv, home: Path, surface: str, work: Path, *, answers: str) -> Path:
    answers_file = work / f"answers-{surface}.toml"
    answers_file.write_text(answers)
    env = outside_env({"SLEEPTALK_HOME": str(home)})
    result = run([venv.sleeptalk, "host", "ensure", "--profile", "default", "--surface", surface,
                  "--route", "cascade", "--agent", "pi", "--answers", answers_file, "--json"], cwd=work, env=env)
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "ready", receipt
    assert receipt["surface"] == surface, receipt
    assert not any(secret in result.stdout for secret in SECRETS), "setup receipt leaked a credential"
    profile = home / "profiles" / "default.toml"
    assert profile.is_file(), profile
    return profile


def use_echo_agent(venv: Venv, profile: Path, work: Path) -> None:
    """Swap the profile's agent for the scaffolded bring-your-own echo agent."""
    agent_dir = work / f"echo-agent-{profile.parent.parent.name}"
    run([venv.sleeptalk, "adapter", "new", "echo", "--kind", "command", "--dir", agent_dir], cwd=work, env=outside_env())
    text = profile.read_text()
    replacement = (
        '[agent]\ntype = "command"\nname = "Echo"\nslug = "default"\n'
        f'workspace_dir = {json.dumps(str(work))}\n\n'
        f'[agent.options]\ncommand = {json.dumps([str(venv.python), str(agent_dir / "agent.py")])}\n'
    )
    text, count = re.subn(r"\[agent\].*?(?=\n\[)", replacement, text, count=1, flags=re.S)
    assert count == 1, "profile has no [agent] section"
    profile.write_text(text)


def doctor_offline(venv: Venv, profile: Path, home: Path, work: Path) -> None:
    result = run([venv.sleeptalk, "doctor", "--config", profile, "--offline", "--json"], cwd=work,
                 env=outside_env({"SLEEPTALK_HOME": str(home)}), check=False)
    report = json.loads(result.stdout.strip().splitlines()[-1])
    failed = [rung for rung in report.get("rungs", []) if rung.get("status") in {"fail", "failed"}]
    assert result.returncode == 0 and not failed, f"offline doctor failed: {failed or report}"


def prove_assets(venv: Venv, work: Path, *, discord: bool) -> None:
    code = r'''
import json, wave
from importlib import resources
root = resources.files("sleeptalk")
assert "site-packages" in str(root), root
from sleeptalk.speech import lexicon
assert lexicon._LEXICON_PATH.is_file() and json.loads(lexicon._LEXICON_PATH.read_text())
from sleeptalk import stt_roundtrip
with wave.open(str(root.joinpath("assets", stt_roundtrip.CLIP_NAME)), "rb") as clip:
    assert clip.getnframes() > 0
schema = root.joinpath("agents/codex_app_server_schema/codex_app_server_protocol.v2.schemas.json")
assert json.loads(schema.read_text())
evals = root.joinpath("evals/data")
assert json.loads(evals.joinpath("manifest.json").read_text())
assert json.loads(evals.joinpath("scenarios/schema.json").read_text())
assert json.loads(root.joinpath("_setup/providers.json").read_text())["providers"], "provider facts missing"
settings_page = root.joinpath("_settings")
assert all(settings_page.joinpath(name).is_file() for name in ("index.html", "settings.js", "settings.css")), "settings page assets missing"
assert root.joinpath("_web/settings.html").is_file(), "phone settings page missing"
templates = root.joinpath("templates/byo")
assert len([item for item in templates.iterdir() if item.is_dir()]) >= 7, "bring-your-own-agent scaffolds missing"
import sleeptalk.media_core  # audioop users must import on every supported Python
import sleeptalk.web_voice.session
print("assets: ok")
'''
    if discord:
        code += r'''
import numpy as np
from sleeptalk.voice_loop.models import bundled_model_path
from sleeptalk.voice_loop.vad import SileroOnnxRuntimeModel
from sleeptalk.voice_loop.smart_turn import SmartTurnScorer
model = SileroOnnxRuntimeModel(model_path=bundled_model_path("silero_vad"), sample_rate=16000)
assert model is not None
session = SmartTurnScorer()._load()
assert session.get_inputs()
print("onnx models: ok")
'''
    venv.py(code, cwd=work)


def prove_web_call(venv: Venv, home: Path, work: Path) -> None:
    env = outside_env({"SLEEPTALK_HOME": str(home)})
    start = run([venv.sleeptalk, "host", "start-voice", "web", "--caller-context", "local",
                 "--request-id", "releaseproof0001", "--ttl-seconds", "60", "--json"], cwd=work, env=env, timeout=120)
    receipt = json.loads(start.stdout.strip().splitlines()[-1])
    try:
        assert receipt["status"] == "started", receipt
        join_url = receipt["join_url"]
        assert join_url.startswith("http://127.0.0.1:"), join_url
        with urllib.request.urlopen(join_url.split("#", 1)[0], timeout=10) as response:
            assert response.status == 200
        listed = json.loads(run([venv.sleeptalk, "host", "list"], cwd=work, env=env).stdout)
        assert receipt["callId"] in [call.get("callId") for call in listed.get("calls", [])], listed
    finally:
        stopped = json.loads(run([venv.sleeptalk, "host", "stop", receipt.get("callId", "missing")], cwd=work, env=env, check=False).stdout or "{}")
    assert stopped.get("status") == "stopped", stopped
    listed = json.loads(run([venv.sleeptalk, "host", "list"], cwd=work, env=env).stdout)
    assert not listed.get("calls"), listed
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(join_url.split("#", 1)[0], timeout=2)
        except (urllib.error.URLError, ConnectionError, OSError):
            break
        time.sleep(0.2)
    else:
        raise AssertionError("the call link still answers after stop")
    log("web: bounded local call started, served, listed, stopped, and cleaned up: ok")


def prove_terminal_session(venv: Venv, profile: Path, home: Path, work: Path) -> None:
    code = f'''
import asyncio
from sleeptalk.builder import build_session
from sleeptalk.config import load_runtime_config
config = load_runtime_config({str(profile)!r}, surface="terminal")
session = build_session(config)
assert type(session.transport).__name__ == "TerminalTransport", type(session.transport)
close = getattr(session.agent, "close", None)
if close:
    asyncio.run(close())
import sounddevice  # PortAudio must load from the installed wheel's environment
print("terminal session: ok")
'''
    venv.py(code, cwd=work, env={"SLEEPTALK_HOME": str(home)})
    log("terminal: session built from a blank-home profile without audio I/O: ok")


def prove_discord_startup(venv: Venv, profile: Path, home: Path, work: Path) -> None:
    root_config = home / "sleeptalk.toml"
    run([venv.python, "-m", "sleeptalk.discord.smoke", "--config", profile, "--root", root_config], cwd=work,
        env=outside_env({"SLEEPTALK_HOME": str(home), "RELEASEPROOF_DISCORD_BOT_TOKEN": "placeholder-discord-token"}))
    log("discord: no-connect startup registered commands from a blank home: ok")


# ---------------------------------------------------------------------------
# Failure injection (offline cases; rejected credentials and TURN are live-only)
# ---------------------------------------------------------------------------


def doctor_failures(venv: Venv, profile: Path, home: Path, work: Path) -> list[dict]:
    result = run([venv.sleeptalk, "doctor", "--config", profile, "--offline", "--json"], cwd=work,
                 env=outside_env({"SLEEPTALK_HOME": str(home)}), check=False)
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode != 0 and report.get("status") == "failed", report
    return [rung for rung in report.get("rungs", []) if rung.get("status") == "fail"]


def live_calls(venv: Venv, home: Path, work: Path) -> list:
    listed = run([venv.sleeptalk, "host", "list"], cwd=work, env=outside_env({"SLEEPTALK_HOME": str(home)}))
    return json.loads(listed.stdout).get("calls", [])


def prove_failure_injection(venvs: dict[str, Venv], homes: dict[str, Path], work: Path) -> None:
    web, discord = venvs["web"], venvs["discord"]

    # An extra is missing: the Web-only install is asked to run a Terminal profile.
    failures = doctor_failures(web, homes["terminal"] / "profiles" / "default.toml", homes["terminal"], work)
    assert any("sleeptalk[terminal]" in " ".join(rung.get("lines", [])) for rung in failures), failures
    log("failure: a missing surface extra fails doctor and names the extra to install: ok")

    # The agent executable is absent: no call link is minted and nothing stays running.
    profile = homes["web"] / "profiles" / "default.toml"
    broken = homes["web"] / "profiles" / "missing-agent.toml"
    broken.write_text(re.sub(r'command = \[.*\]', 'command = ["release-proof-missing-agent"]', profile.read_text()))
    env = outside_env({"SLEEPTALK_HOME": str(homes["web"])})
    start = run([web.sleeptalk, "host", "start-voice", "web", "--profile", "missing-agent", "--caller-context", "local",
                 "--request-id", "releaseproofmissing1", "--json"], cwd=work, env=env, check=False, timeout=120)
    receipt = json.loads(start.stdout.strip().splitlines()[-1])
    assert start.returncode != 0 and receipt.get("status") == "error" and not receipt.get("join_url"), receipt
    assert not live_calls(web, homes["web"], work)
    failures = doctor_failures(web, broken, homes["web"], work)
    assert any("release-proof-missing-agent" in " ".join(rung.get("lines", [])) for rung in failures), failures
    log("failure: a missing agent program stops call start before a link and fails doctor by name: ok")

    # The browser call service is unavailable: an actionable error and no leftover call.
    start = run([web.sleeptalk, "host", "start-voice", "web", "--caller-context", "chat",
                 "--rendezvous", "https://127.0.0.1:9", "--request-id", "releaseproofoffline1", "--json"],
                cwd=work, env=env, check=False, timeout=120)
    receipt = json.loads(start.stdout.strip().splitlines()[-1])
    assert receipt.get("status") == "error" and "unreachable" in str(receipt.get("error", "")), receipt
    assert not live_calls(web, homes["web"], work)
    log("failure: an unreachable call service returns an actionable error and leaves no call running: ok")

    # A bundled model is corrupt: loaders fail fast with a clear error instead of hanging.
    code = r'''
import shutil, time
from sleeptalk.voice_loop.models import bundled_model_path
from sleeptalk.voice_loop.vad import SileroOnnxRuntimeModel
from sleeptalk.voice_loop.smart_turn import SmartTurnScorer
loaders = {
    "silero_vad": lambda: SileroOnnxRuntimeModel(model_path=bundled_model_path("silero_vad"), sample_rate=16000),
    "smart_turn_v3": lambda: SmartTurnScorer()._load(),
}
for name, load in loaders.items():
    path = bundled_model_path(name)
    shutil.copy(path, path + ".intact")
    try:
        with open(path, "r+b") as handle:
            handle.write(b"\0" * 4096)
            handle.truncate(8192)
        started = time.monotonic()
        try:
            load()
        except Exception as exc:
            assert time.monotonic() - started < 10, name
            assert "protobuf" in str(exc).lower() or "invalid" in str(exc).lower(), exc
        else:
            raise AssertionError(f"{name} loaded a corrupt model")
    finally:
        shutil.move(path + ".intact", path)
    load()
print("ok")
'''
    discord.py(code, cwd=work)
    log("failure: corrupt voice models fail fast with a clear error, and restored models load: ok")


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def prove_rollback(work: Path, wheel: Path, source: Path, python: str, record: dict) -> None:
    previous_source = work / "previous-source"
    shutil.copytree(source, previous_source)
    pyproject = previous_source / "pyproject.toml"
    text = pyproject.read_text()
    version = re.search(r'^version = "([^"]+)"', text, flags=re.M).group(1)
    previous_version = f"{version}rc0"
    pyproject.write_text(text.replace(f'version = "{version}"', f'version = "{previous_version}"', 1))
    previous_dist = work / "previous-dist"
    run(["uv", "build", "-q", "--wheel", "--out-dir", previous_dist, previous_source], cwd=work, env=outside_env())
    (previous_wheel,) = previous_dist.glob("*.whl")

    tools = Venv(work / "rollback-tools", python)
    tools.install(str(wheel))
    runtimes = work / "runtimes"
    code = f'''
import json
from pathlib import Path
from sleeptalk.bootstrap import install_artifact
root = Path({str(runtimes)!r})
previous = install_artifact(Path({str(previous_wheel)!r}), {sha256(previous_wheel)!r}, surface="web", agent="pi", root=root)
candidate = install_artifact(Path({str(wheel)!r}), {sha256(wheel)!r}, surface="web", agent="pi", root=root)
print(json.dumps({{"previous": previous["runtime_id"], "candidate": candidate["runtime_id"]}}))
'''
    ids = json.loads(tools.py(code, cwd=work, timeout=600).strip().splitlines()[-1])
    assert ids["previous"] != ids["candidate"], ids
    for key, expected in (("previous", previous_version), ("candidate", version)):
        runtime = runtimes / ids[key]
        reported = run([runtime / "bin" / "python", "-c", "from importlib.metadata import version; print(version('sleeptalk'))"],
                       cwd=work, env=outside_env()).stdout.strip()
        assert reported == expected, (key, reported, expected)
        run([runtime / "bin" / "sleeptalk", "--help"], cwd=work, env=outside_env())
    log("rollback: previous and candidate artifacts coexist; previous runtime runs its own version: ok")

    # Upgrade an existing installation in place, then restore the previous artifact.
    existing = Venv(work / "upgrade-in-place", python)
    existing.install(str(previous_wheel))
    home = work / "home-upgrade"
    profile = ensure(existing, home, "web", work, answers=PLACEHOLDER_ANSWERS)
    version_code = "from importlib.metadata import version; print(version('sleeptalk'))"
    for requirement, expected in ((str(wheel), version), (str(previous_wheel), previous_version)):
        run(["uv", "pip", "install", "-q", "--reinstall-package", "sleeptalk", "--python", existing.python, requirement],
            env=outside_env())
        assert existing.py(version_code, cwd=work).strip() == expected, expected
        doctor_offline(existing, profile, home, work)
    log("rollback: an existing installation upgrades, then restores the previous artifact with its profile intact: ok")
    record["rollback_drill"] = {
        "result": "passed",
        "method": (
            "previous and candidate wheels installed side by side as isolated runtimes, and an existing "
            "installation upgraded in place then restored; offline doctor passes on the same profile after each step"
        ),
        "drill_previous_version": previous_version,
    }


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True, help="Where BUILD_RECORD.json and ROLLBACK.json are written.")
    parser.add_argument("--python", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    args = parser.parse_args()

    work = args.workdir / "surfaces"
    work.mkdir(parents=True)
    install_stub_binaries(work)
    commit = run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    dirty = bool(run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT).stdout.strip())
    record: dict = {
        "schema_version": 1,
        "source_commit": commit,
        "tracked_tree_clean": not dirty,
        "python": args.python,
        "platform": f"{platform.system()} {platform.machine()}",
        "uv": run(["uv", "--version"]).stdout.strip(),
        "artifacts": {args.wheel.name: sha256(args.wheel), args.sdist.name: sha256(args.sdist)},
        "resolved_dependencies": {},
    }

    source = prove_artifacts(work, args.wheel, args.sdist, record)

    base_answers = PLACEHOLDER_ANSWERS
    discord_answers = PLACEHOLDER_ANSWERS + (
        'guild_id = "100000000000000001"\nclient_id = "100000000000000002"\n'
        'token = "placeholder-discord-token"\ntoken_env = "RELEASEPROOF_DISCORD_BOT_TOKEN"\n'
    )
    venvs: dict[str, Venv] = {}
    homes: dict[str, Path] = {}
    for surface, extra, answers in (
        ("web", "web", base_answers),
        ("terminal", "terminal", base_answers + 'capture_mode = "headphones"\n'),
        ("discord", "discord", discord_answers),
    ):
        venv = venvs[surface] = Venv(work / f"venv-{surface}", args.python)
        venv.install(f"{args.wheel}[{extra}]")
        record["resolved_dependencies"][extra] = venv.freeze()
        home = homes[surface] = work / f"home-{surface}"
        profile = ensure(venv, home, surface, work, answers=answers)
        doctor_offline(venv, profile, home, work)
        prove_assets(venv, work, discord=surface == "discord")
        log(f"{surface}: blank install, first-time setup, offline doctor, and bundled assets: ok")
        if surface == "web":
            use_echo_agent(venv, profile, work)
            prove_web_call(venv, home, work)
        elif surface == "terminal":
            prove_terminal_session(venv, profile, home, work)
        else:
            prove_discord_startup(venv, profile, home, work)

    prove_failure_injection(venvs, homes, work)
    prove_rollback(work, args.wheel, source, args.python, record)

    args.dist.mkdir(parents=True, exist_ok=True)
    (args.dist / "BUILD_RECORD.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    wheel_hash = sha256(args.wheel)
    (args.dist / "ROLLBACK.json").write_text(json.dumps({
        "schema_version": 1,
        "candidate": {"source_commit": commit, "wheel": args.wheel.name, "sha256": wheel_hash},
        "previous_release": None,
        "previous_release_note": "No Sleeptalk release has been published. The first publication has no installable rollback; record its artifact here for the next one.",
        "rollback_procedure": [
            "Download the previous release's wheel and its SHA256SUMS from the immutable release bundle.",
            "sleeptalk host bootstrap --artifact <previous.whl> --sha256 <previous sha256> --surface <surface> --agent <agent>",
            "Point the service or agent at the printed runtime's bin/sleeptalk; the candidate runtime stays installed beside it.",
        ],
        "drill": record["rollback_drill"],
    }, indent=2) + "\n")
    log(f"release proof outside the checkout: ok (records in {args.dist})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
