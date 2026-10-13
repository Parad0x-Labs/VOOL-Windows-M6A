"""Decide pass/fail from CodeQL SARIF inside the job, without GitHub code scanning.

The repository cannot store code-scanning alerts, so the CodeQL workflow analyzes with
upload disabled and this script reads the SARIF it wrote:
  - every finding is listed in the log and the job summary;
  - with --changed-files, a finding of security severity >= 7.0 (high or critical) in a file
    the pull request changes fails the job. Findings in untouched files are reported, not
    blocking, so existing code does not hold every pull request hostage.
A missing or unreadable SARIF file fails the job: an analysis that produced nothing is
never reported as clean.

Usage: python codeql_gate.py SARIF_DIR [--changed-files LIST]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

BLOCKING_SEVERITY = 7.0


def _rules(run: dict) -> dict[str, dict]:
    tool = run.get("tool", {})
    components = [tool.get("driver", {}), *tool.get("extensions", [])]
    return {rule["id"]: rule for comp in components for rule in comp.get("rules", []) if "id" in rule}


def findings(sarif_dir: Path) -> list[dict]:
    files = sorted(sarif_dir.glob("*.sarif"))
    if not files:
        raise SystemExit(f"no SARIF output in {sarif_dir}: CodeQL did not produce results")
    out = []
    for path in files:
        for run in json.loads(path.read_text(encoding="utf-8"))["runs"]:
            rules = _rules(run)
            for result in run.get("results", []):
                rule_id = result.get("ruleId") or result.get("rule", {}).get("id", "?")
                props = rules.get(rule_id, {}).get("properties", {})
                try:
                    severity = float(props.get("security-severity", 0))
                except ValueError:
                    severity = 0.0
                loc = (result.get("locations") or [{}])[0].get("physicalLocation", {})
                out.append(
                    {
                        "rule": rule_id,
                        "severity": severity,
                        "file": loc.get("artifactLocation", {}).get("uri", "?"),
                        "line": loc.get("region", {}).get("startLine", 0),
                        "message": (result.get("message", {}).get("text", "").splitlines() or [""])[0][:160],
                    }
                )
    return sorted(out, key=lambda f: (-f["severity"], f["file"], f["line"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sarif_dir", type=Path)
    parser.add_argument("--changed-files", type=Path)
    args = parser.parse_args()

    results = findings(args.sarif_dir)
    changed = None
    if args.changed_files:
        changed = {line.strip() for line in args.changed_files.read_text(encoding="utf-8").splitlines() if line.strip()}
    blocking = [
        f for f in results if changed is not None and f["severity"] >= BLOCKING_SEVERITY and f["file"] in changed
    ]

    lines = [f"CodeQL findings: {len(results)} total, {len(blocking)} blocking"]
    for f in results:
        mark = "BLOCKING" if f in blocking else "report"
        lines.append(f"  [{mark}] {f['severity']:.1f} {f['rule']} {f['file']}:{f['line']} {f['message']}")
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("```\n" + "\n".join(lines) + "\n```\n")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
