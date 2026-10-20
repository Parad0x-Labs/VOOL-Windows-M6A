"""On Windows, a git operation is refused when the repository can send git's writes outside the folders git reports.

The Windows write gate allows repo.git only when the top of the repository's work tree, its git
folder and its shared git folder are inside folders marked in VOOL_WINDOWS_DISPOSABLE_WORKSPACES
(tests/test_windows_gate_checks_where_git_writes.py). From inside those folders git could still
write elsewhere: through a link (a symbolic link, or a junction on Windows) in place of a folder in
the git folder (objects, refs/heads, logs, lfs, rr-cache), through a file in the git folder that is
also another file's name (a hard link) and that git rewrites in place (COMMIT_EDITMSG, MERGE_MSG),
through Git LFS, which keeps its objects in `lfs.storage` when the repository's own config sets
it, and through a commit's automatic housekeeping, whose gc writes the objects `gc.repackFilter`
selects into the folder a repository-set `gc.repackFilterTo` names. Reproduced 2026-10-20 on Linux
through VOOL's GitRunner: commits, branches, tags, merges and a conflicting cherry-pick wrote there
with every check passing. Now such a repository is refused
before anyone is asked to approve the call, the refusal says why, and nothing inside or outside
the repository changes. A repository cloned from a local folder, whose objects git hard-links, and
one whose config holds only the Git LFS keys git-lfs writes itself still reach the approval ask,
and the approved commit runs.

Sessions are opened and bound through the production tool door, every repository is real, and
repo.git goes through the real executor with a spy on the permission decision, as in the file
above. The Windows decision is switched on through the gate's `_IS_WINDOWS`, so the file runs on
every platform; folder links are junctions on Windows and symbolic links elsewhere.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from tests.test_windows_gate_checks_where_git_writes import (
    MODES,
    _assert_refused_before_approval,
    _bound_session,
    _commit,
    _files,
    _flag,
    _git,
    _journal_steps,
    _repo,
    _state,
    world,
)

#: Junctions exist only on Windows; symbolic links everywhere (on Windows only where the account may make them).
_DIR_LINKS = ("junction", "symlink") if os.name == "nt" else ("symlink",)


def _dir_link(kind: str, link: Path, target: Path) -> None:
    if kind == "junction":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # Windows without the symbolic-link privilege or Developer Mode
        if os.name != "nt":
            raise
        pytest.skip(f"this Windows account cannot create symbolic links ({exc})")


def _move_out(repo: Path, name: str, outside: Path, kind: str) -> Path:
    """Move `.git/<name>` (created empty when git has not made it yet) to `outside` and leave a link to it."""
    inner = repo / ".git" / name
    moved = outside / name.replace("/", "-")
    if inner.exists():
        shutil.move(str(inner), str(moved))
    else:
        moved.mkdir(parents=True)
    _dir_link(kind, inner, moved)
    return moved


def _marked_repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    marked = tmp_path / "marked"
    repo = _repo(marked / "project")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _flag(monkeypatch, marked)
    return marked, repo, outside


def _refused(tmp_path: Path, monkeypatch, world, mode: str, prepare, *, says: str) -> None:
    """`prepare(repo, outside)` sets up the redirect after the session is bound; the commit is then
    refused before approval with `says` in the reason, and nothing changes inside or outside."""
    marked, repo, outside = _marked_repo(tmp_path, monkeypatch)
    session = _bound_session(repo)
    prepare(repo, outside)
    (repo / "change.txt").write_bytes(b"pending\n")
    before, outside_before = _state(repo), _files(outside)

    execution = _commit(session, marked, mode)

    _assert_refused_before_approval(execution, world)
    assert says in execution.response_text, execution.response_text
    assert _state(repo) == before
    assert _files(outside) == outside_before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", _DIR_LINKS)
@pytest.mark.parametrize("folder", ("objects", "refs/heads", "logs", "lfs", "rr-cache"))
def test_a_link_in_place_of_a_folder_in_the_git_folder_is_refused(
    tmp_path: Path, monkeypatch, world, folder: str, kind: str, mode: str
) -> None:
    _refused(tmp_path, monkeypatch, world, mode, lambda repo, outside: _move_out(repo, folder, outside, kind),
             says=f"the repository's git folder holds a link ({folder})")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", ("COMMIT_EDITMSG", "MERGE_MSG"))
def test_a_file_in_the_git_folder_hard_linked_to_a_file_elsewhere_is_refused(
    tmp_path: Path, monkeypatch, world, name: str, mode: str
) -> None:
    def prepare(repo: Path, outside: Path) -> None:
        (outside / "precious.txt").write_bytes(b"precious\n")
        inner = repo / ".git" / name
        inner.unlink(missing_ok=True)
        os.link(outside / "precious.txt", inner)

    _refused(tmp_path, monkeypatch, world, mode, prepare,
             says=f"a file in the repository's git folder ({name}) is also another file's name")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("key", ("lfs.storage", "lfs.somefuturesetting"))
def test_a_git_lfs_key_in_the_repository_config_is_refused(
    tmp_path: Path, monkeypatch, world, key: str, mode: str
) -> None:
    def prepare(repo: Path, outside: Path) -> None:
        _git(repo, "config", key, str(outside / "lfs"))

    _refused(tmp_path, monkeypatch, world, mode, prepare, says=f"sets keys that can move git's writes elsewhere: {key}")


@pytest.mark.parametrize("mode", MODES)
def test_lfs_storage_in_the_worktree_config_is_refused(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    def prepare(repo: Path, outside: Path) -> None:
        _git(repo, "config", "extensions.worktreeConfig", "true")
        _git(repo, "config", "--worktree", "lfs.storage", str(outside / "lfs"))

    _refused(tmp_path, monkeypatch, world, mode, prepare, says="sets keys that can move git's writes elsewhere: lfs.storage")


def _gc_filter_to(repo: Path, outside: Path) -> Path:
    """Enough packs for a commit's automatic gc to repack everything, filtering blobs into `outside`."""
    for index in range(3):
        (repo / f"file{index}.txt").write_bytes(f"{index}\n".encode() * 50)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"pack {index}")
        _git(repo, "repack", "-q")
    filtered = outside / "filtered"
    filtered.mkdir()
    for key, value in (("gc.auto", "1"), ("gc.autoPackLimit", "1"), ("gc.autoDetach", "false"),
                       ("gc.repackFilter", "blob:none"), ("gc.repackFilterTo", str(filtered / "pack"))):
        _git(repo, "config", key, value)
    return filtered


def test_without_the_gate_a_commit_s_housekeeping_writes_into_gc_repack_filter_to(tmp_path: Path, monkeypatch) -> None:
    """The premise, with no gate in front: VOOL's own commit, through GitRunner, runs git's automatic
    gc, and that gc writes a pack into the outside folder."""
    from core.repoops.gitops import GitRunner

    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    _, repo, outside = _marked_repo(tmp_path, monkeypatch)
    filtered = _gc_filter_to(repo, outside)
    (repo / "change.txt").write_bytes(b"pending\n")

    result = GitRunner(root=repo).commit(message="housekeeping runs after this")

    assert result.ok, result.to_dict()
    assert any(path.suffix == ".pack" for path in filtered.iterdir()), "the commit's gc wrote no pack outside"


@pytest.mark.parametrize("mode", MODES)
def test_gc_repack_filter_to_in_the_repository_config_is_refused(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    _refused(tmp_path, monkeypatch, world, mode, lambda repo, outside: _gc_filter_to(repo, outside),
             says="sets keys that can move git's writes elsewhere: gc.repackfilterto")


def _approved_commit_runs(session: str, marked: Path, repo: Path, mode: str, world) -> None:
    from core.mode_permission_policy import resolve_approval

    (repo / "change.txt").write_bytes(b"pending\n")
    before = _state(repo)
    task, chat = f"repo-git-{uuid.uuid4().hex}", f"repo-git-session-{uuid.uuid4().hex}"
    asked = _commit(session, marked, mode, task_id=task, chat_session=chat)
    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    assert _state(repo) == before
    token = asked.details["approval_request"]["approval_id"]
    assert (resolve_approval(token, decision="allow") or {}).get("status") == "approved"
    ran = _commit(session, marked, mode, task_id=task, chat_session=chat, token=token)
    assert ran.ok and ran.status == "executed", (ran.status, ran.response_text)
    assert _git(repo, "rev-parse", "HEAD~1").strip() == before["head"]
    assert _git(repo, "show", "HEAD:change.txt") == "pending\n"


@pytest.mark.parametrize("mode", MODES)
def test_a_repository_cloned_from_a_local_folder_with_hard_linked_objects_still_commits(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    marked = tmp_path / "marked"
    source = _repo(marked / "source")
    clone = marked / "clone"
    _git(marked, "clone", "-q", str(source), str(clone))
    _git(clone, "config", "user.name", "fx")
    _git(clone, "config", "user.email", "fx@local")
    objects = [path for path in (clone / ".git" / "objects").rglob("*") if path.is_file()]
    assert any(os.stat(path).st_nlink > 1 for path in objects), "git clone of a local folder hard-links its objects"
    _flag(monkeypatch, marked)
    session = _bound_session(clone)

    _approved_commit_runs(session, marked, clone, mode, world)


@pytest.mark.parametrize("mode", MODES)
def test_git_lfs_keys_that_move_no_write_still_commit(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    marked, repo, _ = _marked_repo(tmp_path, monkeypatch)
    _git(repo, "config", "lfs.repositoryformatversion", "0")
    _git(repo, "config", "lfs.https://example.invalid/project.git/info/lfs.access", "basic")
    _git(repo, "config", "lfs.url", "https://example.invalid/project.git/info/lfs")
    session = _bound_session(repo)

    _approved_commit_runs(session, marked, repo, mode, world)
