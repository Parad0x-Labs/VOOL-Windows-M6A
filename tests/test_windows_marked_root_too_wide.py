"""Windows: a marked folder that would open a whole drive, the profile or the temp folders is no write root.

VOOL_WINDOWS_DISPOSABLE_WORKSPACES (and the owner's list, core/windows_disposable_store.py, and the
broker's start list) took any existing local folder as a write root. Marking `C:\\`, `%USERPROFILE%`
or a folder that holds TEMP, TMP or LOCALAPPDATA (where VOOL itself is installed) would have let a
gated write land anywhere there. Such an entry is now skipped with a warning, judged on resolved
paths (a junction on either side is judged by its target), and it leaves the capability report's
marked folders and the folders chat offers as writable. A normal folder, under Documents or
elsewhere, is still marked.

The Windows decisions are driven on Linux through the gate's `_IS_WINDOWS` flag, as the gate's other
tests do; the POSIX root `/` stands in for a drive root, and symlinks stand in for junctions (both
are followed by `os.path.realpath`). Case-insensitive matching (`os.path.normcase` on Windows) can
only be shown on a Windows host.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from core import windows_disposable_store as store
from core import windows_known_folders
from core import windows_write_gate as gate
from core.agent_runtime import write_root_honesty
from core.windows_broker import process as broker_process
from core.windows_broker import session
from core.windows_broker.process import BrokerStatus, FlaggedFolder

CASES = ["drive_root", "profile_root", "holds_TEMP", "holds_TMP", "holds_LOCALAPPDATA", "control_documents"]


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A profile with Documents, AppData\\Local and AppData\\Local\\Temp, every variable pointing at them,
    and one ordinary marked folder outside the profile that must stay marked in every row."""
    profile = tmp_path / "Users" / "VOOLT"
    documents = profile / "Documents"
    local = profile / "AppData" / "Local"
    temp = local / "Temp"
    for folder in (documents, temp):
        folder.mkdir(parents=True)
    scratch = documents / "scratch"
    scratch.mkdir()
    elsewhere = tmp_path / "work" / "keep"
    elsewhere.mkdir(parents=True)
    # A holder of TEMP/TMP/LOCALAPPDATA that is not the profile or one of its ancestors.
    side = tmp_path / "side"
    (side / "Temp").mkdir(parents=True)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    for name in ("TEMP", "TMP", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(temp if name != "LOCALAPPDATA" else local))
    gate._WARNED.clear()
    return {"profile": profile, "documents": documents, "scratch": scratch, "local": local, "temp": temp,
            "elsewhere": elsewhere, "side": side}


def _marked_for(case: str, machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """The folder marked in this row (and the variable it is checked against, where it has one)."""
    if case == "drive_root":
        return Path("/")
    if case == "profile_root":
        return machine["profile"]
    if case.startswith("holds_"):
        name = case.removeprefix("holds_")
        # Only this one variable points into `side`; the others stay in the profile.
        monkeypatch.setenv(name, str(machine["side"] / "Temp"))
        return machine["side"]
    return machine["scratch"]


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


@pytest.mark.parametrize("case", CASES)
def test_a_too_wide_marked_root_is_skipped_everywhere(machine, monkeypatch, caplog, case: str) -> None:
    marked = _marked_for(case, machine, monkeypatch)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(marked), str(machine["elsewhere"])]))
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        roots = gate.flagged_roots()
    report = gate.capability_report()
    offered = [_norm(root) for root in write_root_honesty.allowed_write_roots({"workspace": str(machine["elsewhere"])})]
    target = (machine["scratch"] if case == "control_documents" else machine["documents"]) / "hello.txt"
    refused = gate.refusal("machine.write_file", {"path": str(target)}, None)
    if case == "control_documents":
        assert roots == [_norm(marked), _norm(machine["elsewhere"])]
        assert report["marked_folders"] == roots
        assert _norm(marked) in offered
        assert refused is None
        assert not [r for r in caplog.records if "is not flagged" in r.getMessage()]
        return
    assert roots == [_norm(machine["elsewhere"])], case
    assert report["marked_folders"] == [_norm(machine["elsewhere"])]
    assert offered == [_norm(machine["elsewhere"])]
    assert refused is not None and "outside the marked folders" in refused
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "is not flagged" in r.getMessage()]
    assert len(warnings) == 1 and str(marked) in warnings[0]


def test_the_profile_is_judged_after_resolving_a_link_to_it(machine, monkeypatch) -> None:
    # USERPROFILE spelled through a link (a junction on Windows) still names the marked folder.
    linked = machine["profile"].parent / "VOOLT-link"
    linked.symlink_to(machine["profile"], target_is_directory=True)
    monkeypatch.setenv("USERPROFILE", str(linked))
    monkeypatch.setenv("HOME", str(machine["elsewhere"].parent / "unrelated-home"))
    monkeypatch.setenv(gate.ENV_VAR, str(machine["profile"]))
    assert gate.flagged_roots() == []


def test_a_temp_reached_through_a_link_is_still_held(machine, monkeypatch) -> None:
    # TEMP spelled through a link that leads into the marked folder.
    link = machine["elsewhere"].parent / "temp-link"
    link.symlink_to(machine["side"] / "Temp", target_is_directory=True)
    monkeypatch.setenv("TEMP", str(link))
    monkeypatch.setenv(gate.ENV_VAR, str(machine["side"]))
    assert gate.flagged_roots() == []


# --- the broker's list, read separately in core/windows_broker/process.py -----------------------


@pytest.mark.parametrize("case", CASES)
def test_the_broker_is_not_handed_a_too_wide_folder(machine, monkeypatch, tmp_path, case: str) -> None:
    marked = _marked_for(case, machine, monkeypatch)
    seen: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, executable: Path, flagged: list[str], journal_dir: Path) -> None:
            seen["flagged"] = flagged
            self.status = BrokerStatus(False, "recorded")

        def start(self) -> BrokerStatus:
            return self.status

    published = tmp_path / "windows" / "broker" / "publish"
    published.mkdir(parents=True)
    (published / "VoolBroker.exe").write_bytes(b"")
    monkeypatch.setattr(broker_process, "_is_windows", lambda: True)
    monkeypatch.setattr(broker_process, "BrokerHost", _Recorder)
    environ = {gate.ENV_VAR: ";".join([str(marked), str(machine["elsewhere"])])}
    broker_process.start_for_api(tmp_path, tmp_path / "vool-home", environ=environ)
    expected = [str(marked), str(machine["elsewhere"])] if case == "control_documents" else [str(machine["elsewhere"])]
    assert seen["flagged"] == expected


class _Host:
    secret = b"s" * 32


class _Client:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def connect(self) -> None:
        pass

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        return {"ok": True, "path": self.paths[arguments["flagged_id"]]}

    def close(self) -> None:
        pass


@pytest.mark.parametrize("case", CASES)
def test_a_too_wide_folder_the_running_broker_reports_is_skipped(machine, monkeypatch, case: str) -> None:
    marked = _marked_for(case, machine, monkeypatch)
    folders = {"a" * 48: str(marked), "b" * 48: str(machine["elsewhere"])}
    host = _Host()
    host.status = BrokerStatus(True, "", r"\\.\pipe\vool-broker-test", 4242,  # type: ignore[attr-defined]
                               tuple(FlaggedFolder(key, path) for key, path in folders.items()))
    session.register(host)  # type: ignore[arg-type]
    monkeypatch.setattr(session, "_client_for", lambda status, secret: _Client(folders))
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    roots = gate.flagged_roots()
    expected = [os.path.normcase(str(marked)), os.path.normcase(str(machine["elsewhere"]))]
    assert roots == (expected if case == "control_documents" else expected[1:])
    assert gate.capability_report()["marked_folders"] == roots


# --- the owner's list -----------------------------------------------------------------------------


@pytest.fixture
def vool_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine) -> Path:
    home = tmp_path / "vool-home"
    home.mkdir()
    monkeypatch.setattr("core.runtime_paths.active_vool_home", lambda *a, **k: home)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    known = {"Profile": str(machine["profile"]), "Documents": str(machine["documents"])}
    monkeypatch.setattr(windows_known_folders, "known_folder", lambda name: known.get(name))
    store._reset_for_tests()
    yield home
    store._reset_for_tests()


@pytest.mark.parametrize("case", ["holds_TEMP", "holds_TMP", "holds_LOCALAPPDATA", "control_documents"])
def test_the_owner_cannot_mark_a_folder_holding_the_temp_or_app_data_folders(vool_home, machine, monkeypatch,
                                                                             case: str) -> None:
    marked = _marked_for(case, machine, monkeypatch)
    if case == "control_documents":
        store.mark(str(marked))
        store._reset_for_tests()
        assert gate.flagged_roots() == [_norm(marked)]
        return
    with pytest.raises(store.MarkRefused, match=case.removeprefix("holds_")):
        store.mark(str(marked))


def test_an_entry_already_in_the_owners_list_that_holds_temp_counts_nowhere(vool_home, machine, monkeypatch) -> None:
    marked = _marked_for("holds_TEMP", machine, monkeypatch)
    target = store.store_path()
    from core.owner_only import ensure_private_dir, require_owner_only

    ensure_private_dir(target.parent)
    target.write_text(json.dumps({"schema": store.SCHEMA, "folders": [{"path": str(marked), "marked_at": ""}]}),
                      encoding="utf-8")
    require_owner_only(str(target))
    store._reset_for_tests()
    assert gate.flagged_roots() == []
    shown = store.load()["folders"]
    assert len(shown) == 1 and "TEMP" in shown[0]["refused"] and shown[0]["active"] is False


@pytest.mark.parametrize("case", ["is_TEMP", "is_TMP", "holds_TEMP", "holds_TMP", "inside_TEMP"])
def test_the_owners_list_judges_raw_temp_and_tmp_as_the_gate_does(vool_home, machine, monkeypatch, case: str) -> None:
    """refusal_reason (what the Settings list and the window bridge ask) refuses a folder that is, or
    holds, the raw TEMP or TMP variable, as the gate skips it; a folder inside them stays markable."""
    name = case.rsplit("_", 1)[1]
    raw_temp = machine["side"] / "Temp"
    monkeypatch.setenv(name, str(raw_temp))
    inside = raw_temp / "job-52"
    inside.mkdir()
    folder = {"is": raw_temp, "holds": machine["side"], "inside": inside}[case.split("_", 1)[0]]
    reason = store.refusal_reason(str(folder))
    if case == "inside_TEMP":
        assert reason == ""
    else:
        assert name in reason, (case, reason)


# --- the test harness's own folders inside TEMP stay marked ---------------------------------------


def test_folders_inside_temp_that_the_harness_marks_stay_marked(machine, monkeypatch) -> None:
    """Only TEMP, TMP and LOCALAPPDATA themselves, or what holds them, are skipped. The folders
    tests/conftest.py `windows_own_tmp_path_is_disposable` marks on Windows all sit inside %TEMP%
    (pytest's basetemp): a test's own tmp_path, its `tempfile-N` folder and the session home's
    workspace. Marked through the harness's own helper, they all stay marked."""
    from tests._windows_disposable_tmp import SESSION_HOME_PREFIX, mark_own_tmp_path_disposable

    basetemp = machine["temp"] / "pytest-of-voolt" / "pytest-7"
    own = basetemp / "test_x0"
    tempfile_root = basetemp / "tempfile-0"
    workspace = machine["temp"] / f"{SESSION_HOME_PREFIX}abc123" / "workspace"
    for folder in (own, tempfile_root, workspace):
        folder.mkdir(parents=True)
    mark_own_tmp_path_disposable(monkeypatch, own, windows=True, tempfile_root=tempfile_root,
                                 session_workspace=workspace)
    assert gate.flagged_roots() == [_norm(own), _norm(tempfile_root), _norm(workspace)]
    # TEMP itself, listed beside them, is the one skipped.
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(machine["temp"]), str(own)]))
    assert gate.flagged_roots() == [_norm(own)]
