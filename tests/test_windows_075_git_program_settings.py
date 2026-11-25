"""On Windows, file tools never change a script that a repository's git settings run, though git never runs there.

Mac 0.7.5 learns which files a repository's settings run (core.fsmonitor, a filter, core.gitProxy, ...) from
`git config --list`. On Windows core.safe_git refuses git, so core.git_metadata_protection reads the repository's
own config itself (core.git_metadata_protection._local_targets). These rows pin that reading: a plain repository,
a linked worktree (its config lives in the repository named by `commondir`), a worktree's own `config.worktree`,
a pointer whose config is missing (refused, never read as "no settings"), a script in another marked folder
that a repository in a marked folder would run, a config saved with a byte-order mark, a value an inline comment
ends, a command word Windows runs with a PATHEXT suffix, and the user's global hooks folder. Repositories are built by hand, so no git binary is needed; the
Windows side is switched through safe_git._REFUSE_GIT and the marked folders through the write gate's variable.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import git_metadata_protection, safe_git, windows_write_gate
from core.git_metadata_protection import REFUSAL_PREFIX, refusal


@pytest.fixture(autouse=True)
def _windows(tmp_path, monkeypatch):
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    home = tmp_path / "user"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv(windows_write_gate.ENV_VAR, raising=False)
    git_metadata_protection._TARGETS_CACHE.clear()
    yield
    git_metadata_protection._TARGETS_CACHE.clear()


def _repo(root: Path, config: str) -> Path:
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "config").write_text("[core]\n\tbare = false\n" + config, encoding="utf-8")
    (root / "tools").mkdir()
    return root


def _worktree(main: Path, tree: Path, *, own_config: str | None = None) -> Path:
    gitdir = main / ".git" / "worktrees" / tree.name
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/side\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(str(tree / ".git") + "\n", encoding="utf-8")
    if own_config is not None:
        (gitdir / "config.worktree").write_text(own_config, encoding="utf-8")
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (tree / "tools").mkdir()
    return tree


def test_a_script_core_fsmonitor_names_is_refused_and_other_files_stay_writable(tmp_path) -> None:
    repo = _repo(tmp_path / "repo", "\tfsmonitor = tools/watch.sh\n")
    assert refusal(repo / "tools" / "watch.sh").startswith(REFUSAL_PREFIX)
    assert refusal(repo / "tools" / "other.sh") == ""


@pytest.mark.parametrize(("section", "line"), [
    ('[filter "x"]', "\tclean = sh tools/run.sh"),
    ("[core]", '\tgitProxy = "sh tools/run.sh"'),
    ("[interactive]", "\tdiffFilter = !tools/run.sh --color"),
    ('[trailer "sign"]', "\tcommand = sh tools/run.sh"),
])
def test_each_program_setting_protects_the_script_it_names(tmp_path, section, line) -> None:
    repo = _repo(tmp_path / "repo", f"{section}\n{line}\n")
    assert refusal(repo / "tools" / "run.sh").startswith(REFUSAL_PREFIX)
    assert refusal(repo / "tools" / "notes.md") == ""


def test_a_linked_worktree_reads_the_settings_of_the_repository_it_belongs_to(tmp_path) -> None:
    main = _repo(tmp_path / "main", "\tfsmonitor = tools/watch.sh\n")
    tree = _worktree(main, tmp_path / "side")
    assert refusal(tree / "tools" / "watch.sh").startswith(REFUSAL_PREFIX)
    assert refusal(tree / "tools" / "other.sh") == ""


def test_a_worktrees_own_config_worktree_is_read_and_protected(tmp_path) -> None:
    main = _repo(tmp_path / "main", "[extensions]\n\tworktreeConfig = true\n")
    tree = _worktree(main, tmp_path / "side", own_config="[core]\n\tfsmonitor = tools/own.sh\n")
    assert refusal(tree / "tools" / "own.sh").startswith(REFUSAL_PREFIX)
    assert refusal(tree / "tools" / "other.sh") == ""


def test_a_pointer_whose_config_is_missing_refuses_every_write_in_that_tree(tmp_path) -> None:
    main = _repo(tmp_path / "main", "")
    tree = _worktree(main, tmp_path / "side")
    (main / ".git" / "config").unlink()
    assert refusal(tree / "tools" / "other.sh").startswith(REFUSAL_PREFIX)


def test_a_continued_value_refuses_every_write_in_that_repository(tmp_path) -> None:
    repo = _repo(tmp_path / "repo", "\tfsmonitor = tools/watch.sh \\\n\t\t--quiet\n")
    assert refusal(repo / "tools" / "other.sh").startswith(REFUSAL_PREFIX)


def test_a_script_in_another_marked_folder_that_a_marked_repository_runs_is_refused(tmp_path, monkeypatch) -> None:
    other = tmp_path / "scripts"
    other.mkdir()
    repo = _repo(tmp_path / "repo", f"\tfsmonitor = {(other / 'watch.sh').as_posix()}\n")
    assert refusal(other / "watch.sh") == ""  # not marked: the gate refuses any write there anyway
    monkeypatch.setenv(windows_write_gate.ENV_VAR, f"{repo}{os.pathsep}{other}")
    git_metadata_protection._TARGETS_CACHE.clear()
    assert refusal(other / "watch.sh").startswith(REFUSAL_PREFIX)
    assert refusal(other / "notes.md") == ""


def test_a_config_saved_with_a_byte_order_mark_is_read_and_ordinary_files_stay_writable(tmp_path) -> None:
    repo = _repo(tmp_path / "repo", "\tfsmonitor = tools/watch.sh\n")
    config = repo / ".git" / "config"
    config.write_bytes(b"\xef\xbb\xbf" + config.read_bytes())
    assert refusal(repo / "tools" / "watch.sh").startswith(REFUSAL_PREFIX)
    assert refusal(repo / "tools" / "other.sh") == ""


@pytest.mark.parametrize("value", ["tools/mon.exe;note", "tools/mon.exe # note", '"tools/mon.exe" ; x'])
def test_an_inline_comment_ends_the_value_as_git_reads_it(tmp_path, value) -> None:
    repo = _repo(tmp_path / "repo", f"\tfsmonitor = {value}\n")
    assert refusal(repo / "tools" / "mon.exe").startswith(REFUSAL_PREFIX)


def test_a_quoted_semicolon_stays_part_of_the_value(tmp_path) -> None:
    repo = _repo(tmp_path / "repo", '\tfsmonitor = "tools/a;b.sh"\n')
    assert refusal(repo / "tools" / "a;b.sh").startswith(REFUSAL_PREFIX)


@pytest.mark.parametrize("suffix", ["", ".exe", ".cmd", ".bat", ".com"])
def test_a_command_word_without_a_suffix_protects_what_windows_would_run(tmp_path, suffix) -> None:
    repo = _repo(tmp_path / "repo", '[filter "x"]\n\tclean = ./tools/clean\n')
    assert refusal(repo / "tools" / ("clean" + suffix)).startswith(REFUSAL_PREFIX)
    assert refusal(repo / "tools" / "clean.txt") == ""


def test_a_linked_worktree_protects_a_program_the_repository_config_names_with_its_suffix(tmp_path) -> None:
    main = _repo(tmp_path / "main", "\tfsmonitor = tools/wt\n")
    tree = _worktree(main, tmp_path / "w")
    assert refusal(tree / "tools" / "wt.exe").startswith(REFUSAL_PREFIX)


@pytest.mark.parametrize("where", [".gitconfig", ".config/git/config"])
def test_a_global_hooks_folder_is_protected_in_every_repository(tmp_path, where) -> None:
    config = tmp_path / "user" / where
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[core]\n\thooksPath = .githooks\n", encoding="utf-8")
    repo = _repo(tmp_path / "repo", "")
    assert refusal(repo / ".githooks" / "pre-commit").startswith(REFUSAL_PREFIX)
    assert refusal(repo / "tools" / "other.sh") == ""


def test_off_windows_without_git_the_mac_rule_is_unchanged(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    monkeypatch.setattr(git_metadata_protection.shutil, "which", lambda _name: None)
    repo = _repo(tmp_path / "repo", "\tfsmonitor = tools/watch.sh\n")
    assert refusal(repo / "tools" / "watch.sh") == ""
