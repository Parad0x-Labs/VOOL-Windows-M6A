"""Windows: a plain deny ACE does not make a private file or folder "not owner-only".

Captest VM, build 5f97b517 (runtime of 25f3b92c), 2026-11-10: the owner had marked six folders. The
first confined Python job (11:09) ran VoolConfine's grant, which puts VOOL's own AppContainer package
SID on every deny-all path (VOOL's state, `%LOCALAPPDATA%\\VOOL` included) as a plain deny:

    C:\\Users\\VOOLT\\AppData\\Local\\VOOL\\config   S-1-15-2-172212990-...-2054702313:(OI)(CI)(N)  VOOLT:(OI)(CI)(F)
    ...\\config\\disposable_workspaces.json        S-1-15-2-172212990-...-2054702313:(N)          VOOLT:(F)

`verify_owner_only` read that deny as a foreign ACE, so GET /api/windows/disposable-folders answered
`{"folders": [], "problem": "not_owner_only:foreign_ace"}`, every mark stopped counting (the running
broker's list shrank to the variable's folders), and "run calc.py in <marked folder>\\proj" kept the
chat's Desktop and was refused as outside the active workspace.

A plain ACCESS_DENIED ACE (type 0x01) only takes access away, so it is accepted for any SID (logged at
INFO when it is not VOOL's own package SID). Everything else is unchanged: the owner is the user, the
DACL is protected, the only allow ACEs are the user's, and every other ACE type (callback, object,
conditional, unknown) still fails closed. A repair (`make_owner_only`, `ensure_private_dir`,
`make_tree_owner_only`) keeps the plain denies it finds, so it never hands back access that the
confinement took away.

The policy half runs everywhere against a fake Windows API (`core.owner_only._WINDOWS` and
`_win_api` are the seams); Mac and Linux keep the POSIX mode check, untouched.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
from pathlib import Path

import pytest

from core import owner_only
from tests.test_owner_only import _ALLOWED, _CI, _DENIED, _OI, _USER, _FakeWinApi

#: VOOL's AppContainer profile name (windows/broker/src/VoolConfine.Core/Identity.cs) and the package
#: SID the VM's icacls showed on the marks file.
_PROFILE = "VOOL.Confine.v1"
_PACKAGE = "S-1-15-2-172212990-1111866249-3997594517-4187459703-2709936754-834187533-2054702313"
_RANDOM = "S-1-5-21-9-8-7-4242"
_EVERYONE = "S-1-1-0"
_USERS = "S-1-5-32-545"
_FULL = 0x001F01FF
#: ACE types that are not plain allow/deny: object allow/deny, callback allow/deny, callback object deny,
#: and a type nobody defined.
_ALLOWED_OBJECT, _DENIED_OBJECT, _ALLOWED_CALLBACK, _DENIED_CALLBACK, _DENIED_CALLBACK_OBJECT = 5, 6, 9, 10, 12
_UNKNOWN = 0x42
_INHERITED = 0x10

_ACE_RE = re.compile(r"\((?P<type>[AD]);(?P<flags>[A-Z]*);(?P<mask>[^;]*);;;(?P<sid>[^)]+)\)")
_FLAG_BITS = {"OI": 0x1, "CI": 0x2, "NP": 0x4, "IO": 0x8}


class _SddlFakeWinApi(_FakeWinApi):
    """The fake, but `set_descriptor` really sets the DACL the SDDL names (in order), so what a repair
    keeps is what the read-back sees."""

    def set_descriptor(self, handle: str, sddl: str) -> None:
        self.set_calls.append((handle, sddl))
        if handle in self.set_fails:
            raise OSError(5, "Access is denied")
        if handle in self.set_is_ignored:
            return
        dacl = []
        for ace in _ACE_RE.finditer(sddl):
            flags = 0
            for index in range(0, len(ace["flags"]), 2):
                flags |= _FLAG_BITS[ace["flags"][index : index + 2]]
            mask = _FULL if ace["mask"] == "FA" else int(ace["mask"], 16)
            dacl.append((_ALLOWED if ace["type"] == "A" else _DENIED, ace["sid"], flags, mask))
        self.descriptors[handle] = (_USER, sddl.split("D:", 1)[1].startswith("P"), dacl)


@pytest.fixture
def fake(monkeypatch):
    api = _SddlFakeWinApi()
    monkeypatch.setattr(owner_only, "_WINDOWS", True)
    monkeypatch.setattr(owner_only, "_win_api", lambda: api)
    return api


def _path(name: str) -> str:
    return os.path.abspath(os.path.join("vool-owner-only-deny-fake", name))


def _file(api: _FakeWinApi, dacl) -> str:
    path = _path("disposable_workspaces.json")
    api.descriptors[path] = (_USER, True, dacl)
    return path


def _folder(api: _FakeWinApi, dacl) -> str:
    path = _path("config")
    api.descriptors[path] = (_USER, True, dacl)
    api.directories.add(path)
    return path


# --- the SID on the VM is VOOL's own -------------------------------------------------------------


def test_the_sid_on_the_vm_is_vools_own_appcontainer_package_sid() -> None:
    """DeriveAppContainerSidFromAppContainerName: SHA-256 of the lower-cased name in UTF-16LE, its first
    28 bytes as seven little-endian sub-authorities under S-1-15-2."""
    digest = hashlib.sha256(_PROFILE.lower().encode("utf-16-le")).digest()
    derived = "S-1-15-2-" + "-".join(str(part) for part in struct.unpack("<7I", digest[:28]))
    assert derived == _PACKAGE
    assert owner_only.vool_confine_package_sid() == _PACKAGE


# --- verify -------------------------------------------------------------------------------------


@pytest.mark.parametrize("sid", [_PACKAGE, _RANDOM], ids=["package_sid", "random_sid"])
def test_a_plain_deny_passes_on_a_file_and_a_folder(fake, sid) -> None:
    path = _file(fake, [(_DENIED, sid, 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.verify_owner_only(path) == owner_only.OwnerOnlyReport(True)
    folder = _folder(fake, [(_DENIED, sid, _OI | _CI, _FULL), (_ALLOWED, _USER, _OI | _CI, _FULL)])
    assert owner_only.verify_owner_only(folder) == owner_only.OwnerOnlyReport(True)


def test_a_deny_for_a_sid_that_is_not_vools_is_logged_at_info(fake, caplog) -> None:
    path = _file(fake, [(_DENIED, _RANDOM, 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    with caplog.at_level(logging.INFO, logger=owner_only.__name__):
        assert owner_only.verify_owner_only(path).ok
    assert any(r.levelno == logging.INFO and _RANDOM in r.getMessage() for r in caplog.records)
    caplog.clear()
    path = _file(fake, [(_DENIED, _PACKAGE, 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    with caplog.at_level(logging.INFO, logger=owner_only.__name__):
        assert owner_only.verify_owner_only(path).ok
    assert not any(_PACKAGE in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("sid", [_PACKAGE, _EVERYONE, _USERS], ids=["package_sid", "everyone", "users"])
def test_an_allow_for_anyone_but_the_user_still_fails(fake, sid) -> None:
    path = _file(fake, [(_DENIED, _PACKAGE, 0, _FULL), (_ALLOWED, _USER, 0, _FULL), (_ALLOWED, sid, 0, 0x120089)])
    assert owner_only.verify_owner_only(path).reason == "foreign_ace"


@pytest.mark.parametrize(
    "ace_type",
    [_DENIED_CALLBACK, _DENIED_CALLBACK_OBJECT, _DENIED_OBJECT, _ALLOWED_CALLBACK, _ALLOWED_OBJECT, _UNKNOWN],
    ids=["callback_deny", "callback_object_deny", "object_deny", "callback_allow", "object_allow", "unknown"],
)
def test_every_other_ace_type_still_fails_closed(fake, ace_type) -> None:
    # The real reader gives no SID for a type it does not parse (`_WinApi.read_descriptor`).
    path = _file(fake, [(ace_type, "", 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.verify_owner_only(path).reason == "foreign_ace"


def test_a_deny_with_no_sid_fails_closed(fake) -> None:
    path = _file(fake, [(_DENIED, "", 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.verify_owner_only(path).reason == "foreign_ace"


def test_denies_alone_do_not_make_a_path_owner_only(fake) -> None:
    """Only the user's allow grants the path; a DACL of denies alone locks the user out too."""
    path = _file(fake, [(_DENIED, _PACKAGE, 0, _FULL)])
    assert owner_only.verify_owner_only(path).reason == "empty_dacl"
    folder = _folder(fake, [(_DENIED, _PACKAGE, _OI | _CI, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    # The deny is inheritable, the user's allow is not: still not a private folder.
    assert owner_only.verify_owner_only(folder).reason == "ace_not_inheritable"


def test_the_rest_of_the_rule_is_unchanged_with_a_deny_present(fake) -> None:
    deny = (_DENIED, _PACKAGE, 0, _FULL)
    path = _path("f.json")
    fake.descriptors[path] = (_USER, False, [deny, (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.verify_owner_only(path).reason == "dacl_not_protected"
    fake.descriptors[path] = (_PACKAGE, True, [deny, (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.verify_owner_only(path).reason == "owner_not_user"


# --- a repair keeps the deny ---------------------------------------------------------------------


def test_make_owner_only_keeps_a_plain_deny_and_drops_a_foreign_allow(fake) -> None:
    path = _path("f.json")
    fake.descriptors[path] = (_USER, False, [
        (_DENIED, _PACKAGE, 0, _FULL), (_ALLOWED, _USER, 0, _FULL), (_ALLOWED, _EVERYONE, 0, 0x120089),
    ])
    assert owner_only.make_owner_only(path).ok
    assert fake.set_calls == [(path, f"O:{_USER}D:P(D;;0x1f01ff;;;{_PACKAGE})(A;;FA;;;{_USER})")]
    assert fake.descriptors[path][2] == [(_DENIED, _PACKAGE, 0, _FULL), (_ALLOWED, _USER, 0, _FULL)]


def test_an_inherited_deny_is_kept_as_an_explicit_one(fake) -> None:
    """A temp file created in the confined config folder inherits the package deny (I); the protected
    DACL the repair sets drops inheritance, so the deny is written as the file's own."""
    path = _path(".disposable-x.tmp")
    fake.descriptors[path] = (_USER, False, [(_DENIED, _PACKAGE, _INHERITED, _FULL), (_ALLOWED, _USER, _INHERITED, _FULL)])
    assert owner_only.make_owner_only(path).ok
    assert fake.descriptors[path][2][0] == (_DENIED, _PACKAGE, 0, _FULL)


def test_ensure_private_dir_on_an_existing_folder_keeps_the_deny(fake, monkeypatch) -> None:
    monkeypatch.setattr(owner_only.os, "makedirs", lambda *_a, **_k: None)
    folder = _folder(fake, [(_DENIED, _PACKAGE, _OI | _CI, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    owner_only.ensure_private_dir(folder)
    assert fake.set_calls == [(folder, f"O:{_USER}D:P(D;OICI;0x1f01ff;;;{_PACKAGE})(A;OICI;FA;;;{_USER})")]
    assert owner_only.verify_owner_only(folder).ok


def test_a_repair_keeps_a_deny_for_any_sid_and_its_own_mask(fake) -> None:
    path = _path("f.json")
    fake.descriptors[path] = (_USER, True, [(_DENIED, _RANDOM, 0, 0x000D0156), (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.make_owner_only(path).ok
    assert fake.descriptors[path][2][0] == (_DENIED, _RANDOM, 0, 0x000D0156)


def test_a_repair_never_keeps_a_callback_or_object_deny(fake) -> None:
    path = _path("f.json")
    fake.descriptors[path] = (_USER, False, [(_DENIED_CALLBACK, "", 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    assert owner_only.make_owner_only(path).ok
    assert fake.set_calls == [(path, f"O:{_USER}D:P(A;;FA;;;{_USER})")]


# --- the marks survive a confined job ------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    from core import windows_disposable_store as store
    from core import windows_known_folders, windows_write_gate

    vool_home = tmp_path / "vool-home"
    vool_home.mkdir()
    monkeypatch.setattr("core.runtime_paths.active_vool_home", lambda *a, **k: vool_home)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.delenv(windows_write_gate.ENV_VAR, raising=False)
    (tmp_path / "profile").mkdir()
    monkeypatch.setattr(windows_known_folders, "known_folder", lambda name: {"Profile": str(tmp_path / "profile")}.get(name))
    store._reset_for_tests()
    yield vool_home
    store._reset_for_tests()


def _confined_job_ran(monkeypatch, config: Path) -> _SddlFakeWinApi:
    """The descriptors the VM showed after VoolConfine's grant: the package deny on the config folder
    (OI)(CI) and on the marks file, beside the user's full control."""
    api = _SddlFakeWinApi()
    monkeypatch.setattr(owner_only, "_WINDOWS", True)
    monkeypatch.setattr(owner_only, "_win_api", lambda: api)
    monkeypatch.setattr(owner_only, "_non_local", lambda _path: False)
    folder = os.path.abspath(str(config))
    api.descriptors[folder] = (_USER, True, [(_DENIED, _PACKAGE, _OI | _CI, _FULL), (_ALLOWED, _USER, _OI | _CI, _FULL)])
    api.directories.add(folder)
    marks = os.path.abspath(str(config / "disposable_workspaces.json"))
    api.descriptors[marks] = (_USER, True, [(_DENIED, _PACKAGE, 0, _FULL), (_ALLOWED, _USER, 0, _FULL)])
    return api


def test_the_api_and_the_running_broker_keep_the_marks_after_a_confined_job(home, tmp_path, monkeypatch) -> None:
    from core import windows_disposable_store as store
    from core import windows_write_gate
    from core.windows_broker import session

    marked = tmp_path / "VOOL Test" / "Allowed Ä folder"
    (marked / "proj").mkdir(parents=True)
    entry = store.mark(str(marked))          # written owner-only the POSIX way, before any job
    store._reset_for_tests()
    started = store.start_folders()
    assert started == [os.path.realpath(marked)]
    monkeypatch.setattr(session, "wired", lambda: True)
    monkeypatch.setattr(session, "checked_folders", lambda: [os.path.normcase(os.path.realpath(marked))])

    _confined_job_ran(monkeypatch, store.store_path().parent)

    listed = store.load()
    assert listed["problem"] == "", listed
    assert [folder["id"] for folder in listed["folders"]] == [entry["id"]]
    assert store.unmarked_since_start() == set()
    assert windows_write_gate.flagged_roots() == [os.path.normcase(os.path.realpath(marked))]
    assert windows_write_gate.inside_flagged(str(marked / "proj"))


def test_a_marks_file_with_a_foreign_allow_still_adds_nothing(home, tmp_path, monkeypatch) -> None:
    from core import windows_disposable_store as store

    marked = tmp_path / "VOOL Test" / "Allowed Ä folder"
    marked.mkdir(parents=True)
    store.mark(str(marked))
    store._reset_for_tests()
    api = _confined_job_ran(monkeypatch, store.store_path().parent)
    marks = os.path.abspath(str(store.store_path()))
    owner, protected, dacl = api.descriptors[marks]
    api.descriptors[marks] = (owner, protected, [*dacl, (_ALLOWED, _EVERYONE, 0, 0x120089)])
    listed = store.load()
    assert listed["folders"] == [] and listed["problem"] == "not_owner_only:foreign_ace"


# --- Mac and Linux are untouched -----------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_off_windows_the_posix_mode_check_is_unchanged(tmp_path, monkeypatch) -> None:
    def _no_windows_api():
        raise AssertionError("the Windows API is never asked off Windows")

    monkeypatch.setattr(owner_only, "_WINDOWS", False)
    monkeypatch.setattr(owner_only, "_win_api", _no_windows_api)
    target = tmp_path / "f.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    assert owner_only.verify_owner_only(target).reason == "mode_too_open"
    assert owner_only.make_owner_only(target).ok
    assert (target.stat().st_mode & 0o777) == 0o600
