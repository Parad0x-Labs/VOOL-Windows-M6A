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
    # Bytes: a text-mode pipe on Windows would turn every "\n" into "\r\n", which git refuses.
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                         input=body.encode(), check=True, capture_output=True).stdout.decode().strip()
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


@_POSIX_SCRIPT
def test_a_log_without_a_format_ignores_the_repositorys_signature_placeholder(tmp_path: Path, monkeypatch) -> None:
    # format.pretty with %G makes a plain `git log` check the signature (through ssh-keygen or gpgsm
    # under the repository's gpg.format), and log is a read, so the signing keys do not refuse it.
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _forged_signature_commit(repo)
    _git(repo, "config", "format.pretty", "%G?|%s")
    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(repo), "log", "-1", "forged"],
                   capture_output=True, timeout=60, env=safe_git.safe_env())
    assert marker.exists(), "control: the repository's format.pretty did not run gpg; the case below proves nothing"
    marker.unlink()

    code, out, _ = GitRunner(root=repo).run("log", "-1", "forged")

    assert code == 0 and out.startswith("commit ") and "forged" in out
    assert not marker.exists(), "VOOL's log ran the user's gpg for the repository's format.pretty"


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


# ── Fresh review of the whole follow-up, 20 Oct ───────────────────────────────────────────────


def _commit_tree_on(repo: Path, branch: str, entries: list[tuple[str, str, bytes]]) -> None:
    # A commit on `branch` (parent HEAD) whose tree adds `entries` (mode, name, content) to HEAD's,
    # built with plumbing so a symlink or an odd case needs no support from the file system.
    def blob(content: bytes) -> str:
        return subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content, check=True,
                              capture_output=True).stdout.decode().strip()
    listing = _git(repo, "ls-tree", "HEAD")
    listing += "".join(f"{mode} blob {blob(content)}\t{name}\n" for mode, name, content in entries)
    tree = subprocess.run(["git", "-C", str(repo), "mktree"], input=listing.encode(), check=True,
                          capture_output=True).stdout.decode().strip()  # bytes: no "\r\n" on Windows
    commit = _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "planted").strip()
    _git(repo, "branch", branch, commit)


@_NEEDS_LFS
def test_control_git_lfs_goes_to_a_url_the_lfsconfig_picks_as_its_remote(tmp_path: Path, monkeypatch,
                                                                         lfs_server) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _commit_lfsconfig(repo, f"http://127.0.0.1:{port}/picked.git", key="remote.lfsdefault")

    subprocess.run(safe_git.hardened_argv(repo, "cherry-pick", "lfs"), capture_output=True, timeout=120,
                   env=safe_git.safe_env())

    assert any("/picked.git/" in hit for hit in hits), f"git-lfs did not use remote.lfsdefault; the case below proves nothing: {hits}"


@_NEEDS_LFS
def test_a_url_picked_as_the_lfs_remote_is_refused_and_never_contacted(tmp_path: Path, monkeypatch,
                                                                       lfs_server) -> None:
    port, hits = lfs_server
    repo = _lfs_repo(tmp_path, monkeypatch, port)
    _commit_lfsconfig(repo, f"http://127.0.0.1:{port}/picked.git", key="remote.lfsdefault")
    hits.clear()

    result = GitRunner(root=repo).cherry_pick("lfs")

    assert result.ok is False, result.to_dict()
    assert "Git LFS would contact 127.0.0.1 (remote.lfsdefault in the work tree's .lfsconfig)" in result.stderr, result.stderr
    assert hits == []


def test_a_branchs_remote_that_is_a_url_counts_as_a_named_lfs_server(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _git(repo, "branch", "other")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD").strip()
    _git(repo, "config", f"branch.{branch}.remote", "http://127.0.0.2:9/x.git")

    refusal = safe_git.lfs_endpoint_refusal(repo, ("merge", "other"))

    assert refusal.startswith(f"Git LFS would contact 127.0.0.2 (branch.{branch}.remote, set in .git/config)"), refusal


def test_picking_one_of_the_projects_remotes_by_name_is_allowed(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _git(repo, "branch", "other")
    subprocess.run(["git", "config", "-f", str(repo / ".lfsconfig"), "remote.lfsdefault", "origin"], check=True,
                   capture_output=True)

    assert safe_git.lfs_endpoint_refusal(repo, ("merge", "other")) == ""


@pytest.mark.parametrize("argv", [("merge", "side"), ("cherry-pick", "side"), ("switch", "side")])
def test_an_lfsconfig_that_is_a_symlink_is_refused(tmp_path: Path, argv: tuple[str, ...]) -> None:
    # git-lfs follows the link once it is checked out; the link's own text ("#x") reads as an
    # empty config, so it must not be parsed in its place.
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _commit_tree_on(repo, "side", [("120000", ".lfsconfig", b"#x"),
                                   ("100644", "#x", b"[lfs]\n\turl = http://127.0.0.4:9/e\n")])

    refusal = safe_git.lfs_endpoint_refusal(repo, argv)

    assert refusal.startswith("the repository's Git LFS servers could not be checked"), refusal


def test_an_lfsconfig_spelled_in_another_case_is_read(tmp_path: Path) -> None:
    # On Windows and macOS git writes .LFSConfig and git-lfs then opens it as .lfsconfig.
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _commit_tree_on(repo, "side", [("100644", ".LFSConfig", b"[lfs]\n\turl = http://127.0.0.5:9/e\n")])

    refusal = safe_git.lfs_endpoint_refusal(repo, ("cherry-pick", "side"))

    assert refusal.startswith("Git LFS would contact 127.0.0.5 (lfs.url in the .LFSConfig in side)"), refusal


@pytest.mark.parametrize("argv", [("commit", "-m", "x"), ("add", "-A"), ("tag", "-a", "v1", "-m", "v1")])
def test_commands_that_never_download_lfs_files_are_not_held_up_by_a_named_server(tmp_path: Path,
                                                                                  argv: tuple[str, ...]) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _commit_lfsconfig(repo, "https://lfs.elsewhere.test/x")

    assert safe_git.lfs_endpoint_refusal(repo, argv) == ""
    assert safe_git.lfs_endpoint_refusal(repo, ("switch", "-c", "y")).startswith("Git LFS would contact lfs.elsewhere.test")


def test_reverting_a_merge_against_its_second_parent_checks_what_that_parent_brings(tmp_path: Path) -> None:
    # M merges "side" (which adds an .lfsconfig) keeping only HEAD's tree. Reverting M against
    # parent 2 brings side's .lfsconfig in; against parent 1 it changes nothing.
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    _git(repo, "switch", "-q", "-c", "side")
    _commit_lfsconfig(repo, "http://127.0.0.6:9/e")
    _git(repo, "switch", "-q", "-")
    _git(repo, "merge", "-q", "--no-ff", "-s", "ours", "-m", "M", "side")

    refusal = safe_git.lfs_endpoint_refusal(repo, ("revert", "-m", "2", "--no-edit", "HEAD"))

    assert "Git LFS would contact 127.0.0.6" in refusal and "revert would write" in refusal, refusal
    assert safe_git.lfs_endpoint_refusal(repo, ("revert", "-m", "1", "--no-edit", "HEAD")) == ""
    unreadable = safe_git.lfs_endpoint_refusal(repo, ("revert", "--mainline=two", "HEAD"))
    assert unreadable.startswith("the repository's Git LFS servers could not be checked"), unreadable


@pytest.mark.parametrize("argv", [("log", "-1", "--format=%G?"), ("show", "-s", "--pretty=format:%GS"),
                                  ("rev-list", "--format=%GK", "HEAD"), ("for-each-ref", "--format=%(signature)")])
def test_format_placeholders_that_check_signatures_keep_the_signing_keys(tmp_path: Path,
                                                                         argv: tuple[str, ...]) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "gpg.format", "x509")

    assert safe_git.executable_config_for(repo, argv) == ["gpg.format (set in .git/config)"], argv


# ── Second review, 20 Oct ──────────────────────────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    repo, _marker = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://git.example.test/project.git")
    return repo


@pytest.mark.parametrize("directory_renames", ["true", "conflict"])
def test_an_lfsconfig_a_folder_rename_moves_to_the_top_is_checked(tmp_path: Path, directory_renames: str) -> None:
    # The other side moves sub/'s files to the top; this side adds sub/.lfsconfig, which git then
    # moves along. Neither side holds a top-level .lfsconfig.
    repo = _project(tmp_path)
    (repo / "sub").mkdir()
    for name in ("a", "b", "c"):
        (repo / "sub" / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "sub")
    _git(repo, "commit", "-q", "-m", "sub")
    _git(repo, "switch", "-q", "-c", "theirs")
    _git(repo, "mv", "sub/a", "sub/b", "sub/c", ".")
    _git(repo, "commit", "-q", "-m", "flatten")
    _git(repo, "switch", "-q", "-")
    subprocess.run(["git", "config", "-f", str(repo / "sub" / ".lfsconfig"), "lfs.url", "http://127.0.0.7:9/e"],
                   check=True, capture_output=True)
    _git(repo, "add", "sub/.lfsconfig")
    _git(repo, "commit", "-q", "-m", "nested lfsconfig")
    _git(repo, "config", "merge.directoryRenames", directory_renames)

    refusal = safe_git.lfs_endpoint_refusal(repo, ("merge", "--no-ff", "--no-edit", "theirs"))

    assert refusal.startswith("Git LFS would contact 127.0.0.7") and "merge would write" in refusal, refusal


def _side_with_lfsconfig(repo: Path) -> None:
    # Branch "side" carries an .lfsconfig naming another server; HEAD is back where it was, so
    # "-" (the previous branch) is side.
    _git(repo, "switch", "-q", "-c", "side")
    _commit_lfsconfig(repo, "http://127.0.0.8:9/e")
    _git(repo, "switch", "-q", "-")


@pytest.mark.parametrize("argv", [("merge", "--no-ff", "--no-edit", "-"), ("cherry-pick", "--no-rerere-autoupdate", "-"),
                                  ("switch", "-c", "m", "-"), ("switch", "-c", "n", ":/LFS server")])
def test_a_commit_named_by_shorthand_is_resolved_and_checked(tmp_path: Path, argv: tuple[str, ...]) -> None:
    repo = _project(tmp_path)
    _side_with_lfsconfig(repo)

    refusal = safe_git.lfs_endpoint_refusal(repo, argv)

    assert refusal.startswith("Git LFS would contact 127.0.0.8"), refusal


@pytest.mark.parametrize("argv", [("merge", "--no-ff", "--no-edit", "--stat"), ("cherry-pick", "--continue"),
                                  ("merge", "no-such-branch"), ("switch", "-c", "n", "no-such-branch")])
def test_a_command_whose_incoming_commit_cannot_be_named_is_refused(tmp_path: Path, argv: tuple[str, ...]) -> None:
    repo = _project(tmp_path)
    _side_with_lfsconfig(repo)

    refusal = safe_git.lfs_endpoint_refusal(repo, argv)

    assert refusal.startswith("the repository's Git LFS servers could not be checked"), refusal


@pytest.mark.parametrize("key", ["http.proxy", "http.https://git.example.test/.proxy"])
def test_a_proxy_the_repository_sets_does_not_refuse_ordinary_work(tmp_path: Path, key: str) -> None:
    # A per-repository proxy is an ordinary corporate setup, and git's own fetch and push use it
    # too; it is not an LFS server the repository picked.
    repo = _project(tmp_path)
    _git(repo, "branch", "other")
    _git(repo, "config", key, "http://proxy.corp.test:3128")

    assert safe_git.lfs_endpoint_refusal(repo, ("merge", "other")) == ""
    assert safe_git.lfs_endpoint_refusal(repo, ("switch", "-c", "p")) == ""


def test_the_users_own_proxy_is_not_held_against_the_repository(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "config", "-f", str(home / ".gitconfig"), "http.proxy", "http://proxy.corp.test:3128"],
                   check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    repo = _project(tmp_path)
    _git(repo, "branch", "other")

    assert safe_git.lfs_endpoint_refusal(repo, ("merge", "other")) == ""


@pytest.mark.parametrize("argv, state", [(("cherry-pick", "--abort"), "ORIG_HEAD"),
                                         (("revert", "--abort"), "sequencer/head"),
                                         (("rebase", "--abort"), "rebase-merge/orig-head")])
def test_an_abort_checks_the_commit_it_goes_back_to(tmp_path: Path, argv: tuple[str, ...], state: str) -> None:
    repo = _project(tmp_path)
    _side_with_lfsconfig(repo)
    if "/" in state:
        (repo / ".git" / state).parent.mkdir(parents=True, exist_ok=True)
        (repo / ".git" / state).write_text(_git(repo, "rev-parse", "side"), encoding="utf-8")
    else:
        _git(repo, "update-ref", state, "side")

    refusal = safe_git.lfs_endpoint_refusal(repo, argv)

    assert refusal.startswith(f"Git LFS would contact 127.0.0.8 (lfs.url in the .lfsconfig in {state})"), refusal


def test_a_project_lfsconfig_is_read_the_same_from_a_subfolder(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    _commit_lfsconfig(repo, "https://git.example.test/project.git/info/lfs")
    (repo / "deeper").mkdir()

    assert safe_git.lfs_endpoint_refusal(repo / "deeper", ("switch", "-c", "x")) == ""


# ── Third review, 20 Oct ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pick", [".", "gone"])
def test_a_branch_that_tracks_a_local_branch_or_a_remote_without_a_url_is_not_a_server(tmp_path: Path,
                                                                                       pick: str) -> None:
    # `git branch --track feat main` writes branch.feat.remote=".", which reaches no server.
    repo = _project(tmp_path)
    _git(repo, "branch", "other")
    _git(repo, "config", "branch.other.remote", pick)

    assert safe_git.lfs_endpoint_refusal(repo, ("merge", "--no-ff", "--no-edit", "other")) == ""
    assert safe_git.lfs_endpoint_refusal(repo, ("switch", "-c", "n")) == ""


def test_a_branchs_remote_that_is_a_path_elsewhere_is_refused(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    _git(repo, "branch", "other")
    _git(repo, "config", "branch.other.remote", str(tmp_path / "elsewhere.git"))

    refusal = safe_git.lfs_endpoint_refusal(repo, ("merge", "other"))

    assert refusal.startswith("Git LFS would contact ") and "branch.other.remote" in refusal, refusal


@pytest.mark.parametrize("target", ["FETCH_HEAD", "fetch_head"])
def test_merging_fetch_head_is_refused_because_it_can_merge_several_heads(tmp_path: Path, target: str) -> None:
    repo = _project(tmp_path)
    _side_with_lfsconfig(repo)
    (repo / ".git" / "FETCH_HEAD").write_text(f"{_git(repo, 'rev-parse', 'HEAD').strip()}\t\tbranch 'a'\n",
                                              encoding="utf-8")

    refusal = safe_git.lfs_endpoint_refusal(repo, ("merge", "--no-ff", "--no-edit", target))

    assert refusal.startswith("the repository's Git LFS servers could not be checked"), refusal


def test_file_names_that_are_not_utf8_do_not_refuse_an_ordinary_merge(tmp_path: Path) -> None:
    # git stores names as bytes; Windows decodes undeclared output in its ANSI code page.
    repo = _project(tmp_path)
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"x\n", check=True,
                          capture_output=True).stdout.strip()
    listing = subprocess.run(["git", "-C", str(repo), "ls-tree", "HEAD"], check=True, capture_output=True).stdout
    for name in ("n\xe9.txt".encode("latin-1"), "окс.txt".encode()):
        listing += b"100644 blob " + blob + b"\t" + name + b"\n"
    tree = subprocess.run(["git", "-C", str(repo), "mktree"], input=listing, check=True,
                          capture_output=True).stdout.decode().strip()
    _git(repo, "branch", "names", _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "names").strip())
    _commit_lfsconfig(repo, "https://git.example.test/project.git/info/lfs")

    assert safe_git.lfs_endpoint_refusal(repo, ("merge", "--no-ff", "--no-edit", "names")) == ""
    assert safe_git.lfs_endpoint_refusal(repo, ("switch", "names")) == ""


# ── Final review, 20 Oct ───────────────────────────────────────────────────────────────────────


def test_a_misspelled_commit_says_which_name_could_not_be_read(tmp_path: Path) -> None:
    repo = _project(tmp_path)

    refusal = safe_git.lfs_endpoint_refusal(repo, ("merge", "nosuch"))

    assert refusal == ("the repository's Git LFS servers could not be checked ('nosuch' is not a revision "
                       "VOOL can check)"), refusal


def test_a_remote_whose_name_has_capitals_is_picked_by_name(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    _git(repo, "remote", "add", "Fork/X", "https://git.example.test/fork.git")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD").strip()
    _git(repo, "config", f"branch.{branch}.remote", "Fork/X")
    _commit_lfsconfig(repo, "https://git.example.test/project.git/info/lfs")

    assert safe_git.lfs_endpoint_refusal(repo, ("switch", "-c", "n")) == ""


def test_reading_a_remotes_url_ignores_the_signing_keys(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    _git(repo, "config", "commit.gpgSign", "true")

    assert safe_git.executable_config_for(repo, ("remote", "get-url", "origin")) == []
