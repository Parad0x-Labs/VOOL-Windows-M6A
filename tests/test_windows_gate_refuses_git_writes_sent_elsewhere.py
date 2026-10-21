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
import subprocess
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
@pytest.mark.parametrize("name", ("objects/pack/pack-" + "ab" * 20 + ".promisor", "objects/loose-object-idx"))
def test_an_object_store_file_git_writes_in_place_hard_linked_elsewhere_is_refused(
    tmp_path: Path, monkeypatch, world, name: str, mode: str
) -> None:
    """A partial-clone fetch rewrites a pack's `.promisor` file in place and git appends to
    `objects/loose-object-idx`, so unlike the rest of the object store these may not be hard links."""
    def prepare(repo: Path, outside: Path) -> None:
        (outside / "precious.txt").write_bytes(b"precious\n")
        os.link(outside / "precious.txt", repo / ".git" / name)

    _refused(tmp_path, monkeypatch, world, mode, prepare,
             says=f"a file in the repository's git folder ({name}) is also another file's name")


@pytest.mark.parametrize("mode", MODES)
def test_a_hard_linked_reflog_named_like_an_object_store_file_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """git appends to a reflog in place, and a branch may be named like an object
    (`objects/ab/<38 hex>`). With the shared git folder at `.git/modules/x` (through `commondir`),
    that branch's reflog sits at `modules/x/logs/refs/heads/objects/ab/...`, which must not pass for
    a submodule's object store."""
    branch = "objects/ab/" + "c" * 38

    def prepare(repo: Path, outside: Path) -> None:
        git_dir = repo / ".git"
        shared = git_dir / "modules" / "x"
        (shared / "logs").mkdir(parents=True)
        for name in ("objects", "refs", "config"):
            shutil.move(str(git_dir / name), str(shared / name))
        shutil.move(str(git_dir / "logs" / "refs"), str(shared / "logs" / "refs"))
        (git_dir / "commondir").write_text("modules/x\n", encoding="utf-8")
        _git(repo, "checkout", "-q", "-b", branch)
        reflog = shared / "logs" / "refs" / "heads" / branch
        shutil.move(str(reflog), str(outside / "reflog"))
        os.link(outside / "reflog", reflog)

    _refused(tmp_path, monkeypatch, world, mode, prepare,
             says=f"a file in the repository's git folder (modules/x/logs/refs/heads/{branch}) is also another file's name")


@pytest.mark.parametrize("folders", ("LOGS/REFS", "Logs/Refs", "log\u017f/ref\u017f"))
def test_a_hard_linked_reflog_in_folders_named_in_another_case_is_refused(tmp_path: Path, folders: str) -> None:
    """Windows folder names ignore case, by Unicode upper case (`ſ` is `S`), so git's append to
    `logs/refs/heads/...` lands in a `LOGS/REFS` or `logſ/refſ` folder the repository ships; the walk
    sees the stored name."""
    from core import git_write_redirects

    git_dir = tmp_path / "repo.git"
    reflog = git_dir / "modules" / "x" / folders / "heads" / "objects" / "ab" / ("c" * 38)
    reflog.parent.mkdir(parents=True)
    (tmp_path / "outside").write_bytes(b"precious\n")
    os.link(tmp_path / "outside", reflog)

    found = git_write_redirects.link_refusal(str(git_dir))

    assert "is also another file's name (a hard link)" in found, found


def test_git_folders_spelled_alike_after_windows_case_folding_are_each_walked(tmp_path: Path, monkeypatch) -> None:
    """Windows `normcase` folds the Kelvin sign `K` to `k`, but NTFS keeps `Ka` and `ka` apart, so a
    git folder and a shared git folder spelled so must both be walked. Windows folding is simulated
    with `str.lower`."""
    from core import git_write_redirects

    monkeypatch.setattr(git_write_redirects.os.path, "normcase", str.lower)
    git_dir, common_dir = tmp_path / "\u212aa", tmp_path / "ka"
    git_dir.mkdir()
    common_dir.mkdir()
    (tmp_path / "outside").write_bytes(b"precious\n")
    os.link(tmp_path / "outside", git_dir / "COMMIT_EDITMSG")

    found = git_write_redirects.link_refusal(str(common_dir), str(git_dir))

    assert "(COMMIT_EDITMSG) is also another file's name" in found, found


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
    """The premise, with no gate in front: a commit runs git's automatic gc, and that gc writes a pack
    into the outside folder. The commit is plain git with hooks off, since VOOL's own git runs no
    automatic housekeeping once PR #37 lands; the refusal below still guards every other git."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    _, repo, outside = _marked_repo(tmp_path, monkeypatch)
    filtered = _gc_filter_to(repo, outside)
    (repo / "change.txt").write_bytes(b"pending\n")

    _git(repo, "-c", f"core.hooksPath={os.devnull}", "add", "-A")  # _git asserts git succeeded
    _git(repo, "-c", f"core.hooksPath={os.devnull}", "commit", "-q", "-m", "housekeeping runs after this")

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
def test_a_local_clone_of_a_housekept_repository_still_commits(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    """After `git gc` a repository's object store also holds `objects/info/commit-graph` and
    `objects/info/packs`, which a local clone hard-links too; git replaces them by renaming a new
    file into place, never by writing in them."""
    marked = tmp_path / "marked"
    source = _repo(marked / "source")
    _git(source, "gc", "-q")
    clone = marked / "clone"
    _git(marked, "clone", "-q", str(source), str(clone))
    _git(clone, "config", "user.name", "fx")
    _git(clone, "config", "user.email", "fx@local")
    for name in ("commit-graph", "packs"):
        assert os.stat(clone / ".git" / "objects" / "info" / name).st_nlink > 1, f"objects/info/{name} is hard-linked"
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


def _source_repo(folder: Path) -> Path:
    """A repository with one LFS-free commit, to be added as a submodule."""
    folder.mkdir(parents=True)
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "config", "user.name", "fx")
    _git(folder, "config", "user.email", "fx@local")
    (folder / "lib.txt").write_bytes(b"lib\n")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "lib")
    return folder


def _add_submodule(repo: Path, source: Path, name: str = "lib") -> Path:
    """`git submodule add` from a local folder (git clones it into `.git/modules/<name>`, hard-linking its objects)."""
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return repo / ".git" / "modules" / name


@pytest.mark.parametrize("mode", MODES)
def test_a_git_lfs_key_in_a_submodule_s_config_is_refused(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    """git and git-lfs run inside a checked-out submodule under the submodule's own config."""
    def prepare(repo: Path, outside: Path) -> None:
        modules = _add_submodule(repo, _source_repo(tmp_path / "sources" / "lib"))
        _git(modules, "config", "lfs.storage", str(outside / "lfs"))

    _refused(tmp_path, monkeypatch, world, mode, prepare,
             says="submodule lib's own git config sets keys that can move git's writes elsewhere: lfs.storage")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", _DIR_LINKS)
def test_a_link_in_a_submodule_s_own_git_folder_is_refused(
    tmp_path: Path, monkeypatch, world, kind: str, mode: str
) -> None:
    """A submodule added from a repository already in the work tree keeps its git folder inside it
    (`lib/.git` is a folder), outside the superproject's git folder."""
    def prepare(repo: Path, outside: Path) -> None:
        embedded = _source_repo(repo / "lib")
        _git(repo, "submodule", "add", "-q", "./lib", "lib")
        _git(repo, "commit", "-q", "-m", "add lib")
        assert (embedded / ".git").is_dir()
        (outside / "lfs").mkdir()
        _dir_link(kind, embedded / ".git" / "lfs", outside / "lfs")

    _refused(tmp_path, monkeypatch, world, mode, prepare, says="the repository's git folder holds a link (lfs)")


@pytest.mark.parametrize("mode", MODES)
def test_a_submodule_whose_git_folder_is_outside_the_marked_folders_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    def prepare(repo: Path, outside: Path) -> None:
        modules = _add_submodule(repo, _source_repo(tmp_path / "sources" / "lib"))
        moved = outside / "lib-git"
        shutil.move(str(modules), str(moved))
        _git(repo, "config", "--file", str(moved / "config"), "core.worktree", str(repo / "lib"))
        gitfile = repo / "lib" / ".git"
        gitfile.unlink()  # Git for Windows hides it, and Windows refuses to overwrite a hidden file
        gitfile.write_text(f"gitdir: {moved.as_posix()}\n", encoding="utf-8")

    _refused(tmp_path, monkeypatch, world, mode, prepare, says="This call also changes submodule lib's git folder")


@pytest.mark.parametrize("mode", MODES)
def test_a_submodule_added_from_a_local_folder_with_hard_linked_objects_still_commits(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    marked, repo, _ = _marked_repo(tmp_path, monkeypatch)
    modules = _add_submodule(repo, _source_repo(tmp_path / "sources" / "lib"))
    objects = [path for path in (modules / "objects").rglob("*") if path.is_file()]
    assert any(os.stat(path).st_nlink > 1 for path in objects), "git submodule add hard-links a local folder's objects"
    session = _bound_session(repo)

    _approved_commit_runs(session, marked, repo, mode, world)


@pytest.mark.parametrize("mode", MODES)
def test_a_submodule_added_from_a_local_folder_in_a_linked_worktree_still_commits(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """A linked worktree keeps its submodules' git folders in its own git folder
    (`.git/worktrees/<name>/modules/<submodule>`), with the objects a local clone hard-links."""
    marked, repo, _ = _marked_repo(tmp_path, monkeypatch)
    worktree = marked / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    _add_submodule(worktree, _source_repo(tmp_path / "sources" / "lib"))
    modules = repo / ".git" / "worktrees" / "linked" / "modules" / "lib"
    assert any(os.stat(path).st_nlink > 1 for path in (modules / "objects").rglob("*") if path.is_file())
    session = _bound_session(worktree)

    _approved_commit_runs(session, marked, worktree, mode, world)


@pytest.mark.parametrize("mode", MODES)
def test_a_git_lfs_object_hard_linked_elsewhere_still_commits(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    """`git lfs pull` from a local folder hard-links `.git/lfs/objects/<aa>/<bb>/<oid>`; git-lfs never rewrites it."""
    marked, repo, outside = _marked_repo(tmp_path, monkeypatch)
    oid = "ab" * 32
    store = repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4]
    store.mkdir(parents=True)
    (outside / "object").write_bytes(b"lfs object\n")
    os.link(outside / "object", store / oid)
    session = _bound_session(repo)

    _approved_commit_runs(session, marked, repo, mode, world)


@pytest.mark.parametrize("mode", MODES)
def test_the_user_s_own_git_lfs_settings_still_commit(tmp_path: Path, monkeypatch, world, mode: str) -> None:
    """System and global config belong to the user (as in core/safe_git.py), so a global `lfs.storage` is theirs."""
    marked, repo, _ = _marked_repo(tmp_path, monkeypatch)
    home = Path(os.environ["HOME"])
    (home / ".gitconfig").write_text(f"[lfs]\n\tstorage = {(home / 'lfs').as_posix()}\n", encoding="utf-8")
    session = _bound_session(repo)

    _approved_commit_runs(session, marked, repo, mode, world)


@pytest.mark.parametrize("setting", ("safe.bareRepository=explicit", "none"))
def test_a_bare_repository_s_own_keys_are_read_whatever_the_user_s_safe_settings(
    tmp_path: Path, monkeypatch, setting: str
) -> None:
    """A push destination is often a bare repository. With the user's `safe.bareRepository=explicit`,
    `git -C <bare repository>` opens no repository and lists no repository keys; the check names the
    git folder itself, and a config with no repository scope at all is not read as a clean one."""
    from core import git_write_redirects

    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    bare = tmp_path / "dest.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
    _git(bare, "config", "gc.repackFilterTo", (tmp_path / "elsewhere").as_posix())
    if setting != "none":
        section, rest = setting.split(".", 1)
        name, value = rest.split("=")
        (home / ".gitconfig").write_text(f"[{section}]\n\t{name} = {value}\n", encoding="utf-8")

    found = git_write_redirects.refusal_for([("the push destination", bare, str(bare), str(bare))])

    assert found.endswith("sets keys that can move git's writes elsewhere: gc.repackfilterto"), found


def test_an_entry_another_git_process_removes_during_the_walk_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """A lock or temporary pack another git process removes between the listing and the look is not a
    refusal; a link put in its place would still be seen."""
    from core import git_write_redirects

    git_dir = tmp_path / "repo.git"
    (git_dir / "objects" / "pack").mkdir(parents=True)
    (git_dir / "index.lock").write_bytes(b"")
    (git_dir / "objects" / "pack" / "tmp_pack_x").write_bytes(b"")
    real_scandir = os.scandir

    def vanishing(folder):
        entries = list(real_scandir(folder))
        for entry in entries:
            if entry.name in {"index.lock", "tmp_pack_x"}:
                os.unlink(entry.path)
        return iter(entries)

    monkeypatch.setattr(git_write_redirects.os, "scandir", vanishing)
    assert git_write_redirects.link_refusal(str(git_dir)) == ""
