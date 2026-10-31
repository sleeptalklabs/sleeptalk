#!/bin/sh
# Install the user command without a project virtual environment.
set -eu

usage() {
  cat <<'EOF'
Usage: sh install.sh [--constraints FILE] [PACKAGE]

Installs this checkout as the sleeptalk command, including Python if needed.
Outside a checkout, supply a reviewed wheel path or package requirement.
Release bundles include this script, a wheel, and constraints.txt:
  sh install.sh --constraints constraints.txt ./sleeptalk-*.whl
Release downloads: https://github.com/sleeptalklabs/sleeptalk/releases
EOF
}

constraints=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --constraints)
      if [ "$#" -lt 2 ] || [ ! -f "$2" ]; then
        echo '--constraints needs an existing constraints file.' >&2
        exit 2
      fi
      constraints=$2
      shift 2 ;;
    --) shift; break ;;
    -*) usage >&2; exit 2 ;;
    *) break ;;
  esac
done
if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi

package=${1:-}
if [ -z "$package" ] && [ -f "$0" ]; then
  script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  if [ -f "$script_dir/../pyproject.toml" ]; then
    package="$(CDPATH= cd -- "$script_dir/.." && pwd)[web]"
  fi
fi
if [ -z "$package" ]; then
  echo 'Run this installer from a checkout, or supply a reviewed package path.' >&2
  exit 2
fi

if [ "$(id -u)" = "0" ] && [ -z "${SLEEPTALK_ALLOW_ROOT:-}" ]; then
  echo 'Run this as your own user, not root: Sleeptalk and its keys belong in your home.' >&2
  echo 'Set SLEEPTALK_ALLOW_ROOT=1 to install for root anyway.' >&2
  exit 2
fi
case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo 'This installer supports macOS and Linux.' >&2; exit 2 ;;
esac

uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then
  uv_bin="$HOME/.local/bin/uv"
fi
if [ -z "$uv_bin" ]; then
  echo 'Installing uv to manage Sleeptalk and Python...'
  scratch=$(mktemp -d)
  trap 'rm -rf "$scratch"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL --max-time 60 \
    https://astral.sh/uv/0.12.18/install.sh -o "$scratch/uv-install.sh"
  # The installer is pinned by hash; it checks each uv download's own hash.
  expected=e62a5ea089de9c6a9b1f767166c9f33194e18d350e9f375c6355fac3ba0fb42b
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$scratch/uv-install.sh" | cut -d' ' -f1)
  else
    actual=$(shasum -a 256 "$scratch/uv-install.sh" | cut -d' ' -f1)
  fi
  if [ "$actual" != "$expected" ]; then
    echo 'The uv installer did not match its pinned checksum; stopping.' >&2
    exit 1
  fi
  UV_UNMANAGED_INSTALL="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 \
    sh "$scratch/uv-install.sh"
  uv_bin="$HOME/.local/bin/uv"
fi

echo 'Installing Sleeptalk...'
if [ -n "$constraints" ]; then
  "$uv_bin" --no-config tool install --reinstall --python 3.11 --constraint "$constraints" -- "$package"
else
  "$uv_bin" --no-config tool install --reinstall --python 3.11 -- "$package"
fi
tool_bin=$("$uv_bin" --no-config tool dir --bin)
if [ ! -x "$tool_bin/sleeptalk" ]; then
  echo 'The installed package did not provide a sleeptalk command.' >&2
  exit 1
fi

case ":$PATH:" in
  *":$tool_bin:"*) echo 'Ready. Run sleeptalk to set up your first call.' ;;
  *)
    if "$uv_bin" --no-config tool update-shell; then
      echo 'Ready. Open a new terminal, then run sleeptalk.'
    else
      printf 'Sleeptalk is installed. Add this directory to your PATH: %s\n' "$tool_bin"
    fi
    echo 'You can also start it now with:'
    quoted_bin=$(printf '%s' "$tool_bin" | sed "s/'/'\\\\''/g")
    printf "  '%s/sleeptalk'\n" "$quoted_bin"
    ;;
esac
