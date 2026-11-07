"""Windows: the owner can mark a disposable workspace folder from the VOOL window (clean-VM job 52).

Port 76afbd36 on a clean Windows install: VOOL could not write any file, because the write gate's
only folder list was VOOL_WINDOWS_DISPOSABLE_WORKSPACES, which an installed user cannot set; chat then
offered `~/Desktop` and `~/Downloads` (bug 3) and Setup ticked "Pick a folder" for a folder chat
refused (bug 4). Owner decision 2026-11-07, 🔐 review conditions:

- only the native window bridge marks (OS folder picker + OS confirm naming the resolved folder);
  the page passes no path, and unmark takes an id;
- the list is owner-only when written, verified when read, and adds nothing on any doubt;
- every entry still goes through the gate's own root checks, and system, credential and app-state
  folders (or what holds them) are never marked; a temp subfolder is fine, the temp root is not;
- a mark counts from the next start; an unmark shrinks at once;
- the broker gets the list at its start and the API logs it;
- chat names only folders it can really write; Setup ticks a folder only once it is marked.

The Windows decisions are driven on Linux through the modules' `_IS_WINDOWS` flags, as the gate's
other tests do; owner-only is the POSIX mode check there (core/owner_only.py). On a Windows runner it
is the file's ACL, which chmod never changes, so there a list is made untrustworthy the Windows way:
an extra ACL entry for Everyone, which the owner-only check reads as a foreign entry.
"""

from __future__ import annotations

import contextlib
import json
import logging
import ntpath
import os
import subprocess
from pathlib import Path

import pytest

from core import windows_disposable_store as store
from core import windows_known_folders, windows_write_gate


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    vool_home = tmp_path / "vool-home"
    vool_home.mkdir()
    monkeypatch.setattr("core.runtime_paths.active_vool_home", lambda *a, **k: vool_home)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.delenv(windows_write_gate.ENV_VAR, raising=False)
    known = {"Profile": str(tmp_path / "profile")}
    (tmp_path / "profile").mkdir()
    monkeypatch.setattr(windows_known_folders, "known_folder", lambda name: known.get(name))
    store._reset_for_tests()
    yield vool_home
    store._reset_for_tests()


def _folder(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True)
    return path


def _restart() -> None:
    store._reset_for_tests()


def _assert_owner_only(path: Path, posix_mode: int) -> None:
    if os.name == "nt":
        from core.owner_only import verify_owner_only

        report = verify_owner_only(path)
        assert report.ok, report.reason
    else:
        assert (path.stat().st_mode & 0o777) == posix_mode


def _open_to_others(path: Path, posix_mode: int) -> None:
    """Make ``path`` readable by others: chmod on POSIX, an Everyone ACL entry on Windows."""
    if os.name != "nt":
        path.chmod(posix_mode)
        return
    icacls = ntpath.join(os.environ["SystemRoot"], "System32", "icacls.exe")
    # "(R)" with no inheritance flags: only this path's own DACL changes, so damaging the folder
    # never pushes an entry down onto the list file and the folder check is proven on its own.
    rights = "(R)"
    # *S-1-1-0 is Everyone; removed again by _close_to_others so pytest can clean the folder.
    subprocess.run([icacls, str(path), "/grant", f"*S-1-1-0:{rights}"], capture_output=True, check=True)


def _close_to_others(path: Path) -> None:
    if os.name == "nt":
        icacls = ntpath.join(os.environ["SystemRoot"], "System32", "icacls.exe")
        with contextlib.suppress(OSError, subprocess.CalledProcessError):
            subprocess.run([icacls, str(path), "/remove:g", "*S-1-1-0"], capture_output=True, check=True)


# --- the store -------------------------------------------------------------------------------


def test_a_mark_is_written_owner_only_and_counts_from_the_next_start(home, tmp_path) -> None:
    scratch = _folder(tmp_path, "scratch")
    assert windows_write_gate.flagged_roots() == []          # this process's start list: empty

    entry = store.mark(str(scratch))

    target = store.store_path()
    assert target.parent == home / "config"
    _assert_owner_only(target, 0o600)
    _assert_owner_only(target.parent, 0o700)
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == store.SCHEMA
    assert windows_write_gate.flagged_roots() == []          # an add waits for the restart
    assert store.load()["folders"][0]["pending_restart"] is True

    _restart()
    assert windows_write_gate.flagged_roots() == [os.path.normcase(os.path.realpath(scratch))]
    assert store.load()["folders"] == [{**entry, "refused": "", "active": True, "pending_restart": False,
                                        "syncs_to_cloud": False}]


def test_an_unmark_shrinks_at_once(home, tmp_path) -> None:
    scratch = _folder(tmp_path, "scratch")
    entry = store.mark(str(scratch))
    _restart()
    assert windows_write_gate.flagged_roots()

    assert store.unmark(entry["id"]) is True
    assert windows_write_gate.flagged_roots() == []
    assert store.unmark(entry["id"]) is False


@pytest.mark.parametrize("damage", ["world_readable", "corrupt", "unknown_schema", "folder_open"])
def test_a_list_that_cannot_be_trusted_adds_nothing(home, tmp_path, damage) -> None:
    scratch = _folder(tmp_path, "scratch")
    store.mark(str(scratch))
    target = store.store_path()
    if damage == "world_readable":
        _open_to_others(target, 0o644)
    elif damage == "corrupt":
        target.write_text("{not json", encoding="utf-8")
    elif damage == "unknown_schema":
        target.write_text(json.dumps({"schema": "other", "folders": [{"path": str(scratch)}]}), encoding="utf-8")
    else:
        _open_to_others(target.parent, 0o755)
    _restart()

    try:
        assert windows_write_gate.flagged_roots() == []
        assert store.load()["problem"]
        with pytest.raises(store.MarkRefused):
            store.mark(str(_folder(tmp_path, "other")))
    finally:
        _close_to_others(target)
        _close_to_others(target.parent)


def test_a_list_that_turns_untrustworthy_after_start_stops_counting(home, tmp_path) -> None:
    scratch = _folder(tmp_path, "scratch")
    store.mark(str(scratch))
    _restart()
    assert windows_write_gate.flagged_roots()

    _open_to_others(store.store_path(), 0o644)
    try:
        assert windows_write_gate.flagged_roots() == []
    finally:
        _close_to_others(store.store_path())


def test_the_variable_still_counts_beside_the_list(home, tmp_path, monkeypatch) -> None:
    env_folder = _folder(tmp_path, "from-env")
    monkeypatch.setenv(windows_write_gate.ENV_VAR, str(env_folder))
    store.mark(str(_folder(tmp_path, "from-list")))
    _restart()
    roots = windows_write_gate.flagged_roots()
    assert roots[0] == os.path.normcase(os.path.realpath(env_folder))
    assert len(roots) == 2


# --- what may never be marked ------------------------------------------------------------------


def test_protected_folders_and_what_holds_them_are_refused(home, tmp_path, monkeypatch) -> None:
    profile = tmp_path / "profile"
    documents = _folder(profile, "Documents")
    appdata = _folder(profile, "AppData")
    ssh = _folder(profile, ".ssh")
    known = {"Profile": str(profile), "Documents": str(documents), "LocalAppData": str(appdata / "Local")}
    (appdata / "Local").mkdir()
    monkeypatch.setattr(windows_known_folders, "known_folder", lambda name: known.get(name))

    refused = {
        "drive": "/",
        "profile": str(profile),
        "profile parent": str(tmp_path),
        "documents itself": str(documents),
        "credentials": str(_folder(ssh, "keys")),
        "app data": str(_folder(appdata, "Roaming")),
        "vool home": str(home),
        "inside vool home": str(_folder(home, "config-like")),
        "the app": str(Path(windows_write_gate.__file__).resolve().parents[1]),
    }
    for label, path in refused.items():
        assert store.refusal_reason(path), label
        with pytest.raises(store.MarkRefused):
            store.mark(path)
    assert store.refusal_reason(str(_folder(documents, "scratch"))) == ""
    assert store.refusal_reason(str(_folder(profile, "dev-scratch"))) == ""


def test_the_temp_root_is_refused_but_a_folder_inside_it_is_not(home, tmp_path, monkeypatch) -> None:
    temp = _folder(tmp_path, "AppData-ish") / "Temp"
    temp.mkdir()
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setattr(store.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(windows_known_folders, "known_folder",
                        lambda name: {"Profile": str(tmp_path / "profile"), "LocalAppData": str(temp.parent)}.get(name))
    assert store.refusal_reason(str(temp))
    assert store.refusal_reason(str(temp.parent))
    assert store.refusal_reason(str(_folder(temp, "job-52"))) == ""
    assert store.refusal_reason(str(_folder(temp.parent, "NotTemp")))


def test_a_link_anywhere_on_the_way_is_refused(home, tmp_path) -> None:
    real = _folder(tmp_path, "real")
    _folder(real, "inner")
    link = tmp_path / "via-link"
    link.symlink_to(real, target_is_directory=True)
    assert "link" in store.refusal_reason(str(link / "inner"))
    with pytest.raises(store.MarkRefused):
        store.mark(str(link / "inner"))


def test_a_folder_in_a_cloud_sync_root_is_marked_with_a_warning(home, tmp_path, monkeypatch) -> None:
    synced = _folder(tmp_path, "OneDrive") / "scratch"
    synced.mkdir()
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    assert store.refusal_reason(str(synced)) == ""
    assert store.in_sync_root(str(synced)) is True


# --- the broker gets the list at its start ----------------------------------------------------


def test_the_broker_start_takes_the_list_and_logs_it(home, tmp_path, monkeypatch, caplog) -> None:
    from core.windows_broker import process, session

    scratch = _folder(tmp_path, "scratch")
    store.mark(str(scratch))
    _restart()
    seen: dict[str, list[str]] = {}

    class _Host:
        def __init__(self, executable, flagged, journal_dir) -> None:
            seen["flagged"] = list(flagged)

        def start(self) -> None:
            return None

    monkeypatch.setattr(process, "_is_windows", lambda: True)
    monkeypatch.setattr(process, "broker_executable", lambda root: Path("VoolBroker.exe"))
    monkeypatch.setattr(process, "BrokerHost", _Host)
    monkeypatch.setattr(session, "register", lambda host: None)
    with caplog.at_level(logging.INFO, logger=process.__name__):
        process.start_for_api(tmp_path, home)
    assert seen["flagged"] == [os.path.realpath(scratch)]
    assert os.path.realpath(scratch) in caplog.text


def test_a_running_broker_list_shrinks_on_unmark(home, tmp_path, monkeypatch) -> None:
    from core.windows_broker import session

    env_folder = os.path.normcase(str(_folder(tmp_path, "from-env")))
    listed = _folder(tmp_path, "from-list")
    entry = store.mark(str(listed))
    _restart()
    store.start_folders()
    monkeypatch.setenv(windows_write_gate.ENV_VAR, env_folder)
    monkeypatch.setattr(session, "wired", lambda: True)
    monkeypatch.setattr(session, "checked_folders", lambda: [env_folder, os.path.normcase(os.path.realpath(listed))])
    assert len(windows_write_gate.flagged_roots()) == 2

    store.unmark(entry["id"])
    assert windows_write_gate.flagged_roots() == [env_folder]


# --- only the native window marks ---------------------------------------------------------------


class _Window:
    def __init__(self, picked) -> None:
        self.picked = picked

    def create_file_dialog(self, *_args, **_kwargs):
        return [self.picked] if self.picked else None


def _api(window, confirm):
    from installer.bundle.vool_window import _WindowApi

    api = _WindowApi()
    api.set_window(window)
    asked: list[str] = []

    def _confirm(title, message):
        asked.append(message)
        return confirm

    api._confirm_natively = _confirm
    return api, asked


@pytest.fixture
def _webview(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(FOLDER_DIALOG=20))


def test_the_window_marks_the_picked_folder_only_after_the_os_confirm(home, tmp_path, _webview) -> None:
    scratch = _folder(tmp_path, "scratch")
    api, asked = _api(_Window(str(scratch)), confirm=False)
    assert api.mark_disposable_folder()["cancelled"] is True
    assert not store.store_path().exists()

    api, asked = _api(_Window(str(scratch)), confirm=True)
    result = api.mark_disposable_folder("/some/page/supplied/path")
    assert result["ok"] is True and result["pending_restart"] is True
    assert os.path.realpath(scratch) in asked[0]
    assert [f["path"] for f in store.load()["folders"]] == [os.path.realpath(scratch)]


def test_a_refused_folder_never_reaches_the_confirm(home, tmp_path, _webview) -> None:
    api, asked = _api(_Window(str(home)), confirm=True)
    result = api.mark_disposable_folder()
    assert result["ok"] is False and result["refused"]
    assert asked == []


def test_unmark_takes_an_id_never_a_path(home, tmp_path, _webview) -> None:
    scratch = _folder(tmp_path, "scratch")
    entry = store.mark(str(scratch))
    api, _asked = _api(_Window(None), confirm=True)
    assert api.unmark_disposable_folder(str(scratch))["ok"] is False
    assert api.unmark_disposable_folder(entry["id"]) == {"ok": True}


def test_no_http_route_writes_the_list() -> None:
    source = (Path(windows_write_gate.__file__).resolve().parent / "web" / "api" / "service.py").read_text(encoding="utf-8")
    assert "windows_disposable_store.load()" in source
    for writer in ("windows_disposable_store.mark", "windows_disposable_store.unmark", "store.mark(", "store.unmark("):
        assert writer not in source


# --- chat names only what it can write (bug 3) -------------------------------------------------


def test_chat_offers_no_folder_when_none_is_marked(home, tmp_path) -> None:
    from core.agent_runtime import write_root_honesty

    workspace = _folder(tmp_path, "workspace")
    context = {"workspace": str(workspace)}
    assert write_root_honesty.allowed_write_roots(context) == []
    text = write_root_honesty.unreachable_write_response("C:\\Users\\x\\Downloads\\notes.txt", source_context=context)
    assert "~/Desktop" not in text and "~/Downloads" not in text
    assert "mark" in text.lower() and "Nothing was written" in text


def test_chat_names_the_marked_folder_it_can_write(home, tmp_path) -> None:
    from core.agent_runtime import write_root_honesty

    workspace = _folder(tmp_path, "workspace")
    store.mark(str(workspace))
    _restart()
    roots = write_root_honesty.allowed_write_roots({"workspace": str(workspace)})
    assert [os.path.normcase(str(r)) for r in roots] == [os.path.normcase(os.path.realpath(workspace))]
    text = write_root_honesty.unreachable_write_response("/elsewhere/x.txt", source_context={"workspace": str(workspace)})
    assert os.path.realpath(workspace) in text and "~/Desktop" not in text


# --- Setup ticks a folder only once it is marked (bug 4) ---------------------------------------


def test_setup_folder_step_waits_for_the_mark(home, tmp_path, monkeypatch) -> None:
    from core import project_store, setup_progress

    picked = _folder(tmp_path, "vool-ws-52")
    monkeypatch.setattr(project_store, "list_projects", lambda: [{"root": str(picked), "exists": True}])
    monkeypatch.delenv("VOOL_WORKSPACE_ROOT", raising=False)

    assert setup_progress.folder_done() is False
    assert setup_progress.folder_unmarked() is True
    folder_step = next(s for s in setup_progress.snapshot()["steps"] if s["id"] == "folder")
    assert folder_step["done"] is False and "mark" in folder_step["note"].lower()

    store.mark(str(picked))
    _restart()
    assert setup_progress.folder_done() is True
    assert "note" not in next(s for s in setup_progress.snapshot()["steps"] if s["id"] == "folder")


def test_off_windows_the_setup_folder_step_is_unchanged(tmp_path, monkeypatch) -> None:
    from core import project_store, setup_progress

    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    picked = _folder(tmp_path, "project")
    monkeypatch.setattr(project_store, "list_projects", lambda: [{"root": str(picked), "exists": True}])
    assert setup_progress.folder_done() is True
    assert setup_progress.folder_unmarked() is False


# --- 🔐 review round 1 -----------------------------------------------------------------------------


def test_without_a_junction_check_every_folder_is_refused(home, tmp_path, monkeypatch) -> None:
    # Before Python 3.12 os.path has no isjunction: an unchecked junction must refuse, not pass.
    scratch = _folder(tmp_path, "scratch")
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    assert store.refusal_reason(str(scratch))
    with pytest.raises(store.MarkRefused):
        store.mark(str(scratch))


def test_on_windows_any_reparse_point_on_the_way_is_refused(home, tmp_path, monkeypatch) -> None:
    # A mount point or another reparse type is no symlink and no junction, but still redirects.
    mounted = _folder(tmp_path, "mounted")
    inner = _folder(mounted, "inner")
    monkeypatch.setattr(store, "_IS_NT", True)
    monkeypatch.setattr(store, "_reparse_attribute",
                        lambda path: os.path.normcase(str(path)) == os.path.normcase(str(mounted)))
    assert "mounted" in store.refusal_reason(str(inner))
    monkeypatch.setattr(store, "_reparse_attribute", lambda path: False)
    assert store.refusal_reason(str(inner)) == ""


def test_an_unmark_shrinks_the_running_broker_list_under_another_spelling_of_home(home, tmp_path, monkeypatch) -> None:
    from core.windows_broker import session

    listed = _folder(tmp_path, "from-list")
    entry = store.mark(str(listed))
    _restart()
    linked_home = tmp_path / "home-link"
    linked_home.symlink_to(home, target_is_directory=True)
    store.start_folders(str(linked_home) + os.sep + "." + os.sep)   # the broker start, spelled its way
    monkeypatch.setattr(session, "wired", lambda: True)
    monkeypatch.setattr(session, "checked_folders", lambda: [os.path.normcase(os.path.realpath(listed))])
    assert windows_write_gate.flagged_roots() == [os.path.normcase(os.path.realpath(listed))]

    store.unmark(entry["id"])
    assert windows_write_gate.flagged_roots() == []


def test_temp_inside_vool_home_lifts_nothing(home, tmp_path, monkeypatch) -> None:
    temp = _folder(home, "tmp")
    monkeypatch.setattr(store.tempfile, "gettempdir", lambda: str(temp))
    assert store.refusal_reason(str(_folder(temp, "job-52")))


def test_temp_inside_a_credential_folder_lifts_nothing(home, tmp_path, monkeypatch) -> None:
    temp = _folder(tmp_path / "profile", ".ssh") / "tmp"
    temp.mkdir()
    monkeypatch.setattr(store.tempfile, "gettempdir", lambda: str(temp))
    assert store.refusal_reason(str(_folder(temp, "job-52")))


def test_the_temp_root_comes_from_the_system_not_from_the_raw_variable(home, tmp_path, monkeypatch) -> None:
    real_temp = _folder(tmp_path, "system-temp")
    monkeypatch.setattr(store.tempfile, "gettempdir", lambda: str(real_temp))
    decoy = _folder(tmp_path, "decoy")
    monkeypatch.setenv("TEMP", str(decoy))
    monkeypatch.setenv("TMP", str(decoy))
    assert store.refusal_reason(str(decoy)) == ""          # not treated as the temp root
    assert store.refusal_reason(str(real_temp))


# --- 🔐 review round 2: OneDrive placeholders are cloud reparse points ----------------------------


def _reparse_stat(tag=None, attributes=0x400):
    import types

    fields = {"st_file_attributes": attributes}
    if tag is not None:
        fields["st_reparse_tag"] = tag
    return types.SimpleNamespace(**fields)


@pytest.mark.parametrize(
    ("label", "stat_result", "refused"),
    [
        ("cloud", _reparse_stat(0x9000001A), False),          # IO_REPARSE_TAG_CLOUD
        ("cloud_3", _reparse_stat(0x9000301A), False),        # IO_REPARSE_TAG_CLOUD_3
        ("cloud_f", _reparse_stat(0x9000F01A), False),        # IO_REPARSE_TAG_CLOUD_F
        ("mount_point", _reparse_stat(0xA0000003), True),     # name surrogate
        ("symlink", _reparse_stat(0xA000000C), True),         # name surrogate
        ("other", _reparse_stat(0x80000014), True),
        ("tag_unreadable", _reparse_stat(None), True),
        ("no_attributes", type("S", (), {})(), True),
        ("plain", _reparse_stat(None, attributes=0x10), False),
    ],
)
def test_only_a_cloud_sync_reparse_point_is_passed_on_the_way(home, tmp_path, monkeypatch, label, stat_result, refused) -> None:
    synced = _folder(tmp_path, "OneDrive-ish")
    inner = _folder(synced, "scratch")
    monkeypatch.setattr(store, "_IS_NT", True)
    real_lstat = os.lstat

    def _lstat(path):
        return stat_result if os.path.normcase(str(path)) == os.path.normcase(str(synced)) else _reparse_stat(None, 0x10)

    monkeypatch.setattr(store, "_lstat", _lstat)
    assert bool(store.refusal_reason(str(inner))) is refused, label
    assert os.lstat is real_lstat


def test_an_unreadable_component_is_refused(home, tmp_path, monkeypatch) -> None:
    inner = _folder(tmp_path, "gone") / "x"
    inner.mkdir()
    monkeypatch.setattr(store, "_IS_NT", True)

    def _lstat(path):
        raise PermissionError(5, "denied")

    monkeypatch.setattr(store, "_lstat", _lstat)
    assert store.refusal_reason(str(inner))
