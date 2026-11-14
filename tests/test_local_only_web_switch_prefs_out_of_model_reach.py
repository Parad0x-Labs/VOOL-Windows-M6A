"""The model cannot turn "Block web search in Local Only" off by writing, moving or deleting the prefs file.

The switch lives in ``user_preferences.json`` under the runtime's data folder. A deleted file reads as "off", so a model
that could write, move or delete that file could turn the switch off. Each row below drives the real tool dispatch
(`core.runtime_execution_tools._dispatch_runtime_tool`) the way a model's tool call arrives, in the worst placement for
the guard: the chat's workspace CONTAINS the runtime's home, and the runtime's home sits inside ~/Documents, which the
machine file lane may otherwise write. Every row then reads the disk back, whatever status the tool reported.

The command lane is shown two ways: the served call with an approved command up to the kernel profile it would run
under (the Linux bwrap argv must hide the data folder behind an empty read-only mount), and, where bwrap exists, a
real confined run that tries to delete the file.

A second group pins that a save by any other writer keeps the switch: an unreadable file is rewritten with the switch
ON, and only the switch's own Settings save sets it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core import user_preferences

_SWITCH = "local_only_block_web"


@pytest.fixture()
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    user_home = (tmp_path / "userhome").resolve()
    vool_home = user_home / "Documents" / "vool"
    vool_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("VOOL_HOME", str(vool_home))
    prefs = user_preferences._prefs_path()
    assert vool_home in prefs.parents, "the runtime home did not move to the test folder"
    prefs.write_text(json.dumps({_SWITCH: True, "show_workflow": False}), encoding="utf-8")
    return {"user_home": user_home, "vool_home": vool_home, "prefs": prefs}


def _intact(home: dict[str, Path], before: bytes) -> bool:
    prefs = home["prefs"]
    return prefs.is_file() and prefs.read_bytes() == before and user_preferences.local_only_block_web_state() is True


def _dispatch(intent: str, arguments: dict, workspace: Path):
    from core.runtime_execution_tools import _dispatch_runtime_tool

    return _dispatch_runtime_tool(
        intent,
        arguments,
        source_context={"workspace_root": str(workspace), "workspace": str(workspace),
                        "session_id": "prefs-reach", "operating_mode": "auto"},
    )


def _workspaces(home: dict[str, Path]) -> list[Path]:
    # the runtime home itself, and the user's home folder that contains it
    return [home["vool_home"], home["user_home"]]


@pytest.mark.parametrize("which", [0, 1])
def test_workspace_write_tools_cannot_write_the_prefs_file(home: dict[str, Path], which: int) -> None:
    workspace = _workspaces(home)[which]
    prefs = home["prefs"]
    relative = prefs.relative_to(workspace).as_posix()
    before = prefs.read_bytes()
    calls = [
        ("workspace.write_file", {"path": relative, "content": json.dumps({_SWITCH: False})}),
        ("workspace.write_file", {"path": str(prefs), "content": "{}"}),
        ("workspace.replace_in_file", {"path": relative, "old_text": "true", "new_text": "false"}),
        ("workspace.apply_unified_diff", {
            "patch": f"--- a/{relative}\n+++ b/{relative}\n@@ -1 +1 @@\n-{before.decode()}\n+{{}}\n",
        }),
        ("workspace.apply_unified_diff", {
            "patch": f"--- a/{relative}\n+++ /dev/null\n@@ -1 +0,0 @@\n-{before.decode()}\n",
        }),
        ("workspace.ensure_directory", {"path": relative}),
    ]
    for intent, arguments in calls:
        result = _dispatch(intent, arguments, workspace)
        assert result is not None and result.ok is False, (intent, result.response_text if result else None)
        assert _intact(home, before), intent


def test_machine_file_tools_cannot_write_move_or_delete_the_prefs_file(home: dict[str, Path]) -> None:
    prefs = home["prefs"]
    before = prefs.read_bytes()
    outside = home["user_home"] / "Documents" / "moved.json"
    calls = [
        ("machine.write_file", {"path": str(prefs), "content": json.dumps({_SWITCH: False})}),
        ("machine.write_file", {"path": "~/Documents/vool/data/user_preferences.json", "content": "{}"}),
        ("machine.ensure_directory", {"path": str(prefs)}),
        ("machine.move_path", {"source": str(prefs), "destination": str(outside)}),
        ("machine.move_path", {"source": str(prefs.parent), "destination": str(outside)}),
        ("operator.move_path", {"source": str(prefs), "destination": str(outside)}),
    ]
    for intent, arguments in calls:
        result = _dispatch(intent, arguments, home["user_home"])
        assert result is None or result.ok is False, (intent, result.response_text)
        assert _intact(home, before), intent
        assert not outside.exists(), intent


def _approve_sandbox_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import execution_gate, policy_engine

    original = policy_engine.get
    monkeypatch.setattr(
        policy_engine, "get",
        lambda key, default=None: True if key == "execution.allow_sandbox_execution" else original(key, default),
    )
    monkeypatch.setattr(execution_gate, "_spend_operator_approved_command", lambda *_a, **_k: True)


_DELETE = "python3 -c \"import os; os.remove('{path}')\""


@pytest.mark.parametrize("which", [0, 1])
def test_an_approved_command_runs_with_the_data_folder_hidden(
    home: dict[str, Path], monkeypatch: pytest.MonkeyPatch, which: int
) -> None:
    """The served command lane, up to the kernel profile: the bwrap argv hides the data folder."""
    from sandbox import job_runner, network_guard

    workspace = _workspaces(home)[which]
    prefs = home["prefs"]
    before = prefs.read_bytes()
    _approve_sandbox_commands(monkeypatch)
    real_which = shutil.which
    monkeypatch.setattr(job_runner.sys, "platform", "linux")
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    # The whole lane is Linux's: the command line is read by the POSIX rules too. Left on Windows,
    # the sandbox's Windows rule looks the bare `python3` up as an .exe on PATH and refuses it there,
    # before any profile is built.
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", False)
    monkeypatch.setattr(job_runner.shutil, "which", lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else real_which(name, *a, **k))
    monkeypatch.setattr(job_runner, "_backend_usable", lambda *_a, **_k: True)
    launched: list[list[str]] = []

    def _spy(argv, *_args, **_kwargs):
        launched.append(list(argv))
        raise OSError("spy: no process is started in this test")

    monkeypatch.setattr(subprocess, "Popen", _spy)
    relative = prefs.relative_to(workspace).as_posix()
    for command in (_DELETE.format(path=relative), _DELETE.format(path=str(prefs))):
        _dispatch("sandbox.run_command", {"command": command}, workspace)
    assert _intact(home, before)
    if job_runner.os.name != "posix":
        # A Windows host: the Linux kernel profile is built only on a POSIX host
        # (JobRunner._linux_bwrap_prefix), so this lane cannot confine the command here. The honest
        # answer is that it never runs at all, confined or not, and the prefs file stays as it was.
        assert launched == [], f"a process was started without the confinement profile: {launched}"
        return
    profiles = [argv for argv in launched if argv and argv[0] == "/usr/bin/bwrap"]
    assert profiles, f"no confined launch was attempted: {launched}"
    data = str(prefs.parent.resolve())
    for argv in profiles:
        pairs = [argv[i:i + 2] for i in range(len(argv) - 1)]
        writable = [i for i in range(len(argv) - 2) if argv[i] == "--bind" and Path(data).is_relative_to(argv[i + 1])]
        assert writable, "the workspace holding the data folder was not bound (the row proves nothing)"
        hide = pairs.index(["--tmpfs", data])
        assert hide > max(writable), "the data folder is hidden before the writable bind that contains it"
        assert ["--remount-ro", data] in pairs[hide:]
        assert argv.index("--") > hide


def test_an_unreadable_protected_list_refuses_the_job_and_never_runs_it_unconfined(
    home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the protected-state list cannot be read, the bwrap profile is not built, and the job is
    refused: no process starts at all, confined or not (Security Architect, PR #230 follow-up)."""
    from core import runtime_state_protection
    from sandbox import job_runner

    workspace = _workspaces(home)[0]
    prefs = home["prefs"]
    before = prefs.read_bytes()
    _approve_sandbox_commands(monkeypatch)
    real_which = shutil.which
    monkeypatch.setattr(job_runner.sys, "platform", "linux")
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(job_runner.shutil, "which", lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else real_which(name, *a, **k))
    monkeypatch.setattr(job_runner, "_backend_usable", lambda *_a, **_k: True)

    def _unreadable():
        raise OSError("the protected-state list cannot be read")

    monkeypatch.setattr(runtime_state_protection, "protected_state_roots", _unreadable)
    launched: list[list[str]] = []

    def _spy(argv, *_args, **_kwargs):
        launched.append(list(argv))
        raise OSError("spy: no process is started in this test")

    monkeypatch.setattr(subprocess, "Popen", _spy)
    result = _dispatch("sandbox.run_command", {"command": _DELETE.format(path=str(prefs))}, workspace)
    assert launched == [], f"a process was started without the protected-state cover: {launched}"
    assert result is None or result.ok is False, getattr(result, "response_text", result)
    assert _intact(home, before)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bwrap (the Linux kernel sandbox)")
def test_a_real_confined_command_cannot_delete_the_prefs_file(home: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = home["vool_home"]
    prefs = home["prefs"]
    before = prefs.read_bytes()
    _approve_sandbox_commands(monkeypatch)
    for command in (_DELETE.format(path=prefs.relative_to(workspace).as_posix()), _DELETE.format(path=str(prefs))):
        _dispatch("sandbox.run_command", {"command": command}, workspace)
        assert _intact(home, before), command


# ---- other writers keep the switch ---------------------------------------------------------------


@pytest.fixture()
def prefs_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "user_preferences.json"
    monkeypatch.setattr(user_preferences, "_prefs_path", lambda: path)
    return path


def _door(body: dict) -> int:
    from core.web.api.registry_authorities import set_prefs_authority
    from core.web.api.runtime import RuntimeServices

    return set_prefs_authority(body, {"Content-Type": "application/json"}, RuntimeServices(display_name="VOOL")).status


def _stored(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8")).get(_SWITCH)


@pytest.mark.parametrize("content", ["{torn write", "[]", json.dumps({_SWITCH: "yes", "humor_percent": 10})])
def test_an_unreadable_file_is_rewritten_with_the_switch_on_by_any_other_writer(prefs_file: Path, content: str) -> None:
    prefs_file.write_text(content, encoding="utf-8")
    handled, _reply = user_preferences.maybe_handle_preference_command("humor 30%")
    assert handled
    assert _stored(prefs_file) is True
    assert user_preferences.local_only_block_web_state() is True


def test_a_settings_save_of_another_field_keeps_an_unreadable_switch_on(prefs_file: Path) -> None:
    prefs_file.write_text("{torn write", encoding="utf-8")
    assert _door({"show_workflow": True}) == 200
    assert _stored(prefs_file) is True


def test_a_direct_save_cannot_turn_the_switch_off(prefs_file: Path) -> None:
    prefs_file.write_text(json.dumps({_SWITCH: True}), encoding="utf-8")
    prefs = user_preferences.load_preferences()
    prefs.local_only_block_web = False
    user_preferences.save_preferences(prefs)
    assert _stored(prefs_file) is True


def test_another_writer_does_not_turn_the_switch_on_either(prefs_file: Path) -> None:
    prefs_file.write_text(json.dumps({_SWITCH: False}), encoding="utf-8")
    prefs = user_preferences.load_preferences()
    prefs.local_only_block_web = True
    user_preferences.save_preferences(prefs)
    assert _stored(prefs_file) is False


def test_no_file_yet_stays_off_after_another_save(prefs_file: Path) -> None:
    assert not prefs_file.exists()
    user_preferences.maybe_handle_preference_command("humor 30%")
    assert _stored(prefs_file) is False


def test_only_the_switchs_own_save_sets_it(prefs_file: Path) -> None:
    prefs_file.write_text("{torn write", encoding="utf-8")
    assert _door({_SWITCH: False}) == 200
    assert _stored(prefs_file) is False
    assert user_preferences.local_only_block_web_state() is False
    assert _door({_SWITCH: True}) == 200
    assert _stored(prefs_file) is True
