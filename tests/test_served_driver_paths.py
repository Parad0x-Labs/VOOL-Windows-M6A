"""Child drivers preserve path bytes without interpreting them as Python escapes."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._blackbox_served_rig import REPO_ROOT, SEED_MANIFEST, run_in_home
from tests.semantic_phase0 import test_semantic_zero_provider_invocations as semantic_driver


@pytest.mark.parametrize("root", [
    r"C:\Users\Satoshi\project with spaces\source",
    r"C:\new\tests\unicode\source",
    '/tmp/a "quoted" checkout',
    '/tmp/line\nbreak/checkout',
])
def test_phase_driver_preserves_the_checkout_path(monkeypatch, root) -> None:
    captured = []
    real_run = subprocess.run

    def capture_driver(command, **kwargs):
        captured.append(command[2])
        return SimpleNamespace(stdout='@@JSON@@{"touches": [], "from_semantic": []}', stderr="", returncode=0)

    monkeypatch.setattr(semantic_driver, "_REPO_ROOT", Path(root))
    monkeypatch.setattr(subprocess, "run", capture_driver)
    semantic_driver._run_phase("0")
    tree = ast.parse(captured[0])  # Compile the entire real driver, including all network seals.
    path_insert = next(node for node in tree.body if (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "sys.path.insert"
    ))
    probe = "import json, sys\n" + ast.unparse(path_insert) + "\nprint(json.dumps(sys.path[0]))"
    completed = real_run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(completed.stdout) == str(Path(root))


def test_served_manifest_seed_runs_in_a_home_with_spaces(tmp_path) -> None:
    home = tmp_path / "guest home with spaces"
    script = SEED_MANIFEST.format(root=REPO_ROOT, base_url="http://127.0.0.1:1", registered=["scripted:2b"])
    assert ast.parse(script)
    assert ast.literal_eval(run_in_home(home, script)) == ["ollama-local:scripted:2b"]


def test_phase_environment_initializes_asyncio_without_allowing_network(monkeypatch) -> None:
    monkeypatch.setenv("VOOL_OPERATOR_SETTING", "must-not-leak")
    monkeypatch.setattr(semantic_driver, "_PHASE_DRIVER", '''
import asyncio, json, os, socket
sysroot = os.environ.get("SystemRoot")
assert os.environ.get("VOOL_OPERATOR_SETTING") is None
def blocked(*args, **kwargs):
    raise OSError("network blocked")
socket.socket.connect = blocked
try:
    socket.create_connection(("127.0.0.1", 1))
except OSError as exc:
    assert str(exc) == "network blocked"
else:
    raise AssertionError("network was not blocked")
print("@@JSON@@" + json.dumps({"touches": [sysroot], "from_semantic": []}))
''')
    log = semantic_driver._run_phase("0")
    assert log.touches == [os.environ.get("SystemRoot")]
