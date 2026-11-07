"""The frozen-corpus loader finds Git at its fixed Windows install path, and still never searches PATH.

T34's Windows suite (run 41943131575) failed four semantic_phase0 differential tests with
"CorpusIntegrityError: trusted git executable is unavailable": the loader's trusted Git list held
only /usr/bin/git, /bin/git and /usr/local/bin/git, so on Windows it found none. tests/_source_guard.py
already trusts Git for Windows at its fixed install paths; the loader must do the same, start it
with the minimal environment Windows needs, and keep refusing when no fixed path holds Git.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "semantic_phase0_base_differential.py"
_WINDOWS_GIT = r"C:\Program Files\Git\cmd\git.exe"


def _differential():
    spec = importlib.util.spec_from_file_location("vool_base_differential_trusted_git", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _as_windows(monkeypatch, module, present: set[str]) -> list[dict]:
    """Make the loader see Windows with Git only at ``present``; record what it would run."""
    monkeypatch.setattr(module, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) in present)
    calls: list[dict] = []

    def _fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 1, b"", b"stopped by the test")

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    return calls


def test_the_loader_runs_git_for_windows_from_its_fixed_install_path(monkeypatch) -> None:
    module = _differential()
    calls = _as_windows(monkeypatch, module, {_WINDOWS_GIT})

    with pytest.raises(module.CorpusIntegrityError, match="frozen corpus object is unavailable"):
        module.load_frozen_corpus(_SCRIPT.parents[1])

    assert len(calls) == 1
    assert calls[0]["argv"][0] == _WINDOWS_GIT
    env = calls[0]["env"]
    # Only the fixed Git directory is on PATH, and Windows gets the SYSTEMROOT it cannot start without.
    assert env["PATH"] == str(Path(_WINDOWS_GIT).parent)
    assert env.get("SYSTEMROOT")
    assert set(env) == {"PATH", "LC_ALL", "SYSTEMROOT"}


def test_the_loader_still_refuses_when_no_fixed_path_holds_git(monkeypatch) -> None:
    module = _differential()
    calls = _as_windows(monkeypatch, module, set())

    with pytest.raises(module.CorpusIntegrityError, match="trusted git executable is unavailable"):
        module.load_frozen_corpus(_SCRIPT.parents[1])

    assert calls == []
