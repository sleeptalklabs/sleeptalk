#!/bin/sh
# Container entrypoint: hand the state volume to the sleeptalk user, then run
# the package CLI as that user. The voice runtime and the agents it launches
# never run as root. A container started with --user skips straight to the CLI.
set -eu

if [ "$(id -u)" = "0" ]; then
  home="${SLEEPTALK_HOME:-/home/sleeptalk}"
  mkdir -p "$home"
  # A bind mount Docker created, or one written by an older root image, is
  # root-owned; adopt it once so the sleeptalk user can write its state.
  if [ "$(stat -c %u "$home")" != "$(id -u sleeptalk)" ]; then
    chown -R sleeptalk:sleeptalk "$home"
  fi
  exec setpriv --reuid=sleeptalk --regid=sleeptalk --init-groups sleeptalk "$@"
fi
exec sleeptalk "$@"
