"""On Windows, a push to a repository on this computer lands only inside the marked folders.

`git push` to a folder or `file://` URL runs `git receive-pack` on this machine, which writes the
pushed objects and refs into the destination's git folder. The Windows write gate kept repo.git
inside the folders marked in VOOL_WINDOWS_DISPOSABLE_WORKSPACES, but a push from a marked repository
to a local repository anywhere else wrote there (reproduced 2026-10-20). Owner decision
(2026-10-20, "Marked folders only"): on Windows such a push is refused unless the destination's git
folder, and its shared git folder for a linked worktree, are inside the marked folders, and a link
or hard link inside them (which would send the writes elsewhere) is refused too. A push to an online
server is unchanged, and off Windows nothing changes. `GitRunner.push_destination_refusal` is what
the runtime asks before a push plan exists and again before consent is spent, and `GitRunner.run`
asks it at dispatch, so the destination's refs are read back from disk after each refused push.
The Windows decision is switched on through the gate's `_IS_WINDOWS`, so the file runs on every
platform; folder links are junctions on Windows and symbolic links elsewhere.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.repoops.gitops import GitRunner

_IDENTITY = {"GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@local",
             "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@local"}


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(_IDENTITY, GIT_CONFIG_NOSYSTEM="1")
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8", timeout=60, env=env)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A marked folder holding the pushing repository, and an unmarked folder beside it."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    marked, elsewhere = tmp_path / "marked", tmp_path / "elsewhere"
    source = marked / "project"
    source.mkdir(parents=True)
    elsewhere.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "fx")
    _git(source, "config", "user.email", "fx@local")
    (source / "README.md").write_bytes(b"seed\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "seed")
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    return {"marked": marked, "elsewhere": elsewhere, "source": source}


def _bare(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True, timeout=60)
    return path


def _dir_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _refs(git_dir: Path) -> str:
    return _git(git_dir, "for-each-ref", "--format=%(refname) %(objectname)")


def _push(source: Path, remote: str) -> tuple[int, str]:
    runner = GitRunner(root=source)
    code, _, err = runner.run(*runner.push_argv(remote=remote, ref="main", sha=runner.head()))
    return code, err


def _assert_refused(world: dict[str, Path], remote: str, destination: Path, *, says: str) -> None:
    before = _refs(destination)
    refusal = GitRunner(root=world["source"]).push_destination_refusal(remote)
    assert says in refusal, refusal
    code, err = _push(world["source"], remote)
    assert code == -1 and err.startswith("git was not run:") and says in err, err
    assert _refs(destination) == before, "nothing lands in the destination"


@pytest.mark.parametrize("form", ("path", "file-url", "named-remote", "push-instead-of"))
def test_a_push_to_a_repository_outside_the_marked_folders_is_refused(world, form: str) -> None:
    destination = _bare(world["elsewhere"] / "dest.git")
    remote = {"path": str(destination), "file-url": destination.as_uri()}.get(form, "dest")
    if form == "named-remote":
        _git(world["source"], "remote", "add", "dest", str(destination))
    elif form == "push-instead-of":
        # The remote's own url is inside the marked folder; git rewrites it to the outside one when pushing.
        _git(world["source"], "remote", "add", "dest", str(world["marked"] / "decoy.git"))
        _git(world["source"], "config", f"url.{destination}.pushInsteadOf", str(world["marked"] / "decoy.git"))

    _assert_refused(world, remote, destination, says="which is outside them")


def test_a_push_to_a_marked_linked_worktree_of_an_outside_repository_is_refused(world) -> None:
    main = world["elsewhere"] / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "trunk")
    _git(main, "commit", "-q", "--allow-empty", "-m", "base")
    worktree = world["marked"] / "linked"
    _git(main, "worktree", "add", "-q", "-b", "side", str(worktree))

    _assert_refused(world, str(worktree), main / ".git", says="which is outside them")


@pytest.mark.parametrize("folder", ("objects", "refs"))
def test_a_marked_destination_whose_git_folder_holds_a_link_is_refused(world, folder: str) -> None:
    destination = _bare(world["marked"] / "dest.git")
    moved = world["elsewhere"] / folder
    shutil.move(str(destination / folder), str(moved))
    _dir_link(destination / folder, moved)

    _assert_refused(world, str(destination), destination, says=f"the repository's git folder holds a link ({folder})")


def test_a_push_to_a_repository_inside_the_marked_folders_lands(world) -> None:
    destination = _bare(world["marked"] / "dest.git")

    assert GitRunner(root=world["source"]).push_destination_refusal(str(destination)) == ""
    code, err = _push(world["source"], str(destination))

    assert code == 0, err
    assert _git(destination, "rev-parse", "refs/heads/main").strip() == _git(world["source"], "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("url", ("https://example.invalid/project.git", "git@example.invalid:project.git"))
def test_a_push_to_an_online_server_is_not_placed(world, url: str) -> None:
    assert GitRunner(root=world["source"]).push_destination_refusal(url) == ""


def test_off_windows_a_push_to_a_repository_anywhere_still_lands(world, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    destination = _bare(world["elsewhere"] / "dest.git")

    code, err = _push(world["source"], str(destination))

    assert code == 0, err
    assert _git(destination, "rev-parse", "refs/heads/main").strip() == _git(world["source"], "rev-parse", "HEAD").strip()


def test_a_marked_destination_whose_gc_writes_elsewhere_is_refused(world) -> None:
    """receive-pack runs the destination's own auto gc, which writes the objects `gc.repackFilter`
    selects into the destination's `gc.repackFilterTo`, wherever it is."""
    destination = _bare(world["marked"] / "dest.git")
    filtered = world["elsewhere"] / "filtered"
    filtered.mkdir()
    for key, value in (("gc.auto", "1"), ("gc.autoPackLimit", "1"), ("gc.autoDetach", "false"),
                       ("gc.repackFilter", "blob:none"), ("gc.repackFilterTo", str(filtered / "pack"))):
        _git(destination, "config", key, value)

    _assert_refused(world, str(destination), destination,
                    says="the push destination's own git config sets keys that can move git's writes elsewhere: "
                         "gc.repackfilterto")
    assert list(filtered.iterdir()) == []


def test_a_push_from_a_repository_whose_git_folder_holds_a_link_is_refused(world) -> None:
    """Every push writes the remote's branch into the pushing repository's own refs."""
    destination = _bare(world["marked"] / "dest.git")
    _git(world["source"], "remote", "add", "dest", str(destination))
    remotes = world["elsewhere"] / "remotes"
    remotes.mkdir()
    _dir_link(world["source"] / ".git" / "refs" / "remotes", remotes)

    _assert_refused(world, "dest", destination, says="the repository's git folder holds a link (refs/remotes)")
    assert list(remotes.iterdir()) == []


@pytest.mark.parametrize("key", ("http.cookieFile", "http.https://example.invalid/.cookieFile"))
def test_a_push_from_a_repository_that_saves_cookies_to_a_file_of_its_choosing_is_refused(world, key: str) -> None:
    _git(world["source"], "config", key, str(world["elsewhere"] / "cookies.txt"))
    _git(world["source"], "config", "http.saveCookies", "true")

    refusal = GitRunner(root=world["source"]).push_destination_refusal("https://example.invalid/project.git")

    assert "sets keys that can move git's writes elsewhere: http." in refusal and refusal.endswith("cookiefile"), refusal
