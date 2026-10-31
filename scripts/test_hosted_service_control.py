import json
import subprocess
from unittest.mock import patch

import pytest

from hosted_service_control import OperatorError, control, request


def test_halt_reads_current_revision_before_changing_state():
    with patch("hosted_service_control.admin_token", return_value="private"), patch(
        "hosted_service_control.request", side_effect=[
            {"mode": "running", "revision": 19}, {"mode": "halted", "revision": 20},
        ],
    ) as send:
        assert control("production", "halt", "suspected abuse")["mode"] == "halted"
        assert send.call_args_list[1].args == (
            "production", "private", "control",
            {"action": "halt", "expectedRevision": 19, "reason": "suspected abuse"},
        )


def test_credential_goes_through_stdin_and_redirects_are_not_followed():
    with patch("hosted_service_control.subprocess.run", return_value=subprocess.CompletedProcess(
        [], 0, json.dumps({"mode": "running", "revision": 2}) + "\n200",
    )) as run:
        request("production", "private-secret", "status")
        command = run.call_args.args[0]
        assert "private-secret" not in " ".join(command)
        assert command[:2] == ["curl", "--disable"]  # Ignore a redirect/trace-enabling curlrc.
        assert "--location" not in command and "-L" not in command
        assert run.call_args.kwargs["input"] == "Authorization: Bearer private-secret\n"


@pytest.mark.parametrize("status", ["301", "401", "409", "429", "503"])
def test_http_failures_are_not_retried_or_dumped(status):
    with patch("hosted_service_control.subprocess.run", return_value=subprocess.CompletedProcess(
        [], 0, "sensitive response\n" + status,
    )) as run:
        with pytest.raises(OperatorError, match=f"HTTP {status}") as caught:
            request("staging", "private", "control", {"action": "halt"})
        assert "sensitive" not in str(caught.value)
        assert run.call_count == 1


def test_control_rejects_empty_reason_before_loading_credentials():
    with patch("hosted_service_control.admin_token") as token:
        with pytest.raises(OperatorError, match="reason"):
            control("production", "resume", " ")
        token.assert_not_called()
