"""The differential driver's child gets what Windows needs to start Python, and nothing of the caller's.

T37 suite 42033494637 (5 Nov 2026) on Windows: every driver child died at ``import asyncio`` with
"OSError: [WinError 10106] The requested service provider could not be loaded or initialized".
The child env held no SYSTEMROOT, without which Winsock cannot load. On Windows the child now gets
SYSTEMROOT from the system (as the Git probe already does), and its home and temp folders point at
its throwaway home, since Path.home() reads USERPROFILE there, not HOME. POSIX is unchanged.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "semantic_phase0_base_differential.py"
_POSIX_KEYS = {
    "PATH", "HOME", "VOOL_HOME", "VOOL_CREDENTIAL_STORE", "VOOL_KEY_STORAGE_MODE", "VOOL_KEY_PASSPHRASE",
    "VOOL_REGISTER_INSTALLED_OLLAMA_MODELS", "VOOL_SKIP_TORCH_GPU_PROBE", "PYTHONDONTWRITEBYTECODE",
}


def _differential():
    spec = importlib.util.spec_from_file_location("vool_base_differential_windows_env", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_windows_child_gets_the_system_root_and_its_own_home(monkeypatch, tmp_path) -> None:
    module = _differential()
    monkeypatch.setattr(module, "_windows_directory", lambda: r"C:\Windows")
    monkeypatch.setenv("SYSTEMROOT", r"D:\planted")

    env = module._corpus_env(str(tmp_path), windows=True)

    assert env["SYSTEMROOT"] == r"C:\Windows"  # from the system, never the caller's env
    assert env["USERPROFILE"] == env["TEMP"] == env["TMP"] == env["HOME"] == str(tmp_path)
    assert set(env) == _POSIX_KEYS | {"SYSTEMROOT", "USERPROFILE", "TEMP", "TMP"}


def test_a_posix_child_env_is_unchanged(tmp_path) -> None:
    env = _differential()._corpus_env(str(tmp_path), windows=False)

    assert set(env) == _POSIX_KEYS
    assert env["HOME"] == env["VOOL_HOME"] == str(tmp_path)
