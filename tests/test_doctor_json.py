"""Stable machine-readable doctor output."""

from __future__ import annotations

import json

from sleeptalk import cli, doctor


def test_doctor_report_json_has_stable_rung_ids() -> None:
    report = doctor.DoctorReport(
        rungs=[
            doctor.Rung("env vars present", doctor.FAIL, ["DEEPGRAM_API_KEY missing"]),
            doctor.Rung("stt round-trip", doctor.SKIP, ["provider unavailable"]),
        ]
    )

    payload = json.loads(report.to_json())

    assert payload == {
        "schema_version": 1,
        "status": "failed",
        "rungs": [
            {
                "id": "env_vars",
                "name": "env vars present",
                "status": "fail",
                "lines": ["DEEPGRAM_API_KEY missing"],
            },
            {
                "id": "stt_key",
                "name": "stt round-trip",
                "status": "skip",
                "lines": ["provider unavailable"],
            },
        ],
    }


def test_doctor_human_output_is_unchanged() -> None:
    report = doctor.DoctorReport(rungs=[doctor.Rung("env vars present", doctor.OK)])

    assert doctor.format_report(report) == "✓ env vars present\ndoctor: OK"


def test_every_doctor_rung_name_has_a_unique_machine_id() -> None:
    names = [
        "file parses",
        "schema valid",
        "component choices",
        "env vars present",
        "agent runtime",
        "binaries on PATH",
        "runtime extras",
        "system dependencies",
        "provider connectivity",
        "hermes dashboard",
        "discord",
        "stt round-trip",
        "language support",
    ]
    ids = [doctor.Rung(name).id for name in names]

    assert len(ids) == len(set(ids))
    assert all(rung_id and " " not in rung_id for rung_id in ids)


def test_cli_doctor_json_is_one_stdout_line(monkeypatch, capsys, tmp_path) -> None:
    async def fake_doctor(_path, *, live=True):
        return doctor.DoctorReport(
            rungs=[doctor.Rung("env vars present", doctor.FAIL, ["missing"])]
        )

    monkeypatch.setattr(doctor, "run_doctor", fake_doctor)

    code = cli.main(
        ["doctor", "--config", str(tmp_path / "profile.toml"), "--json"]
    )

    assert code == 1
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert json.loads(output[0])["rungs"][0]["id"] == "env_vars"
