"""Record, and refuse, every filesystem or process call that names the marker host `attkr9`.

The Windows non-local path tests send paths such as `//attkr9/s/x` or `C:attkr9\\x` into VOOL and
must prove that nothing ever reaches for them: on Windows, a stat of `\\\\attkr9\\s` is an SMB
connection (and an NTLM hash sent to whoever answers), so "refused, but only after a lookup" is
still the bug. A status code alone cannot show that, so this helper watches the calls themselves.

Three parts, as the plan for these tests describes:

1. **A process-wide audit hook**, installed once when this module is first imported and idle
   unless a recorder is active. It sees the calls that raise audit events, including the ones made
   from C: `open` (both `open()` and `os.open`), `os.listdir`, `os.scandir`, `os.chdir`,
   `os.mkdir`, `os.rename`, `os.remove`, `sqlite3.connect` and `subprocess.Popen` (argv and cwd),
   plus a few neighbours of the same kind (rmdir, chmod, utime, link, symlink, truncate, walk,
   glob, the shutil copy/move/rmtree/archive events, tempfile, `os.startfile`, `os.system`, and the
   `os.exec*`, `os.spawn*` and `os.posix_spawn*` process starts).
   An audit hook cannot be removed, which is why it is gated by `_ACTIVE` and returns at once
   while no recorder runs.
2. **monkeypatch wrappers** for the calls that raise no audit event: `os.stat`, `os.lstat`,
   `os.access`, `os.readlink`, `os.statvfs` and `shutil.disk_usage`, plus, where `nt` exists,
   `nt._getfinalpathname`, `nt._getdiskusage`, `nt._getvolumepathname`, `nt._findfirstfile` and
   the `nt._path_is*` / `_path_exists` / `_path_lexists` predicates. They are wrapped at the
   module attribute, so `os.path.*` and `pathlib` (which look `os.stat` up at call time) are seen
   too. Where a stdlib module bound one of them under its own name (`ntpath.isdir` is
   `nt._path_isdir`, `ntpath._nt_readlink` is `os.readlink`), that binding is wrapped as well.
   The lexical `nt._path_*` helpers (`_path_normpath`, `_path_splitroot`, ...) never touch a disk
   and are deliberately left alone.
3. **Any call naming `attkr9` is recorded with its repository frames and refused.** "Naming" means
   the marker appears, in any letter case, anywhere in the argument: a str, bytes, an
   `os.PathLike`, any item of an argv list, a cwd, or a value of the environment a process is
   started with. Letter case does not count because Windows ignores it: `\\\\ATTKR9\\s` and
   `\\\\attkr9\\s` are the same share, and `os.path.normcase` lowers every path it touches there.
   That matters on Linux CI, where `//attkr9/s/x` normalises to `/attkr9/s/x` and
   `\\\\attkr9\\s` is merely an odd relative name that ends up joined under a workspace: the
   marker survives both. A refused call raises `FileNotFoundError`, as a missing share would; the
   bool predicates (`os.access`, `nt._path_exists`, ...) answer False instead, which is what they
   answer for a missing path, so a refused lookup does not turn into an exception the caller was
   never written to expect. A process start naming the marker raises `PermissionError`.

Process starts that do not name the marker still run; every one is listed in `Recorder.popen`
(with `names_marker` telling them apart), so a test can also prove that no browser was started.

What it cannot see: only this test process is watched. Work handed to another process (over its
stdin, a pipe, a socket or a file it reads, as the media editor hands a job to its worker) runs
unrecorded, and so does whatever that process later starts. A test that drives such a lane relies
on the lane's own status and decision assertions to show the request never left VOOL, plus the
absence of a process start naming the marker.

Usage in a test module (the tests/ ruff config allows the fixture imports):

    from tests._nonlocal_touch_recorder import recorder, windows_host

    def test_x(tmp_path, recorder, windows_host):
        ...local setup with real tmp paths...
        windows_host.on()          # the seam turns on only here
        ...drive the request...
        assert recorder.touches == [], recorder.report()
        assert recorder.marker_popen == [], recorder.report()

`windows_host` never turns the seam on by itself: with `core.windows_path_policy._IS_WINDOWS`
True, a POSIX absolute path such as `tmp_path` counts as root-relative, so local fixtures (real
files, opened media projects, created projects) must be made before `windows_host.on()`.
"""
from __future__ import annotations

import contextlib
import errno
import functools
import importlib
import os
import re
import shutil
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from core import windows_path_policy

MARKER = "attkr9"

_REFUSED = "refused by the non-local touch recorder (the path names the marker host)"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOTS = tuple({os.path.normcase(_REPO), os.path.normcase(os.path.realpath(_REPO))})
_THIS_FILE = os.path.normcase(os.path.abspath(__file__))
_MAX_FRAMES = 16


@dataclass(frozen=True)
class MarkerForm:
    """One spelling of a non-local path that carries the marker host.

    `id` is a pytest id that never contains the marker; `kind` is what the classifier answers;
    `network` says whether the network tier (`network_only=True`) refuses it too, which decides the
    sentence the user is shown.
    """

    id: str
    value: str
    kind: str
    network: bool

    @property
    def sentence(self) -> str:
        return "is a network or device path" if self.network else "is not a full local path"


#: The forms from the plan, network kinds first. Every one carries the marker.
MARKER_FORMS: tuple[MarkerForm, ...] = (
    MarkerForm("unc-forward", "//attkr9/s/x", "unc", True),
    MarkerForm("unc-backslash", "\\\\attkr9\\s\\x", "unc", True),
    MarkerForm("device-unc", "\\\\?\\UNC\\attkr9\\s", "device", True),
    MarkerForm("device-pipe", "\\\\.\\pipe\\attkr9", "device", True),
    MarkerForm("root-relative-backslash", "\\attkr9\\x", "root_relative", False),
    MarkerForm("root-relative-forward", "/attkr9/x", "root_relative", False),
    MarkerForm("drive-relative", "C:attkr9\\x", "drive_relative", False),
)
NETWORK_FORMS: tuple[MarkerForm, ...] = tuple(form for form in MARKER_FORMS if form.network)
PARTIAL_FORMS: tuple[MarkerForm, ...] = tuple(form for form in MARKER_FORMS if not form.network)


def form_params(forms: tuple[MarkerForm, ...] = MARKER_FORMS) -> list[Any]:
    """`pytest.param(form, id=form.id)` for each form.

    Use these ids rather than pytest's default ones: a default id is the value itself, and a short
    test name plus an id carrying `attkr9` would put the marker into `tmp_path`'s own name.
    """
    return [pytest.param(form, id=form.id) for form in forms]


@dataclass(frozen=True)
class Touch:
    op: str
    path: str
    frames: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.op} {self.path!r} at {' <- '.join(reversed(self.frames)) or '(no repository frame)'}"


@dataclass(frozen=True)
class Spawn:
    """One process start. `env` lists only the `NAME=value` entries that name the marker."""

    event: str
    argv: tuple[str, ...]
    cwd: str
    names_marker: bool
    frames: tuple[str, ...]
    env: tuple[str, ...] = ()

    def __str__(self) -> str:
        where = " <- ".join(reversed(self.frames)) or "(no repository frame)"
        env = f" env={list(self.env)!r}" if self.env else ""
        return f"{self.event} argv={list(self.argv)!r} cwd={self.cwd!r}{env} at {where}"


@dataclass
class Recorder:
    """What one test saw. `touches` and `popen` are plain lists, so `== []` reads naturally."""

    tmp_root: str = ""
    touches: list[Touch] = field(default_factory=list)
    popen: list[Spawn] = field(default_factory=list)

    @property
    def marker_popen(self) -> list[Spawn]:
        return [spawn for spawn in self.popen if spawn.names_marker]

    def names_marker(self, text: str) -> bool:
        # The cheap test first: almost every path a served turn looks up does not name the marker.
        if MARKER not in text.casefold():
            return False
        return MARKER in self._without_tmp_dir_name(text).casefold()

    def report(self) -> str:
        lines = [f"{len(self.touches)} touch(es), {len(self.marker_popen)} marker process start(s)"]
        lines += [f"  touch: {touch}" for touch in self.touches]
        lines += [f"  spawn: {spawn}" for spawn in self.popen]
        return "\n".join(lines)

    def _without_tmp_dir_name(self, text: str) -> str:
        # pytest names each test's directory after the test, ids included. Drop that one path
        # component so a marker that is only there (never in a path the code built) is not a touch.
        root = self.tmp_root
        if root and os.path.normcase(text).startswith(root) and text[len(root):len(root) + 1] in ("/", "\\"):
            rest = re.split(r"[\\/]+", text[len(root):].lstrip("\\/"), maxsplit=1)
            return rest[1] if len(rest) > 1 else ""
        return text


_ACTIVE: list[Recorder] = []
_LOCAL = threading.local()


def _texts(value: Any) -> list[str]:
    """Every string an argument carries: str, bytes, PathLike, and each item of an argv list."""
    if value is None or isinstance(value, (bool, int)):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return [bytes(value).decode("utf-8", "replace")]
    if isinstance(value, os.PathLike):
        try:
            return _texts(os.fspath(value))
        except Exception:  # an odd PathLike is not this helper's concern
            return []
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _texts(item)]
    return []


def _repository_frames() -> tuple[str, ...]:
    # sys._getframe reads no source file, so recording never raises an audit event of its own.
    frames: list[str] = []
    frame = sys._getframe(1)
    while frame is not None:
        filename = os.path.normcase(frame.f_code.co_filename)
        if filename != _THIS_FILE:
            for root in _REPO_ROOTS:
                if filename.startswith(root + os.sep):
                    frames.append(f"{frame.f_code.co_filename[len(root) + 1:].replace(os.sep, '/')}:{frame.f_lineno}")
                    break
        frame = frame.f_back
    frames.reverse()
    return tuple(frames[-_MAX_FRAMES:])


def _record_touch(op: str, *values: Any) -> str:
    """Record each marker-bearing path in `values`; answer the first one, or "" for none."""
    recorders = list(_ACTIVE)
    if not recorders:
        return ""
    texts = [text for value in values for text in _texts(value)]
    first = ""
    frames: tuple[str, ...] | None = None
    for active in recorders:
        for text in texts:
            if active.names_marker(text):
                frames = frames if frames is not None else _repository_frames()
                active.touches.append(Touch(op, text, frames))
                first = first or text
    return first


def _env_entries(env: Any) -> list[tuple[str, list[str]]]:
    """Each `NAME=value` of a process environment, with the value split into its path-list items.

    `None` is the environment the child inherits: this process's own. Split, because a list such as
    `VOOL_WINDOWS_DISPOSABLE_WORKSPACES` holds several paths, and only the first of them could be
    excused as this test's own temporary directory otherwise.
    """
    env = os.environ if env is None else env
    if not hasattr(env, "items"):
        return []
    entries = []
    for name, value in list(env.items()):
        texts = _texts(value)
        entries.append((f"{''.join(_texts(name))}={''.join(texts)}", [part for text in texts for part in text.split(os.pathsep)]))
    return entries


def _record_spawn(event: str, executable: Any, argv: Any, cwd: Any, env: Any = None) -> bool:
    recorders = list(_ACTIVE)
    if not recorders:
        return False
    argv_texts = tuple(_texts(argv))
    cwd_text = " ".join(_texts(cwd))
    env_entries = _env_entries(env)
    named = False
    frames = _repository_frames()
    for active in recorders:
        env_named = tuple(entry for entry, parts in env_entries if any(active.names_marker(part) for part in parts))
        names = bool(env_named) or any(
            active.names_marker(text) for text in (*_texts(executable), *argv_texts, cwd_text)
        )
        active.popen.append(Spawn(event, argv_texts, cwd_text, names, frames, env_named))
        named = named or names
    return named


#: audit event -> indexes of its path arguments (CPython's documented argument order).
_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "open": (0,),
    "os.listdir": (0,),
    "os.scandir": (0,),
    "os.chdir": (0,),
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.utime": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.truncate": (0,),
    "os.walk": (0,),
    "os.fwalk": (0,),
    "os.listxattr": (0,),
    "os.getxattr": (0,),
    "os.startfile": (0,),
    "os.startfile/2": (0,),
    "glob.glob": (0,),
    "glob.glob/2": (0, 2),
    "pathlib.Path.glob": (0,),
    "pathlib.Path.rglob": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.move": (0, 1),
    "shutil.make_archive": (0, 2),
    "shutil.unpack_archive": (0, 1),
    "sqlite3.connect": (0,),
    "tempfile.mkstemp": (0,),
    "tempfile.mkdtemp": (0,),
}


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _ACTIVE or getattr(_LOCAL, "busy", False):
        return
    _LOCAL.busy = True
    try:
        indexes = _PATH_EVENTS.get(event)
        if indexes is not None:
            path = _record_touch(event, *(args[i] for i in indexes if i < len(args)))
            if path:
                raise FileNotFoundError(errno.ENOENT, _REFUSED, path)
            return
        if event == "subprocess.Popen":  # (executable, args, cwd, env)
            executable, argv, cwd, env = (*args, None, None, None, None)[:4]
            if _record_spawn(event, executable, argv, cwd, env):
                raise PermissionError(errno.EACCES, f"process start {_REFUSED}")
        elif event == "os.system":
            if _record_spawn(event, None, args[:1], None):
                raise PermissionError(errno.EACCES, f"process start {_REFUSED}")
        elif event in ("os.exec", "os.posix_spawn", "os.spawn"):
            # os.exec and os.posix_spawn (posix_spawnp too): (path, args, env); os.spawn: (mode, path, args, env)
            path, argv, env = (*args, None)[:3] if event != "os.spawn" else (*args, None)[1:4]
            if _record_spawn(event, path, argv, None, env):
                raise PermissionError(errno.EACCES, f"process start {_REFUSED}")
    finally:
        _LOCAL.busy = False


sys.addaudithook(_audit)


_WRAPPED = "__nonlocal_touch_wrapper__"
_NT_RAISING = ("_getfinalpathname", "_getdiskusage", "_getvolumepathname", "_findfirstfile")
_OS_RAISING = ("stat", "lstat", "readlink", "statvfs")


def _raising_wrapper(op: str, original: Any) -> Any:
    @functools.wraps(original)
    def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
        named = _record_touch(op, path)
        if named:
            raise FileNotFoundError(errno.ENOENT, _REFUSED, named)
        return original(path, *args, **kwargs)

    setattr(wrapper, _WRAPPED, True)
    return wrapper


def _predicate_wrapper(op: str, original: Any) -> Any:
    @functools.wraps(original)
    def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
        if _record_touch(op, path):
            return False
        return original(path, *args, **kwargs)

    setattr(wrapper, _WRAPPED, True)
    return wrapper


def _binding_modules() -> list[Any]:
    """Modules that may hold one of the wrapped callables under their own name."""
    names = ("os", "nt", "ntpath", "posixpath", "genericpath", "shutil", "pathlib", "pathlib._local", "pathlib._abc")
    found: list[Any] = []
    for name in names:
        module = sys.modules.get(name)
        if module is None:
            with contextlib.suppress(ImportError):
                module = importlib.import_module(name)
        if module is not None and all(module is not seen for seen in found):
            found.append(module)
    return found


def _install_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    pairs: list[tuple[Any, Any]] = []
    for name in _OS_RAISING:
        original = getattr(os, name, None)
        if original is not None and not getattr(original, _WRAPPED, False):
            pairs.append((original, _raising_wrapper(f"os.{name}", original)))
    if not getattr(os.access, _WRAPPED, False):
        pairs.append((os.access, _predicate_wrapper("os.access", os.access)))
    if not getattr(shutil.disk_usage, _WRAPPED, False):
        pairs.append((shutil.disk_usage, _raising_wrapper("shutil.disk_usage", shutil.disk_usage)))
    nt = sys.modules.get("nt")
    if nt is not None:
        for name in sorted(vars(nt)):
            original = getattr(nt, name)
            if getattr(original, _WRAPPED, False) or not callable(original):
                continue
            if name in _NT_RAISING:
                pairs.append((original, _raising_wrapper(f"nt.{name}", original)))
            elif name.startswith("_path_is") or name in ("_path_exists", "_path_lexists"):
                pairs.append((original, _predicate_wrapper(f"nt.{name}", original)))
    modules = _binding_modules()
    for original, wrapper in pairs:
        for module in modules:
            for attribute, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, attribute, wrapper)
        # `os.stat in os.supports_follow_symlinks` must stay true for callers that ask.
        for capability in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks", "supports_effective_ids"):
            members = getattr(os, capability, None)
            if isinstance(members, (set, frozenset)) and original in members:
                monkeypatch.setattr(os, capability, type(members)(members | {wrapper}))


@contextlib.contextmanager
def record_touches(monkeypatch: pytest.MonkeyPatch | None = None, *, tmp_root: Any = None) -> Iterator[Recorder]:
    """Activate a recorder until the block exits.

    With `monkeypatch`, the wrappers are undone with the rest of that test's patches; without it
    (a script), they are undone when the block exits. `tmp_root` is pytest's base temp directory,
    whose per-test directory names are excused (see `Recorder._without_tmp_dir_name`).
    """
    active = Recorder(tmp_root=os.path.normcase(os.fspath(tmp_root)) if tmp_root else "")
    with contextlib.ExitStack() as stack:
        patcher = monkeypatch if monkeypatch is not None else stack.enter_context(pytest.MonkeyPatch.context())
        _install_wrappers(patcher)
        _ACTIVE.append(active)
        try:
            yield active
        finally:
            _ACTIVE.remove(active)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Recorder]:
    """Record marker touches and process starts for the whole test, set up before its body."""
    with record_touches(monkeypatch, tmp_root=tmp_path_factory.getbasetemp()) as active:
        yield active


class WindowsHost:
    """The `core.windows_path_policy._IS_WINDOWS` seam, turned on when the test says so."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def on(self) -> WindowsHost:
        self._monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
        return self

    __call__ = on

    def off(self) -> WindowsHost:
        self._monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", False)
        return self

    @property
    def is_on(self) -> bool:
        return windows_path_policy._IS_WINDOWS is True


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> WindowsHost:
    """Call `windows_host.on()` (or `windows_host()`) once local setup is done; undone at teardown.

    Only `windows_path_policy._IS_WINDOWS` moves. `os.name`, `sys.platform` and the separate
    `windows_write_gate._IS_WINDOWS` seam are left exactly as they are.
    """
    return WindowsHost(monkeypatch)
