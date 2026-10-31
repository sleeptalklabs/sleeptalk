#!/usr/bin/env bash
# Prove release artifacts are honest: wheel+sdist build, blank-venv install,
# no server `app` import, web host resolves, checksums/rollback pointers, and
# (when Docker is available) a package-only container entrypoint.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT"
MODE="all"
TIME_INSTALL=0

usage() {
  cat <<'EOF'
Usage: prove_release_wheel.sh [--docs] [--time-install] [--help]

  (default)     Build wheel+sdist, blank-venv smoke, checksums, dry-run publish,
                and container smoke when docker is available.
  --docs        Fail if first-run / quickstart docs still teach `cd server`.
  --time-install
                Print blank-venv wheel install seconds (perf probe).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --docs) MODE="docs"; shift ;;
    --time-install) TIME_INSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

DOCS_PATHS=(
  "$ROOT/README.md"
  "$ROOT/docs/launch-support-matrix.md"
)

check_docs() {
  local bad=0
  local path
  for path in "${DOCS_PATHS[@]}"; do
    if [[ ! -f "$path" ]]; then
      echo "missing docs file: $path" >&2
      bad=1
      continue
    fi
    if grep -nF 'cd server' "$path" >/tmp/sleeptalk-docs-cd-server.txt 2>/dev/null; then
      echo "forbidden first-run 'cd server' in $path:" >&2
      cat /tmp/sleeptalk-docs-cd-server.txt >&2
      bad=1
    fi
  done
  # Product install docs must name the package path, not the monorepo server.
  if ! grep -Eq "sleeptalk\[web\]|\./sleeptalk" \
      "$ROOT/README.md"; then
    echo "install docs must mention package install (sleeptalk[web])" >&2
    bad=1
  fi
  if [[ "$bad" -ne 0 ]]; then
    return 1
  fi
  echo "prove_release_wheel --docs: ok"
}

if [[ "$MODE" == "docs" ]]; then
  check_docs
  exit 0
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/sleeptalk-release-wheel.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

DIST="$WORKDIR/dist"
mkdir -p "$DIST"

# --- Lane 1: build wheel + sdist ---
cd "$PKG"
uv build --out-dir "$DIST"
test -n "$(echo "$DIST"/sleeptalk-*.whl)"
test -n "$(echo "$DIST"/sleeptalk-*.tar.gz)"
WHEEL="$(echo "$DIST"/sleeptalk-*.whl)"
SDIST="$(echo "$DIST"/sleeptalk-*.tar.gz)"
python3 "$ROOT/scripts/audit_release_contents.py" "$WHEEL" "$SDIST"

# The constraints pin dependencies without installing unselected extras. The
# standalone installer and wheel work outside a checkout, with no Node or Git.
# CI may stamp only the project's version; the tested dependency lock is fixed.
uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes \
  --no-header --no-annotate --output-file "$DIST/constraints.txt" >/dev/null
cp "$ROOT/scripts/install.sh" "$DIST/install.sh"

# --- Lane 8: checksums + rollback pointer ---
(
  cd "$DIST"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$(basename "$WHEEL")" "$(basename "$SDIST")" constraints.txt install.sh > SHA256SUMS
  else
    shasum -a 256 "$(basename "$WHEEL")" "$(basename "$SDIST")" constraints.txt install.sh > SHA256SUMS
  fi
)
# ROLLBACK.json and BUILD_RECORD.json are written by prove_release_surfaces.py
# after its rollback drill; a Git tag is not an installable rollback.

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIST" && sha256sum -c SHA256SUMS)
else
  (cd "$DIST" && shasum -a 256 -c SHA256SUMS)
fi

# --- Lanes 2-3 (+ optional install timing): blank venv ---
VENV="$WORKDIR/venv"
uv venv "$VENV"
PYTHON="$VENV/bin/python"
INSTALL_LOG="$WORKDIR/install-time.txt"
if [[ "$TIME_INSTALL" -eq 1 ]]; then
  START="$(date +%s.%N)"
  UV_PROJECT_ENVIRONMENT="$VENV" uv pip install --python "$PYTHON" --constraint "$DIST/constraints.txt" "${WHEEL}[web,remote-entry]"
  END="$(date +%s.%N)"
  python3 - <<PY
start = float("$START")
end = float("$END")
print(f"{end - start:.3f}")
PY
else
  UV_PROJECT_ENVIRONMENT="$VENV" uv pip install --python "$PYTHON" --constraint "$DIST/constraints.txt" "${WHEEL}[web,remote-entry]"
fi

"$VENV/bin/sleeptalk" --help >/dev/null
"$PYTHON" - <<'PY'
try:
    import app  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit("import app unexpectedly succeeded from wheel install")

from sleeptalk.webcalls import resolve_web_call
from sleeptalk.remote_crypto import canonical, sign, verify
from sleeptalk.remote_host import RemoteHost
from sleeptalk.remote_relay import SetupRelay
from importlib import resources
from cryptography.hazmat.primitives.asymmetric import ed25519
from sleeptalk import setup, settings_page
import sys

assert "sleeptalk.wizard" not in sys.modules, "shared setup imports terminal UI"

resolve_web_call()
web_root = resources.files("sleeptalk").joinpath("_web")
assert web_root.joinpath("index.html").is_file()
assert web_root.joinpath("sleep.mp4").is_file()
remote_root = resources.files("sleeptalk").joinpath("_remote_entry")
for name in ("index.html", "app.js", "style.css", "assets.json", "THIRD_PARTY_NOTICES.txt"):
    assert remote_root.joinpath(name).is_file()
import hashlib, json
for name, expected in json.loads(remote_root.joinpath("assets.json").read_text()).items():
    assert hashlib.sha256(remote_root.joinpath(name).read_bytes()).hexdigest() == expected
guard = resources.files("sleeptalk").joinpath("agents/pi_permission_extension.js")
assert guard.is_file() and 'pi.on("tool_call"' in guard.read_text()
key = ed25519.Ed25519PrivateKey.generate()
document = {"proof": "installed-wheel", "version": 1}
assert canonical(document) == b'{"proof":"installed-wheel","version":1}'
verify(document, sign(document, key, purpose="receipt"), key.public_key(), purpose="receipt")
print("blank-wheel web host: ok")
print("blank-wheel permission extension and remote cryptography extra: ok")
PY

# Discovery must work outside the checkout without creating a state directory.
(
  cd "$WORKDIR"
  "$VENV/bin/sleeptalk" host discover --schema --json > discovery-schema.json
  DISCOVERY_EXIT=0
  SLEEPTALK_HOME="$WORKDIR/discovery-home" \
    "$VENV/bin/sleeptalk" host discover \
    --workspace "$WORKDIR" --agent codex --surface web --json \
    > discovery.json || DISCOVERY_EXIT=$?
  test "$DISCOVERY_EXIT" -eq 2
  "$PYTHON" - <<'PY'
import json
from pathlib import Path

import sleeptalk.onboarding as onboarding

assert "site-packages" in onboarding.__file__, onboarding.__file__
schema = json.loads(Path("discovery-schema.json").read_text())
assert schema == onboarding.contract_schema()
receipt = onboarding.DiscoveryReceipt.model_validate_json(Path("discovery.json").read_text())
assert receipt.operation == "discover"
assert receipt.status == "needs_input"
assert receipt.selected_agent == "codex"
assert receipt.plan.surface == "web"
assert not Path("discovery-home").exists()
assert str(Path.cwd()) not in receipt.model_dump_json()
print("blank-wheel host discovery outside checkout: ok")
PY
  "$PYTHON" - "$WHEEL" <<'PY'
import hashlib
from pathlib import Path
import sys
from sleeptalk.bootstrap import install_artifact

wheel = Path(sys.argv[1])
digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
root = Path.cwd() / "isolated-runtimes"
first = install_artifact(wheel, digest, surface="web", agent="codex", root=root)
assert install_artifact(wheel, digest, surface="web", agent="codex", root=root) == first
assert (root / first["runtime_id"] / "bin" / "sleeptalk").is_file()
print("blank-wheel isolated bootstrap and reuse: ok")
PY
)

"$PYTHON" - <<'PY'
import asyncio

import aiohttp

from sleeptalk.call_provider import CallContext
from sleeptalk.local_rendezvous import LocalCallProvider


async def prove() -> None:
    async with LocalCallProvider() as provider:
        ticket = await provider.mint(
            CallContext("wheel", "proof", "local-user"), ttl_seconds=5
        )
        assert ticket.join_url.startswith("http://127.0.0.1:")
        async with aiohttp.ClientSession() as session:
            async with session.get(provider.origin) as response:
                assert response.status == 200
                assert "SLEEP.TALK" in await response.text()


asyncio.run(prove())
print("blank-wheel localhost rendezvous: ok")
PY

# The private page and its authentication contract must exist in the wheel,
# independent of the repository and without contacting a real provider.
"$VENV/bin/python" - <<'PY'
import asyncio
from pathlib import Path
import tempfile
import time
from aiohttp.test_utils import TestClient, TestServer
from sleeptalk.credential_store import CredentialStore
from sleeptalk.private_input import InputSpec, InputSession, atomic_json

async def prove_private_page():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        spec = InputSpec(request_id="wheel-private-001", profile="wheel-private", workspace=str(root),
                         agent="codex", surface="web", tts_provider="cartesia", profile_path=str(root / "profiles/voice.toml"))
        directory = root / "request"
        atomic_json(directory / "request.json", spec.model_dump())
        atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(), "expires_at": time.time()+60, "refs": {}})
        setup = InputSession(directory, store=CredentialStore(root / "store", backend="file"))
        # An agent's key request opens in the settings page, served from the wheel.
        from sleeptalk.settings_page import SettingsPage
        page = SettingsPage(setup.profile_path, profiles_dir=setup.profile_path.parent, environ={},
                            state_dir=root / "page-state", request=setup)
        async with TestClient(TestServer(page.app())) as client:
            page.origin = str(client.make_url("/"))[:-1]
            async with client.get("/") as response:
                assert response.status == 200
                assert "Sleeptalk settings" in await response.text()
            async with client.get("/settings.js") as response:
                assert response.status == 200
                assert "/api/request/key" in await response.text()
            async with client.get("/api/state") as response:
                assert response.status == 401
        import importlib.resources as resources
        assert resources.files("sleeptalk").joinpath("_web/settings.html").is_file()
asyncio.run(prove_private_page())
print("blank-wheel settings page for key requests: ok")
PY

# Host ensure must provision a blank home from the installed wheel, without
# relying on the source checkout or exposing local answers in its receipt.
HOST_HOME="$WORKDIR/host-home"
HOST_BIN="$WORKDIR/host-bin"
HOST_ANSWERS="$WORKDIR/host-answers.toml"
HOST_RECEIPT="$WORKDIR/host-ensure.json"
mkdir -p "$HOST_BIN"
printf "%s\n" "#!/bin/sh" "exit 0" > "$HOST_BIN/pi"
printf "%s\n" "#!/bin/sh" "exit 0" > "$HOST_BIN/ffmpeg"
chmod +x "$HOST_BIN/pi" "$HOST_BIN/ffmpeg"
printf "%s\n" \
  'name = "Wheel Host"' \
  'stt_key = "wheel-deepgram-secret"' \
  'tts_key = "wheel-cartesia-secret"' \
  'tts_voice_id = "wheel-voice"' > "$HOST_ANSWERS"
SLEEPTALK_HOME="$HOST_HOME" PATH="$HOST_BIN:$PATH" \
  "$VENV/bin/sleeptalk" host ensure \
  --profile default --surface web --route cascade --agent pi \
  --answers "$HOST_ANSWERS" --json > "$HOST_RECEIPT"
"$PYTHON" - "$HOST_RECEIPT" "$HOST_HOME" <<'PY'
import json
from pathlib import Path
import sys

receipt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
home = Path(sys.argv[2])
assert receipt["status"] == "ready", receipt
assert receipt["surface"] == "web", receipt
assert receipt["route"] == "cascade", receipt
assert receipt["agent"] == "pi", receipt
blob = json.dumps(receipt)
assert "wheel-deepgram-secret" not in blob, blob
assert "wheel-cartesia-secret" not in blob, blob
profile = (home / "profiles" / "default.toml").read_text(encoding="utf-8")
# Keys live in the private store; the profile only references them.
assert "[credentials.refs]" in profile, profile
assert "wheel-deepgram-secret" not in profile and "wheel-cartesia-secret" not in profile
env = home / ".env"
assert not env.exists() or "wheel-deepgram-secret" not in env.read_text(encoding="utf-8")
print("blank-wheel host ensure: ok")
PY

# --- Plan 07: artifacts, surfaces, bounded call, and rollback outside the checkout ---
# Use the Python selected for the blank install, not an older system Python
# that may lack tarfile's safe extraction filter.
UV_CONSTRAINT="$DIST/constraints.txt" "$PYTHON" "$ROOT/scripts/prove_release_surfaces.py" \
  --workdir "$WORKDIR" --wheel "$WHEEL" --sdist "$SDIST" --dist "$DIST" \
  --python "$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# --- Lane 10: dry-run publish (no upload) ---
uv publish --dry-run --trusted-publishing never "$WHEEL" "$SDIST" >/dev/null

# --- Lane 7: container (optional locally; required shape check always) ---
if grep -nF 'COPY server/' "$ROOT/Dockerfile" >/dev/null; then
  echo "Dockerfile must not COPY server/ into the published image" >&2
  exit 1
fi
if ! grep -nF 'ENTRYPOINT ["sleeptalk-entrypoint"]' "$ROOT/Dockerfile" >/dev/null \
  || ! grep -nF 'exec setpriv --reuid=sleeptalk --regid=sleeptalk --init-groups sleeptalk "$@"' "$ROOT/scripts/docker-entrypoint.sh" >/dev/null; then
  echo "Dockerfile must run the package CLI (sleeptalk) through sleeptalk-entrypoint as the sleeptalk user" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  IMAGE="sleeptalk-release-prove:$$"
  docker build -t "$IMAGE" "$ROOT"
  docker run --rm "$IMAGE" --help >/dev/null
  if docker run --rm --entrypoint /bin/sh "$IMAGE" -c 'test -e /app/server'; then
    echo "published image unexpectedly contains /app/server" >&2
    docker rmi "$IMAGE" >/dev/null 2>&1 || true
    exit 1
  fi
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  echo "container smoke: ok"
else
  echo "container smoke: skipped (Docker daemon not available); Dockerfile shape checked"
fi

# Mirror checksums next to the package build output for CI artifacts.
mkdir -p "$PKG/dist"
cp -f "$WHEEL" "$SDIST" "$DIST/constraints.txt" "$DIST/install.sh" "$DIST/SHA256SUMS" "$DIST/ROLLBACK.json" "$DIST/BUILD_RECORD.json" "$PKG/dist/"

check_docs
echo "prove_release_wheel: ok"
