"""The Skill Codex signer publishes where VOOL runs no git program (Windows), through the dulwich library.

Owner decision D18 A (16 Nov 2026), to Security Review's design bar. Rows:
- No git process starts, and a hook planted in the checkout never runs.
- The checkout's config naming a program, an include, a proxy, a credential setting, a URL rewrite or an
  extra header refuses before any request.
- Only https:// or a full local path is a remote; ssh, git://, file:// and a relative path refuse.
- A redirect refuses with the token sent only once, in the Authorization header, and never in an error.
- A link, a submodule or a ``.git`` path in the remote's tree refuses before a file is written.
- github_authority.Repository uses this route exactly when core/safe_git refuses git.
The fixtures build their repositories with the git program; the route under test never runs it.
"""
from __future__ import annotations

import base64
import os
import subprocess

import pytest

from core import safe_git
from core.registry.codec import RegistryError
from registry_service import dulwich_git, github_authority
from registry_service.github_authority import BOT, FOLDER, STEPS, Superseded
from tests.registry.test_skill_codex_fix_redirects import servers

TOKEN = "fixture-token-5f1c"


def git(path, *args, stdin: str | None = None) -> str:
    result = subprocess.run(["git", "-C", str(path), "-c", "user.name=sls", "-c", "user.email=sls@example.test",
                             "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, input=stdin)
    assert result.returncode == 0, result.stderr
    return result.stdout


def library(path, token: str = "") -> dulwich_git.Checkout:
    return dulwich_git.Checkout(path, branch="main", token=token, folder=FOLDER, steps=STEPS, identity=BOT,
                                superseded=Superseded)


@pytest.fixture
def pair(tmp_path):
    """A bare remote holding one commit with the state folder, and a clone of it."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    git(seed, "checkout", "-q", "-b", "main")
    (seed / FOLDER).mkdir()
    (seed / FOLDER / "state.json").write_text('{"epoch": 1}', encoding="utf-8")
    (seed / "README.md").write_text("signer\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(work)], check=True, capture_output=True)
    return origin, seed, work


@pytest.fixture
def no_git_program(monkeypatch):
    """Any process start from here on fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("a process was started: " + repr(args[0] if args else kwargs))

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "posix_spawn", refuse, raising=False)
    monkeypatch.setattr(os, "posix_spawnp", refuse, raising=False)


def test_save_pushes_the_state_with_no_process_and_no_hook(pair, tmp_path, monkeypatch) -> None:
    origin, _seed, work = pair
    marker = tmp_path / "hook-ran"
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-push", "reference-transaction"):
        hook = work / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\necho ran > '" + str(marker) + "'\n", encoding="utf-8")
        hook.chmod(0o755)
    checkout = library(work)
    checkout.start()
    (work / FOLDER / "state.json").write_text('{"epoch": 2}', encoding="utf-8")
    started = []

    def refuse(*args, **kwargs):
        started.append(args)
        raise AssertionError("a process was started")

    with monkeypatch.context() as during:
        during.setattr(subprocess, "Popen", refuse)
        assert checkout.save("Signer state") is True

    assert started == [] and not marker.exists()
    assert git(origin, "show", "main:" + FOLDER + "/state.json") == '{"epoch": 2}'
    assert git(origin, "log", "-1", "--format=%an <%ae>|%s", "main").strip() == BOT[0] + " <" + BOT[1] + ">|Signer state"
    assert checkout.save("Signer state") is False  # nothing new to say


def test_start_fresh_and_untouched_start_no_process(pair, no_git_program) -> None:
    _origin, _seed, work = pair
    checkout = library(work)

    checkout.start()
    checkout.fresh()

    assert checkout.untouched(FOLDER + "/state.json")
    (work / FOLDER / "state.json").write_text('{"epoch": 9}', encoding="utf-8")
    assert not checkout.untouched(FOLDER + "/state.json")


def test_a_remote_state_change_stops_the_run(pair) -> None:
    _origin, seed, work = pair
    checkout = library(work)
    checkout.start()
    (seed / FOLDER / "state.json").write_text('{"epoch": 5}', encoding="utf-8")
    git(seed, "commit", "-qam", "another run")
    git(seed, "push", "-q", "origin", "main")

    with pytest.raises(Superseded):
        checkout.fresh()


@pytest.mark.parametrize("key", ["core.fsmonitor", "core.sshCommand", "credential.helper", "core.hooksPath",
                                 "filter.lfs.process", "filter.lfs.smudge", "diff.x.textconv"])
def test_a_checkout_config_naming_a_program_refuses_before_any_request(pair, key, monkeypatch) -> None:
    _origin, _seed, work = pair
    git(work, "config", key, "/bin/true")
    asked = []
    monkeypatch.setattr(dulwich_git.Checkout, "_remote", lambda self, opened: asked.append(1))

    with pytest.raises(RegistryError, match="names programs git would run"):
        library(work).start()
    assert asked == []


@pytest.mark.parametrize(("key", "value"), [("include.path", "elsewhere.cfg"), ("includeIf.gitdir:/x/.path", "elsewhere.cfg"),
                                          ("http.proxy", "http://proxy.example:8080"),
                                          ("http.https://github.com.proxy", "http://proxy.example:8080"),
                                          ("remote.origin.proxy", "http://proxy.example:8080"),
                                          ("credential.username", "someone"),
                                          ("url.https://evil.example/.insteadOf", "https://github.com/"),
                                          ("url.https://evil.example/.pushInsteadOf", "https://github.com/"),
                                          ("http.extraHeader", "Authorization: Basic eA==")])
def test_a_checkout_config_that_redirects_or_reaches_credentials_refuses(pair, key, value, monkeypatch) -> None:
    _origin, _seed, work = pair
    git(work, "config", key, value)
    asked = []
    monkeypatch.setattr(dulwich_git.Checkout, "_remote", lambda self, opened: asked.append(1))

    with pytest.raises(RegistryError, match=r"git config (has settings VOOL won't follow|names programs)"):
        library(work).start()
    assert asked == []


@pytest.mark.parametrize("url", ["ssh://git@github.com/o/r.git", "git@github.com:o/r.git", "git://github.com/o/r.git",
                                 "file:///tmp/r.git", "relative/r.git", "http://github.com/o/r.git",
                                 "https://x-access-token:" + TOKEN + "@github.com/o/r.git", "ext::sh -c touch% /tmp/x"])
def test_a_remote_that_is_not_https_or_a_local_folder_refuses(pair, url) -> None:
    _origin, _seed, work = pair
    git(work, "remote", "set-url", "origin", url)

    with pytest.raises(RegistryError, match="origin must be") as refused:
        library(work, TOKEN).start()
    assert TOKEN not in str(refused.value)


@pytest.fixture
def direct(monkeypatch):
    """The fixture servers are on this machine, so no proxy from the environment sits in between."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_https_transport_refuses_redirects_with_its_token_sent_once(tmp_path, direct, status) -> None:
    from dulwich import client

    with servers(status) as (url, source, target):
        transport = library(tmp_path, TOKEN)._https(client, url + "/o/r.git")
        with pytest.raises(RegistryError, match="git redirect refused") as refused:
            transport.get_refs("/o/r.git")
    expected = "Basic " + base64.b64encode(("x-access-token:" + TOKEN).encode()).decode()
    assert len(source) == 1 and source[0]["authorization"] == expected
    assert target == []
    assert TOKEN not in str(refused.value) and TOKEN not in repr(refused.value.__cause__)


def test_the_token_is_never_in_a_request_line_or_an_error(tmp_path, direct) -> None:
    from dulwich import client

    with servers(500) as (url, source, _target):
        transport = library(tmp_path, TOKEN)._https(client, url + "/o/r.git")
        with pytest.raises(RegistryError) as refused:
            transport.get_refs("/o/r.git")
    assert source and all(TOKEN not in value for value in source[0].values() if not value.startswith("Basic "))
    assert TOKEN not in str(refused.value)


def _remote_with(tmp_path, entry: str):
    """A remote whose second commit adds ``entry``: a symlink, a submodule or a .git path."""
    origin = tmp_path / "bad.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "bad-seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "one")
    git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "bad-work"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(work)], check=True, capture_output=True)
    blob = git(seed, "rev-parse", "HEAD:README.md").strip()
    if entry == "symlink":
        git(seed, "update-index", "--add", "--cacheinfo", "120000," + blob + ",authority/state.json")
    elif entry == "submodule":
        git(seed, "update-index", "--add", "--cacheinfo", "160000," + git(seed, "rev-parse", "HEAD").strip() + ",vendor")
    else:  # git's own index won't take a .git path, so the tree is written by hand, as a hostile remote would
        inner = git(seed, "mktree", stdin="100644 blob " + blob + "\t.git\n").strip()
        top = git(seed, "mktree", stdin="100644 blob " + blob + "\tREADME.md\n040000 tree " + inner + "\tsub\n").strip()
        commit = git(seed, "commit-tree", top, "-p", "HEAD", "-m", "two").strip()
        git(seed, "push", "-q", "origin", commit + ":refs/heads/main")
        return work
    git(seed, "commit", "-q", "-m", "two")
    git(seed, "push", "-q", "origin", "main")
    return work


@pytest.mark.parametrize("entry", ["symlink", "submodule", "dot-git"])
def test_a_link_submodule_or_git_path_in_the_remote_refuses_before_a_file_is_written(tmp_path, entry) -> None:
    work = _remote_with(tmp_path, entry)
    before = sorted(str(p.relative_to(work)) for p in work.rglob("*") if ".git" not in p.relative_to(work).parts)

    with pytest.raises(RegistryError, match="won't write"):
        library(work).start()

    after = sorted(str(p.relative_to(work)) for p in work.rglob("*") if ".git" not in p.relative_to(work).parts)
    assert after == before


@pytest.mark.parametrize("name", [b"..", b".", b".git", b".GIT", b"a:b", b"a\\b", b"C:", b".git ", b"x. "])
def test_a_path_part_vool_wont_write_is_refused(name) -> None:
    with pytest.raises(RegistryError, match="won't write"):
        dulwich_git._safe_part(name)


def test_the_repository_takes_this_route_exactly_when_git_is_refused(pair, monkeypatch) -> None:
    _origin, _seed, work = pair
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    assert github_authority.Repository(work)._no_git_program() is None
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    assert isinstance(github_authority.Repository(work, token=TOKEN)._no_git_program(), dulwich_git.Checkout)


# ---- Windows path aliases in the remote's tree (Security Review, 16 Nov) ----

def _remote_with_paths(tmp_path, paths: list[str]):
    """A remote whose second commit holds exactly ``paths`` (one blob each), written as a hostile remote would."""
    origin = tmp_path / "alias.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "alias-seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "one")
    git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "alias-work"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(work)], check=True, capture_output=True)
    blob = git(seed, "rev-parse", "HEAD:README.md").strip()

    def tree(level: dict) -> str:
        lines = []
        for name, child in sorted(level.items()):
            lines.append(("040000 tree " + tree(child) if child else "100644 blob " + blob) + "\t" + name + "\n")
        return git(seed, "mktree", stdin="".join(lines)).strip()

    top: dict = {"README.md": {}}
    for path in paths:
        level = top
        for part in path.split("/"):
            level = level.setdefault(part, {})
    commit = git(seed, "commit-tree", tree(top), "-p", "HEAD", "-m", "two").strip()
    git(seed, "push", "-q", "origin", commit + ":refs/heads/main")
    return work


def _plain_files(work) -> list[str]:
    return sorted(str(p.relative_to(work)) for p in work.rglob("*") if ".git" not in p.relative_to(work).parts)


@pytest.mark.parametrize("paths", [["git~1/config"], ["GIT~2/hooks/pre-commit"], ["sub/x~1"], ["nul"], ["con.txt"],
                                   ["aux .json"], ["COM1"], ["lpt9.log"], ["conin$"], ["CONOUT$.x"], ["com¹"],
                                   ["lpt³.txt"], ["A/x", "a/y"], ["Notes.md", "notes.MD"]])
def test_a_windows_short_name_device_name_or_case_twin_refuses_before_a_file_is_written(tmp_path, paths) -> None:
    work = _remote_with_paths(tmp_path, paths)
    before = _plain_files(work)

    with pytest.raises(RegistryError, match="won't write"):
        library(work).start()

    assert _plain_files(work) == before


@pytest.mark.parametrize("name", [b"git~1", b"GIT~12", b"a~1b", b"nul", b"NUL.txt", b"con ", b"prn.tar.gz", b"com0",
                                  b"lpt\xc2\xb2", b"conin$"])
def test_a_windows_alias_part_is_refused(name) -> None:
    with pytest.raises(RegistryError, match="won't write"):
        dulwich_git._safe_part(name)


@pytest.mark.parametrize("name", [b"console.txt", b"null", b"com10", b"nul-notes", b"tilde~", b"a~b", b"state.json"])
def test_ordinary_names_near_the_aliases_are_kept(name) -> None:
    assert dulwich_git._safe_part(name) == name.decode()


# ---- a local-folder origin (Security Review, 16 Nov) ----

def test_a_local_origin_reached_through_a_link_refuses_before_it_is_opened(pair, tmp_path, monkeypatch) -> None:
    origin, _seed, work = pair
    linked = tmp_path / "linked-origin.git"
    linked.symlink_to(origin, target_is_directory=True)
    git(work, "remote", "set-url", "origin", str(linked))
    opened = []
    monkeypatch.setattr(dulwich_git.Checkout, "_open_local", lambda self, url: opened.append(url))

    with pytest.raises(RegistryError, match="origin's path goes through a link"):
        library(work).start()
    assert opened == []


def test_a_local_origin_whose_own_config_names_a_program_refuses(pair) -> None:
    origin, _seed, work = pair
    git(origin, "config", "core.fsmonitor", "/bin/true")

    with pytest.raises(RegistryError, match="origin's own git config names programs"):
        library(work).start()


def test_hooks_in_a_local_bare_origin_never_run_on_a_push(pair, tmp_path, monkeypatch) -> None:
    origin, _seed, work = pair
    marker = tmp_path / "origin-hook-ran"
    for name in ("pre-receive", "update", "post-receive", "post-update", "reference-transaction", "proc-receive"):
        hook = origin / "hooks" / name
        hook.write_text("#!/bin/sh\necho " + name + " >> '" + str(marker) + "'\n", encoding="utf-8")
        hook.chmod(0o755)
    checkout = library(work)
    checkout.start()
    (work / FOLDER / "state.json").write_text('{"epoch": 3}', encoding="utf-8")

    with monkeypatch.context() as during:
        during.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("process started")))
        during.setattr(os, "system", lambda *a, **k: (_ for _ in ()).throw(AssertionError("process started")))
        assert checkout.save("Signer state") is True

    assert not marker.exists()
    assert git(origin, "show", "main:" + FOLDER + "/state.json") == '{"epoch": 3}'


# ---- a whole signer run starts no process where git is refused (Security Review, 16 Nov) ----

def test_a_signer_run_starts_no_process_where_vool_runs_no_git(tmp_path, monkeypatch) -> None:
    from tests.registry import test_skill_codex_github_signer as signer_rows
    from tests.registry.test_skill_codex_authority import creator, draft

    world = signer_rows.world.__wrapped__(tmp_path, monkeypatch)
    _, token, csrf = creator(world, "101", "alice")
    assert draft(world, token, csrf, "alice/notes").json()["state"] == "checking"
    path = signer_rows.checkout(world)
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    started = []

    def refuse(*args, **kwargs):
        started.append(args)
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)

    report = signer_rows.signer(world, path).run_once()

    assert report["admitted"] == ["alice/notes 1.0.0"] and started == []
