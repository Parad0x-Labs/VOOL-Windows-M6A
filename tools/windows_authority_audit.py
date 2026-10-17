"""Pin source-level Windows authority doors without mistaking declarations for enforcement.

The ledger is deliberately conservative. Static paths and call sites are leads for
review, not proof of reachability, authentication, OS isolation, or release parity.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

from core.command_registry.census import ownership_table
from tools.windows_parity_inventory import ROOT, current_inventory

SNAPSHOT = ROOT / "docs" / "windows_parity" / "authority_audit.json"
STATUSES = frozenset({"authenticated", "brokered", "safely_disabled", "unresolved"})
REVIEW_FIELDS = frozenset({"authority_status", "enforcement_proof", "proof_status", "gap"})
SOURCE_FILES = [
    *sorted((ROOT / "core" / "web" / "api").glob("*.py")),
    ROOT / "apps" / "vool_api_server.py",
]
EFFECT_ROOTS = (
    ROOT / "apps", ROOT / "core", ROOT / "sandbox", ROOT / "installer",
    ROOT / "network", ROOT / "relay",
)
PROCESS_CALLS = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "os.system", "os.popen", "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
})
NETWORK_CALLS = frozenset({
    "requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "requests.request", "_requests.request", "httpx.get", "httpx.post", "httpx.put", "httpx.patch",
    "httpx.delete", "httpx.request", "urllib.request.urlopen", "urlopen",
    "socket.create_connection", "socket.socket.connect", "socket.socket.sendto",
    "sock.connect", "sock.connect_ex",
})
FILE_CALLS = frozenset({
    "os.replace", "os.rename", "os.remove", "os.unlink", "os.mkdir", "os.makedirs",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.move", "shutil.rmtree",
})


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _function_sites(file: Path) -> tuple[ast.AST, list[tuple[int, int, str]]]:
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    functions = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return tree, functions


def _enclosing_function(line: int, functions: list[tuple[int, int, str]]) -> str:
    matches = [row for row in functions if row[0] <= line <= row[1]]
    return min(matches, key=lambda row: row[1] - row[0])[2] if matches else "<module>"


def _route_sites() -> dict[str, list[dict[str, object]]]:
    paths = {row["path"] for row in current_inventory()["route_candidates"]}
    sites: dict[str, list[dict[str, object]]] = {path: [] for path in paths}
    for file in SOURCE_FILES:
        tree, functions = _function_sites(file)
        parents = {
            id(child): parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in paths:
                selected_by = None
                parent = parents.get(id(node))
                while parent is not None:
                    if isinstance(parent, ast.If) and any(item is node for item in ast.walk(parent.test)):
                        selected_by = parent
                        break
                    parent = parents.get(id(parent))
                branch_calls = []
                if selected_by is not None:
                    branch_calls = sorted({
                        _name(call.func)
                        for statement in selected_by.body for call in ast.walk(statement)
                        if isinstance(call, ast.Call) and _name(call.func)
                    })[:20]
                sites[node.value].append({
                    "source": file.relative_to(ROOT).as_posix(),
                    "line": node.lineno,
                    "function": _enclosing_function(node.lineno, functions),
                    "selected_by_if": selected_by.lineno if selected_by is not None else None,
                    "branch_calls": branch_calls,
                })
    return {path: sorted(rows, key=lambda row: (row["source"], row["line"])) for path, rows in sites.items()}


def _tool_sites() -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, object]]]]:
    contracts: dict[str, dict[str, str]] = {}
    file = ROOT / "core" / "runtime_tool_contracts.py"
    tree, _ = _function_sites(file)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _name(node.func) != "RuntimeToolContract":
            continue
        intent = next(
            (kw.value.value for kw in node.keywords
             if kw.arg == "intent" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)),
            None,
        )
        if intent:
            handler = next(
                (kw.value.value for kw in node.keywords
                 if kw.arg == "handler" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)),
                "",
            )
            contracts[intent] = {"source": f"core/runtime_tool_contracts.py:{node.lineno}", "handler": handler}

    dispatch: dict[str, list[dict[str, object]]] = {}
    file = ROOT / "core" / "runtime_execution_tools.py"
    tree, functions = _function_sites(file)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if _enclosing_function(node.lineno, functions) != "_dispatch_runtime_tool":
            continue
        names = {child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)}
        if "intent" not in names:
            continue
        intents = {
            child.value for child in ast.walk(node.test)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        # Calls in a branch are syntactic leads; imported and nested calls still
        # need manual, transitive effect review before any authority claim.
        calls = sorted({
            _name(child.func) for statement in node.body for child in ast.walk(statement)
            if isinstance(child, ast.Call) and _name(child.func)
        })
        for intent in intents:
            dispatch.setdefault(intent, []).append({
                "source": f"core/runtime_execution_tools.py:{node.lineno}",
                "function": _enclosing_function(node.lineno, functions),
                "branch_calls": calls[:20],
            })
    return contracts, dispatch


def _composed_tool_sites() -> list[dict[str, object]]:
    """Literal contracts imported into the builtin list from other modules."""
    rows: list[dict[str, object]] = []
    for relative in ("core/code_assistant/contracts.py", "core/repoops/contracts.py"):
        file = ROOT / relative
        tree, _ = _function_sites(file)
        literal_expansions: dict[str, dict[str, str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            if not isinstance(node.value, ast.Call) or _name(node.value.func) != "dict":
                continue
            literal_expansions[node.targets[0].id] = {
                kw.arg: kw.value.value for kw in node.value.keywords
                if kw.arg and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
            }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _name(node.func) != "RuntimeToolContract":
                continue
            fields: dict[str, str] = {}
            for kw in node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Name):
                    fields.update(literal_expansions.get(kw.value.id, {}))
                elif kw.arg and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    fields[kw.arg] = kw.value.value
            if "intent" not in fields:
                continue
            rows.append({
                "intent": fields["intent"],
                "contract_source": f"{relative}:{node.lineno}",
                "declared_side_effect_class": fields.get("side_effect_class", ""),
                "declared_approval_requirement": fields.get("approval_requirement", ""),
                "dispatch_source": (
                    "core/runtime_execution_tools.py:3183" if fields["intent"].startswith("code.task.")
                    else "core/runtime_execution_tools.py:3190"
                ),
                "authority_status": "unresolved",
                "gap": "Composed builtin contract; inner authority and transitive effects require review.",
            })
    return sorted(rows, key=lambda row: row["intent"])


def _effect_kind(call: ast.Call) -> str:
    name = _name(call.func)
    if name in PROCESS_CALLS:
        return "process_launch"
    if name in NETWORK_CALLS or name.endswith((
        ".sendmail", ".send_message", ".sendall", ".sendto", ".urlopen",
    )):
        return "network_send"
    if name in FILE_CALLS or name.endswith((".write_text", ".write_bytes", ".unlink", ".mkdir")):
        return "filesystem_mutation"
    if name == "os.write":
        return "filesystem_mutation"
    if name == "os.open":
        flags = call.args[1] if len(call.args) > 1 else next(
            (kw.value for kw in call.keywords if kw.arg == "flags"), None
        )
        if flags is not None and any(
            _name(part).split(".")[-1] in {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}
            for part in ast.walk(flags)
        ):
            return "filesystem_mutation"
    if name in {"open", "Path.open"} or name.endswith(".open"):
        mode = ""
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
            mode = call.args[1].value
        for keyword in call.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                mode = keyword.value.value
        if any(char in mode for char in "wax+"):
            return "filesystem_mutation"
    return ""


def _direct_effect_sites() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for root in EFFECT_ROOTS:
        for file in sorted(root.rglob("*.py")):
            tree, functions = _function_sites(file)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                kind = _effect_kind(node)
                if not kind:
                    continue
                rows.append({
                    "source": file.relative_to(ROOT).as_posix(),
                    "line": node.lineno,
                    "function": _enclosing_function(node.lineno, functions),
                    "call": _name(node.func),
                    "effect_kind": kind,
                    "authority_status": "unresolved",
                    "gap": "Candidate direct effect; reachability, caller authority and JobRunner/broker bypass require review.",
                })
    return sorted(rows, key=lambda row: (row["source"], row["line"], row["call"]))


def _source_fingerprint() -> str:
    """Invalidate reviews after Python, native-host, installer or release-trust changes."""
    digest = hashlib.sha256()
    files = {file for root in EFFECT_ROOTS for file in root.rglob("*.py")}
    files.update({ROOT / "tools" / "windows_authority_audit.py", ROOT / "tools" / "windows_parity_inventory.py"})
    workflow = ROOT / ".github" / "workflows" / "windows-release-gauntlet.yml"
    if workflow.is_file():
        files.add(workflow)
    ci_entry = ROOT / "ops" / "windows_native_ci_user.ps1"
    if ci_entry.is_file():
        files.add(ci_entry)
    native_suffixes = {".cs", ".csproj", ".iss", ".ps1", ".cmd", ".bat", ".vbs", ".sh"}
    for file in (ROOT / "installer").rglob("*"):
        if file.is_file() and file.suffix.lower() in native_suffixes and not {"bin", "obj"} & set(file.parts):
            files.add(file)
    for file in (ROOT / "config" / "release").rglob("*"):
        if file.is_file() and file.suffix.lower() in {".json", ".toml"}:
            files.add(file)
    for file in sorted(files, key=lambda path: path.relative_to(ROOT).as_posix()):
        digest.update(file.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        # Git checks these text sources out with platform-specific line endings. Pin source
        # content consistently across CI hosts; exact build/payload hashes remain separate.
        digest.update(file.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def current_audit() -> dict[str, object]:
    inventory = current_inventory()
    route_sites = _route_sites()
    tool_contracts, tool_dispatch = _tool_sites()
    http: dict[str, list[dict[str, object]]] = {}
    for row in ownership_table():
        if row["transport"] == "http":
            http.setdefault(row["path"], []).append(row)
    routes = []
    for row in inventory["route_candidates"]:
        path = row["path"]
        routes.append({
            "path": path,
            "source_sites": route_sites[path],
            "method_routes": http.get(path, []),
            "authority_status": "unresolved",
            "gap": "No installed Windows host authentication/approval proof or complete transitive effect trace; path literal may not be a reachable route.",
        })
    tools = []
    for row in inventory["builtin_tool_candidates"]:
        intent = row["intent"]
        contract = tool_contracts.get(intent, {})
        sites = tool_dispatch.get(intent, [])
        if not sites and intent.startswith("vool-browser."):
            sites = [{"source": "core/runtime_execution_tools.py:3073", "function": "_dispatch_runtime_tool", "branch_calls": ["handle_intent"], "match": "prefix"}]
        lane = "runtime" if sites else "external_or_unmapped"
        tools.append({
            "intent": intent,
            "contract_source": contract.get("source", ""),
            "contract_handler": contract.get("handler", ""),
            "declared_side_effect_class": row["side_effect_class"],
            "declared_approval_requirement": row["approval_requirement"],
            "dispatch_lane": lane,
            "dispatch_sites": sites,
            "authority_status": "unresolved",
            "gap": "Contract and branch are not proof of Windows host approval, OS brokering, or transitive effect containment.",
        })
    for row in tools:
        if row["intent"] == "sandbox.run_command":
            row.update({
                "authority_status": "safely_disabled",
                "proof_status": "safely_disabled",
                "enforcement_proof": "sandbox/job_runner.py refuses native Windows execution without an OS isolation backend; regression-covered fail-closed path.",
                "gap": "Native Windows command execution remains unavailable until the broker is integrated and installed-artifact tested.",
            })
        elif row["intent"] == "browser.render":
            row.update({
                "authority_status": "safely_disabled",
                "proof_status": "safely_disabled",
                "enforcement_proof": "core/runtime_execution_tools.py exposes an explicit unavailable stub with no browser process launch.",
                "gap": "Browser rendering is unavailable on native Windows; this is not utility parity.",
            })
    dynamic_doors = [
        {
            "door": "plugin/MCP dynamic registrations",
            "source": "core/plugin_executor.py; core/tool_registry.py",
            "authority_status": "unresolved",
            "gap": "Native heuristic-only fallback is refused, but all dynamic providers and alternative launch paths need broker/installed-artifact proof.",
        },
        {
            "door": "plugin/MCP confined_argv native Windows fallback",
            "source": "core/plugin_executor.py:264-277",
            "authority_status": "safely_disabled",
            "proof_status": "safely_disabled",
            "enforcement_proof": "core/plugin_executor.py refuses the heuristic-only native Windows fallback before process launch; regression covered.",
            "gap": "The fallback is disabled; brokered plugin/MCP utility remains unavailable.",
        },
        {
            "door": "native Windows execution broker CLI",
            "source": "installer/windows_broker/Program.cs; installer/windows_broker/WindowsBroker.cs; installer/windows_broker/NativeProcessLauncher.cs",
            "authority_status": "unresolved",
            "gap": "Local AppContainer and Job tests are component evidence only. Broker is unintegrated; executable-path and cleanup races, caller authority, and full tool-policy parity remain unresolved.",
        },
        {
            "door": "background and scheduled work",
            "source": "core/web/api/runtime.py; core/runtime.py",
            "authority_status": "unresolved",
            "gap": "Direct process/network effects and startup policy are not covered by HTTP/tool declarations.",
        },
    ]
    return {
        "schema": "vool.windows_authority_audit.v1",
        "scope": "Current Windows source; static discovery and command census, not installed-artifact evidence.",
        "source_fingerprint": _source_fingerprint(),
        "release_evidence": {
            "signed_artifact_sha256": "",
            "signature_verification": "",
            "windows_10_x64_install_test": "",
            "windows_11_x64_install_test": "",
        },
        "route_candidates": routes,
        "builtin_tool_candidates": tools,
        "composed_tool_candidates": _composed_tool_sites(),
        "census_only_http": sorted(
            [
                {
                    **row,
                    "authority_status": "unresolved",
                    "gap": "Method-level census route has no matching path-literal candidate; authentication and transitive effects require review.",
                }
                for path, rows in http.items() if not route_sites.get(path) for row in rows
            ],
            key=lambda row: row["census_id"],
        ),
        "direct_effect_candidates": _direct_effect_sites(),
        "dynamic_doors": dynamic_doors,
        "limitations": [
            "Only individually reviewed fail-closed paths may be safely_disabled; all other authority claims require source and installed-artifact enforcement.",
            "Method-level HTTP census is ownership metadata, not native Windows authentication proof.",
            "Branch calls and direct-effect sites are syntactic leads, not complete interprocedural reachability or a closed egress census.",
            "Dynamic routes, plugins, callbacks, native code and third-party libraries may add doors.",
            "Release check remains fail-closed until an installed-artifact attestation verifier is implemented.",
        ],
    }


def _installed_evidence_valid(evidence: object) -> bool:
    if not isinstance(evidence, dict):
        return False
    for key in ("source_refs", "negative_tests", "installed_tests"):
        values = evidence.get(key)
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            return False
    digest = evidence.get("installed_artifact_sha256")
    return isinstance(digest, str) and len(digest) == 64 and all(
        char in "0123456789abcdef" for char in digest.lower()
    )


def validate(audit: dict[str, object]) -> list[str]:
    inventory = current_inventory()
    problems: list[str] = []
    if not isinstance(audit.get("release_evidence"), dict):
        problems.append("release_evidence missing")
    for field in (
        "route_candidates", "builtin_tool_candidates", "composed_tool_candidates",
        "census_only_http", "direct_effect_candidates", "dynamic_doors",
    ):
        rows = audit.get(field)
        if not isinstance(rows, list):
            continue
        identities = [_identity(field, row) for row in rows if isinstance(row, dict)]
        if len(identities) != len(rows) or len(set(identities)) != len(identities):
            problems.append(f"{field} has duplicate or invalid identities")
        for row in rows:
            if not isinstance(row, dict) or row.get("authority_status") not in {"authenticated", "brokered"}:
                continue
            if not _installed_evidence_valid(row.get("evidence")):
                problems.append(f"{field} {_identity(field, row)} lacks structured installed enforcement evidence")
    for field, key in (("route_candidates", "path"), ("builtin_tool_candidates", "intent")):
        rows = audit.get(field)
        if not isinstance(rows, list):
            problems.append(f"{field} missing")
            continue
        expected = {row[key] for row in inventory[field]}
        actual = {row.get(key) for row in rows if isinstance(row, dict)}
        if len(rows) != len(actual) or actual != expected:
            problems.append(f"{field} identities differ from source inventory")
        for row in rows:
            if not isinstance(row, dict):
                problems.append(f"{field} has invalid authority status")
                continue
            if row.get("authority_status") not in STATUSES:
                problems.append(f"{field} has invalid authority status")
            elif row["authority_status"] != "unresolved" and (
                not row.get("enforcement_proof") or row.get("proof_status") != row["authority_status"]
            ):
                problems.append(f"{field} {row[key]} has no enforcement proof")
            if not row.get("gap") and row.get("authority_status") == "unresolved":
                problems.append(f"{field} {row[key]} lacks gap")
    for field in ("composed_tool_candidates", "census_only_http", "direct_effect_candidates", "dynamic_doors"):
        rows = audit.get(field)
        if not isinstance(rows, list):
            problems.append(f"{field} missing")
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("authority_status") not in STATUSES:
                problems.append(f"{field} invalid authority status")
            elif row["authority_status"] != "unresolved" and (
                not row.get("enforcement_proof") or row.get("proof_status") != row["authority_status"]
            ):
                problems.append(f"{field} non-unresolved row has no matching enforcement proof")
    return problems


def _source_row(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key not in REVIEW_FIELDS}


def _identity(field: str, row: dict[str, object]) -> object:
    if field == "route_candidates":
        return row.get("path")
    if field in {"builtin_tool_candidates", "composed_tool_candidates"}:
        return row.get("intent")
    if field == "census_only_http":
        return row.get("census_id")
    if field == "dynamic_doors":
        return row.get("door")
    return (row.get("source"), row.get("line"), row.get("function"), row.get("call"))


def carry_reviews(current: dict[str, object], previous: dict[str, object]) -> None:
    """Keep reviewed authority only when the row's source evidence is identical."""
    if previous.get("source_fingerprint") != current.get("source_fingerprint"):
        return
    if isinstance(previous.get("release_evidence"), dict):
        current["release_evidence"] = previous["release_evidence"]
    for field in (
        "route_candidates", "builtin_tool_candidates", "composed_tool_candidates",
        "census_only_http", "direct_effect_candidates", "dynamic_doors",
    ):
        old = {
            _identity(field, row): row
            for row in previous.get(field, []) if isinstance(row, dict)
        }
        for row in current[field]:
            prior = old.get(_identity(field, row))
            if prior is None or _source_row(prior) != _source_row(row):
                continue
            for key in REVIEW_FIELDS:
                if key in prior:
                    row[key] = prior[key]


def source_drift(snapshot: dict[str, object], current: dict[str, object]) -> bool:
    """Review metadata may evolve; route, contract and effect evidence may not drift."""
    if snapshot.keys() != current.keys():
        return True
    for field in (
        "route_candidates", "builtin_tool_candidates", "composed_tool_candidates",
        "census_only_http", "direct_effect_candidates", "dynamic_doors",
    ):
        old = [_source_row(row) for row in snapshot[field]]
        new = [_source_row(row) for row in current[field]]
        if old != new:
            return True
    return any(
        snapshot[field] != current[field]
        for field in ("schema", "scope", "source_fingerprint", "limitations")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--release-check", action="store_true")
    args = parser.parse_args()
    current = current_audit()
    if args.write:
        if SNAPSHOT.is_file():
            carry_reviews(current, json.loads(SNAPSHOT.read_text(encoding="utf-8")))
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Pinned {len(current['route_candidates'])} paths, {len(current['builtin_tool_candidates'])} tools, {len(current['direct_effect_candidates'])} direct-effect leads")
        return 0
    if args.check or args.release_check:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        problems = validate(snapshot)
        if source_drift(snapshot, current):
            problems.append("authority audit differs from current source; refresh and review")
        if args.release_check:
            unresolved = sum(
                row["authority_status"] == "unresolved"
                for field in ("route_candidates", "builtin_tool_candidates", "composed_tool_candidates", "census_only_http", "direct_effect_candidates", "dynamic_doors")
                for row in snapshot[field]
            )
            if unresolved:
                problems.append(f"{unresolved} unresolved authority rows")
            evidence = snapshot.get("release_evidence", {})
            for key in (
                "signed_artifact_sha256", "signature_verification",
                "windows_10_x64_install_test", "windows_11_x64_install_test",
            ):
                if not isinstance(evidence, dict) or not evidence.get(key):
                    problems.append(f"release evidence missing: {key}")
            # A source ledger cannot verify an Authenticode signature or VM report.
            # Until an installed-artifact attestation verifier is wired here, this
            # release command must remain a hard no-go even with filled strings.
            problems.append("installed-artifact attestation verifier not implemented")
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    print(json.dumps(current, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
