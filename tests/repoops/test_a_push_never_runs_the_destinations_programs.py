"""A push to a local repository must not run that repository's programs.

`git push` to a folder or `file://` URL runs `git receive-pack` on this machine inside the
destination, and that git drops the pushing side's `-c` hardening (core/safe_git.py): it ran the
destination's pre-receive hook, a `core.hooksPath` hook, a `core.alternateRefsCommand`, and with
`receive.denyCurrentBranch=updateInstead` rewrote the destination's checked-out files (reproduced
20 Oct 2026). The runtime now refuses such a push before consent is asked for or spent, and the
runner refuses it again at dispatch. A local bare remote with no hooks still pushes: every other
repoops test pushes to one.

Each hook or program writes a marker file; the marker, the destination's refs and its files are
read back from disk, independent of what the runtime reports.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.repoops.gitops import GitRunner, _push_transport
from tests.repoops._harness import build_repo, context, door, git, head, remote_ref
from tests.repoops.test_push_dispatch_authority_r5 import REF, _disk, _drive
from tests.repoops.test_repoops_unknown_effect import world


def _hook(git_dir: Path, name: str, marker: Path) -> None:
    hook = git_dir / "hooks" / name
    hook.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)


def _authorization(sid: str) -> dict:
    return dict(_disk(sid).get("authorization") or {})


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
def test_a_hook_in_the_local_remote_refuses_the_plan_and_never_runs(world, tmp_path) -> None:
    root, bare, forge = world
    marker = tmp_path / "PRE_RECEIVE_RAN"
    _hook(bare, "pre-receive", marker)
    ctx = context(root)

    with pytest.raises(AssertionError, match=r"has git hooks a push would run"):
        _drive(root, forge, ctx, notes="a reviewed change\n")

    assert not marker.exists()
    assert remote_ref(bare, REF) == ""


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
def test_a_hook_added_after_consent_refuses_the_push_and_keeps_the_consent(world, tmp_path) -> None:
    root, bare, forge = world
    ctx = context(root)
    sid, _ = _drive(root, forge, ctx, notes="a reviewed change\n")
    marker = tmp_path / "POST_RECEIVE_RAN"
    _hook(bare, "post-receive", marker)

    refused = door("repo.push", {"repo_session_id": sid, "simulate": False}, ctx)

    assert refused.ok is False and refused.status == "push_destination_refused", refused.response_text
    assert "post-receive" in refused.response_text
    assert not marker.exists()
    assert remote_ref(bare, REF) == ""
    assert not _authorization(sid).get("consumed")


def _runner_world(tmp_path: Path) -> tuple[GitRunner, Path, Path]:
    root, bare = build_repo(tmp_path)
    return GitRunner(root=root), root, bare


def _push(runner: GitRunner, remote: str = "origin") -> tuple[int, str, str]:
    return runner.run(*runner.push_argv(remote=remote, ref="pushed", sha=runner.head()))


@pytest.mark.skipif(os.name == "nt", reason="the program is a POSIX shell command")
def test_a_program_named_by_the_destinations_config_refuses_the_push(tmp_path) -> None:
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "ALTERNATE_REFS_RAN"
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True, timeout=60)
    (bare / "objects" / "info" / "alternates").write_text(f"{other / 'objects'}\n", encoding="utf-8")
    git(bare, "config", "core.alternateRefsCommand", f"sh -c 'echo ran > {marker}; true'")

    code, _, err = _push(runner)

    assert code != 0 and "core.alternaterefscommand" in err
    assert not marker.exists()
    assert remote_ref(bare, "pushed") == ""


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
def test_a_hooks_path_in_the_destination_refuses_the_push(tmp_path) -> None:
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "HOOKS_PATH_RAN"
    elsewhere = tmp_path / "elsewhere-hooks"
    elsewhere.mkdir()
    hook = elsewhere / "post-receive"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    git(bare, "config", "core.hooksPath", str(elsewhere))

    code, _, err = _push(runner)

    assert code != 0 and "core.hookspath" in err
    assert not marker.exists()
    assert remote_ref(bare, "pushed") == ""


def test_update_instead_on_a_checked_out_destination_refuses_the_push(tmp_path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    git(dest, "init", "-q", "-b", "main")
    (dest / "a.txt").write_text("theirs\n", encoding="utf-8")
    git(dest, "add", ".")
    git(dest, "commit", "-q", "-m", "seed")
    git(dest, "config", "receive.denyCurrentBranch", "updateInstead")
    root = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(dest), str(root))
    (root / "a.txt").write_text("pushed over their checkout\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "change")
    runner = GitRunner(root=root)

    code, _, err = runner.run(*runner.push_argv(remote="origin", ref="main", sha=runner.head()))

    assert code != 0 and "updateInstead" in err
    assert (dest / "a.txt").read_text(encoding="utf-8") == "theirs\n"


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
@pytest.mark.parametrize("form", ["file_url", "file_url_with_host", "bare_path", "dot_git_suffix", "push_instead_of"])
def test_every_way_of_naming_a_local_remote_is_checked(tmp_path, form) -> None:
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "PRE_RECEIVE_RAN"
    _hook(bare, "pre-receive", marker)
    remote = "origin"
    if form == "file_url":
        git(root, "remote", "set-url", "origin", f"file://{bare}")
    elif form == "file_url_with_host":
        # git ignores a file:// URL's host on POSIX; VOOL does not guess and refuses the form.
        git(root, "remote", "set-url", "origin", f"file://somehost{bare}")
    elif form == "bare_path":
        remote = str(bare)
    elif form == "dot_git_suffix":
        remote = str(bare)[: -len(".git")]  # receive-pack tries <path>.git too
    else:
        git(root, "remote", "set-url", "origin", "https://example.invalid/remote.git")
        git(root, "config", f"url.{tmp_path}/.pushInsteadOf", "https://example.invalid/")

    code, _, err = _push(runner, remote)

    assert code != 0 and err.startswith("git was not run:"), err
    assert not marker.exists()
    assert remote_ref(bare, "pushed") == ""


@pytest.mark.parametrize("url", ["ext::sh -c true", "fake::anything", "ftp://example.invalid/r.git", "svn://example.invalid/r"])
def test_a_transport_whose_destination_vool_cannot_read_is_refused(tmp_path, url) -> None:
    runner, root, bare = _runner_world(tmp_path)
    git(root, "remote", "set-url", "origin", url)

    code, _, err = _push(runner)

    assert code != 0 and "VOOL pushes only over" in err, err


def test_a_remote_helper_chosen_by_vcs_is_refused(tmp_path, monkeypatch) -> None:
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "HELPER_RAN"
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    helper = helpers / "git-remote-fake"
    helper.write_text(f"#!/bin/sh\necho ran > '{marker}'\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{helpers}{os.pathsep}{os.environ.get('PATH', '')}")
    git(root, "config", "remote.origin.vcs", "fake")

    code, _, err = _push(runner)

    # Refused either by the push destination check or by safe_git's repository-config check, which
    # also names remote.origin.vcs and runs first once that check covers it.
    assert code != 0 and ("remote helper" in err or "remote.origin.vcs" in err), err
    assert remote_ref(bare, "pushed") == ""
    assert not marker.exists()


def test_a_clean_local_bare_remote_still_pushes(tmp_path) -> None:
    runner, root, bare = _runner_world(tmp_path)

    code, _, err = _push(runner)

    assert code == 0, err
    assert remote_ref(bare, "pushed") == head(root)


@pytest.mark.parametrize("url, transport", [
    ("https://github.com/o/r.git", "network"),
    ("ssh://git@github.com/o/r.git", "network"),
    ("git@github.com:o/r.git", "network"),
    ("git://example.invalid/r.git", "network"),
    ("/srv/git/r.git", "local"),
    ("../r.git", "local"),
    ("r.git", "local"),
    ("./host:path", "local"),
    ("file:///srv/git/r.git", "local"),
    ("ext::sh -c true", "other"),
    ("fake://x/y", "other"),
])
def test_the_transport_is_read_the_way_git_reads_it(url, transport) -> None:
    assert _push_transport(url) == transport


def test_network_remotes_are_not_checked_as_folders(tmp_path) -> None:
    runner, root, bare = _runner_world(tmp_path)
    git(root, "remote", "set-url", "origin", "https://example.invalid/o/r.git")

    assert runner.push_destination_refusal("origin") == ""


# -- fresh review, 20 Oct: five more ways a push reached a destination's programs --------------------


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
def test_a_configured_probe_remote_cannot_redirect_the_check(tmp_path) -> None:
    # A bare path is read through a throwaway remote; config that pre-defines a pushurl for a remote
    # of that name must not make VOOL check a clean repository while git pushes to the hooked one.
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "PRE_RECEIVE_RAN"
    _hook(bare, "pre-receive", marker)
    clean = tmp_path / "clean.git"
    subprocess.run(["git", "init", "-q", "--bare", str(clean)], check=True, timeout=60)
    git(root, "config", "remote.vool-push-target.pushurl", str(clean))

    code, _, err = _push(runner, str(bare))

    assert code != 0 and "pre-receive" in err, err
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="the program is a POSIX shell command")
def test_a_program_in_the_destinations_worktree_config_refuses_the_push(tmp_path) -> None:
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "ALTERNATE_REFS_RAN"
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True, timeout=60)
    (bare / "objects" / "info" / "alternates").write_text(f"{other / 'objects'}\n", encoding="utf-8")
    git(bare, "config", "extensions.worktreeConfig", "true")
    git(bare, "config", "--worktree", "core.alternateRefsCommand", f"sh -c 'echo ran > {marker}; true'")

    code, _, err = _push(runner)

    assert code != 0 and "core.alternaterefscommand" in err, err
    assert not marker.exists()
    assert remote_ref(bare, "pushed") == ""


@pytest.mark.skipif(os.name == "nt", reason="the program is a POSIX shell script")
def test_a_gc_hook_in_the_destination_refuses_the_push(tmp_path) -> None:
    # receive-pack runs `gc --auto` after the push, and gc runs gc.recentObjectsHook.
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "GC_HOOK_RAN"
    program = tmp_path / "gc-hook.sh"
    program.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
    program.chmod(0o755)
    for key, value in (("gc.auto", "1"), ("gc.autoDetach", "false"), ("gc.pruneExpire", "1.second.ago"),
                       ("gc.recentObjectsHook", str(program))):
        git(bare, "config", key, value)
    loose, attempt = 0, 0
    while loose < 3:  # unreachable loose objects in objects/17 make `gc --auto` decide to run
        sha = subprocess.run(["git", "--git-dir", str(bare), "hash-object", "-w", "--stdin"], input=f"blob{attempt}",
                             capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        attempt += 1
        if sha.startswith("17"):
            loose += 1
            os.utime(bare / "objects" / sha[:2] / sha[2:], (1e9, 1e9))

    code, _, err = _push(runner)

    assert code != 0 and "gc.recentobjectshook" in err, err
    assert not marker.exists()
    assert remote_ref(bare, "pushed") == ""


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
def test_a_submodule_is_never_pushed_to_a_remote_nothing_checked(tmp_path) -> None:
    runner, root, bare = _runner_world(tmp_path)
    marker = tmp_path / "SUBMODULE_PRE_RECEIVE_RAN"
    sub_remote = tmp_path / "sub-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(sub_remote)], check=True, timeout=60)
    _hook(sub_remote, "pre-receive", marker)
    seed = tmp_path / "sub-seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "s.txt").write_text("s\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "--no-verify", str(sub_remote), "main")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_remote), "sub")
    git(root / "sub", "switch", "-q", "-c", "feature")  # on-demand pushes a submodule on the same branch
    (root / "sub" / "s.txt").write_text("changed in the submodule\n", encoding="utf-8")
    git(root / "sub", "commit", "-q", "-am", "submodule change")
    git(root, "commit", "-q", "-am", "bump the submodule")
    git(root, "config", "push.recurseSubmodules", "on-demand")
    marker.unlink(missing_ok=True)

    code, _, err = runner.run("push", "origin", "HEAD:refs/heads/pushed")

    assert code == 0, err  # the superproject's own clean remote still receives the push
    assert not marker.exists()
    assert remote_ref(bare, "pushed") == head(root)
    assert "--no-recurse-submodules" in runner.push_argv(remote="origin", ref="r", sha="a" * 40)


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")
def test_a_relative_destination_is_read_from_the_top_of_the_work_tree(tmp_path) -> None:
    # git push runs from the work tree's top, so `evil.git` names <top>/evil.git even when VOOL's
    # session root is a subfolder that holds a clean `evil.git` of its own.
    _, root, _ = _runner_world(tmp_path)
    marker = tmp_path / "PRE_RECEIVE_RAN"
    (root / "sub").mkdir()
    (root / "sub" / "keep.txt").write_text("k\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "sub")
    subprocess.run(["git", "init", "-q", "--bare", str(root / "sub" / "evil.git")], check=True, timeout=60)
    subprocess.run(["git", "init", "-q", "--bare", str(root / "evil.git")], check=True, timeout=60)
    _hook(root / "evil.git", "pre-receive", marker)
    runner = GitRunner(root=root / "sub")

    code, _, err = _push(runner, "evil.git")

    assert code != 0 and "pre-receive" in err, err
    assert not marker.exists()


def test_the_refusal_never_shows_a_password_in_a_url(tmp_path) -> None:
    runner, root, _ = _runner_world(tmp_path)
    git(root, "remote", "set-url", "origin", "svn://alice:hunter2@example.invalid/r")

    code, _, err = _push(runner)

    assert code != 0 and "hunter2" not in err and "alice" not in err, err


def test_a_refusal_the_runner_makes_at_dispatch_is_recorded_as_not_sent(world, tmp_path, monkeypatch) -> None:
    # The plan and the push re-check the destination first; if only the runner's own check catches it
    # (the destination changed in between), the push is recorded as failed and safe to retry, never as
    # an unknown outcome, even when the refusal names a timeout.
    from core.repoops.gitops import GitRunner as Runner

    root, bare, forge = world
    ctx = context(root)
    sid, _ = _drive(root, forge, ctx, notes="a reviewed change\n")
    calls = {"n": 0}
    real = Runner.push_destination_refusal

    def late(self, remote):
        calls["n"] += 1
        return real(self, remote) if calls["n"] < 2 else "the push destination could not be checked (TimeoutExpired)"

    monkeypatch.setattr(Runner, "push_destination_refusal", late)

    pushed = door("repo.push", {"repo_session_id": sid, "simulate": False}, ctx)

    assert pushed.ok is False and pushed.status == "failed", pushed.response_text
    assert remote_ref(bare, REF) == ""
