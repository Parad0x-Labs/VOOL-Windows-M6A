"""ops/first_run_pact_journeys.py on Windows: import cleanly, then refuse to run.

The journeys stop the daemon with os.killpg and SIGKILL, which Windows does not
have. The script must say so plainly instead of crashing at import.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "first_run_pact_journeys.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_imports_without_sigkill(monkeypatch):
    monkeypatch.delattr(signal, "SIGKILL")
    module = _load("pact_journeys_no_sigkill")
    assert callable(module.main)


def test_main_refuses_off_posix_before_writing(monkeypatch, tmp_path, capsys):
    module = _load("pact_journeys_refusal")
    out = tmp_path / "journeys"
    monkeypatch.setattr(module, "OUT", out)
    monkeypatch.setattr(module, "JOURNEYS", {})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(os, "name", "nt")

    assert module.main() == 2

    assert not out.exists()
    assert "unsupported platform" in capsys.readouterr().err
