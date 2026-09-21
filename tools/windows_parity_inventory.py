"""Static route and built-in tool seeds for the Windows parity review.

This is a change detector, not a reachability or security certificate. Dynamic routes,
plugins, and direct effects still require live inventory and installed-artifact tests.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "windows_parity" / "source_inventory.json"
_PATH = re.compile(r"^/[A-Za-z0-9_./{}<>:*-]{0,159}$")
_CLASSIFICATIONS = frozenset({
    "unreviewed", "shared_reuse", "shared_refactor", "windows_adapter", "windows_native",
    "default_disabled", "compatibility_only", "outside_desktop",
})


def _route_candidates() -> list[dict[str, object]]:
    locations: dict[str, set[str]] = {}
    files = sorted((ROOT / "core" / "web" / "api").glob("*.py"))
    files.append(ROOT / "apps" / "vool_api_server.py")
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        source = file.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if not value.startswith("//") and _PATH.fullmatch(value):
                locations.setdefault(value, set()).add(source)
    return [
        {"path": path, "sources": sorted(sources), "classification": "unreviewed"}
        for path, sources in sorted(locations.items())
    ]


def _tool_candidates() -> list[dict[str, str]]:
    file = ROOT / "core" / "runtime_tool_contracts.py"
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    tools: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "RuntimeToolContract":
            continue
        fields = {
            keyword.arg: keyword.value.value
            for keyword in node.keywords
            if keyword.arg and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str)
        }
        intent = fields.get("intent", "")
        if not intent:
            continue
        if intent in tools:
            raise ValueError(f"duplicate built-in tool intent: {intent}")
        tools[intent] = {
            "intent": intent,
            "side_effect_class": fields.get("side_effect_class", ""),
            "approval_requirement": fields.get("approval_requirement", ""),
            "classification": "unreviewed",
        }
    return [tools[intent] for intent in sorted(tools)]


def current_inventory() -> dict[str, object]:
    return {
        "schema": "vool.windows_source_inventory.v1",
        "route_candidates": _route_candidates(),
        "builtin_tool_candidates": _tool_candidates(),
        "limitations": [
            "Static path literals are candidates, not a proof of route reachability.",
            "Dynamic paths, plugin/MCP tools, UI surfaces, background jobs, and direct effects need separate review.",
        ],
    }


def inventory_drift(snapshot: dict[str, object], current: dict[str, object]) -> list[str]:
    problems: list[str] = []
    for field, key in (("route_candidates", "path"), ("builtin_tool_candidates", "intent")):
        old_rows = snapshot.get(field, [])
        new_rows = current.get(field, [])
        if not isinstance(old_rows, list) or not isinstance(new_rows, list):
            return [f"{field} is malformed"]
        old = {row.get(key) for row in old_rows if isinstance(row, dict)}
        new = {row.get(key) for row in new_rows if isinstance(row, dict)}
        if old != new:
            problems.append(f"{field}: added={sorted(new - old)} removed={sorted(old - new)}")
        if field == "builtin_tool_candidates":
            old_contracts = {
                row["intent"]: (row.get("side_effect_class"), row.get("approval_requirement"))
                for row in old_rows
                if isinstance(row, dict) and "intent" in row
            }
            new_contracts = {
                row["intent"]: (row.get("side_effect_class"), row.get("approval_requirement"))
                for row in new_rows
                if isinstance(row, dict) and "intent" in row
            }
            changed = sorted(intent for intent in old & new if old_contracts[intent] != new_contracts[intent])
            if changed:
                problems.append(f"{field}: changed declarations={changed}")
    return problems


def carry_review_classifications(current: dict[str, object], previous: dict[str, object]) -> None:
    """Keep human review on unchanged candidates when regenerating the source seed."""
    for field, key in (("route_candidates", "path"), ("builtin_tool_candidates", "intent")):
        old_rows = previous.get(field, [])
        if not isinstance(old_rows, list):
            continue
        prior = {row.get(key): row for row in old_rows if isinstance(row, dict)}
        for row in current[field]:
            old = prior.get(row[key])
            if old is None:
                continue
            if field == "builtin_tool_candidates" and (
                old.get("side_effect_class") != row["side_effect_class"]
                or old.get("approval_requirement") != row["approval_requirement"]
            ):
                continue
            classification = old.get("classification", "unreviewed")
            if classification in _CLASSIFICATIONS:
                row["classification"] = classification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="refresh the reviewed source snapshot")
    parser.add_argument("--check", action="store_true", help="fail if candidate identities drifted")
    parser.add_argument("--release-check", action="store_true", help="fail while any candidate is unreviewed")
    args = parser.parse_args()
    current = current_inventory()
    if args.write:
        if SNAPSHOT.is_file():
            carry_review_classifications(current, json.loads(SNAPSHOT.read_text(encoding="utf-8")))
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0
    if args.check or args.release_check:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        problems = inventory_drift(snapshot, current)
        if args.release_check:
            for field in ("route_candidates", "builtin_tool_candidates"):
                unreviewed = [row for row in snapshot[field] if row.get("classification") not in _CLASSIFICATIONS - {"unreviewed"}]
                if unreviewed:
                    problems.append(f"{field}: {len(unreviewed)} candidates still unreviewed")
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    print(json.dumps(current, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
