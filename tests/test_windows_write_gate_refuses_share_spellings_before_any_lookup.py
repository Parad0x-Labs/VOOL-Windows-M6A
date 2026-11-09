"""On Windows, the write gate refuses a share, a device path, a doubled-separator drive path and a
mapped network drive by its spelling, before anything resolves, stats or lists it.

Windows CI (RECONCILE-351, bucket F1): an operator move whose paths are only in its quoted text
(`move "\\\\host\\s\\x\\a.txt" to ...`) reached `windows_write_gate.refusal` with the share as its
source, and `_inside` ran `os.path.realpath` on it before refusing. On Windows that lookup is already
the SMB connection (and the user's NTLM hash sent to whoever answers). The status and the reply were
right; the lookup before them was the bug.

The Linux CI shards miss it, because there only `windows_path_policy._IS_WINDOWS` is switched on and
the write gate stays off, so the move handler's own lexical refusal answers first. Here every
Windows switch the lanes read is on (the gate's, the path policy's, the known folders', the
write-root honesty and marked-file lanes'), as on a Windows host, and every lookup a path can reach
is spied on: `os.path.realpath`, `os.stat`, `os.lstat`, `exists`, `isdir`, `scandir`, and
`nt._path_exists` where it exists. None may be called with a refused spelling, in the operator move
and disk-usage lanes, the machine write lane, the workspace edit lane or the gate's own folder-list
checks, while a local path in a marked folder is still looked up and allowed.

`C:` is a local drive here and `Z:` a mapped network drive; any other drive letter is one Windows
cannot name, which counts as remote. Every refused value names the marker host `attkr9`, and no test
name or id does, so a spied call naming it is a lookup of a refused path.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from pathlib import Path
from typing import Any

import pytest

from core import runtime_paths, windows_known_folders, windows_path_policy
from core import windows_write_gate as gate

MARKER = "attkr9"

#: Spellings the gate must refuse without a lookup: (id, folder spelling).
REFUSED_FOLDERS: tuple[tuple[str, str], ...] = (
    ("unc-backslash", "\\\\attkr9\\s\\x"),
    ("unc-forward", "//attkr9/s/x"),
    ("device-unc", "\\\\?\\UNC\\attkr9\\s"),
    ("device-pipe", "\\\\.\\pipe\\attkr9"),
    ("nt-object", "\\??\\UNC\\attkr9\\s"),
    ("doubled-forward", "C://attkr9/s/x"),
    ("doubled-backslash", "C:\\\\attkr9\\s\\x"),
    ("doubled-mixed", "C:/\\attkr9\\s"),
    ("mapped-drive", "Z:\\attkr9\\x"),
    ("mapped-drive-forward", "Z:/attkr9/x"),
    ("unknown-drive", "Q:\\attkr9\\x"),
)
FOLDER_PARAMS = [pytest.param(value, id=form_id) for form_id, value in REFUSED_FOLDERS]


def _on(folder: str, name: str) -> str:
    return folder + ("\\" if "\\" in folder else "/") + name


def _drive_kind(drive: str) -> bool | None:
    return {"C:": False, "Z:": True}.get(drive.upper().rstrip("\\/"))


class Spy:
    """Every spied lookup of a value that names the marker host."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def wrap(self, op: str, original: Any) -> Any:
        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            text = path if isinstance(path, str) else os.fsdecode(path) if isinstance(path, (bytes, os.PathLike)) else ""
            if MARKER in str(text).casefold():
                self.calls.append((op, str(text)))
            return original(path, *args, **kwargs)

        return wrapper


@pytest.fixture
def spy(monkeypatch) -> Spy:
    found = Spy()
    for module in {os.path, posixpath, ntpath}:
        for name in ("realpath", "exists", "lexists", "isdir", "isfile", "islink"):
            original = getattr(module, name, None)
            if original is not None:
                monkeypatch.setattr(module, name, found.wrap(f"{module.__name__}.{name}", original))
    for name in ("stat", "lstat", "scandir", "listdir"):
        monkeypatch.setattr(os, name, found.wrap(f"os.{name}", getattr(os, name)))
    nt = sys.modules.get("nt")
    if nt is not None:
        for name in ("_path_exists", "_path_isdir", "_path_isfile", "_getfinalpathname"):
            if hasattr(nt, name):
                monkeypatch.setattr(nt, name, found.wrap(f"nt.{name}", getattr(nt, name)))
    return found


@pytest.fixture
def windows(tmp_path: Path, monkeypatch) -> Path:
    """Every Windows switch the lanes read, on; a marked folder; returns that folder."""
    home = tmp_path / "user"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    marked = tmp_path / "marked"
    marked.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    for name in ("TEMP", "TMP", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "workspace").mkdir()
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    runtime_paths.configure_runtime_home(tmp_path / "vool")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", _drive_kind)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    from core import windows_marked_file_ops
    from core.agent_runtime import write_root_honesty

    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    monkeypatch.setattr(windows_marked_file_ops, "_ON_WINDOWS", True)
    yield marked
    runtime_paths.configure_runtime_home(None)


def _operator(intent) -> tuple[bool, str, str]:
    from core.local_operator_actions import dispatch_operator_action

    result = dispatch_operator_action(intent, task_id="gate-task", session_id="gate-operator")
    return result.ok, result.status, result.response_text


REFUSED_STATUSES = {gate.STATUS, windows_path_policy.STATUS}


@pytest.mark.parametrize("folder", FOLDER_PARAMS)
def test_a_move_whose_paths_are_only_in_its_quoted_text_is_refused_without_a_lookup(folder, windows, spy) -> None:
    from core.operator.models import OperatorActionIntent

    move = f'move "{_on(folder, "a.txt")}" to "{_on(folder, "b")}"'
    ok, status, text = _operator(OperatorActionIntent(kind="move_path", raw_text=move))

    assert spy.calls == []
    assert (ok, status in REFUSED_STATUSES, "nothing was" in text.lower()) == (False, True, True), text


@pytest.mark.parametrize("folder", FOLDER_PARAMS)
def test_a_parsed_move_and_a_disk_usage_ask_are_refused_without_a_lookup(folder, windows, spy) -> None:
    from core.local_operator_actions import parse_operator_action_intent

    asks = {
        "move": f'move "{_on(folder, "a.txt")}" to "{_on(folder, "b")}"',
        "move into a marked folder": f'move "{_on(folder, "a.txt")}" to "{windows / "b"}"',
        "disk usage": f'how much disk space does "{folder}" use',
    }
    observed = {}
    for case, text in asks.items():
        intent = parse_operator_action_intent(text)
        assert intent is not None, case
        ok, status, _reply = _operator(intent)
        observed[case] = (ok, status in REFUSED_STATUSES)

    assert spy.calls == []
    assert observed == dict.fromkeys(asks, (False, True))


@pytest.mark.parametrize("folder", FOLDER_PARAMS)
def test_every_gated_write_lane_refuses_the_spelling_before_a_lookup(folder, windows, spy) -> None:
    file_path = _on(folder, "x.txt")
    marked_file = str(windows / "x.txt")
    calls = {
        "machine.write_file": ("machine.write_file", {"path": file_path, "content": "x"}, None),
        "machine.ensure_directory": ("machine.ensure_directory", {"path": folder}, None),
        "machine.move_path source": ("machine.move_path", {"source": file_path, "destination": marked_file}, None),
        "machine.move_path destination": ("machine.move_path", {"source": marked_file, "destination": file_path}, None),
        "operator.move_path source": ("operator.move_path", {"source_path": file_path, "destination_path": str(windows)}, None),
        "operator.move_path destination": ("operator.move_path", {"source_path": marked_file, "destination_path": folder}, None),
        "workspace.replace_in_file": ("workspace.replace_in_file", {"path": "x.txt", "old": "a", "new": "b"}, folder),
        "workspace.write_file": ("workspace.write_file", {"path": "x.txt", "content": "x"}, folder),
        "workspace.apply_unified_diff": ("workspace.apply_unified_diff", {"patch": "..."}, folder),
        "media.export": ("media.export", {"output_path": _on(folder, "out.mp4")}, None),
    }
    refused = {}
    for case, (intent, arguments, workspace) in calls.items():
        try:
            reason = gate.refusal(intent, arguments, workspace)
        except Exception as exc:  # a lane that raises its own refusal still refuses
            reason = f"raised {type(exc).__name__}"
        refused[case] = bool(reason)

    assert spy.calls == []
    assert refused == dict.fromkeys(calls, True)
    # What a lane asks to pick a marked destination (the marked-file edit and move lanes) answers
    # the same, without a lookup.
    assert gate.inside_flagged(file_path) is False
    assert gate.inside_flagged(folder) is False
    assert spy.calls == []


@pytest.mark.parametrize("folder", FOLDER_PARAMS)
def test_the_gate_s_own_folder_lists_skip_the_spelling_without_a_lookup(folder, windows, spy) -> None:
    from core import windows_disposable_store

    # The variable's list is split on `os.pathsep`, which on the Linux shards is `:` and would split
    # a drive path into two other entries; there the drive spellings are judged through
    # `_windows_root`, which reads each entry, directly.
    if os.pathsep not in folder:
        assert gate.flagged_roots({gate.ENV_VAR: folder}) == []
    assert gate._windows_root(folder) is None
    gate.wide_root_reason(folder)
    assert windows_disposable_store.refusal_reason(folder)

    assert spy.calls == []


def test_a_local_path_in_a_marked_folder_is_still_looked_up_and_allowed(windows, spy, monkeypatch) -> None:
    looked_up: list[str] = []
    real = posixpath.realpath

    def realpath(path, *args, **kwargs):
        looked_up.append(os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    target = str(windows / "sub" / "b.txt")

    assert gate.refusal("machine.write_file", {"path": target, "content": "x"}, None) is None
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, windows) is None
    assert gate.inside_flagged(target) is True
    assert target in looked_up and str(windows) in looked_up
    assert spy.calls == []


def test_a_local_drive_path_is_not_refused_by_its_spelling(windows) -> None:
    for value in ("C:\\Users\\you\\a.txt", "C:/Users/you/a.txt", "a.txt", str(windows / "a.txt")):
        assert gate._refused_spelling(value) == "", value


def test_a_mapped_or_unknown_drive_is_refused_by_its_spelling_even_when_drive_relative(windows, monkeypatch) -> None:
    monkeypatch.setenv("HOMEDRIVE", "C:")
    for value in ("Z:\\x", "z:/x", "Z:x", "Q:\\x", "%HOMEDRIVE%\\\\host\\s"):
        assert gate._refused_spelling(value), value
