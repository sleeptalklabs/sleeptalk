"""Local speech models: a pinned catalog, a cache directory, and a checksummed download.

Nothing here is bundled with the package. A model is fetched the first time the
user chooses Local speech, verified against the pinned SHA-256, and unpacked
into the cache. The catalog is the only place that knows URLs and hashes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
READY_MARKER = ".sleeptalk-ready"
Progress = Callable[[int, int], None]

# English speakers occupy IDs 0-27 in sherpa-onnx's Kokoro v1.0 bundle.
# Keep this order: it is the native speaker mapping, not a preference list.
KOKORO_EN_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky", "am_adam",
    "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx",
    "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
)


@dataclass(frozen=True)
class LocalModel:
    id: str
    kind: str  # "stt" | "tts"
    label: str
    url: str
    sha256: str
    archive_dir: str
    size_mb: int
    license: str
    languages: tuple[str, ...] = ("en",)
    sample_rate: int = 0
    #: TTS voice names in speaker-id order; STT models leave this empty.
    voices: tuple[str, ...] = field(default_factory=tuple)
    #: Threads that measured well on Apple Silicon for this model.
    num_threads: int = 4
    #: Versioned storage without changing existing profile model IDs.
    cache_dir: str = ""
    # Individually checksummed files for models distributed without an archive.
    files: tuple[tuple[str, str, int], ...] = ()
    runtime: str = "sherpa"

    @property
    def archive_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]


MODELS: dict[str, LocalModel] = {
    "moonshine-tiny-en": LocalModel(
        id="moonshine-tiny-en", kind="stt", label="Moonshine tiny (English)",
        url=f"{_RELEASES}/asr-models/sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
        sha256="d5fe6ec4334fef36255b2a4010412cad4c007e33103fec62fb5d17cad88086f2",
        archive_dir="sherpa-onnx-moonshine-tiny-en-int8", size_mb=108, license="MIT",
        sample_rate=16_000,
    ),
    "moonshine-small-streaming-en": LocalModel(
        id="moonshine-small-streaming-en", kind="stt",
        label="Moonshine Small Streaming (English)",
        url='https://download.moonshine.ai/model/small-streaming-en/quantized_26_08_21',
        sha256='3a99429804a7c450b2604b26149cda503373bfa34ad83f651967cd3e2d0167fe',
        archive_dir="", size_mb=143, license="MIT", sample_rate=16_000,
        cache_dir="moonshine-small-streaming-en-26_08_21", runtime="moonshine",
        files=(
            ('adapter.ort', 'c665f742364febad597cc9ac1e0b341ffbee0e24a1466e2f3bde95e6e4771762', 2870368),
            ('cross_kv.ort', 'e2d3417144e9514055ebfefe8dcc4c0a55a55adcb8530435844c75c53e352bf6', 5356536),
            ('decoder_kv.ort', '1a05465b1dd955858dfcbee039c0020fb5dd982b0f5094c34e61735d518d771b', 81878600),
            ('encoder.ort', '2d4d973e91e8aca08c51e7e7efa28a46ab265b63d809d5294d18b86bcd85b993', 44148576),
            ('frontend.model.ort', '09b1210ae30dc5f0f3e45f0ebab914c254741323114f53fbbe5ae62cca35058f', 26944),
            ('frontend.weights.ort', '7ef97521bd4bad3928f5bb6808586f4fcc6e92bd5990394112eed7d4052ec338', 7769464),
            ('streaming_config.json', '26f02b6afb22d60871a5efd85c3d38e569cc0ddb6c5eb6e93d3260152ae8a47a', 512),
            ('tokenizer.bin', '6884b35fd6377d4c4d32336a0bc152f36b64d1e45b6503683cdc238250a8472d', 249974),
        ),
    ),
    "kokoro-en": LocalModel(
        id="kokoro-en", kind="tts", label="Kokoro v1.0 82M (English, 28 voices)",
        # Full-precision ONNX export. Pin the official release asset digest.
        url=f"{_RELEASES}/tts-models/kokoro-multi-lang-v1_0.tar.bz2",
        sha256="c5f7e2d2caf082bc1d20fb70334a61d99d20b484500aad32e7cf84c128ea3298",
        archive_dir="kokoro-multi-lang-v1_0", size_mb=350, license="Apache-2.0",
        cache_dir="kokoro-en-v1_0",
        sample_rate=24_000, num_threads=8,
        voices=KOKORO_EN_VOICES,
    ),
    "piper-lessac-medium": LocalModel(
        id="piper-lessac-medium", kind="tts", label="Piper Lessac medium (English)",
        url=f"{_RELEASES}/tts-models/vits-piper-en_US-lessac-medium-int8.tar.bz2",
        sha256="f1c6d0295cf16087b05f80fdca5b44daca5cd78e2c425d419a42ba34929805f9",
        archive_dir="vits-piper-en_US-lessac-medium-int8", size_mb=21, license="MIT",
        sample_rate=22_050, voices=("lessac",),
    ),
    "pocket-tts-en": LocalModel(
        id="pocket-tts-en", kind="tts", label="Kyutai Pocket TTS (English)",
        url=f"{_RELEASES}/tts-models/sherpa-onnx-pocket-tts-int8-2026-01-26.tar.bz2",
        sha256="2f3b88823cbbb9bf0b2477ec8ae7b3fec417b3a87b6bb5f256dba66f2ad967cb",
        archive_dir="sherpa-onnx-pocket-tts-int8-2026-01-26", size_mb=98, license="CC-BY-4.0",
        sample_rate=24_000, num_threads=2, cache_dir="pocket-tts-en-int8-2026-01-26",
        voices=("pocket-bria", "pocket-loona"),
    ),
}

DEFAULT_STT_MODEL = "moonshine-small-streaming-en"
# Kokoro sounds clearly better; Piper stays as the small, quickest option.
DEFAULT_TTS_MODEL = "kokoro-en"
# Heart: Kokoro v1.0's lead voice, and the quickest measured here.
DEFAULT_VOICE = "af_heart"


class LocalModelError(RuntimeError):
    """A model could not be found, fetched, verified, or unpacked."""


def get_model(model_id: str, kind: str | None = None) -> LocalModel:
    model = MODELS.get((model_id or "").strip())
    if model is None or (kind and model.kind != kind):
        wanted = [m.id for m in MODELS.values() if not kind or m.kind == kind]
        raise LocalModelError(f"unknown local {kind or 'speech'} model {model_id!r}; choose one of {', '.join(wanted)}")
    return model


def models_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("SLEEPTALK_MODELS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    cache = env.get("XDG_CACHE_HOME", "").strip()
    base = Path(cache).expanduser() if cache else Path.home() / ".cache"
    return base / "sleeptalk" / "models"


def model_path(model: LocalModel, root: Path | None = None) -> Path:
    return (root or models_dir()) / (model.cache_dir or model.id)


def is_ready(model: LocalModel, root: Path | None = None) -> bool:
    try:
        return (model_path(model, root) / READY_MARKER).read_text().strip() == model.sha256
    except (OSError, UnicodeError):
        return False


def download_info(model: LocalModel, root: Path | None = None) -> dict:
    """Model transfer size, not installed disk space or runtime dependencies."""
    return {
        "bytes": sum(size for _, _, size in model.files) if model.files else model.size_mb * 1_000_000,
        "approximate": not bool(model.files),
        "ready": is_ready(model, root),
    }


def missing_models(model_ids: list[str], root: Path | None = None) -> list[LocalModel]:
    return [get_model(model_id) for model_id in model_ids if not is_ready(get_model(model_id), root)]


def _safe_members(archive: tarfile.TarFile, prefix: str):
    for member in archive.getmembers():
        name = member.name
        if name.startswith("./"):
            name = name[2:]
        if not name.startswith(prefix + "/") and name != prefix:
            raise LocalModelError(f"archive entry outside the model directory: {member.name}")
        if member.issym() or member.islnk() or ".." in Path(name).parts:
            raise LocalModelError(f"refusing archive entry {member.name}")
        yield member


def ensure_model(model: LocalModel, *, root: Path | None = None, progress: Progress | None = None,
                 opener: Callable[[str], object] | None = None) -> Path:
    """Download, verify, and unpack one model if it is not already ready. Blocking."""

    root = root or models_dir()
    target = model_path(model, root)
    if is_ready(model, root):
        return target
    root.mkdir(parents=True, exist_ok=True)
    if model.files:
        return _ensure_files(model, root, progress, opener)
    digest = hashlib.sha256()
    opener = opener or (lambda url: urllib.request.urlopen(url, timeout=60))
    with tempfile.TemporaryDirectory(prefix=f"{model.id}-", dir=root) as scratch:
        archive_path = Path(scratch) / model.archive_name
        try:
            with opener(model.url) as response, archive_path.open("wb") as handle:  # type: ignore[attr-defined]
                total = int(getattr(response, "headers", {}).get("Content-Length") or model.size_mb * 1_000_000)
                done = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        except OSError as exc:
            raise LocalModelError(f"could not download {model.label}: {type(exc).__name__}") from None
        if digest.hexdigest() != model.sha256:
            raise LocalModelError(f"{model.label} did not match its pinned checksum; not installed")
        unpack = Path(scratch) / "unpack"
        unpack.mkdir()
        try:
            with tarfile.open(archive_path, "r:bz2") as archive:
                archive.extractall(unpack, members=_safe_members(archive, model.archive_dir))
        except tarfile.TarError as exc:
            raise LocalModelError(f"could not unpack {model.label}: {type(exc).__name__}") from None
        unpacked = unpack / model.archive_dir
        if not unpacked.is_dir():
            raise LocalModelError(f"{model.label} archive did not contain {model.archive_dir}/")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(unpacked), str(target))
    (target / READY_MARKER).write_text(model.sha256 + "\n")
    return target


def _ensure_files(model: LocalModel, root: Path, progress: Progress | None, opener) -> Path:
    """Stage a complete pinned file set before replacing the cached model."""
    target = model_path(model, root)
    opener = opener or (lambda url: urllib.request.urlopen(url, timeout=60))
    total = sum(size for _, _, size in model.files)
    done = 0
    with tempfile.TemporaryDirectory(prefix=f"{model.id}-", dir=root) as scratch:
        stage = Path(scratch) / "model"
        stage.mkdir()
        for name, expected, size in model.files:
            if Path(name).name != name or name in (".", ".."):
                raise LocalModelError("invalid model filename")
            digest = hashlib.sha256()
            count = 0
            try:
                with opener(f"{model.url}/{name}") as response, (stage / name).open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            except OSError as exc:
                raise LocalModelError(f"could not download {model.label}: {type(exc).__name__}") from None
            if count != size or digest.hexdigest() != expected:
                raise LocalModelError(f"{model.label} did not match its pinned checksum; not installed")
        (stage / READY_MARKER).write_text(model.sha256 + "\n")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(stage), str(target))
    return target


def resolve_tts_model(model_id: str, voice_id: str) -> LocalModel:
    """The TTS model a profile means: an explicit model, else the one that owns the voice."""

    name = (voice_id or "").strip()
    from sleeptalk.speech.local_voices import is_custom

    if is_custom(name):
        return get_model("pocket-tts-en", "tts")
    if name == "af":
        # The v0.19 blend: Kokoro speaks it as Bella (voice_index).
        return get_model("kokoro-en", "tts")
    chosen = get_model((model_id or "").strip() or DEFAULT_TTS_MODEL, "tts")
    if not name or name in chosen.voices:
        return chosen
    # A voice that belongs to another local model names that model: profile
    # defaults fill in Kokoro, but "lessac" can only mean Piper.
    for model in MODELS.values():
        if model.kind == "tts" and name in model.voices:
            return model
    return chosen


def required_models(stt_model: str, tts_model: str, voice_id: str) -> list[LocalModel]:
    return [get_model(stt_model or DEFAULT_STT_MODEL, "stt"), resolve_tts_model(tts_model, voice_id)]


def kokoro_accent(voice_id: str) -> str:
    """The eSpeak-NG language Kokoro reads English in for this voice: British
    for its British voices (bf_, bm_), American otherwise. Kokoro v1.0 takes
    English pronunciation from eSpeak-NG; its lexicon files don't change it
    (measured 2026-10-09: the US and GB lexicons give identical audio)."""
    return "en" if (voice_id or "").strip().startswith(("bf_", "bm_")) else "en-us"


def voice_index(model: LocalModel, voice_id: str) -> int:
    """Speaker id for a voice name. The empty name is the default voice (Kokoro's
    Heart) when the model has it, else the model's first voice. Kokoro v1.0
    renumbered its speakers, so numbers are refused there; the v0.19 blend
    "af" (Bella and Sarah mixed) speaks as Bella."""

    name = (voice_id or "").strip()
    if not name:
        return model.voices.index(DEFAULT_VOICE) if DEFAULT_VOICE in model.voices else 0
    if model.id == "kokoro-en" and name == "af":
        name = "af_bella"
    if model.id == "kokoro-en" and name.isdigit():
        raise LocalModelError(
            "Kokoro v1.0 changed its speaker numbers; use a voice name such as 'af_heart' or 'af_bella' instead."
        )
    from sleeptalk.speech.local_voices import is_custom, reference_path

    if model.id == "pocket-tts-en" and is_custom(name):
        try:
            reference_path(name)
        except ValueError as exc:
            raise LocalModelError(str(exc)) from None
        return 0  # Pocket conditions on reference audio, not a speaker index.
    if name in model.voices:
        return model.voices.index(name)
    if name.isdigit() and int(name) < max(len(model.voices), 1):
        return int(name)
    raise LocalModelError(f"{model.label} has no voice {name!r}; choose one of {', '.join(model.voices)}")


def resample_int16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Mono int16 PCM at one rate -> another. Uses soxr when present, else linear."""

    if source_rate == target_rate or not pcm:
        return pcm
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    try:
        import soxr

        out = soxr.resample(samples, source_rate, target_rate)
        return np.asarray(out, dtype=np.int16).tobytes()
    except ImportError:
        count = int(round(len(samples) * target_rate / source_rate))
        positions = np.linspace(0, len(samples) - 1, count)
        out = np.interp(positions, np.arange(len(samples)), samples.astype(np.float32))
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
