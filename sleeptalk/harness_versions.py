"""Read-only harness inventory and upstream release audit; never an updater."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen


def inventory() -> dict:
    return json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


def install_spec(harness: str) -> str:
    entry = inventory()["harnesses"][harness]
    separator = "==" if entry["upstream"]["kind"] == "pypi" else "@"
    return entry["upstream"]["package"] + separator + entry["recommended"]


def pi_install_spec() -> str:
    pi = inventory()["harnesses"]["pi"]
    return f'{pi["upstream"]["package"]}@{pi["recommended"]}'


def omp_install_command() -> str:
    version = inventory()["harnesses"]["omp"]["recommended"]
    return f"curl -fsSL https://omp.sh/install | sh -s -- --binary --ref v{version}"


def omp_sdk_policy() -> dict:
    return inventory()["harnesses"]["omp"]["protected_sdk"]


def cursor_sdk_policy() -> dict:
    return inventory()["harnesses"]["cursor_sdk"]


def cursor_sdk_install_spec() -> str:
    return f'cursor-sdk=={cursor_sdk_policy()["recommended"]}'


def inspect_cursor(binary: str) -> dict:
    return _inspect_cli(binary, "cursor_cli")


def classify_pi(version: str) -> str:
    # Untested != incompatible. In particular, do not reject older versions
    # just because they predate the recommendation for fresh installations.
    return "verified" if version in inventory()["harnesses"]["pi"]["verified"] else "unverified"


def inspect_pi(binary: str) -> dict:
    return _inspect_cli(binary, "pi")


def inspect_omp(binary: str) -> dict:
    result = _inspect_cli(binary, "omp")
    # A compiled binary with the same version is not the isolated SDK.
    root = Path(result["binary"]).resolve().parent.parent
    sdk = {"version": None, "compatibility": "unavailable"}
    try:
        package = json.loads((root / "package.json").read_text())
        if package.get("name") == omp_sdk_policy()["package"] and (root / "src/sdk.ts").is_file():
            version = package.get("version")
            sdk = {"version": version, "compatibility": "verified" if version in omp_sdk_policy()["verified"] else "unverified"}
    except (OSError, ValueError, AttributeError):
        pass
    return {**result, "protected_sdk": sdk}


def _inspect_cli(binary: str, harness: str) -> dict:
    selected = shutil.which(os.path.expanduser(binary))
    result = {"binary": selected or binary, "version": None, "compatibility": "unavailable"}
    if not selected:
        return result
    selected = str(Path(selected).absolute())
    result["binary"] = selected
    # A version inspection must not inherit provider credentials or load the
    # caller's project. This doesn't prove behavior of arbitrary wrappers.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP") if k in os.environ}
    try:
        with tempfile.TemporaryDirectory(prefix=f"sleeptalk-{harness}-version-") as directory:
            env.update(HOME=directory, USERPROFILE=directory, PI_CODING_AGENT_DIR=directory,
                       XDG_CONFIG_HOME=directory, XDG_CACHE_HOME=directory, NO_COLOR="1")
            proc = subprocess.run([selected, "--version"], env=env, cwd=directory,
                                  capture_output=True, text=True, timeout=10, check=True)
        version = proc.stdout.strip()
        if harness == "omp":
            version = version.removeprefix("omp/")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            return {**result, "compatibility": "unverified"}
        verified = version in inventory()["harnesses"][harness]["verified"]
        status = "verified" if verified else "unverified"
        if version in inventory()["harnesses"][harness].get("handshake_verified", []):
            status = "handshake-only"
        return {**result, "version": version, "compatibility": status}
    except (OSError, subprocess.SubprocessError):
        return result


def latest_version(source: dict) -> str:
    kind = source["kind"]
    if kind == "npm":
        url = f'https://registry.npmjs.org/{quote(source["package"], safe="")}/latest'
    elif kind == "pypi":
        url = f'https://pypi.org/pypi/{quote(source["package"], safe="")}/json'
    elif kind == "github":
        url = f'https://api.github.com/repos/{source["repository"]}/releases/latest'
    elif kind == "cursor-installer":
        url = "https://cursor.com/install"
    else:
        raise ValueError("Unknown release source")
    request = Request(url, headers={"User-Agent": "sleeptalk-harness-audit"})
    with urlopen(request, timeout=15) as response:
        payload = response.read(4_000_001)
    if len(payload) > 4_000_000:
        raise ValueError("Release metadata exceeds limit")
    if kind == "cursor-installer":
        match = re.search(r'https://downloads\.cursor\.com/lab/([0-9.]+-[a-z0-9]+)/', payload.decode())
        if not match:
            raise ValueError("Cursor installer has no recognized release")
        return match.group(1)
    data = json.loads(payload)
    if kind == "github":
        if data.get("draft") or data.get("prerelease"):
            raise ValueError("Release is not stable")
        version = data["tag_name"].removeprefix("v")
    else:
        version = data["info"]["version"] if kind == "pypi" else data["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.+-]{0,79}", version):
        raise ValueError("Invalid release version")
    return version


def audit_releases(manifest: dict) -> dict:
    def check(item):
        name, spec = item
        try:
            latest = latest_version(spec["upstream"])
            return name, {"latest": latest, "changed": latest != spec["latest_observed"], "status": "ok"}
        except Exception:
            # A timeout or bad response must never masquerade as up-to-date.
            return name, {"latest": None, "changed": None, "status": "unavailable"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(check, manifest["harnesses"].items()))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--upstream", action="store_true", help="Check public release metadata; never install or upgrade.")
    parser.add_argument("--pi-bin", help="Inspect this existing Pi executable with --version (no prompt).")
    parser.add_argument("--omp-bin", help="Inspect this existing OMP executable and its protected SDK eligibility (no prompt).")
    parser.add_argument("--cursor-bin", help="Inspect an existing Cursor CLI executable (no prompt or upgrade).")
    parser.add_argument("--json", action="store_true", help="Print the complete inventory as JSON.")
    parser.add_argument("--fail-on-change", action="store_true", help="With --upstream, exit 1 when a release differs from the last review.")


def run(args: argparse.Namespace) -> int:
    report = inventory()
    if args.pi_bin:
        report["installed_pi"] = inspect_pi(args.pi_bin)
    if getattr(args, "omp_bin", None):
        report["installed_omp"] = inspect_omp(args.omp_bin)
    if getattr(args, "cursor_bin", None):
        report["installed_cursor"] = inspect_cursor(args.cursor_bin)
    if args.upstream:
        report["audited_at"] = datetime.now(timezone.utc).isoformat()
        report["upstream_audit"] = audit_releases(report)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f'Harness inventory (last reviewed {report["checked_at"]})')
        for name, spec in report["harnesses"].items():
            verified = ", ".join(spec["verified"]) or "not yet qualified under this policy"
            print(f'{spec["label"]}: verified={verified}; fresh-install recommendation={spec.get("recommended", "pending")}; last observed upstream={spec["latest_observed"]}')
            if "protected_sdk" in spec:
                sdk = spec["protected_sdk"]
                print(f'  CLI evidence: {spec["verification_surface"]}; protected SDK verified={", ".join(sdk["verified"])}; SDK recommendation={sdk["recommended"]}; Bun tested={sdk["bun"]}')
            if args.upstream:
                audit = report["upstream_audit"][name]
                print(f'  upstream: {audit["latest"] or "unavailable"}; review={"required" if audit["changed"] else audit["status"]}')
        if args.pi_bin:
            pi = report["installed_pi"]
            print(f'Existing Pi: {pi["version"] or "unknown"} ({pi["compatibility"]}); installation unchanged.')
            if pi["compatibility"] == "unverified":
                print("This version is unverified, not rejected. Use adapter test with your profile to check its protocol.")
        if "installed_omp" in report:
            omp = report["installed_omp"]
            print(f'Existing OMP: {omp["version"] or "unknown"} (CLI {omp["compatibility"]}); protected SDK {omp["protected_sdk"]["compatibility"]}; installation unchanged.')
            print("Protected mode requires an exact verified npm SDK and Bun. CLI version evidence alone does not establish protected startup.")
        if "installed_cursor" in report:
            cursor = report["installed_cursor"]
            print(f'Existing Cursor CLI: {cursor["version"] or "unknown"} ({cursor["compatibility"]}); installation unchanged.')
            print("ACP handshake evidence does not qualify authenticated turns or the separate Python SDK.")
        print("Version evidence does not certify a custom build or live voice. No installation was changed.")
    audits = report.get("upstream_audit", {}).values()
    if any(item["status"] != "ok" for item in audits):
        return 2
    if args.pi_bin and report["installed_pi"]["compatibility"] == "unavailable":
        return 2
    if "installed_omp" in report and report["installed_omp"]["compatibility"] == "unavailable":
        return 2
    if "installed_cursor" in report and report["installed_cursor"]["compatibility"] == "unavailable":
        return 2
    return int(args.fail_on_change and any(item["changed"] for item in audits))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
