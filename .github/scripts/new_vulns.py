"""Fail a pull request that brings in a known vulnerability its base branch does not have.

Audits two checkouts with the same advisory data at the same moment:
  - Python: uv.lock exported to pinned requirements, then pip-audit (OSV/PyPI advisories);
  - npm: mobile/package-lock.json with `npm audit --package-lock-only`.

A finding is new when its (package, advisory) pair is absent on the base, so a version bump
that keeps an already-known advisory is not new, and one that adds an advisory is. Any tool
crash fails the job: an audit that did not run is never reported as clean.

Usage: python new_vulns.py BASE_DIR HEAD_DIR
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

NPM_LOCK = Path("mobile") / "package-lock.json"


def _run(cmd: list[str], cwd: Path, ok_codes: tuple[int, ...] = (0,)) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode not in ok_codes:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"audit tool failed ({proc.returncode}): {' '.join(cmd)} in {cwd}")
    return proc.stdout


def python_findings(root: Path) -> set[tuple[str, str]]:
    if not (root / "uv.lock").is_file():
        return set()
    with tempfile.TemporaryDirectory() as tmp:
        reqs = Path(tmp) / "requirements.txt"
        report = Path(tmp) / "pip-audit.json"
        _run(
            [
                sys.executable,
                "-m",
                "uv",
                "export",
                "--frozen",
                "--all-extras",
                "--no-hashes",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "-o",
                str(reqs),
            ],
            root,
        )
        # pip-audit exits 1 when it finds vulnerabilities; any other nonzero code is a crash.
        _run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                str(reqs),
                "--no-deps",
                "--disable-pip",
                "--progress-spinner",
                "off",
                "-f",
                "json",
                "-o",
                str(report),
            ],
            root,
            ok_codes=(0, 1),
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
    return {(dep["name"].lower(), vuln["id"]) for dep in payload["dependencies"] for vuln in dep.get("vulns", [])}


def npm_findings(root: Path) -> set[tuple[str, str]]:
    lock = root / NPM_LOCK
    if not lock.is_file():
        return set()
    # npm audit exits 1 when it finds vulnerabilities; the JSON body is still complete.
    out = _run(["npm", "audit", "--package-lock-only", "--json"], lock.parent, ok_codes=(0, 1))
    payload = json.loads(out)
    if "error" in payload:
        raise SystemExit(f"npm audit failed: {payload['error']}")
    findings = set()
    for name, entry in payload.get("vulnerabilities", {}).items():
        for via in entry.get("via", []):
            # String entries point at another vulnerable package; objects are the advisories.
            if isinstance(via, dict):
                findings.add((name, f"{via.get('url') or via.get('source')} ({via.get('severity')})"))
    return findings


def main() -> int:
    base, head = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    failed = False
    for label, audit in (("python (uv.lock)", python_findings), ("npm (mobile)", npm_findings)):
        base_set, head_set = audit(base), audit(head)
        new = sorted(head_set - base_set)
        fixed = sorted(base_set - head_set)
        print(f"{label}: base={len(base_set)} head={len(head_set)} new={len(new)} fixed={len(fixed)}")
        for pkg, advisory in new:
            print(f"  NEW   {pkg}: {advisory}")
        for pkg, advisory in fixed:
            print(f"  fixed {pkg}: {advisory}")
        failed = failed or bool(new)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
