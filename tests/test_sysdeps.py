from sleeptalk import sysdeps
from sleeptalk.config import ensure_shared_root, packaged_example_root
from pathlib import Path


def test_packaged_example_root_matches_repo() -> None:
    repo = Path(__file__).resolve().parents[1] / "sleeptalk.example.toml"
    assert packaged_example_root().read_bytes() == repo.read_bytes()


def test_ensure_shared_root_writes_once(tmp_path) -> None:
    written = ensure_shared_root(tmp_path)
    assert written == tmp_path / "sleeptalk.toml"
    assert written.is_file()
    assert ensure_shared_root(tmp_path) is None


def test_have_libopus_finds_homebrew_path_when_find_library_misses(
    monkeypatch, tmp_path
) -> None:
    dylib = tmp_path / "libopus.dylib"
    dylib.write_bytes(b"")
    monkeypatch.setattr(sysdeps.ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(sysdeps, "LIBOPUS_PATHS", (str(dylib),))
    assert sysdeps.have_libopus()


def test_terminal_extra_install_is_auto_installable() -> None:
    cmd = sysdeps.terminal_extra_install_command()
    assert cmd[0] != "sudo"
    assert "sounddevice>=0.5.0" in cmd
    assert sysdeps.python_packages_install_command("pkg")[0] != "sudo"


def test_missing_terminal_extra_tracks_sounddevice(monkeypatch) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None):
        if name == "sounddevice":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    assert sysdeps.missing_terminal_extra() == sysdeps.TERMINAL_EXTRA_HINT


def test_terminal_extra_requires_every_declared_runtime_import(monkeypatch) -> None:
    monkeypatch.setattr(
        sysdeps,
        "_module_available",
        lambda name: name != "numpy",
    )

    assert sysdeps.missing_terminal_extra() == sysdeps.TERMINAL_EXTRA_HINT


def test_have_libopus_false_when_absent(monkeypatch) -> None:
    monkeypatch.setattr(sysdeps.ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(sysdeps, "LIBOPUS_PATHS", ("/no/such/libopus.dylib",))
    assert not sysdeps.have_libopus()


def test_missing_discord_extra_tracks_discord_py(monkeypatch) -> None:
    import importlib.util

    real = importlib.util.find_spec

    def fake(name: str, package: str | None = None):
        if name == "discord":
            return None
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    assert sysdeps.missing_discord_extra() == sysdeps.DISCORD_EXTRA_HINT


def test_discord_extra_requires_every_declared_runtime_import(monkeypatch) -> None:
    monkeypatch.setattr(
        sysdeps,
        "_module_available",
        lambda name: name != "soxr",
    )

    assert sysdeps.missing_discord_extra() == sysdeps.DISCORD_EXTRA_HINT


def test_discord_extra_install_uses_concrete_packages() -> None:
    command = sysdeps.discord_extra_install_command()

    assert "sleeptalk[discord]" not in command
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    packages = {canonicalize_name(Requirement(value).name): Requirement(value)
                for value in sysdeps.DISCORD_EXTRA_PACKAGES}
    assert packages["discord-py"].extras == {"voice"}
    assert str(packages["discord-py"].specifier) == ">=2.5.0"
    assert "soxr>=0.5.0" in command


def test_missing_web_extra_tracks_silero_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(sysdeps, "_module_available", lambda name: name != "onnxruntime")
    assert sysdeps.missing_web_extra() == sysdeps.WEB_EXTRA_HINT
    monkeypatch.setattr(sysdeps, "_module_available", lambda name: True)
    assert sysdeps.missing_web_extra() is None
    assert set(sysdeps.WEB_EXTRA_MODULES) <= set(sysdeps.TERMINAL_EXTRA_MODULES) | {"numpy"}


def test_web_surface_repairs_its_runtime_extra(monkeypatch) -> None:
    from sleeptalk import wizard

    monkeypatch.setattr(sysdeps, "missing_web_extra", lambda: sysdeps.WEB_EXTRA_HINT)
    assert wizard.missing_runtime_extra("web") == sysdeps.WEB_EXTRA_HINT
    installed = []
    monkeypatch.setattr(sysdeps, "install_web_extra", lambda: installed.append("web") or None)
    assert wizard.ensure_runtime_extra("web") is None and installed == ["web"]


def test_repair_requirements_respect_extra_and_python_markers(monkeypatch) -> None:
    monkeypatch.setattr(sysdeps, "requires", lambda _name: [
        "numpy>=1.26",
        "aiohttp>=3.12.14",
        'discord.py[voice]>=2.5.0; extra == "discord"',
        'claude-agent-sdk==0.2.165; extra == "claude"',
        'future-audio>=1; python_version >= "99" and extra == "discord"',
        'old-audio>=1; python_version < "3"',
    ])

    assert sysdeps.runtime_requirements("discord", core=("numpy", "old-audio")) == (
        "numpy>=1.26", "discord.py[voice]>=2.5.0",
    )
    assert sysdeps.runtime_requirements("web", core=("numpy",)) == ("numpy>=1.26",)


def test_discord_does_not_require_unused_provider_sdks(monkeypatch) -> None:
    monkeypatch.setattr(sysdeps, "_module_available", lambda name: name not in {"openai", "websockets"})
    assert sysdeps.missing_discord_extra() is None
    assert not any("openai" in item or "websockets" in item for item in sysdeps.DISCORD_EXTRA_PACKAGES)
