"""Logging for the supervised world.

Console stays primary in the foreground; under a supervisor (Docker,
systemd, nohup) stdout is captured anyway. Additionally, every run appends
to <layout.logs_dir>/<slug>.log with size-based rotation (1 MB x 2 files),
so a crash's FATAL line survives the process.

[log] controls basic log level and opt-in performance traces.

Every handler on the root logger formats through ``redact``, so a key that
slips into a message, a library's exception text, or a URL is masked before
it reaches the console or the file. The logs directory is private to the
user, since it also holds transcripts and usage.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

MAX_BYTES = 1024 * 1024  # 1 MB, plus one rotated file
BACKUP_COUNT = 1  # 2 files total, à la Paseo's 10m/2-file default


# Shapes a secret takes in log text. Each keeps its label and drops the value.
_SECRET_PATTERNS = (
    # URL query parameters: ?token=…, &api_key=…
    (re.compile(r"(?i)([?&](?:token|key|api[_-]?key|access[_-]?token|auth|password|secret|sig|signature)=)[^&\s'\"<>]+"), r"\1***"),
    # Header-style pairs: Authorization: …, xi-api-key=…, "x-api-key": "…"
    (re.compile(r"(?i)((?:authorization|xi-api-key|x-api-key|x-hume-api-key|api[_-]?key|access[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|token|bot|basic)\s+)?[^\s\"',;}]+"), r"\1***"),
    # Scheme + credential: Bearer …, Token …, Bot … (a long run with a digit in it)
    (re.compile(r"(?i)\b(bearer|token|bot|basic)\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{16,}"), r"\1 ***"),
    # NAME_TOKEN=…, NAME_SECRET=…, NAME_API_KEY=…, NAME_PASSWORD=… (dotenv and shell style)
    (re.compile(r"\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))=(?:'[^']*'|\"[^\"]*\"|\S+)"), r"\1=***"),
    # JSON-ish "token": "…", "password": "…", "secret": "…"
    (re.compile(r'(?i)("(?:[a-z_]*token|password|secret)"\s*:\s*")[^"]+'), r"\1***"),
    # Credentials in a URL: scheme://user:pass@host, and #token= fragments
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)(#(?:token|key|access_token)=)[^&\s'\"<>]+"), r"\1***"),
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***"),
    # Discord bot tokens
    (re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"), "***"),
    # Well-known key prefixes: OpenAI/OpenRouter/Anthropic sk-, Groq gsk_, xAI, ElevenLabs sk_, Google AIza
    (re.compile(r"\b(sk-(?:ant-|or-v1-|proj-)?|sk_|gsk_|xai-|tgp_v1_|AIza)[A-Za-z0-9_-]{16,}"), r"\1***"),
)


def redact(text: str) -> str:
    """``text`` with anything shaped like a secret replaced by ``***``."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's formatter; the whole formatted line, traceback included, is redacted."""

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Route every handler of ``logger`` (the root by default) through ``redact``."""
    for handler in (logger or logging.getLogger()).handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))


def redact_default_output() -> None:
    """Mask secrets in log lines a command prints without configuring logging
    (Python's last-resort stderr handler) and on any handler already set."""
    fallback = logging.StreamHandler()
    fallback.setLevel(logging.WARNING)
    fallback.setFormatter(RedactingFormatter())
    logging.lastResort = fallback
    install_redaction()


def setup_file_logging(
    *,
    logs_dir: Path,
    slug: str,
    level: str = "WARNING",
) -> Path:
    """Attach the rotating file sink; returns the log file path."""
    logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        logs_dir.chmod(0o700)
    except OSError:
        pass  # a shared, pre-made logs dir is the operator's call
    path = logs_dir / f"{slug}.log"
    # Keep runtime logs readable by this user only, including existing files.
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
        path.chmod(0o600)
    except OSError:
        pass
    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(getattr(logging, level.upper(), logging.WARNING))
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.WARNING))
    install_redaction(root)
    quiet_library_loggers()
    return path


#: The env var the telemetry writers read. Setting it here is what makes
#: latency logging automatic: nobody has to export anything by hand.
LATENCY_LOG_ENV = "VOICE_LATENCY_LOG_PATH"
LATENCY_ENABLED_ENV = "SLEEPTALK_LATENCY_LOG_ENABLED"
LATENCY_LOG_NAME = "voice_latency.jsonl"


def setup_latency_log(
    *,
    logs_dir: Path,
    enabled: bool = True,
    configured_path: str = "",
) -> Path | None:
    """Point every surface's latency traces at one file; returns that path.

    Terminal, web and Discord all write the same trace envelope, so they are
    only comparable when they share a destination. An env var already set by
    the caller wins — a one-off run can still redirect its own traces.
    """
    # A path alone is an explicit CLI opt-in, but a profile's Off wins over
    # inherited paths. Every writer checks this, including capture callbacks.
    os.environ[LATENCY_ENABLED_ENV] = "1" if enabled else "0"
    if not enabled:
        return None
    existing = os.environ.get(LATENCY_LOG_ENV, "").strip()
    if existing:
        return Path(existing).expanduser()
    path = (
        Path(configured_path).expanduser()
        if configured_path.strip()
        else logs_dir / LATENCY_LOG_NAME
    )
    os.environ[LATENCY_LOG_ENV] = str(path)
    return path


#: Library loggers whose INFO output is trivia for end users (RTCP sender
#: reports, voice-WS schema notes). The session story belongs to sleeptalk's
#: own loggers; per-stage telemetry lives in the voice_latency JSONL files.
_NOISY_LOGGERS = (
    "discord.ext.voice_recv",
    "discord.ext.voice_recv.reader",
    "discord.voice_state",
    "discord.gateway",
    "ffmpeg",
)


def quiet_library_loggers() -> None:
    """Demote known-noisy library loggers to WARNING (clean stream for users)."""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


_log = logging.getLogger("sleeptalk.logging")


def profile_logs_dir(config_path: str | Path) -> Path:
    """The logs directory beside a profile's state: ``<home>/logs`` for
    ``<home>/profiles/x.toml`` (and a repo's ``config/profiles``)."""
    parent = Path(config_path).resolve().parent
    if parent.name == "profiles":
        state_root = parent.parent
        if state_root.name == "config":
            state_root = state_root.parent
        return state_root / "logs"
    return parent / "logs"


def setup_profile_logging(config_path: str | Path, config: Any) -> tuple[Path, Path | None]:
    """The profile's log file and latency traces, for any process that runs a call.

    `sleeptalk run` and the background host behind an agent's call link both
    use it, so a call's turn timeline lands in the same place however it began.
    """
    logs_dir = profile_logs_dir(config_path)
    log_path = setup_file_logging(logs_dir=logs_dir, slug=config.agent.slug, level=config.log.level)
    _log.info("logging to %s", log_path)
    latency_path = setup_latency_log(
        logs_dir=logs_dir, enabled=config.log.latency, configured_path=config.log.latency_path,
    )
    if latency_path is not None:
        _log.info("latency traces to %s", latency_path)
    return log_path, latency_path
