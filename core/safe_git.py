"""Run git on a repository VOOL did not author without letting that repository run programs.

A repository's own local config can make git execute arbitrary programs: ``core.fsmonitor`` runs
on ``git status``, ``filter.<x>.clean/process`` runs when status compares a racily-clean file,
``diff.<x>.textconv/command`` and ``diff.external`` run on ``git diff``, a local-path remote's
``uploadpack``/``receivepack`` runs on fetch/push, and hooks such as ``post-index-change`` run
when status refreshes the index. Reproduced 2026-10-18: VOOL's ``workspace.git_status`` ran a
program named by a repo's ``core.fsmonitor``, with no sandbox and no approval, on Windows and on
every other platform.

Three layers:
- every invocation neutralises what can be overridden from the command line (no optional locks,
  so no index refresh or hooks; fsmonitor off; hooks pointed at a path that does not exist; no
  external diff or textconv; a plain pager);
- every invocation runs with a scrubbed environment: no inherited ``GIT_*`` variable except the
  author/committer identity, so ``GIT_CONFIG_COUNT``/``GIT_CONFIG_PARAMETERS`` cannot inject a
  program and ``GIT_DIR``/``GIT_INDEX_FILE`` cannot redirect git away from the checked repository;
- a repository whose own config (includes expanded) defines any key of a program-running KIND
  (by its final name segment, so new keys of a known kind are caught too) is refused before git
  touches it. "Its own config" is every scope git reports other than system and global: the local
  ``config`` and, when ``extensions.worktreeConfig`` is on, the worktree's ``config.worktree``.
  Reproduced 2026-10-19: a ``filter.<x>.clean`` set with ``git config --worktree`` passed a
  ``--local``-only check and ran during a repo.git commit, on every platform. Any scope git may
  add later is checked too (fail closed). Each checked-out submodule's own config is checked the
  same way, since git runs inside those submodules too (also reproduced 2026-10-19); a submodule
  with no checkout runs nothing because hardened_argv() keeps git from diffing in or rebuilding from its git
  folder.
System and global config belong to the user (for example Git LFS's global filters) and are not
second-guessed. `alias.*` is not refused: an alias runs only when invoked by name, and VOOL
never invokes one. repo.push is VOOL's one transport command; safe_env() also drops
GIT_SSH/GIT_SSH_COMMAND. Not closed here: a push to a local-path remote runs that other
repository's receive hooks (git clears the -c hardening for its local receive-pack); tracked in
the TDL. Residual, documented rather than claimed closed: a process editing .git/config
between the check and the run (the same check/use class as the Windows filesystem race).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

#: Final key segments (lowercase) whose value git runs as a program or shell command.
_EXECUTABLE_KEY_SUFFIXES = frozenset({
    "command",            # diff.<x>.command, core.sshCommand, core.alternateRefsCommand, ...
    "sshcommand", "alternaterefscommand", "gitproxy",
    "program",            # gpg.program, gpg.<fmt>.program
    "editor",             # core.editor, sequence.editor
    "askpass",            # core.askPass
    "pager",              # core.pager, pager.<cmd>
    "external",           # diff.external
    "textconv",
    "clean", "smudge", "process",   # filter.<x>.*
    "driver",             # merge.<x>.driver
    "helper",             # credential.helper, credential.<url>.helper
    "uploadpack", "receivepack",    # remote.<x>.*: run locally for local-path remotes
    "fsmonitor", "hookspath",
    "cmd",                # difftool/mergetool/browser/man.<x>.cmd
    "vcs",                # remote.<x>.vcs: runs git-remote-<vcs>
    "difffilter",         # interactive.diffFilter
})
#: Final-segment endings that name a program however the key is spelled before them:
#: gpg.ssh.defaultKeyCommand, gc.recentObjectsHook (runs during a commit's auto gc),
#: uploadpack.packObjectsHook (runs when a local clone or fetch serves this repository),
#: sendemail.<x>Cmd.
_EXECUTABLE_KEY_ENDINGS = ("command", "cmd", "hook")
#: Whole keys (fnmatch, lowercase) that pull in configuration git would then obey.
_EXECUTABLE_KEY_PATTERNS = ("include.path", "includeif.*.path", "pager.*", "submodule.*.update",
                            "protocol.allow", "protocol.*.allow", "browser.*.path", "man.*.path",
                            "difftool.*.path", "mergetool.*.path",
                            # Signing or signature checks switched on make git run gpg, gpgsm or
                            # ssh-keygen without the repository naming a program, and gpg.format picks
                            # which; only the values in _SAFE_VALUES pass.
                            "commit.gpgsign", "tag.gpgsign", "tag.forcesignannotated", "push.gpgsign",
                            "log.showsignature", "merge.verifysignatures", "gpg.format",
                            # Git LFS runs a custom transfer agent's program for push and fetch.
                            "lfs.customtransfer.*", "lfs.standalonetransferagent",
                            # Config-defined hooks (hook.<name>.command, .event, .enabled; git 2.5x):
                            # hardening's core.hooksPath does not reach them.
                            "hook.*")
#: Inherited environment variables git may keep: identity only.
_ALLOWED_GIT_ENV = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})


def is_executable_key(key: str) -> bool:
    lowered = key.strip().lower()
    last = lowered.rsplit(".", 1)[-1]
    if last in _EXECUTABLE_KEY_SUFFIXES:
        return True
    if last.endswith(_EXECUTABLE_KEY_ENDINGS) and not lowered.startswith(("advice.", "alias.")):
        return True  # advice.<x>Hook only toggles a message; alias.<x> is a name VOOL never invokes
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _EXECUTABLE_KEY_PATTERNS)


def safe_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a hardened git child: inherited GIT_* scrubbed except identity, and git
    never prompts on a terminal."""
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items()
           if not key.upper().startswith("GIT_") or key.upper() in _ALLOWED_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def hardening_args() -> list[str]:
    """Global git options that keep a command from running repository-chosen programs. Run diff,
    log, show, status and commit through hardened_argv(), which adds the submodule settings these
    lack; ``git apply`` never diffs a submodule and may use these alone."""
    return [
        "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "diff.external=",
        "-c", "core.pager=cat",
    ]


def executable_local_config(cwd: Path, *, timeout: float = 20) -> list[str]:
    """The program-running keys the repository's own config defines (empty when none).

    "Own" covers every config scope git reads here except the user's system and global files, so
    a key set in ``config.worktree`` is caught exactly like one in ``config``. It also covers each
    checked-out submodule's config, reported as ``<submodule path>:<key>``: status, diff and commit
    in the superproject run git inside every checked-out submodule, and that git obeys the
    submodule's own config."""
    return sorted({key for key, _ in _executable_config(Path(cwd), timeout=timeout, depth=0)})


def executable_config_for(cwd: Path, argv: Sequence[str], *, timeout: float = 20) -> list[str]:
    """The program-running keys that matter for running ``git <argv>`` here (empty when none), each
    as ``key (where it is set)`` for a refusal message.

    Like executable_local_config, except that the signing and signature-check switches
    (_SAFE_VALUES) are left out when ``argv`` can neither sign nor verify: a repository that signs
    its own commits can still be read. Any other command, and an empty ``argv``, keeps them."""
    signs = _may_sign_or_verify(argv)
    found = {f"{key} ({_where(scope)})" for key, scope in _executable_config(Path(cwd), timeout=timeout, depth=0)
             if signs or key.rpartition(":")[2] not in _SAFE_VALUES}
    return sorted(found)


#: Subcommands that never sign or check a signature unless an option asks for it.
_SIGNING_FREE_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "for-each-ref", "rev-list", "merge-base",
    "cat-file", "describe", "symbolic-ref", "config", "branch",
})
#: Options that make one of those sign or verify after all (log/show --show-signature, branch has none).
_SIGNING_OPTIONS = ("--show-signature", "--gpg-sign", "--verify-signatures", "-S")


def _may_sign_or_verify(argv: Sequence[str]) -> bool:
    if not argv or argv[0] not in _SIGNING_FREE_SUBCOMMANDS:
        return True
    return any(arg == option or arg.startswith(option) for arg in argv[1:] for option in _SIGNING_OPTIONS)


def _where(scope: str) -> str:
    return {"local": "set in .git/config", "worktree": "set in config.worktree",
            "command": "set by a -c option"}.get(scope, f"set in git's {scope} config")


#: Nested submodules deeper than this are refused rather than checked.
_MAX_SUBMODULE_DEPTH = 8


def _executable_config(cwd: Path, *, timeout: float, depth: int) -> list[tuple[str, str]]:
    """``(key, scope)`` of each program-running entry, submodule keys as ``<path>:<key>``."""
    # Fails outside a repository, which the callers already treat as "not checked".
    inside_work_tree = _rev_parse(cwd, "--is-inside-work-tree", timeout=timeout) == "true"
    scoped = _config_entries(cwd, ["--show-scope"], timeout=timeout)
    if len(scoped) % 2:
        raise RuntimeError("git config scopes could not be read")
    keys = []
    for index in range(0, len(scoped) - 1, 2):
        (scope, _), (key, value) = scoped[index], scoped[index + 1]
        if scope in _USER_CONFIG_SCOPES or not is_executable_key(key):
            continue
        if is_safe_value(key, value):
            continue  # signing switched off, or the default format, runs nothing
        keys.append((key, scope))
    # Bare, or inside the git directory: there is no work tree, so git recurses into no submodule.
    submodules = _checked_out_submodules(cwd, timeout=timeout) if inside_work_tree else []
    for path, submodule in submodules:
        if depth >= _MAX_SUBMODULE_DEPTH:
            raise RuntimeError("submodules are nested too deeply, or a submodule's git folder does not resolve")
        keys.extend((f"{path}:{key}", scope)
                    for key, scope in _executable_config(submodule, timeout=timeout, depth=depth + 1))
    return keys


def _checked_out_submodules(cwd: Path, *, timeout: float) -> list[tuple[str, Path]]:
    """``(path, folder)`` of each gitlink in the index whose folder holds a checkout (git runs
    status and diff inside exactly those; a gitlink without one is covered by hardened_argv's
    ``diff.submodule=short``). ``cwd`` must be inside a work tree."""
    listing = subprocess.run(hardened_argv(cwd, "ls-files", "--stage", "--full-name", "-z", "--", ":/"),
                             capture_output=True, timeout=timeout, env=safe_env())
    if listing.returncode != 0:
        raise RuntimeError(f"git index could not be read ({listing.returncode})")
    gitlinks = []
    for entry in listing.stdout.split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if meta.startswith(b"160000 "):
            gitlinks.append(name.decode("utf-8", "surrogateescape"))
    if not gitlinks:
        return []
    toplevel = Path(_rev_parse(cwd, "--show-toplevel", timeout=timeout))
    found = []
    for path in gitlinks:
        folder = toplevel / path
        if (folder / ".git").exists():
            found.append((path, folder))
    return found


def _rev_parse(cwd: Path, option: str, *, timeout: float) -> str:
    probe = subprocess.run(hardened_argv(cwd, "rev-parse", option), capture_output=True, timeout=timeout,
                           env=safe_env())
    answer = probe.stdout.decode("utf-8", "surrogateescape").strip()
    if probe.returncode != 0 or not answer:
        raise RuntimeError(f"git rev-parse {option} failed ({probe.returncode})")
    return answer


_DEFINITE_FALSE = frozenset({"false", "no", "off", "0"})


def _definitely_false(value: str) -> bool:
    return value.lower() in _DEFINITE_FALSE


#: Keys that make git run a signing or verifying program without naming one, and the values that run
#: nothing. Each value set in a repository-owned scope must pass, or the repository is refused: a bare
#: key (true), an empty value and anything else fail closed. ``gpg.format`` passes only as git's
#: default, so a repository cannot swap the user's own signing program for gpgsm or ssh-keygen.
_SAFE_VALUES = {
    **dict.fromkeys(("commit.gpgsign", "tag.gpgsign", "tag.forcesignannotated", "push.gpgsign",
                     "log.showsignature", "merge.verifysignatures"), _definitely_false),
    "gpg.format": lambda value: value == "openpgp",
}


def is_safe_value(key: str, value: str | None) -> bool:
    """Whether ``key`` set to ``value`` runs nothing although the key can (a signing switch that is a
    definite false, ``gpg.format=openpgp``). A key set without a value is never safe."""
    test = _SAFE_VALUES.get(key.strip().lower())
    return test is not None and value is not None and test(value)


#: Config scopes that belong to the user, not to the repository (``git config --show-scope``).
_USER_CONFIG_SCOPES = frozenset({"system", "global"})


def _config_entries(cwd: Path, options: list[str], *, timeout: float) -> list[tuple[str, str | None]]:
    """``git config --list --includes --null`` with ``options``, split into ``(lowercase name, value)``
    fields; the value is None for a key set without one. With ``--show-scope`` each entry is a scope
    field followed by its key field."""
    probe = subprocess.run(
        ["git", "-C", str(cwd), "config", *options, "--includes", "--null", "--list"],
        capture_output=True, timeout=timeout, env=safe_env(),
    )
    if probe.returncode not in (0, 1):  # 1: no entries at all
        raise RuntimeError(f"git config could not be read ({probe.returncode})")
    fields = []
    for entry in probe.stdout.split(b"\0"):
        if entry:
            name, newline, value = entry.partition(b"\n")
            fields.append((name.decode("utf-8", "replace").strip().lower(),
                           value.decode("utf-8", "replace") if newline else None))
    return fields


def lfs_endpoint_refusal(cwd: Path, argv: Sequence[str] = (), *, timeout: float = 20) -> str:
    """Why Git LFS must not run for ``git <argv>`` here ("" when it may): the repository names an LFS
    server that is neither this project's git host nor one the user named.

    The user's own git-lfs (their global ``filter.lfs.*``) downloads objects whenever git writes
    LFS files into the work tree, and asks the server the repository picks: ``lfs.url``,
    ``lfs.pushurl`` or ``remote.<x>.lfs(push)url`` from the repository's own config or from the
    ``.lfsconfig`` it carries, in the work tree, the index, HEAD or a commit ``argv`` brings in.
    Trusted are the hosts of this repository's remotes (where git-lfs goes when nothing is named)
    and hosts in the user's system or global LFS settings; URLs are compared after git's
    ``insteadOf`` rewriting, by host. A trusted server can still point git-lfs at storage
    elsewhere; that is the server's choice, not the repository's. A check that cannot finish
    refuses."""
    try:
        named: list[tuple[str, str]] = []
        trusted: set[str] = set()
        remote_urls: list[str] = []
        scoped = _config_entries(cwd, ["--show-scope"], timeout=timeout)
        if len(scoped) % 2:
            raise RuntimeError("git config scopes could not be read")
        for index in range(0, len(scoped) - 1, 2):
            (scope, _), (key, value) = scoped[index], scoped[index + 1]
            user = scope in _USER_CONFIG_SCOPES
            if _is_lfs_endpoint_key(key) and value:
                if user:
                    trusted.add(_endpoint_host(_expanded_url(cwd, value, timeout=timeout)))
                else:
                    named.append((value, f"{key}, {_where(scope)}"))
            elif user and key.startswith("lfs.") and "://" in key:
                trusted.add(_endpoint_host(key[len("lfs."):].rpartition(".")[0]))  # lfs.<url>.<setting>
            elif fnmatch.fnmatchcase(key, "remote.*.url") or fnmatch.fnmatchcase(key, "remote.*.pushurl"):
                if value:
                    remote_urls.append(value)
        for source, entries in _lfsconfig_files(cwd, argv, timeout=timeout):
            named.extend((value, f"{key} in {source}") for key, value in entries
                         if _is_lfs_endpoint_key(key) and value)
        if not named:
            return ""
        trusted.update(_endpoint_host(_expanded_url(cwd, url, timeout=timeout)) for url in remote_urls)
        trusted.discard("")
        for url, source in named:
            host = _endpoint_host(_expanded_url(cwd, url, timeout=timeout))
            if not host or host not in trusted:
                shown = host.partition(":")[2] if host.startswith("path:") else host
                return (f"Git LFS would contact {shown or 'a server VOOL cannot read'} ({source}), which is not "
                        "this project's git host or a server in your own git settings")
        return ""
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return f"the repository's Git LFS servers could not be checked ({type(exc).__name__})"


def _is_lfs_endpoint_key(key: str) -> bool:
    return key in {"lfs.url", "lfs.pushurl"} or any(
        fnmatch.fnmatchcase(key, pattern) for pattern in ("remote.*.lfsurl", "remote.*.lfspushurl"))


def _lfsconfig_files(cwd: Path, argv: Sequence[str], *, timeout: float) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """``(where, entries)`` for each ``.lfsconfig`` git-lfs could read for ``git <argv>``: the work
    tree's file, the index's, HEAD's, and that of each argument that names a commit."""
    found: list[tuple[str, list[tuple[str, str | None]]]] = []
    if _rev_parse(cwd, "--is-inside-work-tree", timeout=timeout) == "true":
        worktree_file = Path(_rev_parse(cwd, "--show-toplevel", timeout=timeout)) / ".lfsconfig"
        if worktree_file.is_file():
            found.append(("the work tree's .lfsconfig", _config_entries(cwd, ["-f", str(worktree_file)],
                                                                        timeout=timeout)))
    specs = {":.lfsconfig": "the index's .lfsconfig", "HEAD:.lfsconfig": "HEAD's .lfsconfig"}
    for arg in list(argv)[1:]:
        if arg and not arg.startswith("-") and "\n" not in arg:
            specs.setdefault(f"{arg}:.lfsconfig", f"the .lfsconfig in {arg}")
    probe = subprocess.run(hardened_argv(cwd, "cat-file", "--batch-check"), input="".join(f"{spec}\n" for spec in specs),
                           capture_output=True, text=True, timeout=timeout, env=safe_env())
    if probe.returncode != 0:
        raise RuntimeError(f"git cat-file failed ({probe.returncode})")
    for (spec, where), line in zip(specs.items(), probe.stdout.splitlines(), strict=True):
        fields = line.split()
        if len(fields) == 3 and fields[1] == "blob":
            found.append((where, _config_entries(cwd, ["--blob", fields[0]], timeout=timeout)))
        elif not line.endswith(" missing") and not line.endswith(" ambiguous"):
            raise RuntimeError(f"{spec} is not a file git-lfs could read")
    return found


def _expanded_url(cwd: Path, url: str, *, timeout: float) -> str:
    """``url`` after git's ``url.<base>.insteadOf`` rewriting (which git-lfs applies too)."""
    if "://" not in url and not _scp_like(url):
        return url  # a local path; a bare word would be read as a remote's name
    probe = subprocess.run(hardened_argv(cwd, "ls-remote", "--get-url", url), capture_output=True, text=True,
                           timeout=timeout, env=safe_env())
    expanded = probe.stdout.strip()
    if probe.returncode != 0 or not expanded:
        raise RuntimeError(f"git ls-remote --get-url failed ({probe.returncode})")
    return expanded


def _scp_like(url: str) -> bool:
    head, colon, _ = url.partition(":")
    return bool(colon) and "/" not in head and len(head) > 1


def _endpoint_host(url: str) -> str:
    """The lowercase host ``url`` reaches, or ``path:<url>`` for a local path or ``file://`` URL."""
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme.lower() == "file":
            return f"path:{url}"
        return (parts.hostname or "").rstrip(".")
    if _scp_like(url):
        return url.partition(":")[0].rpartition("@")[2].strip("[]").lower().rstrip(".")
    return f"path:{url}"


def active_hooks(cwd: Path, *, timeout: float = 20) -> list[str]:
    """Hooks git would run here without hardening (the repository's own, or the user's
    core.hooksPath). Hardening disables them; a mutation that would have run one is refused
    instead, so a pre-commit check is never skipped silently."""
    probe = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(cwd), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True, timeout=timeout, env=safe_env(),
    )
    if probe.returncode != 0:
        raise RuntimeError(f"git hooks directory could not be resolved ({probe.returncode})")
    hooks = Path(probe.stdout.strip())
    if not hooks.is_absolute():
        hooks = Path(cwd) / hooks
    if not hooks.is_dir():
        return []
    return sorted(entry.name for entry in hooks.iterdir() if entry.is_file() and not entry.name.endswith(".sample"))


def hardened_argv(cwd: Path, *argv: str) -> list[str]:
    """A hardened ``git -C <cwd> <argv>``. Diff-family commands also get ``--no-ext-diff --no-textconv``.
    Run it with ``env=safe_env()``."""
    # A changed gitlink whose folder is missing makes `diff.submodule=diff` (the user's or the
    # repository's) run a child diff in .git/modules/<name>, under that folder's own config and
    # attributes, out of reach of --no-ext-diff. `short` and no status summary run nothing there.
    # `submodule.recurse` would make switch/checkout/merge rebuild such a submodule from that folder
    # (running its filters), and `log.showSignature` would make log run the user's gpg on a forged
    # signature the repository planted.
    command = ["git", *hardening_args(), "-c", "diff.submodule=short", "-c", "status.submoduleSummary=false",
               "-c", "submodule.recurse=false", "-c", "log.showSignature=false", "-C", str(cwd), *argv]
    if argv and argv[0] in {"diff", "log", "show"}:
        command[len(command) - len(argv) + 1:len(command) - len(argv) + 1] = ["--no-ext-diff", "--no-textconv"]
    return command


read_only_argv = hardened_argv


__all__ = ["active_hooks", "executable_config_for", "executable_local_config", "hardened_argv", "hardening_args",
           "is_executable_key", "is_safe_value", "lfs_endpoint_refusal",
           "read_only_argv", "safe_env"]
