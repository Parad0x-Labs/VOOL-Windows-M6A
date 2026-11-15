"""Windows: every text in an MCP or plugin tool's arguments is judged for a share or a device path (K4 07).

An MCP or plugin tool's arguments mean whatever its server says, so no key set tells which of them is a
path. The tool doors (`tool_call_refusal`) judged only the full rule's own keys on the network tier, so
`{"blob": "\\\\evil\\s\\x"}` for an MCP tool passed. Now every text is judged: under any key, inside nested
mappings and lists, the keys themselves, top-level `_` keys (the MCP bridge forwards them), and as
expanded (`%VAR%`). Arguments that are not a mapping are read as the bridge reads them, and refused when
they can't be read. When the tool registry can't be read, both rules apply.

On Windows today MCP and plugin tools are refused outright (`core.plugin_executor`), so this is the second
line. The Windows rule is switched on through `windows_path_policy._IS_WINDOWS`.
"""
from __future__ import annotations

import pytest

from core import windows_path_policy as policy

MCP = "mcp.files.read"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(policy, "_IS_WINDOWS", True)


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"blob": "\\\\evil\\s\\x.txt"}, "unc"),
    ({"opts": {"files": ["notes.txt", "\\\\.\\PhysicalDrive0"]}}, "device"),
    ({"\\\\evil\\s": "x"}, "unc"),
    ({"_cwd": "\\\\evil\\s"}, "unc"),
    ({"where": "//evil/s/x"}, "unc"),
    ({"link": "file://evil/s/x"}, "unc"),
    ([("blob", "\\\\evil\\s\\x")], "unc"),
], ids=["unknown-key", "nested-list", "key", "underscore-key", "slashes", "file-url", "pairs"])
def test_on_windows_a_share_anywhere_in_a_server_tools_arguments_is_refused(windows, arguments, kind) -> None:
    refusal = policy.tool_call_refusal(MCP, arguments)
    assert refusal is not None, arguments
    assert refusal.kind == kind, refusal


def test_on_windows_a_share_behind_a_variable_the_server_expands_is_refused(windows, monkeypatch) -> None:
    monkeypatch.setenv("VOOLSHARE", "\\\\evil\\s")
    refusal = policy.tool_call_refusal(MCP, {"blob": "%VOOLSHARE%\\x"})
    assert refusal is not None and refusal.kind == "unc", refusal


def test_on_windows_arguments_that_cannot_be_read_are_refused(windows) -> None:
    refusal = policy.tool_call_refusal(MCP, 5)
    assert refusal is not None and refusal.kind == "unjudged", refusal


def test_on_windows_an_unreadable_registry_applies_both_rules(windows, monkeypatch) -> None:
    from core import tool_registry

    def broken(_intent):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(tool_registry, "tool_for_intent", broken)
    refusal = policy.tool_call_refusal("plugin.notes.sync", {"blob": "\\\\evil\\s\\x"})
    assert refusal is not None and refusal.kind == "unc", refusal
    # The full rule still applies too: a VOOL path field that is not a full local path.
    refusal = policy.tool_call_refusal("plugin.notes.sync", {"path": "\\x.txt"})
    assert refusal is not None and refusal.kind == "root_relative", refusal


def test_on_windows_local_arguments_pass_and_cyclic_ones_end(windows) -> None:
    assert policy.tool_call_refusal(MCP, {"target": "C:\\Users\\you\\a.txt", "count": 3, "opts": {"x": ["a", None]}}) is None
    loop: dict = {"name": "x"}
    loop["self"] = loop
    assert policy.tool_call_refusal(MCP, {"opts": loop}) is None


def test_on_windows_a_vool_tools_text_fields_are_not_walked(windows) -> None:
    """The full rule's key sets are unchanged for VOOL's own tools: content may mention a share."""
    assert policy.tool_call_refusal("workspace.write_file", {"path": "a.txt", "content": "\\\\evil\\s\\x"}) is None


def test_off_windows_nothing_is_judged(monkeypatch) -> None:
    monkeypatch.setattr(policy, "_IS_WINDOWS", False)
    assert policy.tool_call_refusal(MCP, {"blob": "\\\\evil\\s\\x.txt"}) is None


def test_sabotage_without_the_walk_a_share_under_an_unknown_key_passes(windows, monkeypatch) -> None:
    monkeypatch.setattr(policy, "_server_tool_refusal", lambda arguments: None)
    assert policy.tool_call_refusal(MCP, {"blob": "\\\\evil\\s\\x.txt"}) is None
