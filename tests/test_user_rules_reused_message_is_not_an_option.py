"""A message reused with ``git commit -C <rev>`` is read as a revision, never as a git option.

Found in review 20 Oct 2026: the rules scan passed the value of -C/-c/--fixup/--squash straight to
``git log``, so ``git commit -C --output=<file>`` made the scan itself overwrite that file (and
``-C --format=%G?`` would have checked a planted signature with the user's gpg) before anyone was
asked to approve the command.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core import user_rules as ur

GLOBAL_GIT_RULE = "add this to global rules: never push the words xxx or ffff to git"


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


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "fx@local"), ("config", "user.name", "fx"),
                 ("commit", "-q", "--allow-empty", "-m", "seed")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


@pytest.mark.parametrize("form", ["-C", "-c", "--reuse-message", "--fixup", "--squash"])
def test_a_reused_message_that_reads_as_an_option_is_refused_and_writes_nothing(home, tmp_path: Path,
                                                                               form: str) -> None:
    repo = _repo(tmp_path)
    handled = ur.handle_rule_command(GLOBAL_GIT_RULE, chat_id="chat-a")
    assert handled is not None
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    option = f"--output={victim}"
    argv = ["git", "commit", "--allow-empty", *([f"{form}={option}"] if form.startswith("--") else [form, option])]

    verdict = ur.sandbox_git_check(argv, cwd=str(repo))

    assert verdict.refusal is not None and verdict.refusal.incomplete, argv
    assert victim.read_text(encoding="utf-8") == "keep\n", "the rules scan ran git log with the value as an option"


def test_a_reused_message_from_a_real_commit_is_still_read(home, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ur.handle_rule_command(GLOBAL_GIT_RULE, chat_id="chat-a")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "ship xxx"], check=True,
                   capture_output=True)

    verdict = ur.sandbox_git_check(["git", "commit", "--allow-empty", "-C", "HEAD"], cwd=str(repo))

    assert verdict.refusal is not None and not verdict.refusal.incomplete
