# sleeptalk — official image.
#
# All state lives in one volume (SLEEPTALK_HOME): profiles, .env, logs.
#   docker run -it -v $PWD/sleeptalk-home:/home/sleeptalk sleeptalk init
#   docker run -d --restart unless-stopped -v $PWD/sleeptalk-home:/home/sleeptalk sleeptalk
#
# The container never supervises itself: --restart unless-stopped (or your
# orchestrator) owns restarts, per the exit-code contract in
# docs/deployment.md (0 clean stop, 1 crash, 2 config error).
#
# Base images are pinned by digest; bump the tag and digest together.

# python:3.11-slim (Python 3.11.16)
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

# Node 22 for the pi agent CLI (pi needs Node 22 or newer; Debian's apt
# nodejs is 20). node:22-bookworm-slim (Node 22.23.3).
COPY --from=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm

# ffmpeg + libopus for Discord voice; pi is the image's agent.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm i -g @earendil-works/pi-coding-agent@1.1.0

# The runtime and the agents it launches run as this user, not root.
RUN useradd --uid 1000 --user-group --home-dir /home/sleeptalk --create-home --shell /bin/sh sleeptalk

# uv 0.12.18
COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY sleeptalk/ ./sleeptalk/
COPY scripts/docker-entrypoint.sh /usr/local/bin/sleeptalk-entrypoint

# Build an immutable wheel, then install it into a dedicated venv so the
# entrypoint is the package CLI and no source tree stays in the image.
RUN uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes \
      --no-header --no-annotate --output-file constraints.txt \
    && uv build --wheel \
    && uv venv /opt/sleeptalk \
    && WHEEL="$(echo dist/sleeptalk-*.whl)" \
    && uv pip install --python /opt/sleeptalk/bin/python --constraint constraints.txt "${WHEEL}[web,discord]" \
    && cd / \
    && rm -rf /build

WORKDIR /app
ENV PATH="/opt/sleeptalk/bin:${PATH}" \
    SLEEPTALK_HOME=/home/sleeptalk \
    HOME=/home/sleeptalk \
    PYTHONUNBUFFERED=1
VOLUME ["/home/sleeptalk"]

# Hands the volume to the sleeptalk user, then runs the package CLI
# (`sleeptalk`) as that user. See scripts/docker-entrypoint.sh.
ENTRYPOINT ["sleeptalk-entrypoint"]
CMD ["run"]
