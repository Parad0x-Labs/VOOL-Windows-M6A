"""The user-rules git check never reaches for a Windows share named in the git command it judges.

The command gate runs sandbox_git_check on every outbound git command (commit, push, tag, ...)
before anyone is asked to approve it. On Windows a lookup of `\\\\host\\share` is an SMB connection
(and an NTLM hash sent to whoever answers), and the check used to make three of them:

- `git -C <share> ...`: the repository root was resolved (project_ids_for_path) on every such
  command, with or without a rule, and `git -C <share> rev-parse` ran while a git ban was in force.
- `-F/--file <share>` and `-t/--template <share>`: the message file was statted and read while a
  git ban was in force.
- a reused message (`commit -C/-c/--fixup/--squash <rev>`): `git log <rev>` looks a value that is
  not a revision up as a file name, and a path inside a revision (`HEAD:<path>`, `:<path>`,
  `:(literal)<path>`) as a path, while a git ban was in force.

The check now refuses each of these on Windows before any lookup or git run. Local paths and real
revisions are checked as before.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core import user_rules as ur
from tests._nonlocal_touch_recorder import recorder, windows_host

GLOBAL_GIT_RULE = "add this to global rules: never push the words xxx or ffff to git"
SHARES = ["\\\\attkr9\\s\\x", "//attkr9/s/x", "\\\\?\\UNC\\attkr9\\s\\x"]
# Plain ids: a test id naming the marker host would reach every process through PYTEST_CURRENT_TEST.
SHARE_IDS = ["unc", "forward-slashes", "long-unc"]
# A share inside a revision: git looks the path part up when the revision does not resolve.
REVISION_PATHS = ["HEAD://attkr9/s/x", ":(literal)//attkr9/s/x", ":0:\\\\attkr9\\s\\x", ":///attkr9/s/x"]
REVISION_PATH_IDS = ["tree-path", "literal-magic", "index-stage", "top-magic"]


@pytest.fixture
def home(tmp_path):
    # A private runtime home and database, as tests/test_user_rules.py sets up.
    from core import runtime_paths
    from storage import db as storage_db

    old_env = os.environ.get("VOOL_HOME")
    old_override = runtime_paths._VOOL_HOME_OVERRIDE
    old_db = storage_db._DEFAULT_DB_PATH_OVERRIDE
    runtime = tmp_path / "home"
    (runtime / "data").mkdir(parents=True)
    os.environ["VOOL_HOME"] = str(runtime)
    runtime_paths.configure_runtime_home(runtime)
    storage_db.configure_default_db_path(runtime / "data" / "vool_web0_v2.db")
    try:
        yield runtime
    finally:
        storage_db._DEFAULT_DB_PATH_OVERRIDE = old_db
        runtime_paths.configure_runtime_home(old_override)
        if old_env is None:
            os.environ.pop("VOOL_HOME", None)
        else:
            os.environ["VOOL_HOME"] = old_env


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "fx@local"), ("config", "user.name", "fx"),
                 ("commit", "-q", "--allow-empty", "-m", "seed")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _ban_git_words() -> None:
    assert ur.handle_rule_command(GLOBAL_GIT_RULE, chat_id="chat-a") is not None


def _assert_share_refused(verdict, recorder) -> None:
    assert verdict.refusal is not None, verdict
    assert "network or device path" in verdict.refusal.reason, verdict.refusal.reason
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


@pytest.mark.parametrize("banned", [False, True], ids=["no-rule", "git-ban"])
@pytest.mark.parametrize("sub", [["commit", "--allow-empty", "-m", "ok"], ["push", "origin", "main"]],
                         ids=["commit", "push"])
@pytest.mark.parametrize("share", SHARES, ids=SHARE_IDS)
def test_a_share_repository_is_refused_before_it_is_looked_up(home, repo, recorder, windows_host, share, sub,
                                                              banned) -> None:
    if banned:
        _ban_git_words()
    windows_host.on()

    verdict = ur.sandbox_git_check(["git", "-C", share, *sub], cwd=str(repo))

    _assert_share_refused(verdict, recorder)


@pytest.mark.parametrize("option", ["-F", "--file", "-t", "--template"])
@pytest.mark.parametrize("share", SHARES, ids=SHARE_IDS)
def test_a_share_message_file_is_refused_before_it_is_read(home, repo, recorder, windows_host, share,
                                                           option) -> None:
    _ban_git_words()
    windows_host.on()

    verdict = ur.sandbox_git_check(["git", "commit", "--allow-empty", option, share], cwd=str(repo))

    _assert_share_refused(verdict, recorder)


@pytest.mark.parametrize("form", ["-C", "-c", "--fixup", "--squash"])
@pytest.mark.parametrize("rev", SHARES + REVISION_PATHS, ids=SHARE_IDS + REVISION_PATH_IDS)
def test_a_reused_message_naming_a_share_is_refused_before_git_log_runs(home, repo, recorder, windows_host,
                                                                        rev, form) -> None:
    _ban_git_words()
    windows_host.on()
    argv = ["git", "commit", "--allow-empty", *([f"{form}={rev}"] if form.startswith("--") else [form, rev])]

    verdict = ur.sandbox_git_check(argv, cwd=str(repo))

    _assert_share_refused(verdict, recorder)


def test_a_local_repository_message_file_and_reused_message_are_still_checked(home, repo, windows_host) -> None:
    _ban_git_words()
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "ship xxx"], check=True,
                   capture_output=True)
    message = repo / "msg.txt"
    message.write_text("ship ffff\n", encoding="utf-8")
    windows_host.on()

    clean = ur.sandbox_git_check(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "fine"], cwd=str(repo))
    from_file = ur.sandbox_git_check(["git", "commit", "--allow-empty", "-F", "msg.txt"], cwd=str(repo))
    reused = ur.sandbox_git_check(["git", "commit", "--allow-empty", "-C", "HEAD"], cwd=str(repo))

    assert clean.refusal is None, clean
    for verdict in (from_file, reused):
        assert verdict.refusal is not None and not verdict.refusal.incomplete, verdict
        assert "network or device path" not in verdict.refusal.reason, verdict.refusal.reason
