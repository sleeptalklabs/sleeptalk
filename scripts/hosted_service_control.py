#!/usr/bin/env python3
"""Inspect or halt hosted call issuance without exposing operator credentials."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


class OperatorError(RuntimeError):
    pass


def admin_token(environment: str) -> str:
    token = os.environ.get("RENDEZVOUS_ADMIN_TOKEN", "").strip()
    if not token:
        try:
            import keyring
            token = keyring.get_password(f"sleeptalk-call-{environment}-admin", "sleeptalk") or ""
        except ImportError:
            raise OperatorError("Keychain support requires: uv run --extra credentials python scripts/hosted_service_control.py ...") from None
        except Exception:
            raise OperatorError("Cannot read the operator credential store.") from None
    if not token or any(c.isspace() for c in token):
        raise OperatorError("No valid admin credential. Use the operator Keychain entry or RENDEZVOUS_ADMIN_TOKEN.")
    return token


def request(environment: str, token: str, path: str, payload: dict | None = None) -> dict:
    # Fixed hosts and no redirect following keep the credential at its intended service.
    host = "call-staging.sleep.talk" if environment == "staging" else "call.sleep.talk"
    command = [
        "curl", "--disable", "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "30",
        "--proto", "=https", "--header", "@-", "--write-out", "\n%{http_code}",
        f"https://{host}/api/admin/{path}",
    ]
    if payload is not None:
        command.extend(["--header", "Content-Type: application/json", "--data-binary", json.dumps(payload)])
    try:
        result = subprocess.run(
            command, input=f"Authorization: Bearer {token}\n", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError:
        raise OperatorError("Cannot run curl; install it before using this command.") from None
    if result.returncode:
        raise OperatorError("Network request failed. Inspect status before retrying a control change.")
    body, _, status = result.stdout.rpartition("\n")
    if status != "200":
        raise OperatorError(f"Admin request returned HTTP {status}. No automatic retry was made.")
    try:
        value = json.loads(body)
    except ValueError:
        raise OperatorError("Admin response was not valid JSON.") from None
    if not isinstance(value, dict) or type(value.get("revision")) is not int or value.get("mode") not in {"running", "halted"}:
        raise OperatorError("Admin response did not contain a valid service state.")
    return value


def control(environment: str, action: str, reason: str | None) -> dict:
    if action != "status" and (not reason or not reason.strip() or len(reason) > 256):
        raise OperatorError("Supply --reason with 1–256 characters for halt or resume.")
    token = admin_token(environment)
    state = request(environment, token, "status")
    if action == "status":
        return state
    return request(environment, token, "control", {
        "action": action, "expectedRevision": state["revision"], "reason": reason.strip(),
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "halt", "resume"))
    parser.add_argument("--staging", action="store_true", help="Use staging instead of production")
    parser.add_argument("--reason", help="Required for halt and resume; recorded by the service")
    args = parser.parse_args()
    try:
        state = control("staging" if args.staging else "production", args.action, args.reason)
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    # Deliberately exclude arbitrary response fields, including the operator's reason.
    safe = {key: state[key] for key in (
        "mode", "revision", "emergencyDisabled", "relayAttemptsUsed", "relayAttemptsLimit",
    ) if key in state}
    print(json.dumps(safe, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
