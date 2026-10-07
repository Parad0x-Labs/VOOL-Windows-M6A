"""CI prerequisite only: prove both existing Bubblewrap modes run as the test user."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 10


def _text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def collect_report() -> dict:
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    report = {
        "schema": "vool.linux-confinement-preflight.v1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "kernel": platform.release(),
        "uid": uid,
        "ok": False,
        "probes": [],
    }
    if not sys.platform.startswith("linux") or uid is None or uid == 0:
        report["error"] = "Linux confinement preflight requires the non-root test user."
        return report
    binary = shutil.which("bwrap")
    if binary is None:
        report["error"] = "Bubblewrap is missing from the test user's PATH."
        return report
    report["binary"] = binary
    for deny_network in (True, False):
        # Keep these root-independent no-ops equivalent to JobRunner's existing probes.
        argv = [binary, *(["--unshare-net"] if deny_network else []),
                "--ro-bind", "/", "/", "--tmpfs", "/tmp", "--", "true"]
        probe = {
            "deny_network": deny_network, "argv": argv,
            "returncode": None, "stdout": "", "stderr": "", "ok": False,
        }
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, shell=False, check=False,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            probe.update(returncode=completed.returncode, stdout=completed.stdout,
                         stderr=completed.stderr, ok=completed.returncode == 0)
        except subprocess.TimeoutExpired as exc:
            probe.update(error="timeout", stdout=_text(exc.stdout), stderr=_text(exc.stderr))
        except (OSError, subprocess.SubprocessError) as exc:
            probe.update(error=type(exc).__name__, errno=getattr(exc, "errno", None), detail=str(exc))
        report["probes"].append(probe)
    report["ok"] = all(probe["ok"] for probe in report["probes"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = collect_report()
    encoded = json.dumps(report, indent=2) + "\n"
    print(encoded, end="", flush=True)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    except OSError as exc:
        print(f"Confinement evidence could not be saved: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
