"""The Skill Codex signer's git steps where VOOL runs no git program (Windows): pure Python, through dulwich.

Owner decision D18 A (16 Nov 2026), to Security Review's design bar. ``github_authority.Repository`` sends
its four steps here (start, fresh, save, and the state-file check) whenever ``core/safe_git`` refuses git.
These rules hold:

- No git process starts. Nothing here calls porcelain, the index, hooks or any filter. Blobs, trees and
  commits are written straight into the object store, so a pre-commit hook, a ``.gitattributes`` filter
  or an LFS driver in the checkout never runs.
- The checkout's own config is judged as ``safe_git`` judges it: any key naming a program refuses
  (``safe_git.is_executable_key``). It is never handed to the transport either, so a proxy, credential
  helper or URL rewrite in it has no effect.
- The remote is ``https://`` or a full path on a local drive (dulwich's in-process local client). ssh,
  git://, file:// and anything else are refused, so no ssh vendor is ever made.
- Redirects are off. Any 3xx refuses with "git redirect refused".
- The token reaches the server only in the Authorization header, and only to the remote's own origin.
  It is never in a URL, an argument, a log line or an error.
- A fast-forward writes only regular files, as plain bytes. A symlink, a submodule, a ``.git`` path, a
  path with a drive, a colon or a backslash, or a ``..`` part is refused before anything is written.
"""
from __future__ import annotations

import os
import ssl
import stat
import time
import urllib.parse
from pathlib import Path, PurePosixPath

from core.registry.codec import RegistryError, require

_REGULAR = (0o100644, 0o100755)
_TREE = 0o040000
_TIMEOUT = 120.0


def _dulwich():
    try:
        from dulwich import client, objects, repo
    except ImportError:
        raise RegistryError("this VOOL has no git library (dulwich), so the signer can't publish from here") from None
    return client, objects, repo


def _redirect_refused(status: int) -> None:
    if 300 <= status < 400:
        raise RegistryError("git redirect refused")


def _safe_part(name: bytes) -> str:
    text = name.decode("utf-8")
    require(text not in {"", ".", ".."} and not any(ch in text for ch in ':\\/\0') and
            text.lower() != ".git" and not text.lower().startswith(".git ") and text.strip() == text.rstrip(". "),
            "the repository holds a path VOOL won't write: " + text[:80])
    return text


class Checkout:
    """One working checkout of the signer's repository, driven without git."""

    def __init__(self, path: Path, *, branch: str, token: str, folder: str, steps: str, identity: tuple[str, str],
                 superseded: type[Exception]):
        self.path, self.branch = Path(path), branch
        self._superseded = superseded  # github_authority.Superseded: a run-wide stop, not one upload's trouble
        self._token = token
        self._folder, self._steps = folder, steps
        self._identity = (identity[0] + " <" + identity[1] + ">").encode("utf-8")
        self._ref = b"refs/heads/" + branch.encode("ascii")
        self.fetched: bytes | None = None

    # ---- the checkout and its config ----

    def _repo(self):
        _client, _objects, repo = _dulwich()
        require((self.path / ".git").is_dir() and not (self.path / ".git").is_symlink(),
                "the signer's checkout must be a plain git checkout")
        opened = repo.Repo(str(self.path))
        self._judge_config(opened)
        return opened

    def _judge_config(self, opened) -> None:
        from core import safe_git

        programs = []
        for name in ("config", "config.worktree"):
            source = Path(opened.controldir()) / name
            if not source.exists():
                continue
            from dulwich.config import ConfigFile

            parsed = ConfigFile.from_path(str(source))
            for section in parsed.sections():
                prefix = ".".join(part.decode("utf-8", "replace") for part in section)
                for key, _value in parsed.items(section):
                    full = prefix + "." + key.decode("utf-8", "replace")
                    if safe_git.is_executable_key(full):
                        programs.append(full)
        if programs:
            raise RegistryError("the checkout's own git config names programs git would run: " + ", ".join(sorted(programs)))

    def _remote(self, opened):
        """The origin's client and path: https or a full local drive path, nothing else."""
        client, _objects, _repo = _dulwich()
        try:
            url = opened.get_config().get((b"remote", b"origin"), b"url").decode("utf-8")
        except KeyError:
            raise RegistryError("the signer's checkout has no origin remote") from None
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            require(bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query and
                    not parsed.fragment, "the origin must be a plain https:// address")
            return self._https(client, url), parsed.path
        require(self._local_path(url), "the origin must be https:// or a folder on a local drive")
        return client.LocalGitClient(), url

    @staticmethod
    def _local_path(url: str) -> bool:
        if os.name == "nt":
            from core import windows_known_folders

            return bool(windows_known_folders.on_local_drive(url))
        return os.path.isabs(url) and "://" not in url

    def _https(self, client, url: str):
        import urllib3

        parsed = urllib.parse.urlsplit(url)
        origin = parsed.scheme + "://" + parsed.netloc
        context = ssl.create_default_context()
        retries = urllib3.Retry(total=2, redirect=0, raise_on_redirect=False, status=0, raise_on_status=False)
        proxy = next((os.environ[name] for name in ("HTTPS_PROXY", "https_proxy") if os.environ.get(name)), "")
        if proxy:
            manager = urllib3.ProxyManager(proxy, ssl_context=context, retries=retries, timeout=_TIMEOUT)
        else:
            manager = urllib3.PoolManager(ssl_context=context, retries=retries, timeout=_TIMEOUT)
        kept = self

        class _Client(client.Urllib3HttpGitClient):
            def _http_request(self, request_url, headers=None, data=None, raise_for_status=True):
                require(request_url.startswith(origin + "/"), "git redirect refused")
                response, read = super()._http_request(request_url, headers, data, raise_for_status=False)
                _redirect_refused(int(response.status))
                require(int(response.status) == 200, "git " + kept._step + " failed: HTTP " + str(int(response.status)))
                require(not response.redirect_location, "git redirect refused")
                return response, read

        username, password = ("x-access-token", self._token) if self._token else (None, None)
        return _Client(origin + "/", pool_manager=manager, config=None, username=username, password=password)

    _step = "fetch"

    # ---- reading trees ----

    def _tree_of(self, opened, commit_id: bytes | None):
        if commit_id is None:
            return None
        return opened[opened[commit_id].tree]

    def _blob_at(self, opened, commit_id: bytes | None, path: str) -> bytes | None:
        tree = self._tree_of(opened, commit_id)
        for part in PurePosixPath(path).parts:
            if tree is None:
                return None
            try:
                mode, sha = tree[part.encode("utf-8")]
            except KeyError:
                return None
            item = opened[sha]
            tree = item if mode == _TREE else None
            if mode != _TREE:
                return item.data if mode in _REGULAR else None
        return None

    def _files(self, opened, tree_id: bytes | None, prefix: str = "") -> dict[str, tuple[int, bytes]]:
        """Every path in a tree, refusing anything but folders and regular files."""
        found: dict[str, tuple[int, bytes]] = {}
        if tree_id is None:
            return found
        for entry in opened[tree_id].items():
            name = _safe_part(entry.path)
            path = prefix + name
            if entry.mode == _TREE:
                found.update(self._files(opened, entry.sha, path + "/"))
            else:
                require(entry.mode in _REGULAR, "the repository holds a link or submodule VOOL won't write: " + path[:80])
                found[path] = (entry.mode, entry.sha)
        return found

    def _head(self, opened) -> bytes | None:
        try:
            return opened.refs[self._ref]
        except KeyError:
            return None

    # ---- the four steps ----

    def _fetch(self, opened) -> bytes:
        self._step = "fetch"
        transport, path = self._remote(opened)
        wanted: list[bytes] = []

        def determine_wants(refs, depth=None):
            sha = refs.get(self._ref)
            require(sha is not None, "the remote has no " + self.branch + " branch")
            wanted[:] = [sha]
            return [sha] if sha not in opened.object_store else []

        try:
            transport.fetch(path, opened, determine_wants=determine_wants)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError("git fetch failed: " + type(exc).__name__) from None
        self.fetched = wanted[0]
        return wanted[0]

    def untouched(self, path: str) -> bool:
        """Whether the checkout's file is exactly what HEAD holds (both absent counts as the same)."""
        opened = self._repo()
        committed = self._blob_at(opened, self._head(opened), path)
        target = self.path / path
        on_disk = target.read_bytes() if target.is_file() and not target.is_symlink() else None
        return committed == on_disk

    def start(self) -> None:
        """Fast-forward to the newest state on the remote; a checkout that moved on its own is refused."""
        opened = self._repo()
        fetched = self._fetch(opened)
        head = self._head(opened)
        if head == fetched:
            return
        if head is not None:
            from dulwich.graph import can_fast_forward

            if can_fast_forward(opened, fetched, head):
                return  # already holds the remote's newest commit, as git merge --ff-only leaves it
            require(can_fast_forward(opened, head, fetched), "git merge failed: not a fast-forward")
        self._move_to(opened, head, fetched)

    def fresh(self) -> None:
        """Stop when a remote state or operator-step commit overtakes this run."""
        opened = self._repo()
        fetched = self._fetch(opened)
        head = self._head(opened)
        if head == fetched:
            return
        from dulwich.graph import find_merge_base

        bases = find_merge_base(opened, [head, fetched]) if head is not None else []
        base = bases[0] if bases else None
        before = self._files(opened, opened[base].tree if base else None)
        after = self._files(opened, opened[fetched].tree)
        moved = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
        if any(path == self._steps or path.startswith(self._folder + "/") for path in moved):
            raise self._superseded("another signer run changed the state meanwhile; this run stopped before sending anything")

    def save(self, message: str) -> bool:
        """Commit and push the state folder; False when nothing changed. Always looks at the remote first."""
        self.fresh()
        opened = self._repo()
        head = self._head(opened)
        require(head is not None, "the signer's checkout has no commit yet")
        tree = self._with_folder(opened, opened[head].tree)
        if tree == opened[head].tree:
            return False
        commit = self._commit(opened, tree, head, message)
        for _ in range(3):
            if self._push(opened, commit):
                opened.refs[self._ref] = commit
                return True
            self.fresh()  # stops here if the remote moved the state itself
            fetched = self.fetched
            commit = self._commit(opened, self._with_folder(opened, opened[fetched].tree), fetched, message)
            # Only unrelated repository files moved: bring them into the checkout before trying again.
            self._move_to(opened, head, fetched, keep=self._folder)
            head = fetched
            opened.refs[self._ref] = commit
        raise RegistryError("the signer's state could not be pushed to GitHub")

    # ---- writing ----

    def _with_folder(self, opened, base_tree: bytes) -> bytes:
        """``base_tree`` with the state folder replaced by the checkout's folder, as plain files."""
        _client, objects, _repo = _dulwich()
        folder_tree = self._tree_from_disk(opened, self.path / self._folder)
        top = objects.Tree()
        for entry in opened[base_tree].items():
            if entry.path != self._folder.encode("utf-8"):
                top.add(entry.path, entry.mode, entry.sha)
        if folder_tree is not None:
            top.add(self._folder.encode("utf-8"), _TREE, folder_tree)
        opened.object_store.add_object(top)
        return top.id

    def _tree_from_disk(self, opened, folder: Path) -> bytes | None:
        _client, objects, _repo = _dulwich()
        if not folder.exists():
            return None
        info = folder.lstat()
        require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                "the state folder in the checkout is not a plain folder")
        tree = objects.Tree()
        for item in sorted(folder.iterdir()):
            name = _safe_part(item.name.encode("utf-8"))
            meta = item.lstat()
            require(not stat.S_ISLNK(meta.st_mode) and not getattr(meta, "st_file_attributes", 0) & 0x400,
                    "the state folder holds a link: " + name)
            if stat.S_ISDIR(meta.st_mode):
                sub = self._tree_from_disk(opened, item)
                if sub is not None:
                    tree.add(name.encode("utf-8"), _TREE, sub)
                continue
            require(stat.S_ISREG(meta.st_mode), "the state folder holds something that is not a file: " + name)
            blob = objects.Blob.from_string(item.read_bytes())
            opened.object_store.add_object(blob)
            tree.add(name.encode("utf-8"), 0o100644, blob.id)
        opened.object_store.add_object(tree)
        return tree.id

    def _commit(self, opened, tree: bytes, parent: bytes, message: str) -> bytes:
        _client, objects, _repo = _dulwich()
        commit = objects.Commit()
        commit.tree = tree
        commit.parents = [parent]
        commit.author = commit.committer = self._identity
        commit.author_time = commit.commit_time = int(time.time())
        commit.author_timezone = commit.commit_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = message.encode("utf-8") + b"\n"
        opened.object_store.add_object(commit)  # no hook, no signing: this is not a porcelain commit
        return commit.id

    def _push(self, opened, commit: bytes) -> bool:
        self._step = "push"
        transport, path = self._remote(opened)
        expected = self.fetched

        def update_refs(refs):
            return {**refs, self._ref: commit} if refs.get(self._ref) == expected else dict(refs)

        def generate(have, want, *, ofs_delta=False, progress=None):
            return opened.generate_pack_data(set(have), set(want), progress=progress, ofs_delta=ofs_delta)

        try:
            result = transport.send_pack(path.encode("utf-8") if isinstance(path, str) else path, update_refs, generate)
        except RegistryError:
            raise
        except Exception as exc:
            if "redirect" in str(exc).lower():
                raise RegistryError("git redirect refused") from None
            return False
        status = (result.ref_status or {}).get(self._ref)  # a ref that went through has no entry (local) or None (https)
        refs = getattr(result, "refs", None) or {}
        return status is None and refs.get(self._ref, commit) == commit

    def _move_to(self, opened, head: bytes | None, target: bytes, *, keep: str | None = None) -> None:
        """Write the files that change from ``head`` to ``target``, after checking each one is unchanged on disk."""
        before = self._files(opened, opened[head].tree if head else None)
        after = self._files(opened, opened[target].tree)
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path) and
                         not (keep and path.startswith(keep + "/")))
        for path in changed:
            target_file = self.path / path
            if path in before:
                committed = opened[before[path][1]].data
                require(target_file.is_file() and not target_file.is_symlink() and target_file.read_bytes() == committed,
                        "the checkout has local changes in " + path)
            else:
                require(not os.path.lexists(target_file), "the checkout has an untracked " + path)
        for path in changed:
            target_file = self.path / path
            if path in after:
                self._no_link_on_the_way(target_file)
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(opened[after[path][1]].data)
            else:
                target_file.unlink()
        opened.refs[self._ref] = target
        try:
            opened.refs.set_symbolic_ref(b"HEAD", self._ref)
        except Exception:
            raise RegistryError("the signer's checkout could not be moved to the new commit") from None

    def _no_link_on_the_way(self, target: Path) -> None:
        current = self.path
        for part in target.relative_to(self.path).parts[:-1]:
            current = current / part
            if os.path.lexists(current):
                info = current.lstat()
                require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
                        not getattr(info, "st_file_attributes", 0) & 0x400,
                        "the checkout holds a link on the way to " + target.name)
