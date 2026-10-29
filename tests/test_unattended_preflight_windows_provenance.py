"""Windows provenance: ancestry must come from psutil, not the absent /bin/ps."""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from core import unattended_preflight as up


def _no_ps(*_args, **_kwargs):
    raise FileNotFoundError("/bin/ps")  # what Windows raises


class _FakePsutil(types.ModuleType):
    """Process table: pid -> (ppid, cmdline). Unknown pids raise like psutil."""

    class Error(Exception):
        pass

    def __init__(self, table):
        super().__init__("psutil")
        self._table = table
        self.NoSuchProcess = self.Error
        self.AccessDenied = self.Error

    def Process(self, pid):  # mirrors psutil
        if pid not in self._table:
            raise self.Error(pid)
        ppid, cmd = self._table[pid]
        outer = self

        class _P:
            def ppid(self):
                return ppid

            def cmdline(self):
                if cmd is None:
                    raise outer.Error("denied")
                return list(cmd)

        return _P()


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(up, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(subprocess, "run", _no_ps)


def test_windows_chain_walks_full_ancestry_with_argv(monkeypatch, windows):
    me, parent = os.getpid(), os.getppid()
    if parent <= 1:
        pytest.skip("no real parent pid to anchor the fake table")
    table = {
        me: (parent, ["C:\\py\\python.exe", "-m", "pytest"]),
        parent: (7001, ["C:\\py\\python.exe", "ops\\pytest_shards.py"]),
        7001: (1, ["C:\\Windows\\explorer.exe"]),
    }
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(table))

    chain = up.provenance_chain()

    assert [e.pid for e in chain] == [me, parent, 7001]
    assert chain[0].argv == ("C:\\py\\python.exe", "-m", "pytest")
    assert chain[1].ppid == 7001
    assert chain[2].argv == ("C:\\Windows\\explorer.exe",)
    assert up.classify_provenance(chain) is True


def test_windows_unreadable_ancestor_is_unknown_not_owned(monkeypatch, windows):
    me, parent = os.getpid(), os.getppid()
    if parent <= 1:
        pytest.skip("no real parent pid to anchor the fake table")
    table = {me: (parent, None), parent: (1, None)}
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(table))

    chain = up.provenance_chain()

    assert chain and all(e.argv == () for e in chain)
    assert up.classify_provenance(chain, repo_root="C:\\nowhere") is False


def test_windows_missing_psutil_fails_closed(monkeypatch, windows):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import raises ImportError

    assert up._ps_command(os.getpid()) is None
    assert up._ps_ppid(os.getpid()) == 1
    assert up.classify_provenance(up.provenance_chain(), repo_root="C:\\nowhere") is False


def test_posix_still_uses_ps(monkeypatch):
    monkeypatch.setattr(up, "_IS_WINDOWS", False, raising=False)
    seen = []

    def fake_run(cmd, **_kw):
        seen.append(cmd)
        return types.SimpleNamespace(stdout="42\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert up._ps_ppid(5) == 42
    assert up._ps_command(5) == ("42",)
    assert seen[0][:3] == ["/bin/ps", "-o", "ppid="]
    assert seen[1][:3] == ["/bin/ps", "-o", "command="]
