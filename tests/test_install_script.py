"""The standalone release installer must work outside a source checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install.sh"


@pytest.fixture
def installer_env(tmp_path):
    binary_dir = tmp_path / "tool bin"
    binary_dir.mkdir()
    (binary_dir / "sleeptalk").write_text("#!/bin/sh\nexit 0\n")
    (binary_dir / "sleeptalk").chmod(0o755)
    uv = binary_dir / "uv"
    uv.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if "install" in args:
    Path(os.environ["INSTALL_LOG"]).write_text(json.dumps(args))
elif "dir" in args:
    print(Path(__file__).parent)
else:
    raise SystemExit("unexpected uv invocation: " + str(args))
''')
    uv.chmod(0o755)
    return {**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}",
            "INSTALL_LOG": str(tmp_path / "install.json"), "SLEEPTALK_ALLOW_ROOT": "1"}


def test_release_wheel_install_passes_constraints_outside_checkout(tmp_path, installer_env):
    wheel = tmp_path / "sleeptalk-0.1.0-py3-none-any.whl"
    wheel.touch()
    constraints = tmp_path / "release constraints.txt"
    constraints.write_text("numpy==2.4.6\n")
    standalone = tmp_path / "install.sh"
    standalone.write_bytes(INSTALLER.read_bytes())

    result = subprocess.run(["sh", str(standalone), "--constraints", str(constraints), str(wheel)],
                            cwd=tmp_path, env=installer_env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    args = json.loads(Path(installer_env["INSTALL_LOG"]).read_text())
    assert args == ["--no-config", "tool", "install", "--reinstall", "--python", "3.11",
                    "--constraint", str(constraints), "--", str(wheel)]
    assert "Ready." in result.stdout


def test_checkout_install_keeps_existing_default(tmp_path, installer_env):
    result = subprocess.run(["sh", str(INSTALLER)], cwd=tmp_path, env=installer_env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    args = json.loads(Path(installer_env["INSTALL_LOG"]).read_text())
    assert args[-1] == str(INSTALLER.parent.parent) + "[web]"


@pytest.mark.parametrize("args", [["--constraints"], ["--constraints", "missing.txt"],
                                  ["--unknown"], ["one.whl", "two.whl"]])
def test_invalid_arguments_fail_before_installing(tmp_path, installer_env, args):
    result = subprocess.run(["sh", str(INSTALLER), *args], cwd=tmp_path, env=installer_env,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert not Path(installer_env["INSTALL_LOG"]).exists()
