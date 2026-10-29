"""The proof harness trusts one git, found by one rule, on every platform.

`tests/_source_guard.py` and `scripts/semantic_phase0_base_differential.py` each looked for git only
at /usr/bin, /bin and /usr/local/bin. On Windows none of those exists, so every journal recovery and
the frozen-corpus load failed closed with "trusted git executable is unavailable". The rule now
lives once in core/safe_git: a system install location, never PATH. On Windows that is Git for
Windows under %ProgramFiles%, and never a drive-root \\usr\\bin\\git, which any user can create.
"""
from __future__ import annotations

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path

from core import safe_git
from tests import _source_guard as guard

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "semantic_phase0_base_differential.py"
_POSIX_GITS = (Path("/usr/bin/git"), Path("/bin/git"), Path("/usr/local/bin/git"))


def test_windows_trusts_the_git_for_windows_install_under_program_files(tmp_path, monkeypatch) -> None:
    if sys.platform == "win32":
        program_files = tmp_path
    else:
        # Off Windows "C:\\Program Files" is one relative file name, so it can be planted under a cwd.
        monkeypatch.chdir(tmp_path)
        program_files = Path("C:\\Program Files")
    git = program_files / "Git" / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"")
    found = safe_git.trusted_git_executable(os_name="nt", environ={"ProgramFiles": str(program_files)})
    assert found == git


def test_windows_never_trusts_a_drive_root_usr_bin_git() -> None:
    # /usr/bin/git exists on this runner; on Windows the same spelling is C:\usr\bin\git, a folder
    # any user may create. With no Program Files install there is no trusted git at all.
    assert safe_git.trusted_git_executable(os_name="nt", environ={}) is None


def test_posix_keeps_its_system_locations() -> None:
    expected = next((path for path in _POSIX_GITS if path.is_file()), None)
    assert safe_git.trusted_git_executable(os_name="posix", environ={"ProgramFiles": "/x"}) == expected


def test_the_trusted_git_env_is_minimal_and_scrubbed() -> None:
    git = Path("C:/Program Files/Git/cmd/git.exe")
    env = safe_git.trusted_git_env(
        git,
        os_name="nt",
        environ={"SystemRoot": "C:\\Windows", "GIT_CONFIG_PARAMETERS": "'core.pager=evil'", "HOME": "h"},
    )
    assert env == {
        "PATH": str(git.parent),
        "LC_ALL": "C",
        "SystemRoot": "C:\\Windows",
        "GIT_TERMINAL_PROMPT": "0",
    }
    posix = safe_git.trusted_git_env(Path("/usr/bin/git"), os_name="posix", environ={"HOME": "h"})
    assert posix == {"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}


def _capture_run(monkeypatch, module, fake_git: Path) -> list[dict]:
    calls: list[dict] = []

    def run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 0, b'{"turns": []}', b"")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(safe_git, "trusted_git_executable", lambda: fake_git)
    return calls


def test_the_source_guard_runs_the_trusted_git_hardened(monkeypatch) -> None:
    fake = Path("/trusted/git")
    calls = _capture_run(monkeypatch, guard, fake)
    guard._trusted_head_bytes("core/semantic/reach.py")
    (call,) = calls
    hardening = safe_git.hardening_args()
    assert call["argv"][: 1 + len(hardening)] == [str(fake), *hardening]
    assert call["env"] == safe_git.trusted_git_env(fake)


def test_the_corpus_loader_runs_the_same_trusted_git_hardened(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("vool_base_differential_trust", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = Path("/trusted/git")
    calls = _capture_run(monkeypatch, module, fake)
    monkeypatch.setattr(module._safe_git, "trusted_git_executable", lambda: fake)
    try:
        module.load_frozen_corpus(_SCRIPT.parents[1])
    except module.CorpusIntegrityError:
        pass  # the empty fake corpus fails the digest pin; only the git call is under test here
    (call,) = calls
    hardening = safe_git.hardening_args()
    assert call["argv"][: 1 + len(hardening)] == [str(fake), *hardening]
    assert call["env"] == safe_git.trusted_git_env(fake)


def test_restore_on_signal_also_answers_ctrl_break(monkeypatch) -> None:
    # Windows has no catchable SIGTERM: Popen.terminate() is TerminateProcess. A supervisor that
    # wants a Windows worker to clean up sends CTRL_BREAK_EVENT, which Python delivers as SIGBREAK.
    monkeypatch.setattr(guard.signal, "SIGBREAK", 21, raising=False)
    registered: list[int] = []
    monkeypatch.setattr(guard.signal, "signal", lambda signum, _handler: registered.append(signum))
    guard.install_restore_on_signal(lambda: None)
    assert registered == [signal.SIGTERM, signal.SIGINT, 21]


def test_windows_refuses_a_program_files_that_is_relative_or_a_network_share(tmp_path, monkeypatch) -> None:
    # %ProgramFiles% is an environment value. A relative one resolves against the working
    # directory and a UNC one names another machine; neither is a system install location.
    planted = tmp_path / "Program Files" / "Git" / "cmd" / "git.exe"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    for program_files in ("Program Files", "\\\\server\\share", "//server/share"):
        found = safe_git.trusted_git_executable(os_name="nt", environ={"ProgramFiles": program_files})
        assert found is None, program_files
