"""Windows: the git spawn guard fires before CreateProcess, in the shapes Windows hands it.

Linux cannot run Windows' subprocess branch, so these rows replay what it does, in the order it does
it: the argv list is turned into one command line (`subprocess.list2cmdline`), shell=True wraps it
as `<ComSpec> /c "<line>"` with ComSpec as the executable, then `sys.audit("subprocess.Popen",
executable, args, cwd, env)` is raised, and only after that `_winapi.CreateProcess` is called. The
first row reads that order from this interpreter's own subprocess.py, so the replay is pinned to the
stdlib rather than to memory. os.spawnv on Windows raises the documented `os.spawn` audit event
(mode, path, argv, env) from C before it spawns; _winapi.CreateProcess raises its own event
(application_name, command_line, current_directory). The real Windows run is listed in
jobs/captest-fixes/git-refused-windows/DIAGNOSIS.md.
"""
from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

from core import safe_git

COMSPEC = r"C:\Windows\System32\cmd.exe"
GIT_EXE = r"C:\Program Files\Git\cmd\git.exe"


@pytest.fixture
def guarded(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(safe_git, "_GUARD_ARMED", safe_git._GUARD_ARMED)  # disarmed again after the row
    safe_git.install_spawn_guard()  # the hook stays in this process; only _GUARD_ARMED turns it off


def _windows_execute_child(args, *, executable=None, shell=False, cwd=None, env=None) -> list:
    """subprocess.Popen._execute_child's Windows order, with CreateProcess recorded, never called."""
    created: list = []
    if not isinstance(args, str):
        args = subprocess.list2cmdline(args)
    if shell:
        executable = COMSPEC
        args = f'{COMSPEC} /c "{args}"'
    sys.audit("subprocess.Popen", executable, args, cwd, env)
    created.append((executable, args))  # _winapi.CreateProcess(executable, args, ...)
    return created


def test_windows_subprocess_audits_before_it_calls_createprocess() -> None:
    source = inspect.getsource(subprocess)
    windows_branch = source[source.index("if _mswindows:", source.index("class Popen")):]
    windows_child = windows_branch[windows_branch.index("def _execute_child"):]
    windows_child = windows_child[:windows_child.index("def _wait")]
    assert "list2cmdline(args)" in windows_child
    assert windows_child.index('sys.audit("subprocess.Popen"') < windows_child.index("_winapi.CreateProcess(")
    assert windows_child.index("/c") < windows_child.index('sys.audit("subprocess.Popen"'), "shell wrapping comes first"


@pytest.mark.parametrize("args, kwargs", [
    (["git", "status"], {}),
    ([GIT_EXE, "commit", "-m", "x"], {}),
    (["git.exe", "push"], {}),
    ("git status", {"shell": True}),
    ("cd sub && git commit -am x", {"shell": True}),
    (["git", "status"], {"shell": True}),
    ([COMSPEC, "/c", "git status"], {}),
    ('cmd /c "git status"', {}),
    ([COMSPEC, "/d", "/s", "/c", '"git status"'], {}),
    (["status"], {"executable": GIT_EXE}),
], ids=["list", "list-full-path", "list-exe", "shell", "shell-chain", "shell-list", "cmd-c-list",
        "cmd-c-line", "cmd-dsc", "executable"])
def test_a_git_start_is_refused_before_createprocess(guarded, args, kwargs) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows):
        _windows_execute_child(args, **kwargs)


def test_createprocess_s_own_audit_event_is_refused_too(guarded) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows):
        sys.audit("_winapi.CreateProcess", None, '"C:\\Program Files\\Git\\cmd\\git.exe" status', None)
    with pytest.raises(safe_git.GitRefusedOnWindows):
        sys.audit("_winapi.CreateProcess", COMSPEC, f'{COMSPEC} /c "git status"', None)


@pytest.mark.parametrize("path, argv", [
    (GIT_EXE, [GIT_EXE, "status"]),
    ("git", ["git", "status"]),
    (COMSPEC, [COMSPEC, "/c", "git status"]),
])
def test_os_spawnv_is_refused_at_its_audit_event(guarded, path, argv) -> None:
    import os

    with pytest.raises(safe_git.GitRefusedOnWindows):
        sys.audit("os.spawn", os.P_WAIT, path, argv, None)


@pytest.mark.parametrize("args, kwargs", [
    ([sys.executable, "-m", "pytest", "-q"], {}),
    ("python -m pytest -q", {"shell": True}),
    ([COMSPEC, "/d", "/s", "/c", r"C:\VOOL\Start_VOOL.bat"], {}),
], ids=["python", "shell-python", "start-vool"])
def test_other_windows_starts_still_reach_createprocess(guarded, args, kwargs) -> None:
    assert len(_windows_execute_child(args, **kwargs)) == 1
