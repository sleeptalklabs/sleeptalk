from pathlib import Path

from sleeptalk._import_boundary import find_package_import_violations


def test_sleeptalk_package_does_not_import_server_app_or_pipecat() -> None:
    violations = find_package_import_violations()
    assert violations == [], "sleeptalk imported server/, app.*, or pipecat:\n" + "\n".join(
        violations
    )


def test_boundary_check_lives_inside_the_package() -> None:
    path = Path(__file__).resolve().parents[1] / "sleeptalk" / "_import_boundary.py"
    assert path.is_file()
