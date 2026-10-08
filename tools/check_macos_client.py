#!/usr/bin/env python3
"""Read-only macOS client assessment. Never launches a client or edits an app."""

import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys


def run_check(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        return {"command": command, "passed": result.returncode == 0,
                "exit_code": result.returncode,
                "output": (result.stdout + result.stderr).strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "passed": False,
                "exit_code": None, "output": str(error)}


def assess(app):
    app = app.expanduser().resolve()
    report = {"app": str(app), "scope": "Read-only bundle and macOS trust assessment; no account/session access.",
              "bundle": {}, "checks": [], "passed": False}
    try:
        if not app.is_dir() or app.suffix != ".app":
            raise ValueError("Provide an existing .app bundle")
        with (app / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        name = info.get("CFBundleExecutable")
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid CFBundleExecutable")
        binary = app / "Contents/MacOS" / name
        if not binary.is_file() or not binary.resolve().is_relative_to(app):
            raise ValueError("Executable missing or resolves outside the bundle")
        keys = ("CFBundleIdentifier", "CFBundleExecutable", "CFBundleShortVersionString", "LSMinimumSystemVersion")
        report["bundle"] = {key: info.get(key) for key in keys}
        report["checks"] = [
            run_check(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)]),
            run_check(["/usr/bin/codesign", "-dvv", str(app)]),
            run_check(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose", str(app)]),
            run_check(["/usr/bin/lipo", "-archs", str(binary)]),
        ]
        identity = report["checks"][1]["output"]
        team = next((line.partition("=")[2].strip() for line in identity.splitlines()
                     if line.startswith("TeamIdentifier=")), None)
        report["team_identifier"] = team
        report["developer_signed"] = bool(team and team != "not set" and "Signature=adhoc" not in identity)
        report["passed"] = all(check["passed"] for check in report["checks"]) and report["developer_signed"]
        report["note"] = "Passing assesses macOS trust only; TGManager client compatibility and session isolation still need validation."
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        report["error"] = str(error)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="Path to an installed or mounted macOS .app bundle")
    args = parser.parse_args()
    report = assess(args.app)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
