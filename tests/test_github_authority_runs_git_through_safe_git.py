"""The Skill Codex signer's git goes through core/safe_git.py, and its text files name their encoding.

registry_service/github_authority.py came in from Mac running ``git`` directly: a checkout whose own config
names a program (``core.fsmonitor``, a filter, a hook) would have git run it, an inherited ``GIT_DIR`` or
``GIT_CONFIG_*`` would redirect git, and on Windows VOOL runs no git at all (safe_git.git_refused_here).
Its ``read_text``/``write_text`` calls had no encoding, so on Windows they read and write cp1252.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

from core import safe_git
from core.registry.codec import RegistryError
from registry_service import github_authority

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    return repo


def test_a_checkout_whose_config_names_a_program_is_refused_before_git_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    repo = _repo(tmp_path)
    ran = tmp_path / "ran"
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {ran}; false"], check=True)
    with pytest.raises(RegistryError, match="program"):
        github_authority.Repository(repo)._git("status")
    assert not ran.exists()


def test_inherited_git_environment_does_not_redirect_the_signer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    repo = _repo(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "planted")
    result = github_authority.Repository(repo)._git("rev-parse", "--absolute-git-dir")
    assert Path(result.stdout.strip()) == (repo / ".git").resolve()


def test_the_signers_own_token_still_reaches_git(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    repo = _repo(tmp_path)
    result = github_authority.Repository(repo, token="t0ken")._git("config", "--get", "http.https://github.com/.extraheader")
    assert result.stdout.strip().startswith("AUTHORIZATION: basic ")


def test_git_is_refused_where_vool_runs_no_git(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    started: list[object] = []
    monkeypatch.setattr(github_authority.subprocess, "run", lambda *a, **k: started.append(a))
    with pytest.raises(RegistryError):
        github_authority.Repository(repo)._git("status")
    assert started == []


def test_text_files_name_their_encoding() -> None:
    tree = ast.parse(Path(github_authority.__file__).read_text(encoding="utf-8"))
    missing = [node.lineno for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in {"read_text", "write_text"}
               and not any(keyword.arg == "encoding" for keyword in node.keywords)]
    assert missing == []
