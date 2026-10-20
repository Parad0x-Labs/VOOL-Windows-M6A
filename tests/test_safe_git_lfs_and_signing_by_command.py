"""Which repository settings refuse git depends on the command VOOL runs (core/safe_git.py).

Signing: a repository that switches on signing for its own commits (``commit.gpgSign=true``, common
in a developer's repository) must still be readable: status, diff, log and the user-rules scans
cannot sign or verify. Every command that could sign or check a signature is still refused, and the
user's gpg never runs for it.

Git LFS (Jesse, 20 Oct: "Trusted servers only"): the user's own git-lfs downloads objects whenever
git writes LFS files into the work tree, from the server the repository names in its config or in
the ``.lfsconfig`` it carries. VOOL lets that happen only for this project's own git host or a
server the user named. The cases run the real git-lfs against a local server that records every
request; the project's remote is ``localhost`` and the other server is ``127.0.0.1``, both the
same recorder, so a request reaching it proves where git-lfs went.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core import safe_git, user_rules
from core.execution.git_tools import git_diff_workspace, git_status_workspace
from core.repoops.gitops import GitRunner

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")

_POSIX_SCRIPT = pytest.mark.skipif(os.name == "nt", reason="the stand-in gpg is a shebang script")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                          check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.test")
    (repo / "notes.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, tmp_path / "MARKER"


def _user_signing_program(tmp_path: Path, monkeypatch, marker: Path) -> None:
    # The user's own gpg.program, in a global config under a private HOME; it leaves the marker.
    script = tmp_path / "fake-gpg"
    script.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdin.buffer.read()\n"
        "sys.stderr.write('\\n[GNUPG:] SIG_CREATED D 1 8 00 0 X\\n')\n"
        "sys.stdout.write('-----BEGIN PGP SIGNATURE-----\\n\\nAA==\\n-----END PGP SIGNATURE-----\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "config", "-f", str(home / ".gitconfig"), "gpg.program", script.as_posix()],
                   check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _forged_signature_commit(repo: Path) -> str:
    # A commit carrying a signature header the repository wrote itself, with no key.
    tree = _git(repo, "write-tree").strip()
    body = (f"tree {tree}\nparent {_git(repo, 'rev-parse', 'HEAD').strip()}\n"
            "author T <t@example.test> 1 +0000\ncommitter T <t@example.test> 1 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n AAAA\n -----END PGP SIGNATURE-----\n\nforged\n")
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                         check=True, capture_output=True, text=True).stdout.strip()
    _git(repo, "branch", "forged", sha)
    return sha


def test_a_repository_that_signs_its_own_commits_can_still_be_read(tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "tag.gpgSign", "true")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")

    status = git_status_workspace({}, workspace_root=repo)
    diff = git_diff_workspace({"cached": True}, workspace_root=repo)
    staged = user_rules._git_text(repo, "diff", "--cached", "--no-color", "--no-ext-diff", "-U0")
    code, out, err = GitRunner(root=repo).run("log", "-1", "--format=%s")

    assert status["ok"] is True, status
    assert diff["ok"] is True and "+two" in diff["response_text"], diff
    assert "+two" in staged
    assert (code, out.strip()) == (0, "init"), err
    assert not marker.exists()


def test_commands_that_sign_are_still_refused_there_and_the_users_gpg_never_runs(tmp_path: Path,
                                                                                 monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _git(repo, "config", "commit.gpgSign", "true")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD").strip()

    commit = GitRunner(root=repo).commit(message="signed")
    tag = GitRunner(root=repo).tag("v1", message="signed")

    assert commit.ok is False and tag.ok is False
    assert "commit.gpgsign (set in .git/config)" in commit.stderr, commit.to_dict()
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert _git(repo, "tag", "--list").strip() == ""
    assert not marker.exists(), "a repository's commit.gpgSign made git run the user's gpg"


@pytest.mark.parametrize("argv", [
    ("commit", "-m", "x"), ("tag", "-a", "v1"), ("merge", "main"), ("push", "origin"), ("cherry-pick", "x"),
    ("log", "--show-signature"), ("show", "--show-signature"), ("log", "-S", "x"), ("status", "--gpg-sign"),
    ("merge-base", "--verify-signatures"), ("frobnicate",), (),
])
def test_any_command_that_can_sign_or_verify_keeps_the_signing_keys(tmp_path: Path, argv: tuple[str, ...]) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "gpg.format", "ssh")

    found = safe_git.executable_config_for(repo, argv)

    assert found == ["commit.gpgsign (set in .git/config)", "gpg.format (set in .git/config)"], argv


@pytest.mark.parametrize("argv", [("status", "--short"), ("diff", "--cached"), ("log", "-1"), ("show", "HEAD"),
                                  ("rev-parse", "HEAD"), ("for-each-ref",), ("config", "--get", "x.y")])
def test_reads_ignore_signing_keys_but_never_programs(tmp_path: Path, argv: tuple[str, ...]) -> None:
    repo, _marker = _repo(tmp_path)
    for key, value in (("commit.gpgSign", "true"), ("push.gpgSign", "if-asked"), ("gpg.format", "x509"),
                       ("log.showSignature", "true"), ("merge.verifySignatures", "true")):
        _git(repo, "config", key, value)
    _git(repo, "config", "filter.planted.clean", "anything")

    assert safe_git.executable_config_for(repo, argv) == ["filter.planted.clean (set in .git/config)"], argv


@_POSIX_SCRIPT
def test_reading_a_repository_that_turns_signature_checks_on_never_runs_gpg(tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _forged_signature_commit(repo)
    _git(repo, "config", "log.showSignature", "true")
    _git(repo, "config", "gpg.format", "openpgp")
    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(repo), "log", "-1", "forged"],
                   capture_output=True, timeout=60, env=safe_git.safe_env())
    assert marker.exists(), "control: the repository's log.showSignature did not run gpg; the case below proves nothing"
    marker.unlink()

    text = user_rules._git_text(repo, "log", "-1", "--no-color", "--format=%B", "forged")
    code, out, _ = GitRunner(root=repo).run("show", "-s", "--format=%B", "forged")

    assert "forged" in text and code == 0 and "forged" in out
    assert not marker.exists(), "VOOL's read ran the user's gpg on a forged signature"


def test_the_refusal_names_where_each_key_is_set(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "filter.a.clean", "x")
    _git(repo, "config", "--worktree", "filter.a.clean", "y")
    _git(repo, "config", "--worktree", "core.sshCommand", "z")

    assert safe_git.executable_config_for(repo, ("status",)) == [
        "core.sshcommand (set in config.worktree)",
        "filter.a.clean (set in .git/config)",
        "filter.a.clean (set in config.worktree)",
    ]
    assert safe_git.executable_local_config(repo) == ["core.sshcommand", "filter.a.clean"]


def test_without_a_command_every_key_still_counts(tmp_path: Path) -> None:
    # The Windows write gate asks GitRunner without a command (windows_write_gate.py), and
    # executable_local_config keeps its bare-key contract (workspace_tools.py, Job 6).
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "commit.gpgSign", "true")

    assert "commit.gpgsign" in GitRunner(root=repo)._repository_refusal(mutating=False)
    assert safe_git.executable_local_config(repo) == ["commit.gpgsign"]


@pytest.mark.parametrize("key, value, safe", [
    ("commit.gpgsign", "false", True), ("Commit.GpgSign", "OFF", True), ("tag.gpgsign", "0", True),
    ("commit.gpgsign", "true", False), ("commit.gpgsign", None, False), ("commit.gpgsign", "", False),
    ("gpg.format", "openpgp", True), ("gpg.format", "ssh", False), ("gpg.format", "OpenPGP", False),
    ("filter.x.clean", "false", False), ("core.sshcommand", "", False),
])
def test_is_safe_value(key: str, value: str | None, safe: bool) -> None:
    assert safe_git.is_safe_value(key, value) is safe


# ── Git LFS: only trusted servers ─────────────────────────────────────────────────────────────

_NEEDS_LFS = pytest.mark.skipif(subprocess.run(["git", "lfs", "version"], capture_output=True).returncode != 0,
                                reason="git-lfs is not installed")


@pytest.fixture
def lfs_server():
    hits: list[str] = []

    class Recorder(BaseHTTPRequestHandler):
        def _refuse(self) -> None:
            hits.append(f"{self.command} {self.path}")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._refuse()

        def do_POST(self) -> None:
            self._refuse()

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], hits
    server.shutdown()
    server.server_close()


def _lfs_repo(tmp_path: Path, monkeypatch, port: int) -> Path:
    # The user's own git-lfs, set up the way `git lfs install` writes the global config. The
    # project's remote is on localhost. Branch "lfs" adds an LFS file whose object nobody has, so
    # bringing it in makes git-lfs ask a server for it.
    home = tmp_path / "home"
    home.mkdir()
    for key, value in (("filter.lfs.process", "git-lfs filter-process"), ("filter.lfs.required", "true"),
                       ("filter.lfs.clean", "git-lfs clean -- %f"), ("filter.lfs.smudge", "git-lfs smudge -- %f")):
        subprocess.run(["git", "config", "-f", str(home / ".gitconfig"), key, value], check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", f"http://localhost:{port}/project.git")
    _git(repo, "switch", "-q", "-c", "lfs")
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    oid = hashlib.sha256(b"nobody has this\n").hexdigest()
    (repo / "big.bin").write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 16\n",
                                  encoding="utf-8")
    _git(repo, "add", ".gitattributes", "big.bin")
    _git(repo, "commit", "-q", "-m", "add an LFS file")
    _git(repo, "switch", "-q", "-")
    _without_lfs_hooks(repo)
    return repo


def _without_lfs_hooks(repo: Path) -> None:
    # git-lfs installs its own hooks whenever it runs, and VOOL refuses a mutation that would run
    # hooks anyway. Without them (a clone made before git-lfs was installed, or hooks removed) the
    # LFS check is what stands between the repository and git-lfs.
    for hook in (repo / ".git" / "hooks").iterdir():
        if not hook.name.endswith(".sample"):
            hook.unlink()


def _commit_lfsconfig(repo: Path, url: str, *, key: str = "lfs.url") -> None:
    subprocess.run(["git", "config", "-f", str(repo / ".lfsconfig"), key, url], check=True, capture_output=True)
    _git(repo, "add", ".lfsconfig")
    _git(repo, "commit", "-q", "-m", "LFS server")
    _without_lfs_hooks(repo)


@_NEEDS_LFS
def test_control_git_lfs_goes_to_the_server_the_repositorys_lfsconfig_names(tmp_path: Path, monkeypatch,
                                                                             lfs_server) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _commit_lfsconfig(repo, f"http://127.0.0.1:{port}/elsewhere")

    subprocess.run(safe_git.hardened_argv(repo, "cherry-pick", "lfs"), capture_output=True, timeout=120,
                   env=safe_git.safe_env())

    assert any("/elsewhere/" in hit for hit in hits), f"git-lfs did not ask the named server; the cases below prove nothing: {hits}"


@_NEEDS_LFS
@pytest.mark.parametrize("where", ["HEAD", "the commit brought in"])
def test_an_lfs_server_the_repository_names_elsewhere_is_refused_and_never_contacted(
        tmp_path: Path, monkeypatch, lfs_server, where: str) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    if where == "HEAD":
        _commit_lfsconfig(repo, f"http://127.0.0.1:{port}/elsewhere")
    else:  # set up without git-lfs, which would try to download the object
        no_lfs = ("-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false")
        _git(repo, *no_lfs, "switch", "-q", "lfs")
        _commit_lfsconfig(repo, f"http://127.0.0.1:{port}/elsewhere")
        _git(repo, *no_lfs, "switch", "-q", "-")
    head = _git(repo, "rev-parse", "HEAD").strip()
    hits.clear()

    result = GitRunner(root=repo).cherry_pick("lfs")

    assert result.ok is False
    assert "Git LFS would contact 127.0.0.1" in result.stderr and ".lfsconfig" in result.stderr, result.to_dict()
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert hits == [], "git-lfs contacted a server the repository chose"


@_NEEDS_LFS
def test_an_lfs_server_set_in_the_repositorys_own_config_is_refused(tmp_path: Path, monkeypatch,
                                                                    lfs_server) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _git(repo, "config", "remote.origin.lfsurl", f"http://user:secret@127.0.0.1:{port}/elsewhere")

    result = GitRunner(root=repo).cherry_pick("lfs")

    assert result.ok is False
    assert "remote.origin.lfsurl, set in .git/config" in result.stderr, result.to_dict()
    assert "secret" not in result.stderr
    assert hits == []


@_NEEDS_LFS
def test_an_lfs_server_on_the_projects_own_git_host_is_allowed(tmp_path: Path, monkeypatch, lfs_server) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _commit_lfsconfig(repo, f"http://localhost:{port}/project.git/info/lfs")

    assert safe_git.lfs_endpoint_refusal(repo, ("cherry-pick", "lfs")) == ""
    result = GitRunner(root=repo).cherry_pick("lfs")

    assert "Git LFS would contact" not in result.stderr, result.to_dict()
    assert any("/project.git/info/lfs/" in hit for hit in hits), hits


@_NEEDS_LFS
def test_an_lfs_server_the_user_named_is_allowed(tmp_path: Path, monkeypatch, lfs_server) -> None:
    port, _hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    subprocess.run(["git", "config", "-f", str(tmp_path / "home" / ".gitconfig"), f"lfs.http://127.0.0.1:{port}/.access",
                    "basic"], check=True, capture_output=True)
    _commit_lfsconfig(repo, f"http://127.0.0.1:{port}/mine")

    assert safe_git.lfs_endpoint_refusal(repo, ("cherry-pick", "lfs")) == ""


@_NEEDS_LFS
def test_a_rewrite_that_sends_lfs_to_another_host_is_refused(tmp_path: Path, monkeypatch, lfs_server) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _git(repo, "config", f"url.http://127.0.0.1:{port}/.insteadOf", f"http://localhost:{port}/lfs/")
    _commit_lfsconfig(repo, f"http://localhost:{port}/lfs/elsewhere")

    result = GitRunner(root=repo).cherry_pick("lfs")

    assert result.ok is False and "Git LFS would contact 127.0.0.1" in result.stderr, result.to_dict()
    assert hits == []


def test_an_lfsconfig_git_cannot_read_is_refused(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    (repo / ".lfsconfig").write_text("[lfs\n url = http://127.0.0.1/x\n", encoding="utf-8")
    _git(repo, "add", ".lfsconfig")
    _git(repo, "commit", "-q", "-m", "broken")

    refusal = safe_git.lfs_endpoint_refusal(repo, ("cherry-pick", "HEAD"))

    assert refusal.startswith("the repository's Git LFS servers could not be checked"), refusal


def test_a_repository_that_names_no_lfs_server_is_not_checked_further(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _git(repo, "branch", "other")

    assert safe_git.lfs_endpoint_refusal(repo, ("merge", "--no-ff", "-m", "a message", "other")) == ""


@pytest.mark.parametrize("url, host", [
    ("https://User:pw@Git.Example.test:8443/x", "git.example.test"), ("git@github.com:o/r.git", "github.com"),
    ("ssh://git@[::1]:22/r", "::1"), ("file:///srv/r.git", "path:file:///srv/r.git"), ("/srv/r.git", "path:/srv/r.git"),
    ("C:/repos/r.git", "path:C:/repos/r.git"),
])
def test_endpoint_hosts(url: str, host: str) -> None:
    assert safe_git._endpoint_host(url) == host


# ── No automatic housekeeping in VOOL's own git ────────────────────────────────────────────────


def _loose_objects(repo: Path) -> int:
    counts = dict(line.split(": ", 1) for line in _git(repo, "count-objects", "-v").splitlines())
    return int(counts["count"])


def _auto_housekeeping_repo(tmp_path: Path, strategy: str) -> Path:
    # Two loose objects in objects/17 trip gc.auto=1; the next commit's automatic housekeeping
    # would pack them. git before 2.54 ignores the strategy and runs gc either way.
    repo, _marker = _repo(tmp_path)
    found = 0
    for index in range(100000):
        content = f"loose {index}\n".encode()
        if hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest().startswith("17"):
            subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content,
                           check=True, capture_output=True)
            found += 1
            if found == 2:
                break
    for key, value in (("maintenance.strategy", strategy), ("maintenance.geometric-repack.auto", "-1"),
                       ("gc.auto", "1"), ("gc.autoDetach", "false"), ("maintenance.autoDetach", "false")):
        _git(repo, "config", key, value)
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    return repo


def test_control_a_commits_automatic_housekeeping_packs_loose_objects(tmp_path: Path) -> None:
    repo = _auto_housekeeping_repo(tmp_path, "gc")
    before = _loose_objects(repo)

    subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(repo), "commit", "-q", "-a", "-m", "gc"],
                   capture_output=True, timeout=120, env=safe_git.safe_env())

    assert before >= 2 and _loose_objects(repo) < before, "control: the commit ran no housekeeping"


@pytest.mark.parametrize("strategy", ["gc", "geometric"])
def test_vool_commits_run_no_automatic_housekeeping(tmp_path: Path, strategy: str) -> None:
    repo = _auto_housekeeping_repo(tmp_path, strategy)
    before = _loose_objects(repo)

    result = GitRunner(root=repo).commit(message="no housekeeping")

    assert result.ok is True, result.to_dict()
    assert _loose_objects(repo) >= before, "VOOL's commit ran automatic housekeeping"


def test_only_a_leading_alias_section_is_exempt_from_the_program_endings() -> None:
    assert not safe_git.is_executable_key("alias.lastcommand")
    assert safe_git.is_executable_key("foo.alias.xcommand")
    assert safe_git.is_executable_key("alias.x.command")


# ── Review findings, 20 Oct ────────────────────────────────────────────────────────────────────


def _no_lfs(*args: str) -> tuple[str, ...]:
    # Set-up git that leaves LFS files as pointers instead of asking a server for them.
    return ("-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false", *args)


def _revert_world(tmp_path: Path, monkeypatch, port: int) -> Path:
    # Commit A adds an LFS file and an .lfsconfig naming another server; commit B removes both.
    # Reverting B brings A's .lfsconfig back, and neither HEAD nor B holds it.
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _git(repo, *_no_lfs("checkout", "lfs", "--", ".gitattributes", "big.bin"))
    subprocess.run(["git", "config", "-f", str(repo / ".lfsconfig"), "lfs.url", f"http://127.0.0.1:{port}/elsewhere"],
                   check=True, capture_output=True)
    _git(repo, *_no_lfs("add", ".lfsconfig"))
    _git(repo, *_no_lfs("commit", "-q", "-m", "A"))
    _git(repo, *_no_lfs("rm", "-q", ".lfsconfig", "big.bin"))
    _git(repo, *_no_lfs("commit", "-q", "-m", "B"))
    _without_lfs_hooks(repo)
    return repo


@_NEEDS_LFS
def test_control_reverting_a_removal_brings_the_other_server_back(tmp_path: Path, monkeypatch, lfs_server) -> None:
    port, hits = lfs_server
    repo = _revert_world(tmp_path, monkeypatch, port)
    hits.clear()

    subprocess.run(safe_git.hardened_argv(repo, "revert", "--no-edit", "HEAD"), capture_output=True, timeout=120,
                   env=safe_git.safe_env())

    assert any("/elsewhere/" in hit for hit in hits), f"control: git-lfs did not ask the server; {hits}"


@_NEEDS_LFS
def test_a_revert_that_would_bring_back_another_lfs_server_is_refused(tmp_path: Path, monkeypatch,
                                                                     lfs_server) -> None:
    port, hits = lfs_server
    repo = _revert_world(tmp_path, monkeypatch, port)
    head = _git(repo, "rev-parse", "HEAD").strip()
    hits.clear()

    result = GitRunner(root=repo).revert("HEAD")

    assert result.ok is False and "Git LFS would contact 127.0.0.1" in result.stderr, result.to_dict()
    assert "the .lfsconfig revert would write" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert hits == []


def test_a_cherry_pick_whose_merged_lfsconfig_names_another_server_is_refused(tmp_path: Path) -> None:
    # Neither side names an LFS server: HEAD has an empty [lfs] section, the picked commit adds a
    # url to [foo]. Merged, the url lands under [lfs].
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    (repo / ".lfsconfig").write_text("[foo]\n#1\n#2\n#3\n#4\n", encoding="utf-8")
    _git(repo, "add", ".lfsconfig")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "switch", "-q", "-c", "side")
    (repo / ".lfsconfig").write_text("[foo]\n#1\n#2\n#3\n#4\n\turl = http://127.0.0.1/elsewhere\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-a", "-m", "foo.url")
    _git(repo, "switch", "-q", "-")
    (repo / ".lfsconfig").write_text("[lfs]\n#1\n#2\n#3\n#4\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-a", "-m", "empty lfs section")

    refusal = safe_git.lfs_endpoint_refusal(repo, ("cherry-pick", "--no-rerere-autoupdate", "side"))

    assert "Git LFS would contact 127.0.0.1" in refusal and "the .lfsconfig cherry-pick would write" in refusal, refusal


def test_an_lfs_url_that_reads_as_an_option_is_refused(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    subprocess.run(["git", "config", "-f", str(repo / ".lfsconfig"), "lfs.url", "-ox@127.0.0.1:9:elsewhere"],
                   check=True, capture_output=True)

    refusal = safe_git.lfs_endpoint_refusal(repo, ("switch", "-c", "x"))

    assert refusal.startswith("the repository's Git LFS servers could not be checked"), refusal


def test_picking_more_than_one_commit_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)

    refusal = safe_git.lfs_endpoint_refusal(repo, ("cherry-pick", "HEAD", "HEAD"))

    assert refusal.startswith("the repository's Git LFS servers could not be checked"), refusal


def test_a_submodules_key_says_it_is_set_in_the_submodule(tmp_path: Path) -> None:
    (tmp_path / "inner").mkdir()
    (tmp_path / "outer").mkdir()
    inner, _marker = _repo(tmp_path / "inner")
    outer, _ = _repo(tmp_path / "outer")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub")
    _git(outer / "sub", "config", "filter.x.clean", "y")

    assert safe_git.executable_config_for(outer, ("status",)) == ["sub:filter.x.clean (set in the submodule's .git/config)"]
