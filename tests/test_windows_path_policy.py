"""The one classifier that decides, on Windows, whether a path or URL names something off this machine.

On Windows `\\\\host\\share\\x`, `//host/share/x`, `\\\\?\\UNC\\host\\share`, `\\\\.\\pipe\\x` and
`file://host/share` all reach the network (an SMB connection, and an NTLM hash sent to whoever
answers) the moment anything stats them. `\\foo` and `C:foo` are not full paths at all: they land on
whatever drive or folder happens to be current. `core.windows_path_policy` names those forms
lexically, without touching the disk, so every door can refuse them before the first lookup.

The pure classifiers are host-independent and are driven here with no seam on Linux CI. The gated
wrappers answer only on Windows; they are driven through `windows_path_policy._IS_WINDOWS`, never
through `os.name` or `sys.platform` (pathlib branches on those). Literal sentences are asserted, never
`refusal_text`, so the same file runs against a base that has no such module.
"""
from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from core import tool_argument_aliases
from core import windows_path_policy as policy
from core.command_registry import registry

# Each spelling with the kind it must get. The network tier keeps every one of these kinds.
_NETWORK_SPELLINGS: dict[str, str] = {
    r"\\attkr9\s\x": "unc",
    "//attkr9/s/x": "unc",
    r"/\attkr9\s": "unc",
    r"\/attkr9/s": "unc",
    r"\\": "unc",
    "//": "unc",
    r"\\?\UNC\attkr9\s": "device",
    "//?/UNC/attkr9/s": "device",
    r"\\?\C:\x": "device",
    r"\\?\GLOBALROOT\Device\HarddiskVolume1\x": "device",
    r"\\.\pipe\attkr9": "device",
    "//./PhysicalDrive0": "device",
    r"\??\C:\x": "nt_namespace",
    r"\??\UNC\attkr9\s": "nt_namespace",
    "/??/C:/x": "nt_namespace",
    "  //attkr9/s  ": "unc",
    "\t" + r"\\attkr9\s" + "\n": "unc",
}

# Not network paths, but not full local paths either: only the full rule refuses them.
_PARTIAL_SPELLINGS: dict[str, str] = {
    r"\foo": "root_relative",
    "/foo": "root_relative",
    r"\attkr9\x": "root_relative",
    "/attkr9/x": "root_relative",
    "C:foo": "drive_relative",
    "C:": "drive_relative",
    r"C:attkr9\x": "drive_relative",
    r"1:\x": "bad_drive",
    r"é:\x": "bad_drive",
}

# What an ordinary user types or a model emits for a local file, and must never be refused.
_LOCAL_PATHS = (
    r"C:\Users\you\a.txt", "c:/x/y", "C:\\", "C:/", r"docs\a.txt", "docs/a.txt", "a.txt", r"..\a", ".",
    "~/Desktop/x", r"~\x", "con.txt", "50%off.txt", r"%UNDEFINED%\x", r"$UNDEFINED\x", "", "   ",
)
_ALLOWED_URLS = (
    "https://x", "https://example.com/a//b", "file:///C:/x", "file://localhost/C:/x", "file:///c|/x",
    "file:///C%3A/My%20Docs/a.txt", "data:text/plain,hi", "about:blank", "mailto:a@b", "/relative/route", r"C:\x",
)
# file: URLs and scheme-less values that still name a host once decoded.
_HOST_URLS = (
    "file://attkr9/s", "FILE://attkr9/s", "file:////attkr9/s", r"file:///\\attkr9\s", "file:///%5C%5Cattkr9/s",
    "file://%61ttkr9/s", "file:/attkr9/s", "file://localhost//attkr9/s", "file://LOCALHOST//attkr9/s",
    "view-source:file://attkr9/s", "  file://attkr9/s/x.html", "//cdn.example.com/x", r"\\attkr9\s",
)

# The registry's filesystem fields, and only those: 50+ other commands carry an HTTP route in `path`.
_COMMAND_FILESYSTEM_FIELDS = (
    ("archaeology.search", "workspace_root"),
    ("archaeology.search", "path_prefix"),
    ("archaeology.trace", "workspace_root"),
    ("archaeology.trace", "path"),
    ("archaeology.recovery_plan", "workspace_root"),
    ("archaeology.recovery_plan", "path"),
    ("archaeology.compare", "workspace_root"),
    ("archaeology.compare", "a"),
    ("archaeology.compare", "b"),
    ("archaeology.locate", "workspace_root"),
    ("agents.start", "workspace"),
    ("approvals.bypass.activate", "workspace_root"),
    ("blackbox.rollback", "workspace_root"),
    ("council.convene", "workspace_root"),
    ("update.rollback", "expected_app_path"),
    ("update.rollback", "expected_data_dir"),
    ("logs.verify", "segment_id"),
)


def _windows(monkeypatch, on: bool = True) -> None:
    monkeypatch.setattr(policy, "_IS_WINDOWS", on)


def _record_alias_lookups(monkeypatch) -> dict[str, list]:
    """Recording pass-throughs over the real alias contract that the gate and the dispatcher read."""
    seen: dict[str, list] = {"schema": [], "bound": []}
    real_schema = tool_argument_aliases.input_schema_for_intent
    real_bind = tool_argument_aliases.bind_known_argument_aliases

    def schema(*args, **kwargs):
        seen["schema"].append((args, kwargs))
        return real_schema(*args, **kwargs)

    def bind(*args, **kwargs):
        bound = real_bind(*args, **kwargs)
        seen["bound"].append(bound)
        return bound

    monkeypatch.setattr(tool_argument_aliases, "input_schema_for_intent", schema)
    monkeypatch.setattr(tool_argument_aliases, "bind_known_argument_aliases", bind)
    return seen


def test_unc_device_and_nt_spellings_are_classified_without_the_seam() -> None:
    for value, kind in _NETWORK_SPELLINGS.items():
        assert policy.classify(value) == kind, value
        assert policy.classify(value, network_only=True) == kind, value
    # A PathLike and bytes are read as the text they carry; on POSIX `//host` keeps both slashes.
    assert policy.classify(PurePosixPath("//attkr9/s")) == "unc"
    assert policy.classify(PureWindowsPath(r"\\attkr9\s\x")) == "unc"
    assert policy.classify(b"//attkr9/s", network_only=True) == "unc"
    assert policy.classify(None) == "" and policy.classify(7) == ""


def test_partial_paths_are_refused_by_the_full_rule_only(monkeypatch) -> None:
    for value, kind in _PARTIAL_SPELLINGS.items():
        assert policy.classify(value) == kind, value
        assert policy.classify(value, network_only=True) == "", value
    # A bare drive root is a valid `drive` value; anything else in that field is classified as a path.
    for value, kind in {"C:": "", "d:/": "", "D:\\": "", "C:foo": "drive_relative", "//h/s": "unc", r"\foo": "root_relative"}.items():
        assert policy.classify_drive(value) == kind, value

    _windows(monkeypatch)
    for value, kind in _PARTIAL_SPELLINGS.items():
        assert policy.refused_form(value) == kind, value
        assert policy.refused_form(value, network_only=True) == "", value
        assert policy.check(value, network_only=True) is None, value
        with pytest.raises(policy.NonLocalPathError) as refused:
            policy.check(value)
        # A ValueError, so every existing `except ValueError` around a resolver carries the sentence.
        assert isinstance(refused.value, ValueError)
        assert (refused.value.value, refused.value.kind) == (value, kind)
        assert "is not a full local path" in str(refused.value) and value in str(refused.value)
    assert policy.refused_drive("C:") == "" and policy.refused_drive("C:attkr9") == "drive_relative"

    # The network tier still refuses a share, with the network sentence.
    with pytest.raises(policy.NonLocalPathError) as refused:
        policy.check("//attkr9/s/x", network_only=True)
    assert refused.value.kind == "unc" and "is a network or device path" in str(refused.value)

    # The sentence is relayed in markdown: the value cannot break out of its backticks or its line.
    with pytest.raises(policy.NonLocalPathError) as refused:
        policy.check("C:attkr9`\r\nx")
    sentence = str(refused.value)
    assert "\r" not in sentence and "\n" not in sentence and sentence.count("`") == 2, sentence


def test_local_paths_and_web_urls_are_allowed(monkeypatch) -> None:
    monkeypatch.delenv("UNDEFINED", raising=False)
    for value in _LOCAL_PATHS:
        assert policy.classify(value) == "", value
        assert policy.classify(value, network_only=True) == "", value
    for url in _ALLOWED_URLS:
        assert policy.classify_url(url) == "", url

    _windows(monkeypatch)
    for value in _LOCAL_PATHS:
        assert policy.refused_form(value) == "", value
    for url in _ALLOWED_URLS:
        assert policy.refused_url(url) == "", url
    for drive in ("C:", "d:/", "D:\\"):
        assert policy.refused_drive(drive) == "", drive


def test_a_variable_or_file_url_that_names_a_host_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("LOGONSERVER", r"\\attkr9")
    for value in (r"%LOGONSERVER%\s", r"$LOGONSERVER\s", r"${LOGONSERVER}\s"):
        assert policy.classify(value) == "unc", value
        assert policy.classify(value, network_only=True) == "unc", value
    for url in _HOST_URLS:
        assert policy.classify_url(url) == "unc", url

    _windows(monkeypatch)
    assert policy.refused_form(r"%LOGONSERVER%\s", network_only=True) == "unc"
    for url in _HOST_URLS:
        assert policy.refused_url(url) == "unc", url


def test_nothing_is_refused_off_windows(monkeypatch) -> None:
    _windows(monkeypatch, on=False)
    monkeypatch.setenv("LOGONSERVER", r"\\attkr9")
    lookups = _record_alias_lookups(monkeypatch)
    every_refused_value = (*_NETWORK_SPELLINGS, *_PARTIAL_SPELLINGS, r"%LOGONSERVER%\s", *_HOST_URLS)

    for value in every_refused_value:
        assert policy.refused_form(value) == "" and policy.refused_form(value, network_only=True) == "", value
        assert policy.refused_drive(value) == "" and policy.refused_url(value) == "", value
        assert policy.check(value) is None and policy.check(value, network_only=True) is None, value
        calls = (
            ("workspace.read_file", {"path": value, "file_path": value, "filepath": value}),
            ("machine.move_path", {"source": value, "destination": value}),
            ("machine.disk_usage", {"drive": value}),
            ("browser.render", {"url": value, "uri": value, "link": value}),
            ("code.task.step", {"task_id": "task-1", "intent": "workspace.read_file", "arguments": {"path": value}}),
        )
        for intent, arguments in calls:
            assert policy.argument_refusal(intent, arguments) is None, (intent, value)
        assert policy.argument_refusal("mcp.files.read_file", {"path": value}, network_only=True) is None, value
        for command_id, field in _COMMAND_FILESYSTEM_FIELDS:
            assert policy.command_input_refusal(command_id, {field: value}) is None, (command_id, field, value)
        chat_fields = {"workspace": value, "workspace_root": value, "cwd": value, "projectRoot": value}
        assert policy.chat_workspace_refusal(dict(chat_fields), dict(chat_fields)) is None, value

    assert lookups == {"schema": [], "bound": []}, "off Windows the alias contract must never be consulted"

    # The same calls refuse once the seam is on, and the pass-throughs above do see the alias lookup:
    # the silence off Windows is the short-circuit, not a check that never ran.
    _windows(monkeypatch)
    assert policy.refused_form("//attkr9/s/x") == "unc" and policy.refused_url("file://attkr9/s") == "unc"
    with pytest.raises(policy.NonLocalPathError):
        policy.check(r"\attkr9\x")
    assert policy.argument_refusal("workspace.read_file", {"path": "//attkr9/s/x"}) is not None
    assert policy.command_input_refusal("archaeology.compare", {"a": "//attkr9/s/x"}) is not None
    assert policy.chat_workspace_refusal({"workspace": "//attkr9/s"}, {}) is not None
    refusal = policy.chat_workspace_refusal({}, {"projectRoot": r"\attkr9\x"})
    assert refusal is not None and (refusal.value, refusal.kind) == (r"\attkr9\x", "root_relative"), refusal
    assert policy.argument_refusal("workspace.read_file", {"filepath": "docs/notes.txt"}) is None
    assert any("workspace.read_file" in (*args, *kwargs.values()) for args, kwargs in lookups["schema"]), lookups


def test_argument_refusal_reads_the_bound_call_and_tiers_its_keys(monkeypatch) -> None:
    lookups = _record_alias_lookups(monkeypatch)
    _windows(monkeypatch)
    step = {"task_id": "task-1"}
    repo_step = {"repo_session_id": "repo-1"}

    # (intent, arguments, network_only, keys the refusal may name, refused value, kind)
    refused = [
        # The model's alias spellings are refused whether read raw or bound onto `path`.
        ("workspace.read_file", {"file_path": "//attkr9/s/x"}, False, {"file_path", "path"}, "//attkr9/s/x", "unc"),
        ("workspace.read_file", {"filepath": r"\attkr9\x"}, False, {"filepath", "path"}, r"\attkr9\x", "root_relative"),
        ("workspace.read_file", {"filename": "C:attkr9.txt"}, False, {"filename", "path"}, "C:attkr9.txt", "drive_relative"),
        ("workspace.read_file", {"dir": r"\\?\UNC\attkr9\s"}, False, {"dir", "path"}, r"\\?\UNC\attkr9\s", "device"),
        # `source` is ambiguous, but under machine.* it is a path, so the full rule applies.
        ("machine.move_path", {"source": r"\attkr9\x", "destination": "b"}, False, {"source"}, r"\attkr9\x", "root_relative"),
        ("machine.move_path", {"source": "a", "destination": "//attkr9/s"}, False, {"destination"}, "//attkr9/s", "unc"),
        ("skill.list", {"workspace": r"\attkr9\x"}, False, {"workspace"}, r"\attkr9\x", "root_relative"),
        ("sandbox.run_command", {"command": "dir", "cwd": "//attkr9/s"}, False, {"cwd"}, "//attkr9/s", "unc"),
        # An email folder is checked with the network tier only.
        ("email.read", {"folder": "//attkr9/s"}, False, {"folder"}, "//attkr9/s", "unc"),
        ("email.send", {"to": ["a@example.com", "//attkr9/s"]}, False, {"to"}, "//attkr9/s", "unc"),
        ("mcp.files.read_file", {"path": "//attkr9/s"}, True, {"path"}, "//attkr9/s", "unc"),
        # A step is checked as the inner call it will run, with the inner intent's own tier.
        ("code.task.step", {**step, "intent": "workspace.read_file", "arguments": {"path": "//attkr9/s"}}, False, {"path"}, "//attkr9/s", "unc"),
        ("repo.step", {**repo_step, "intent": "workspace.read_file", "arguments": {"path": "//attkr9/s"}}, False, {"path"}, "//attkr9/s", "unc"),
        ("code.task.step", {**step, "intent": "workspace.read_file", "arguments": {"file_path": r"\attkr9\x"}}, False,
         {"file_path", "path"}, r"\attkr9\x", "root_relative"),
        ("code.task.step", {**step, "intent": "email.read", "arguments": {"folder": "//attkr9/s"}}, False, {"folder"}, "//attkr9/s", "unc"),
        # Lists and tuples are checked item by item; items that are not text are skipped, not fatal.
        ("workspace.read_file", {"paths": [3, None, "notes.txt", "//attkr9/s/x"]}, False, {"paths"}, "//attkr9/s/x", "unc"),
        ("workspace.read_file", {"targets": ("notes.txt", r"\attkr9\x")}, False, {"targets"}, r"\attkr9\x", "root_relative"),
        # A drive field takes a bare drive root; a URL field takes the URL rule.
        ("machine.disk_usage", {"drive": "//attkr9/s"}, False, {"drive"}, "//attkr9/s", "unc"),
        ("machine.disk_usage", {"drive": "C:attkr9"}, False, {"drive"}, "C:attkr9", "drive_relative"),
        ("machine.find_largest", {"drive": "C:", "path": r"\attkr9\x"}, False, {"path"}, r"\attkr9\x", "root_relative"),
        ("browser.render", {"url": "file://attkr9/s/x.html"}, False, {"url"}, "file://attkr9/s/x.html", "unc"),
        ("browser.render", {"uri": "file:////attkr9/s"}, False, {"uri", "url"}, "file:////attkr9/s", "unc"),
        ("browser.render", {"link": "view-source:file://attkr9/s"}, False, {"link", "url"}, "view-source:file://attkr9/s", "unc"),
    ]
    for intent, arguments, network_only, keys, value, kind in refused:
        refusal = policy.argument_refusal(intent, arguments, network_only=network_only)
        assert refusal is not None, (intent, arguments)
        assert refusal.key in keys and (refusal.value, refusal.kind) == (value, kind), (intent, arguments, refusal)

    allowed = [
        ("email.read", {"folder": "INBOX"}, False),
        ("email.read", {"folder": "/Inbox"}, False),
        ("email.send", {"to": ["a@example.com", "/team"]}, False),
        ("mcp.files.read_file", {"path": r"\foo"}, True),
        ("mcp.files.read_file", {"path": "C:foo"}, True),
        ("code.task.step", {**step, "intent": "email.read", "arguments": {"folder": "/Inbox"}}, False),
        # Content is never classified, whatever it mentions and however it is spelled.
        ("workspace.write_file", {"path": "notes.txt", "content": "//attkr9/s"}, False),
        ("workspace.write_file", {"path": "notes.txt", "text": r"\\attkr9\s"}, False),
        ("workspace.apply_unified_diff", {"patch": "--- a///attkr9/s/x.txt\n+++ b///attkr9/s/x.txt\n@@ -1 +1 @@\n-a\n+b\n"}, False),
        ("sandbox.run_command", {"command": r"type \\attkr9\s\x", "cwd": "repo"}, False),
        ("workspace.read_file", {"path": "notes.txt", "query": "//attkr9/s"}, False),
        # Server-owned `_` keys are never the model's to set and are skipped.
        ("workspace.read_file", {"path": "notes.txt", "_trusted_workspace_root": "//attkr9/s"}, False),
        ("machine.disk_usage", {"drive": "C:"}, False),
        ("machine.disk_usage", {"drive": "d:/"}, False),
        ("machine.disk_usage", {"drive": "D:\\"}, False),
        ("browser.render", {"url": "https://example.com/a"}, False),
        ("browser.render", {"url": "/relative/route"}, False),
        ("machine.read_file", {"path": r"C:\Users\you\a.txt"}, False),
        ("workspace.read_file", {"filepath": "docs/notes.txt"}, False),
    ]
    for intent, arguments, network_only in allowed:
        assert policy.argument_refusal(intent, arguments, network_only=network_only) is None, (intent, arguments)

    # The check read the call the dispatcher will run: the alias contract bound `filepath` onto `path`.
    assert {"path": "docs/notes.txt"} in lookups["bound"], lookups


def test_command_input_refusal_names_only_filesystem_fields(monkeypatch) -> None:
    commands = registry()
    for command_id, field in _COMMAND_FILESYSTEM_FIELDS:
        spec = commands.lookup(command_id)
        assert spec is not None and field in {f.name for f in dataclasses.fields(spec.input_schema)}, (command_id, field)
    route_calls = {"wallet.status": {"path": "/api/wallet/status"}, "chat.pin": {"path": "/api/chat/pin"}}
    for command_id in route_calls:
        assert "path" in {f.name for f in dataclasses.fields(commands.lookup(command_id).input_schema)}, command_id

    _windows(monkeypatch)
    for command_id, field in _COMMAND_FILESYSTEM_FIELDS:
        for value, kind in (("//attkr9/s", "unc"), (r"\attkr9\s", "root_relative"), ("C:attkr9", "drive_relative")):
            refusal = policy.command_input_refusal(command_id, {field: value})
            assert refusal is not None and tuple(refusal) == (field, value, kind), (command_id, field, value, refusal)
        for local in (r"C:\Users\you\repo", "src/app.py", "seg-0001"):
            assert policy.command_input_refusal(command_id, {field: local}) is None, (command_id, field, local)
    # A route path in `path` is an HTTP route, not a file: never refused.
    for command_id, input_data in route_calls.items():
        assert policy.command_input_refusal(command_id, input_data) is None, command_id
