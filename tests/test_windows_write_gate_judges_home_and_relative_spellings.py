"""On Windows, the write gate also judges where `~`, a root-relative, a drive-relative and an
unanchored relative path lead, by spelling, before anything looks them up.

Security Architect additions to RECONCILE-351 F1 (move-share-lexical):

- A redirected profile (`USERPROFILE=\\\\server\\home\\me`) turns `~\\x` into a path on that share,
  so the gate judges the spelling with `~` expanded too.
- A root-relative path (`\\x`) lands on whatever drive is current, so on a Windows host it is refused
  outright, as `windows_path_policy.classify` calls it `root_relative`. A drive-relative path (`Z:x`)
  is judged by its drive, and a relative path with no workspace to anchor it (`x\\y`) by the drive of
  the folder it is read from (the working directory), with the same `_drive_is_remote` rule.

Every refused row spies on the lookups (`realpath`, `stat`, `lstat`, `exists`, `isdir`, `scandir`,
`nt._path_exists`, ...) and asserts none names the marker host. The fixtures are the ones of
tests/test_windows_write_gate_refuses_share_spellings_before_any_lookup.py: every Windows switch on,
a marked folder, `C:` local and `Z:` a mapped network drive. Root-relative refusal needs the gate's
`_NT_SPELLING` seam, which says the host spells paths as Windows does; on the Linux shards it is off,
because there every POSIX root (the marked tmp folder too) spells as root-relative.
"""
from __future__ import annotations

import os

import pytest

from core import windows_write_gate as gate
from tests.test_windows_write_gate_refuses_share_spellings_before_any_lookup import (
    spy,
    windows,
)

SHARE_HOME = "\\\\attkr9\\home\\me"


def _write_lanes(path: str, marked) -> dict[str, tuple[str, dict, object]]:
    marked_file = str(marked / "x.txt")
    return {
        "machine.write_file": ("machine.write_file", {"path": path, "content": "x"}, None),
        "machine.ensure_directory": ("machine.ensure_directory", {"path": path}, None),
        "machine.move_path source": ("machine.move_path", {"source": path, "destination": marked_file}, None),
        "machine.move_path destination": ("machine.move_path", {"source": marked_file, "destination": path}, None),
        "operator.move_path source": ("operator.move_path", {"source_path": path, "destination_path": str(marked)}, None),
        "media.export": ("media.export", {"output_path": path}, None),
    }


def _refused(calls) -> dict[str, bool]:
    refused = {}
    for case, (intent, arguments, workspace) in calls.items():
        try:
            reason = gate.refusal(intent, arguments, workspace)
        except Exception as exc:  # a lane that raises its own refusal still refuses
            reason = f"raised {type(exc).__name__}"
        refused[case] = bool(reason)
    return refused


@pytest.mark.parametrize("typed", ["~\\attkr9.txt", "~/attkr9.txt", "~\\attkr9\\x.txt"])
def test_a_home_path_on_a_redirected_profile_share_is_refused_without_a_lookup(typed, windows, spy, monkeypatch) -> None:
    from core.local_operator_actions import dispatch_operator_action
    from core.operator.models import OperatorActionIntent

    monkeypatch.setenv("USERPROFILE", SHARE_HOME)
    monkeypatch.setenv("HOME", SHARE_HOME)

    calls = _write_lanes(typed, windows)
    assert _refused(calls) == dict.fromkeys(calls, True)
    move = OperatorActionIntent(kind="move_path", raw_text=f'move "{typed}" to "{windows}"')
    result = dispatch_operator_action(move, task_id="gate-task", session_id="gate-operator")
    assert (result.ok, result.status) == (False, gate.STATUS)
    assert gate.inside_flagged(typed) is False

    assert spy.calls == []
    assert gate._refused_spelling(typed) in {"unc", "device", "nt_namespace"}


def test_a_home_path_on_a_local_profile_is_not_refused_by_its_spelling(windows, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\you")
    assert gate._refused_spelling("~\\a.txt") == ""


@pytest.mark.parametrize("typed", ["attkr9\\y.txt", "attkr9/y.txt", "attkr9.txt"])
def test_an_unanchored_relative_path_read_from_a_network_drive_is_refused_without_a_lookup(
    typed, windows, spy, monkeypatch
) -> None:
    calls = _write_lanes(typed, windows)
    # machine.write_file and ensure_directory place a relative path in the home folder, which is local
    # here; every other lane reads it from the working directory, which is on the mapped drive Z:.
    del calls["machine.write_file"], calls["machine.ensure_directory"]
    monkeypatch.setattr(os, "getcwd", lambda: "Z:\\work")

    refused = _refused(calls)
    flagged = gate.inside_flagged(typed)
    kind = gate._refused_spelling(typed)
    monkeypatch.undo()

    assert spy.calls == []
    assert refused == dict.fromkeys(calls, True)
    assert flagged is False
    assert kind == gate.RELATIVE_OFF_LOCAL


def test_an_unanchored_relative_path_read_from_a_local_drive_is_not_refused_by_its_spelling(windows, monkeypatch) -> None:
    monkeypatch.setattr(os, "getcwd", lambda: "C:\\work")
    assert gate._refused_spelling("x\\y.txt") == ""
    assert gate._refused_spelling("C:x\\y.txt") == ""


def test_a_relative_machine_write_is_judged_by_the_home_folder_it_lands_in(windows, spy, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", "Z:\\attkr9-profile")
    monkeypatch.setenv("HOME", "Z:\\attkr9-profile")
    calls = {
        "machine.write_file": ("machine.write_file", {"path": "attkr9\\x.txt", "content": "x"}, None),
        "machine.ensure_directory": ("machine.ensure_directory", {"path": "attkr9"}, None),
    }
    assert _refused(calls) == dict.fromkeys(calls, True)
    assert spy.calls == []


@pytest.mark.parametrize("typed", ["Z:attkr9\\y.txt", "z:attkr9", "Q:attkr9\\y.txt"])
def test_a_drive_relative_path_on_a_network_or_unknown_drive_is_refused_without_a_lookup(typed, windows, spy) -> None:
    calls = _write_lanes(typed, windows)
    assert _refused(calls) == dict.fromkeys(calls, True)
    assert gate.inside_flagged(typed) is False
    assert spy.calls == []
    assert gate._refused_spelling(typed) == gate.REMOTE_DRIVE


@pytest.mark.parametrize("typed", ["\\attkr9\\x.txt", "/attkr9/x.txt", "\\attkr9"])
def test_a_root_relative_path_is_refused_outright_on_a_windows_host_without_a_lookup(typed, windows, spy, monkeypatch) -> None:
    from core.local_operator_actions import dispatch_operator_action
    from core.operator.models import OperatorActionIntent

    monkeypatch.setattr(gate, "_NT_SPELLING", True, raising=False)

    calls = _write_lanes(typed, windows)
    calls["workspace.write_file"] = ("workspace.write_file", {"path": "a.txt", "content": "x"}, typed)
    assert _refused(calls) == dict.fromkeys(calls, True)
    move = OperatorActionIntent(kind="move_path", raw_text=f'move "{typed}" to "{typed}"')
    result = dispatch_operator_action(move, task_id="gate-task", session_id="gate-operator")
    assert result.ok is False
    assert gate.inside_flagged(typed) is False
    assert spy.calls == []
    assert gate._refused_spelling(typed) == "root_relative"


def test_a_root_relative_spelling_is_left_to_the_host_off_windows(windows) -> None:
    # On a POSIX host `/x` is that host's own full path (the marked tmp folder is one).
    assert gate._NT_SPELLING is (os.name == "nt")
    if os.name != "nt":
        assert gate._refused_spelling(str(windows)) == ""
