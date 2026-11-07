"""Off Windows, `git apply` and GNU patch write a patched file with the LF endings VOOL checked.

VOOL patches a file only when neither the file nor the patch holds a carriage return, so that every byte
it does not change comes back as it was. git's own line-ending conversion would undo that: with
`core.autocrlf=true` (Git for Windows' default, and settable in any repository or user config), or with
`core.eol=crlf` on a file the repository marks `text`, `git apply` writes every line of the file it
changes with CRLF, the lines the patch never touched included. A native Windows build of GNU patch
reads and writes in text mode and does the same unless it is given `--binary`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import core.runtime_state_protection as runtime_state_protection
import core.windows_write_gate as gate
from core.execution import workspace_tools
from core.execution.workspace_tools import apply_unified_diff_workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The tool strips the patch's final newline; the marker keeps the last hunk line whole.
PATCH = "--- a/q.txt\n+++ b/q.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n\\ No newline at end of file\n"


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "data").mkdir()
    monkeypatch.setattr(runtime_state_protection, "protected_state_roots", lambda: (root / "data",))
    (root / "q.txt").write_bytes(b"a\nb\nc\n")
    return root


def _git_config(root: Path, key: str, value: str) -> None:
    subprocess.run(["git", "-C", str(root), "config", key, value], check=True)


def _apply(root: Path) -> dict:
    return apply_unified_diff_workspace({"patch": PATCH}, workspace_root=root, session_id="s")


def test_core_autocrlf_does_not_rewrite_the_file_with_crlf(workspace: Path) -> None:
    _git_config(workspace, "core.autocrlf", "true")
    result = _apply(workspace)
    assert result["ok"] is True and result["details"]["engine"] == "git_apply", (result["status"], result["response_text"])
    assert (workspace / "q.txt").read_bytes() == b"a\nB\nc\n"


def test_core_eol_crlf_does_not_rewrite_a_text_file_with_crlf(workspace: Path) -> None:
    (workspace / ".gitattributes").write_bytes(b"* text\n")
    _git_config(workspace, "core.eol", "crlf")
    result = _apply(workspace)
    assert result["ok"] is True and result["details"]["engine"] == "git_apply", (result["status"], result["response_text"])
    assert (workspace / "q.txt").read_bytes() == b"a\nB\nc\n"
    assert (workspace / ".gitattributes").read_bytes() == b"* text\n"


def test_without_conversion_config_the_file_still_keeps_lf(workspace: Path) -> None:
    result = _apply(workspace)
    assert result["ok"] is True and result["details"]["engine"] == "git_apply", (result["status"], result["response_text"])
    assert (workspace / "q.txt").read_bytes() == b"a\nB\nc\n"


def test_gnu_patch_is_told_to_read_and_write_bytes(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Sabotage target: drop `--binary` from the GNU patch call. On POSIX its effect cannot be seen (reads
    # and writes never convert there), so the call itself is checked; the Windows runner checks the effect.
    if shutil.which("patch") is None:
        pytest.skip("needs GNU patch")
    real_which, real_run = shutil.which, subprocess.run
    monkeypatch.setattr(
        workspace_tools.shutil, "which", lambda cmd, *a, **k: None if cmd == "git" else real_which(cmd, *a, **k)
    )
    calls: list[list[str]] = []

    def recording_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(workspace_tools.subprocess, "run", recording_run)
    # One line off, so VOOL's own engine refuses it and GNU patch places it at an offset.
    patch = "--- a/q.txt\n+++ b/q.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n\\ No newline at end of file\n"
    result = apply_unified_diff_workspace({"patch": patch}, workspace_root=workspace, session_id="s")
    assert result["ok"] is True and result["details"]["engine"] == "patch", (result["status"], result["response_text"])
    patch_calls = [cmd for cmd in calls if Path(cmd[0]).name.startswith("patch")]
    assert patch_calls and all("--binary" in cmd for cmd in patch_calls), calls
    assert (workspace / "q.txt").read_bytes() == b"a\nB\nc\n"
