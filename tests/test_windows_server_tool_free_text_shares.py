"""MCP and plugin tool calls on Windows: any free text holding `\\\\` or `//` is refused as a share.

Owner decision D11 (option A): an MCP or plugin tool's arguments mean whatever its server says, so
every text in them is judged, under any argument name, and a text holding a backslash pair or a slash
pair (a URL's own `scheme://` aside) is refused, mixed forms and pairs glued to other characters
included. The Windows rule is switched on through `_IS_WINDOWS`, so these rows run for real on Linux.
"""

from __future__ import annotations

import json

import pytest

from core import windows_path_policy as policy

MCP_INTENT = "mcp.demo.run"
BUILTIN_INTENT = "workspace.write_file"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(policy, "_IS_WINDOWS", True)


@pytest.fixture
def mcp_tool(windows, monkeypatch):
    monkeypatch.setattr("core.execution.mcp_bridge.is_mcp_intent", lambda intent: intent.strip() == MCP_INTENT)


@pytest.mark.parametrize(
    "text",
    [
        r"\\server\share",
        r"\\server\share\file.txt",
        "//server/share",
        "//server/share/file.txt",
        r"\\?\UNC\server\share",
        r"\\.\pipe\x",
    ],
)
def test_a_share_under_a_free_text_name_is_refused(mcp_tool, text):
    for name in ("query", "command", "content", "selector", "anything"):
        refusal = policy.tool_call_refusal(MCP_INTENT, {name: text})
        assert refusal is not None, (name, text)
        assert refusal.key == name and refusal.value == text
        assert refusal.kind in policy.NETWORK_KINDS, refusal


@pytest.mark.parametrize(
    "text",
    [
        r"/\server\share",
        r"\/server/share",
        r"//server\share",
        r"\\server/share",
        r"copy \\server\share\x .",
        r"cmd /c dir&\\server\share\x.exe",
        r"run^//server/share",
        r"x=\\server\share",
        r"C:\\server\share",
        "C://server/share",
        r"C:\Users\you\\server\share",
        "see https://example.com//server/share",
    ],
)
def test_mixed_and_embedded_pairs_are_refused(mcp_tool, text):
    refusal = policy.tool_call_refusal(MCP_INTENT, {"text": text})
    assert refusal is not None, text
    assert refusal.kind == "unc", (text, refusal)


@pytest.mark.parametrize("text", ["//button[@id='go']", "// a code comment", "a//b"])
def test_xpath_and_code_comments_are_refused_by_decision(mcp_tool, text):
    refusal = policy.tool_call_refusal(MCP_INTENT, {"selector": text})
    assert refusal is not None and refusal.kind == "unc", (text, refusal)


@pytest.mark.parametrize(
    "arguments",
    [
        {r"\\server\share": True},
        {"opts": {"//server/share": "read"}},
        {"opts": [{"inner": json.dumps([r"\\server\share"])}]},
        {"payload": json.dumps({"note": "//server/share"})},
        {"files": ("a.txt", {"deep": ["//server/share"]})},
    ],
)
def test_shares_in_keys_nested_values_and_json_texts_are_refused(mcp_tool, arguments):
    refusal = policy.tool_call_refusal(MCP_INTENT, arguments)
    assert refusal is not None and refusal.kind == "unc", (arguments, refusal)


def test_non_mapping_arguments_naming_a_share_are_refused(mcp_tool):
    for arguments in (json.dumps({"q": "//server/share"}), [r"\\server\share"], r"\\server\share"):
        refusal = policy.tool_call_refusal(MCP_INTENT, arguments)
        assert refusal is not None and refusal.kind == "unc", (arguments, refusal)


def test_a_file_url_to_a_share_under_any_name_is_refused(mcp_tool):
    for text in ("file://server/share/x", "file:////server/share", "open file://server/share now"):
        refusal = policy.tool_call_refusal(MCP_INTENT, {"note": text})
        assert refusal is not None and refusal.kind == "unc", (text, refusal)


def test_a_share_hidden_in_a_variable_is_refused(mcp_tool, monkeypatch):
    monkeypatch.setenv("VOOL_TEST_SHARE_ROOT", r"\\server\share")
    refusal = policy.tool_call_refusal(MCP_INTENT, {"command": r"type %VOOL_TEST_SHARE_ROOT%\x"})
    assert refusal is not None and refusal.kind == "unc", refusal


def test_a_variable_vool_cannot_resolve_is_refused(mcp_tool, monkeypatch):
    monkeypatch.delenv("VOOL_TEST_UNSET_SHARE_VAR", raising=False)
    refusal = policy.tool_call_refusal(MCP_INTENT, {"target": r"%VOOL_TEST_UNSET_SHARE_VAR%\x"})
    assert refusal is not None and refusal.kind == "unresolved_variable", refusal


def test_a_plugin_tool_is_judged_the_same_way(windows, monkeypatch):
    class Contract:
        source = "plugin:demo"

    monkeypatch.setattr("core.execution.mcp_bridge.is_mcp_intent", lambda intent: False)
    monkeypatch.setattr(
        "core.tool_registry.tool_for_intent", lambda intent: Contract() if intent == "demo.plugin_tool" else None
    )
    refusal = policy.tool_call_refusal("demo.plugin_tool", {"body": r"see \\server\share"})
    assert refusal is not None and refusal.kind == "unc", refusal


def test_an_intent_with_surrounding_space_is_still_a_server_tool(mcp_tool):
    refusal = policy.tool_call_refusal(f" {MCP_INTENT} ", {"body": "//server/share"})
    assert refusal is not None and refusal.kind == "unc", refusal


def test_unreadable_registries_apply_the_server_walk(windows, monkeypatch):
    def unavailable(intent):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("core.execution.mcp_bridge.is_mcp_intent", unavailable)
    monkeypatch.setattr("core.tool_registry.tool_for_intent", unavailable)
    refusal = policy.tool_call_refusal("some.tool", {"body": r"\\server\share"})
    assert refusal is not None and refusal.kind == "unc", refusal


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": r"C:\Users\you\notes.txt"},
        {"path": "C:/Users/you/notes.txt"},
        {"path": "docs/notes.txt"},
        {"path": r"docs\notes.txt"},
        {"query": "hello world", "count": 3, "flag": None, "ok": True},
        {"text": "a sentence with a / slash and a \\ backslash"},
        {"url": "https://example.com/a/b?q=1#frag"},
        {"url": "https://example.com/a%20b"},
        {"note": "read https://example.com/docs then C:\\Users\\you\\a.txt"},
        {"items": ["one", "two", {"nested": "C:\\data\\x.csv"}]},
        # A JSON text spells a backslash as `\\`, which D11 refuses, so a JSON payload passes with `/`.
        {"payload": json.dumps({"path": "C:/Users/you/a.txt"})},
        {"_trusted_workspace_root": "//server/share", "name": "notes"},
    ],
)
def test_local_paths_and_ordinary_text_still_pass(mcp_tool, arguments):
    assert policy.tool_call_refusal(MCP_INTENT, arguments) is None


def test_no_arguments_still_pass(mcp_tool):
    assert policy.tool_call_refusal(MCP_INTENT, None) is None
    assert policy.tool_call_refusal(MCP_INTENT, {}) is None


def test_a_builtin_tool_keeps_its_own_rule(windows, monkeypatch):
    monkeypatch.setattr("core.execution.mcp_bridge.is_mcp_intent", lambda intent: False)
    # Content of VOOL's own tools is never judged; only the server walk reads every text.
    arguments = {"path": "notes.txt", "content": r"\\server\share"}
    assert policy.tool_call_refusal(BUILTIN_INTENT, arguments) is None


def test_off_windows_nothing_changes(monkeypatch):
    monkeypatch.setattr(policy, "_IS_WINDOWS", False)
    looked_up: list[str] = []

    def spy(intent):
        looked_up.append(intent)
        return True

    monkeypatch.setattr("core.execution.mcp_bridge.is_mcp_intent", spy)
    for arguments in (
        {"body": r"\\server\share"},
        {"body": "//server/share"},
        {r"\\server\share": True},
        json.dumps({"q": "//server/share"}),
        {"target": "%VOOL_TEST_UNSET_SHARE_VAR%"},
        {"path": "//server/share"},
    ):
        assert policy.tool_call_refusal(MCP_INTENT, arguments) is None, arguments
    assert looked_up == []
