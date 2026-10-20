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
second-guessed. An alias is not refused for a name like ``alias.subcmd``: it runs only when
invoked by name, and VOOL never invokes one (``alias.command``, ``alias.cmd`` and ``alias.<x>.command``
still are). repo.push is VOOL's one transport command; safe_env() also drops GIT_SSH/GIT_SSH_COMMAND. Not closed here: a push to a local-path remote runs that other
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
    lack; ``git apply`` never diffs a submodule and may use these alone.

    VOOL's git also runs no automatic housekeeping: a commit, merge or cherry-pick would otherwise
    start ``git maintenance run --auto`` (``gc --auto`` before git 2.29), whose gc or repack can run
    ``gc.recentObjectsHook``. The user's own git and scheduled maintenance are unaffected."""
    return [
        "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "diff.external=",
        "-c", "core.pager=cat",
        "-c", "maintenance.auto=false",
        "-c", "gc.auto=0",
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
    found = {f"{key} ({_where(scope, submodule=':' in key)})"
             for key, scope in _executable_config(Path(cwd), timeout=timeout, depth=0)
             if signs or key.rpartition(":")[2] not in _SAFE_VALUES}
    return sorted(found)


#: Subcommands that never sign or check a signature unless an option asks for it.
_SIGNING_FREE_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "for-each-ref", "rev-list", "merge-base",
    "cat-file", "describe", "symbolic-ref", "config", "branch", "remote",
})
#: Options that make one of those sign or verify after all (log/show --show-signature, branch has none).
_SIGNING_OPTIONS = ("--show-signature", "--gpg-sign", "--verify-signatures", "-S")


def _may_sign_or_verify(argv: Sequence[str]) -> bool:
    if not argv or argv[0] not in _SIGNING_FREE_SUBCOMMANDS:
        return True
    # %G<x> (log, show, rev-list) and %(signature...) (for-each-ref) verify signatures as well.
    return any(arg.startswith(_SIGNING_OPTIONS) or "%G" in arg or "%(signature" in arg for arg in argv[1:])


def _where(scope: str, *, submodule: bool = False) -> str:
    owner = "the submodule's " if submodule else ""
    return {"local": f"set in {owner}.git/config", "worktree": f"set in {owner}config.worktree",
            "command": "set by a -c option"}.get(scope, f"set in {owner}git {scope} config")


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
    ``insteadOf`` rewriting, by host. A remote picked by ``remote.lfsdefault``,
    ``branch.<x>.remote`` and the like counts as a named server when it is a URL rather than one of
    the repository's remotes, since git-lfs then contacts that URL. A proxy the repository sets
    (``http.proxy``) is not counted: git's own fetch and push go through it as well. A trusted server can still point
    git-lfs at storage elsewhere; that is the server's choice, not the repository's. Commands that
    never write LFS files into the work tree (add, commit, tag, branch, worktree remove and prune,
    push without git-lfs's hook) are not checked. A check that cannot finish refuses."""
    if argv and (argv[0] in _LFS_FREE_SUBCOMMANDS or tuple(argv[:2]) in _LFS_FREE_WORKTREE_COMMANDS):
        return ""
    try:
        named: list[tuple[str, str]] = []
        trusted: set[str] = set()
        remote_urls: list[str] = []
        picks: list[tuple[str, str, bool]] = []  # (value, source, user's) of keys that pick a remote
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
            elif _is_remote_pick_key(key) and value:
                picks.append((value, f"{key}, {_where(scope)}", user))
            elif user and key.startswith("lfs.") and "://" in key:
                trusted.add(_endpoint_host(key[len("lfs."):].rpartition(".")[0]))  # lfs.<url>.<setting>
            elif fnmatch.fnmatchcase(key, "remote.*.url") or fnmatch.fnmatchcase(key, "remote.*.pushurl"):
                if value:
                    remote_urls.append(value)
        remotes = {key[len("remote."):].rpartition(".")[0] for key, _ in scoped
                   if fnmatch.fnmatchcase(key, "remote.*.url")}
        for source, entries in _lfsconfig_files(cwd, argv, timeout=timeout):
            for key, value in entries:
                if _is_lfs_endpoint_key(key) and value:
                    named.append((value, f"{key} in {source}"))
                elif _is_remote_pick_key(key) and value:
                    picks.append((value, f"{key} in {source}", False))
        for value, source, user in picks:  # a remote's name is fine, and so is a word that names none
            if value.lower() not in remotes and _could_be_a_server(value):  # config names come lowercased
                if user:
                    trusted.add(_endpoint_host(_expanded_url(cwd, value, timeout=timeout)))
                else:
                    named.append((value, source))
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
    except RuntimeError as exc:  # says what could not be read; never a URL
        return f"the repository's Git LFS servers could not be checked ({exc})"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return f"the repository's Git LFS servers could not be checked ({type(exc).__name__})"


#: Subcommands VOOL runs that never write LFS files into the work tree (git-lfs uploads on push
#: only from its pre-push hook, and hooks are refused; branch only moves refs, so a branch carrying
#: a hostile .lfsconfig can still be deleted).
_LFS_FREE_SUBCOMMANDS = frozenset({"add", "commit", "tag", "push", "branch"})
#: Worktree housekeeping that deletes a worktree or its record and never checks one out.
_LFS_FREE_WORKTREE_COMMANDS = frozenset({("worktree", "remove"), ("worktree", "prune")})


def _is_remote_pick_key(key: str) -> bool:
    """Keys whose value names the remote git-lfs talks to (a remote's name, or a URL)."""
    return key in {"remote.lfsdefault", "remote.lfspushdefault", "remote.pushdefault"} or any(
        fnmatch.fnmatchcase(key, pattern) for pattern in ("branch.*.remote", "branch.*.pushremote"))


def _could_be_a_server(value: str) -> bool:
    """Whether a remote pick that is no remote's name reaches somewhere: a URL, an scp-like address
    or a path. ``.`` (a branch tracking a local branch) and other bare words reach nothing."""
    return value != "." and any(mark in value for mark in ("/", "\\", ":"))


def _is_lfs_endpoint_key(key: str) -> bool:
    return key in {"lfs.url", "lfs.pushurl"} or any(
        fnmatch.fnmatchcase(key, pattern) for pattern in ("remote.*.lfsurl", "remote.*.lfspushurl"))


def _lfsconfig_files(cwd: Path, argv: Sequence[str], *, timeout: float) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """``(where, entries)`` for each ``.lfsconfig`` git-lfs could read for ``git <argv>``: the work
    tree's file, the index's, HEAD's, and that of each argument that names a commit. Names are
    matched without regard to case (on Windows and macOS ``.LFSConfig`` is the same file), and one
    that is not a regular file (a symlink git-lfs would follow) is refused."""
    found: list[tuple[str, list[tuple[str, str | None]]]] = []
    if _rev_parse(cwd, "--is-inside-work-tree", timeout=timeout) == "true":
        worktree_file = Path(_rev_parse(cwd, "--show-toplevel", timeout=timeout)) / ".lfsconfig"
        if worktree_file.is_file():
            found.append(("the work tree's .lfsconfig", _config_entries(cwd, ["-f", str(worktree_file)],
                                                                        timeout=timeout)))
        index = subprocess.run(hardened_argv(cwd, "ls-files", "-s", "-z", "--full-name", "--",
                                                     ":(top,icase,literal).lfsconfig"),
                               capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
        if index.returncode != 0:
            raise RuntimeError(f"git ls-files failed ({index.returncode})")
        for line in filter(None, index.stdout.split("\0")):
            info, _, name = line.partition("\t")
            fields = info.split()
            if "/" in name or len(fields) != 3:
                raise RuntimeError("the index's .lfsconfig is not a file git-lfs could read")
            found.append((f"the index's {name}", _lfsconfig_blob(cwd, fields[0], fields[1], timeout=timeout)))
    revisions = {"HEAD": "HEAD's {name}"}
    for arg, commit in _incoming_commits(cwd, argv, timeout=timeout):
        revisions.setdefault(commit, f"the {{name}} in {arg}")
    result = _merge_result_tree(cwd, argv, timeout=timeout)
    if result:  # what the merge, cherry-pick or revert writes, which neither side may hold as such
        revisions[result] = f"the {{name}} {argv[0]} would write"
    for where, mode, oid in _tree_lfsconfigs(cwd, revisions, timeout=timeout):
        found.append((where, _lfsconfig_blob(cwd, mode, oid, timeout=timeout)))
    return found


def _lfsconfig_blob(cwd: Path, mode: str, oid: str, *, timeout: float) -> list[tuple[str, str | None]]:
    if mode not in {"100644", "100755"}:
        raise RuntimeError("a .lfsconfig is not a regular file")
    return _config_entries(cwd, ["--blob", oid], timeout=timeout)


def _tree_lfsconfigs(cwd: Path, revisions: dict[str, str], *, timeout: float) -> list[tuple[str, str, str]]:
    """``(where, mode, oid)`` of every top-level ``.lfsconfig``, in any case, in the tree of each
    revision (``revisions`` maps a revision to a description with a ``{name}`` slot); arguments that
    are not revisions are skipped."""
    probe = subprocess.run(hardened_argv(cwd, "cat-file", "--batch-check"),
                           input="".join(f"{revision}^{{tree}}\n" for revision in revisions),
                           capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
    if probe.returncode != 0:
        raise RuntimeError(f"git cat-file failed ({probe.returncode})")
    found = []
    for (revision, where), line in zip(revisions.items(), probe.stdout.splitlines(), strict=True):
        fields = line.split()
        if line.endswith((" missing", " ambiguous")):
            continue
        if len(fields) != 3 or fields[1] != "tree":
            raise RuntimeError(f"{revision} could not be read")
        listing = subprocess.run(hardened_argv(cwd, "ls-tree", "-z", fields[0]), capture_output=True, encoding="utf-8", errors="surrogateescape",
                                 timeout=timeout, env=safe_env())
        if listing.returncode != 0:
            raise RuntimeError(f"git ls-tree failed ({listing.returncode})")
        for entry in filter(None, listing.stdout.split("\0")):
            info, _, name = entry.partition("\t")
            if name.lower() == ".lfsconfig":
                mode, _, oid = info.split()
                found.append((where.replace("{name}", name), mode, oid))
    return found


#: Options of the commands VOOL runs that take a value (-m is cherry-pick's and revert's mainline,
#: merge's message; -c/-C/--orphan name the branch switch creates, -b/-B the one worktree add does).
_VALUE_OPTIONS = frozenset({"-m", "--message", "--mainline", "-s", "--strategy", "-X", "--strategy-option",
                            "-c", "-C", "--create", "--force-create", "--orphan", "-b", "-B", "--reason"})

#: Commands whose positional arguments are the commits they bring into the work tree.
_BRINGING_COMMANDS = frozenset({"merge", "cherry-pick", "revert", "switch"})

#: Where an in-progress operation keeps the commit --abort goes back to (or an autostash it applies).
_ABORT_STATE = ("ORIG_HEAD", "MERGE_AUTOSTASH", "rebase-merge/orig-head", "rebase-apply/orig-head",
                "rebase-merge/autostash", "rebase-apply/autostash", "sequencer/head")


def _revision_args(argv: Sequence[str]) -> list[str]:
    """The arguments of ``git <argv>`` that may name a revision (not options or their values, nor
    the paths after ``--``; a lone ``-`` is the previous branch)."""
    found, skip_value = [], False
    for arg in list(argv)[1:]:
        if arg == "--" and not skip_value:
            break  # paths follow (checkout -- <path> restores from the index, which is checked anyway)
        if skip_value:
            skip_value = False
        elif arg in _VALUE_OPTIONS:
            skip_value = True
        elif arg == "-" or (arg and not arg.startswith("-")):
            found.append(arg)
    return found


def _resolve_commit(cwd: Path, revision: str, *, timeout: float) -> str:
    probe = subprocess.run(hardened_argv(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                         "@{-1}" if revision == "-" else revision),
                           capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError(f"{revision!r} is not a revision VOOL can check")
    return _rev_parse(cwd, f"{probe.stdout.strip()}^{{commit}}", timeout=timeout)


#: Options with which switch and worktree add read their target as a start point, not a branch.
_START_POINT_OPTIONS = frozenset({"-c", "-C", "--create", "--force-create", "--orphan", "-d", "--detach",
                                  "-b", "-B"})


def _checkout_target_commits(cwd: Path, name: str, *, as_branch: bool, timeout: float) -> list[str]:
    """Every commit ``git switch <name>`` or ``git worktree add <path> <name>`` may check out. git reads
    the name as a local branch first (``rev-parse`` would prefer a tag of the same name, and is
    checked too). As a branch (``as_branch``, not a ``-c``/``--detach`` start point) with no local
    branch of that name, git creates a tracking branch from a remote's, found through each remote's
    fetch refspec, which the repository sets: so every ref ending in ``/<name>`` is checked. A name
    that is none of them refuses."""
    found: list[str] = []
    if name == "-" or name.startswith("@{-"):
        # The previous branch: rev-parse would prefer a tag of its name, git switches to the branch.
        full = subprocess.run(hardened_argv(cwd, "rev-parse", "--symbolic-full-name", "@{-1}" if name == "-" else name),
                              capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
        if full.returncode != 0 or "ambiguous" in full.stderr:  # git names no ref when a tag shares the name
            raise RuntimeError(f"the branch {name!r} names cannot be read")
        if full.stdout.strip().startswith("refs/heads/"):
            found.append(_rev_parse(cwd, f"{full.stdout.strip()}^{{commit}}", timeout=timeout))
    elif not name.startswith("refs/"):
        local = subprocess.run(hardened_argv(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}"),
                               capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
        if local.returncode == 0 and local.stdout.strip():
            found.append(local.stdout.strip())
        elif as_branch:
            listing = subprocess.run(hardened_argv(cwd, "for-each-ref", "--format=%(objectname) %(refname)"),
                                     capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout,
                                     env=safe_env())
            if listing.returncode != 0:
                raise RuntimeError(f"git for-each-ref failed ({listing.returncode})")
            for line in listing.stdout.splitlines():
                oid, _, ref = line.partition(" ")
                if ref.endswith(f"/{name}"):
                    commit = subprocess.run(hardened_argv(cwd, "rev-parse", "--verify", "--quiet", f"{oid}^{{commit}}"),
                                            capture_output=True, encoding="utf-8", errors="surrogateescape",
                                            timeout=timeout, env=safe_env())
                    if commit.returncode == 0 and commit.stdout.strip():  # a tag of a tree is no branch
                        found.append(commit.stdout.strip())
    try:
        found.append(_resolve_commit(cwd, name, timeout=timeout))
    except RuntimeError:
        if not found:
            raise
    return list(dict.fromkeys(found))


def _incoming_commits(cwd: Path, argv: Sequence[str], *, timeout: float) -> list[tuple[str, str]]:
    """``(argument, commit)`` for each commit ``git <argv>`` brings into the work tree. For merge,
    cherry-pick, revert and switch every target must resolve, and one of them without a target
    (merge's upstream, a sequencer step) must be an --abort; --abort brings in the commit the
    operation in progress goes back to and any autostash. Other commands' arguments are taken
    as revisions where they resolve."""
    if not argv:
        return []
    found: list[tuple[str, str]] = []
    if "--abort" in argv[1:]:
        for state in _ABORT_STATE:
            if "/" in state:
                where = subprocess.run(hardened_argv(cwd, "rev-parse", "--git-path", state), capture_output=True,
                                       encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
                if where.returncode != 0 or not where.stdout.strip():
                    raise RuntimeError(f"git rev-parse --git-path failed ({where.returncode})")
                path = Path(where.stdout.strip())
                path = path if path.is_absolute() else Path(cwd) / path
                if path.is_file():
                    found.append((state, _resolve_commit(cwd, path.read_text(encoding="utf-8").strip(),
                                                         timeout=timeout)))
            else:
                probe = subprocess.run(hardened_argv(cwd, "rev-parse", "--verify", "--quiet", f"{state}^{{commit}}"),
                                       capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
                if probe.returncode == 0 and probe.stdout.strip():
                    found.append((state, probe.stdout.strip()))
        return found
    targets = _revision_args(argv)
    if argv[0] == "switch" or tuple(argv[:2]) == ("worktree", "add"):
        as_branch = not any(arg in _START_POINT_OPTIONS or arg.startswith(("-b", "-B")) for arg in argv[1:])
        if argv[0] == "worktree":
            # worktree add <path> alone checks out a branch guessed from the folder's name (or, with
            # worktree.guessRemote, a remote's); -b/-B/--detach without a commit start at HEAD.
            if "--" in argv or (as_branch and len(targets) < 3):
                raise RuntimeError("git worktree add without an explicit commit cannot be checked")
            targets = targets[2:]  # after "add" and the new worktree's path
        return [(arg, commit) for arg in targets
                for commit in _checkout_target_commits(cwd, arg, as_branch=as_branch, timeout=timeout)]
    if argv[0] in _BRINGING_COMMANDS:
        if argv[0] != "switch" and not targets:
            raise RuntimeError(f"git {argv[0]} without a commit cannot be checked")
        if argv[0] == "merge" and any(target.upper() == "FETCH_HEAD" for target in targets):
            raise RuntimeError("git merge FETCH_HEAD merges every fetched head and cannot be checked")
        if any(arg in {"--continue", "--skip", "--quit"} for arg in argv[1:]):
            raise RuntimeError(f"git {argv[0]} continuing a sequence cannot be checked")
        return [(arg, _resolve_commit(cwd, arg, timeout=timeout)) for arg in targets]
    for arg in targets:
        try:
            found.append((arg, _resolve_commit(cwd, arg, timeout=timeout)))
        except RuntimeError:
            continue
    return found


def _mainline(argv: Sequence[str]) -> str:
    for index, arg in enumerate(argv):
        value = argv[index + 1] if arg in {"-m", "--mainline"} and index + 1 < len(argv) else (
            arg.partition("=")[2] if arg.startswith("--mainline=") else None)
        if value is not None:
            if not value.isdigit() or value == "0":
                raise ValueError("the mainline parent cannot be read")
            return value
    return "1"


def _holds_lfsconfig_anywhere(cwd: Path, commits: Sequence[str], *, timeout: float) -> bool:
    """Whether any of ``commits`` holds an ``.lfsconfig`` at any depth, in any case: rename
    detection (directory renames among them) can only move such a file, never make one."""
    for commit in commits:
        listing = subprocess.run(hardened_argv(cwd, "ls-tree", "-r", "-z", "--name-only", commit),
                                 capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
        if listing.returncode != 0:
            raise RuntimeError(f"git ls-tree failed ({listing.returncode})")
        if any(path.rpartition("/")[2].lower() == ".lfsconfig" for path in listing.stdout.split("\0")):
            return True
    return False


def _parents(cwd: Path, commit: str, *, timeout: float) -> list[str]:
    listing = subprocess.run(hardened_argv(cwd, "rev-list", "--no-walk", "--parents", commit), capture_output=True,
                             encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
    if listing.returncode != 0 or not listing.stdout.strip():
        raise RuntimeError(f"git rev-list --parents failed ({listing.returncode})")
    return listing.stdout.split()[1:]


def _merge_result_tree(cwd: Path, argv: Sequence[str], *, timeout: float) -> str:
    """The tree a merge, cherry-pick or revert of one commit would write ("" for other commands,
    for --abort, and when no side and no merge base holds an ``.lfsconfig``, so the result cannot)."""
    if not argv or argv[0] not in {"merge", "cherry-pick", "revert"} or "--abort" in argv[1:]:
        return ""
    targets = _revision_args(argv)
    if len(targets) != 1:
        raise RuntimeError(f"git {argv[0]} of {len(targets) or 'no'} commits cannot be checked")
    target = _resolve_commit(cwd, targets[0], timeout=timeout)
    if argv[0] == "merge":
        bases = subprocess.run(hardened_argv(cwd, "merge-base", "--all", "HEAD", target), capture_output=True,
                               encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
        if bases.returncode not in (0, 1):  # 1: unrelated histories
            raise RuntimeError(f"git merge-base failed ({bases.returncode})")
        sides, involved = ["HEAD", target], [*bases.stdout.split(), "HEAD", target]
    else:
        if not _parents(cwd, target, timeout=timeout):
            # A root commit has no base, so nothing can be renamed: each path of the result comes
            # from HEAD or from the commit, both already checked.
            return ""
        parent = _rev_parse(cwd, f"{target}^{_mainline(argv)}", timeout=timeout)
        base, theirs = (parent, target) if argv[0] == "cherry-pick" else (target, parent)
        sides, involved = ["--merge-base", base, "HEAD", theirs], [base, "HEAD", theirs]
    if not _holds_lfsconfig_anywhere(cwd, involved, timeout=timeout):
        return ""
    # merge.renormalize would run the user's filters (git-lfs among them) on both sides.
    command = ["git", *hardening_args(), "-c", "merge.renormalize=false", "-C", str(cwd),
               "merge-tree", "--write-tree", "--no-messages", *sides]
    probe = subprocess.run(command, capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout, env=safe_env())
    tree = probe.stdout.split("\n", 1)[0].strip()
    if probe.returncode not in (0, 1) or len(tree) not in (40, 64):  # 1: the result has conflicts
        raise RuntimeError(f"git merge-tree failed ({probe.returncode})")
    return tree


def _expanded_url(cwd: Path, url: str, *, timeout: float) -> str:
    """``url`` after git's ``url.<base>.insteadOf`` rewriting (which git-lfs applies too)."""
    if url.startswith("-"):
        raise ValueError("an LFS server URL starts with '-'")
    if "://" not in url and not _scp_like(url):
        return url  # a local path; a bare word would be read as a remote's name
    probe = subprocess.run(hardened_argv(cwd, "ls-remote", "--get-url", url), capture_output=True, encoding="utf-8", errors="surrogateescape",
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
    # signature the repository planted. A repository's `format.pretty` with `%G` does the same for a
    # log or show without --format (under its `gpg.format`, ssh-keygen or gpgsm); `medium` is git's
    # default, and a `pretty.medium` alias cannot replace a built-in format.
    command = ["git", *hardening_args(), "-c", "diff.submodule=short", "-c", "status.submoduleSummary=false",
               "-c", "submodule.recurse=false", "-c", "log.showSignature=false", "-c", "format.pretty=medium",
               "-C", str(cwd), *argv]
    if argv and argv[0] in {"diff", "log", "show"}:
        command[len(command) - len(argv) + 1:len(command) - len(argv) + 1] = ["--no-ext-diff", "--no-textconv"]
    return command


read_only_argv = hardened_argv


__all__ = ["active_hooks", "executable_config_for", "executable_local_config", "hardened_argv", "hardening_args",
           "is_executable_key", "is_safe_value", "lfs_endpoint_refusal",
           "read_only_argv", "safe_env"]
