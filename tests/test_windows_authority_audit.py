from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path

import tools.windows_authority_audit as audit_tool
from tools.windows_authority_audit import SNAPSHOT, carry_reviews, current_audit, source_drift, validate


def test_authority_audit_covers_pinned_candidates_and_direct_effects() -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = current_audit()

    assert not source_drift(pinned, current)
    assert validate(pinned) == []
    assert len(pinned["route_candidates"]) == 339
    assert len(pinned["builtin_tool_candidates"]) == 136
    assert len(pinned["composed_tool_candidates"]) == 34
    assert pinned["direct_effect_candidates"]
    assert pinned["census_only_http"]
    for field in (
        "route_candidates", "builtin_tool_candidates", "composed_tool_candidates",
        "census_only_http", "direct_effect_candidates", "dynamic_doors",
    ):
        assert all(row["authority_status"] in {"authenticated", "brokered", "safely_disabled", "unresolved"} for row in pinned[field])
    assert all(row["source_sites"] for row in pinned["route_candidates"])
    assert all(row["contract_source"] for row in pinned["builtin_tool_candidates"])


def test_authority_findings_http_counts_match_snapshot() -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    mapped = [row for row in pinned["route_candidates"] if row["method_routes"]]
    method_rows = sum(len(row["method_routes"]) for row in mapped) + len(pinned["census_only_http"])
    paths = {row["path"] for row in mapped} | {row["path"] for row in pinned["census_only_http"]}
    findings = (Path(__file__).resolve().parents[1] / "docs/windows_parity/authority_findings.md").read_text(
        encoding="utf-8"
    )

    assert f"{method_rows} HTTP rows across {len(paths)} paths" in findings
    assert f"{len(mapped)} paths overlap this candidate set" in findings
    assert f"remaining {len(pinned['route_candidates']) - len(mapped)} static candidates" in findings
    effects = pinned["direct_effect_candidates"]
    unresolved = sum(
        row["authority_status"] == "unresolved"
        for field in (
            "route_candidates", "builtin_tool_candidates", "composed_tool_candidates",
            "census_only_http", "direct_effect_candidates", "dynamic_doors",
        )
        for row in pinned[field]
    )
    assert f"{len(effects):,} lexical direct-effect leads" in findings
    assert f"{unresolved:,} unresolved" in findings


def test_composed_contract_literal_expansions_keep_effect_metadata() -> None:
    current = current_audit()
    composed = {row["intent"]: row for row in current["composed_tool_candidates"]}

    assert composed["repo.inspect"]["declared_side_effect_class"] == "read_only"
    assert composed["repo.inspect"]["declared_approval_requirement"] == "none"
    assert composed["repo.diff"]["declared_side_effect_class"] == "read_only"
    assert composed["repo.diff"]["declared_approval_requirement"] == "none"
    assert composed["repo.push"]["declared_side_effect_class"] == "network_publish"
    assert composed["repo.push"]["declared_approval_requirement"] == "explicit_user_opt_in"
    assert composed["code.task.step"]["declared_side_effect_class"] == "workspace_write"


def test_direct_effect_scan_catches_descriptor_writes_and_socket_connects() -> None:
    calls = [
        node for node in ast.walk(ast.parse("os.open(path, os.O_WRONLY | os.O_CREAT); os.write(fd, data); sock.connect(addr)"))
        if isinstance(node, ast.Call)
    ]
    assert [audit_tool._effect_kind(call) for call in calls] == [
        "filesystem_mutation", "filesystem_mutation", "network_send"
    ]
    read_call = next(node for node in ast.walk(ast.parse("os.open(path, os.O_RDONLY)")) if isinstance(node, ast.Call))
    assert audit_tool._effect_kind(read_call) == ""

    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    sites = {(row["source"], row["call"]) for row in pinned["direct_effect_candidates"]}
    assert ("core/liquefy/store.py", "os.write") in sites
    assert ("network/nat_probe.py", "sock.connect") in sites


def test_native_and_release_trust_changes_invalidate_authority_reviews(tmp_path, monkeypatch) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "windows_authority_audit.py").write_text("audit", encoding="utf-8")
    (tools_dir / "windows_parity_inventory.py").write_text("inventory", encoding="utf-8")
    broker = tmp_path / "installer" / "windows_broker" / "Broker.cs"
    broker.parent.mkdir(parents=True)
    broker.write_text("before", encoding="utf-8")
    release = tmp_path / "config" / "release" / "trusted_publisher_keys.json"
    release.parent.mkdir(parents=True)
    release.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(audit_tool, "ROOT", tmp_path)
    monkeypatch.setattr(audit_tool, "EFFECT_ROOTS", ())

    baseline = audit_tool._source_fingerprint()
    broker.write_text("after", encoding="utf-8")
    assert audit_tool._source_fingerprint() != baseline
    baseline = audit_tool._source_fingerprint()
    release.write_text('{"key": "changed"}', encoding="utf-8")
    assert audit_tool._source_fingerprint() != baseline
    baseline = audit_tool._source_fingerprint()
    workflow = tmp_path / ".github" / "workflows" / "windows-release-gauntlet.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on: push\n", encoding="utf-8")
    assert audit_tool._source_fingerprint() != baseline


def test_ledger_does_not_promote_loopback_or_declared_approval_to_authentication() -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    routes = {row["path"]: row for row in pinned["route_candidates"]}
    tools = {row["intent"]: row for row in pinned["builtin_tool_candidates"]}

    for path in ("/api/mode", "/api/settings/credentials", "/api/cloud/usepod/spend-approval/confirm"):
        assert routes[path]["authority_status"] == "unresolved"
        assert routes[path]["source_sites"]
    for intent in ("email.send", "marketplace.purchase_knowledge", "workspace.write_file"):
        assert tools[intent]["authority_status"] == "unresolved"
        assert tools[intent]["contract_source"]
    assert tools["sandbox.run_command"]["authority_status"] == "safely_disabled"
    assert tools["browser.render"]["authority_status"] == "safely_disabled"
    assert all(row["authority_status"] == "unresolved" for row in pinned["direct_effect_candidates"])


def test_non_unresolved_claim_needs_enforcement_proof() -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    altered = deepcopy(pinned)
    altered["route_candidates"][0]["authority_status"] = "authenticated"
    assert "has no enforcement proof" in " ".join(validate(altered))

    altered = deepcopy(pinned)
    altered["builtin_tool_candidates"][0]["authority_status"] = "brokered"
    assert "has no enforcement proof" in " ".join(validate(altered))


def test_authority_claim_needs_structured_installed_evidence_and_unique_identity() -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    altered = deepcopy(pinned)
    row = altered["direct_effect_candidates"][0]
    row.update(authority_status="brokered", proof_status="brokered", enforcement_proof="claimed")
    assert "lacks structured installed enforcement evidence" in " ".join(validate(altered))

    altered = deepcopy(pinned)
    altered["dynamic_doors"].append(dict(altered["dynamic_doors"][0]))
    assert "dynamic_doors has duplicate or invalid identities" in validate(altered)

    altered = deepcopy(pinned)
    altered["route_candidates"].append(None)
    assert "route_candidates has duplicate or invalid identities" in validate(altered)


def test_reviews_survive_identical_source_and_reset_after_source_change() -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = current_audit()
    carry_reviews(current, pinned)
    assert current == pinned

    changed = current_audit()
    changed["source_fingerprint"] = "changed-source"
    carry_reviews(changed, pinned)
    tools = {row["intent"]: row for row in changed["builtin_tool_candidates"]}
    # Source-derived fail-closed classifications are regenerated from current code;
    # only manually carried review evidence is discarded on fingerprint drift.
    assert tools["sandbox.run_command"]["authority_status"] == "safely_disabled"


def test_census_row_with_empty_literal_sites_remains_unresolved(monkeypatch) -> None:
    sites = audit_tool._route_sites()
    sites["/api/mode"] = []
    monkeypatch.setattr(audit_tool, "_route_sites", lambda: sites)

    audit = audit_tool.current_audit()
    assert any(row["path"] == "/api/mode" for row in audit["census_only_http"])
    assert all(row["authority_status"] == "unresolved" for row in audit["census_only_http"])


def test_release_check_rejects_placeholder_artifact_evidence(tmp_path, monkeypatch, capsys) -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    pinned["release_evidence"] = {key: "yes" for key in pinned["release_evidence"]}
    fake_snapshot = tmp_path / "authority_audit.json"
    fake_snapshot.write_text(json.dumps(pinned), encoding="utf-8")
    monkeypatch.setattr(audit_tool, "SNAPSHOT", fake_snapshot)
    monkeypatch.setattr("sys.argv", ["windows_authority_audit", "--release-check"])

    assert audit_tool.main() == 1
    assert "installed-artifact attestation verifier not implemented" in capsys.readouterr().out
