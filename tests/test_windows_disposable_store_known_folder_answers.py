"""Windows: which known-folder answers let the owner mark a disposable folder (owner decisions D13 A, D14 A).

- A PC without OneDrive (the shell says there is no such folder) can still mark.
- A known-folder lookup that fails or errors refuses, OneDrive included.
- While any known folder is redirected away from its usual place in the profile (Documents moved
  elsewhere, OneDrive folder backup, a Documents junction), no folder is marked at all.
- With every known folder where Windows puts it, the test-contract rows `control_documents` (a
  folder inside Documents) and `inside_TEMP` (a folder inside the raw TEMP) are still marked.
- Each known folder is asked once per check, so a lookup that answers once and then fails cannot
  leave the profile's private folders unsealed.

The shell is faked on Linux in two places: `windows_known_folders`' own `ctypes.windll` calls, to
prove how its answers are read, and `known_folder` itself, which notes each answer the way the real
one does, to drive the store with real POSIX folders.
"""
from __future__ import annotations

import ctypes
import os
import types
from pathlib import Path

import pytest

from core import windows_disposable_store as store
from core import windows_known_folders as kf
from core import windows_path_policy
from core import windows_write_gate as gate

#: The real lookup, taken before any fixture replaces it.
_REAL_KNOWN_FOLDER = kf.known_folder
#: The answers `windows_known_folders.outcome` gives (spelled out, so each row runs on its own).
FOUND, ABSENT, FAILED = "found", "absent", "failed"
_USER = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


# --- how windows_known_folders reads the shell ------------------------------------------------


def _fake_windll(monkeypatch, answers: dict[str, object]) -> None:
    """SHGetKnownFolderPath answering per folder id: a path (S_OK), an HRESULT, or an exception."""
    by_id = {kf._FOLDER_IDS[name].strip("{}").upper(): value for name, value in answers.items()}

    def _get(guid_ref, flags, token, found_ref):
        guid = guid_ref._obj
        import uuid

        key = str(uuid.UUID(fields=(guid.data1, guid.data2, guid.data3, guid.data4[0], guid.data4[1],
                                    int.from_bytes(bytes(guid.data4[2:]), "big")))).upper()
        value = by_id.get(key, 0x80070002)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, str):
            found_ref._obj.value = value
            return 0
        return ctypes.c_long(value - (1 << 32) if value >= 1 << 31 else value).value

    windll = types.SimpleNamespace(shell32=types.SimpleNamespace(SHGetKnownFolderPath=_get),
                                   ole32=types.SimpleNamespace(CoTaskMemFree=lambda pointer: None))
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    monkeypatch.setattr(kf, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(kf, "_drive_is_remote", lambda drive: False)


@pytest.mark.parametrize(
    ("label", "answer", "expected"),
    [
        ("found", r"C:\Users\me\OneDrive", FOUND),
        ("file_not_found", 0x80070002, ABSENT),
        ("path_not_found", 0x80070003, ABSENT),
        ("not_on_this_system", 0x80070057, ABSENT),
        ("e_fail", 0x80004005, "failed: shell answer 0x80004005"),
        ("access_denied", 0x80070005, "failed: shell answer 0x80070005"),
        ("raises", OSError("shell gone"), "failed: OSError"),
        ("share", r"\\server\home\OneDrive", "failed: not a full local path"),
    ],
)
def test_the_shell_answer_is_read_as_found_absent_or_failed(monkeypatch, label, answer, expected) -> None:
    _fake_windll(monkeypatch, {"OneDrive": answer})
    reported = kf.known_folder("OneDrive")
    assert kf.outcome("OneDrive") == expected, label
    assert (reported is not None) is (expected == FOUND)


def test_a_shell_that_answers_nothing_and_says_nothing_failed(monkeypatch) -> None:
    monkeypatch.setattr(kf, "_IS_WINDOWS", True)
    monkeypatch.setattr(kf, "_shell_known_folder", lambda name: None)
    assert kf.known_folder("OneDrive") is None
    assert kf.outcome("OneDrive").startswith(FAILED)


def test_off_windows_nothing_is_asked(monkeypatch) -> None:
    monkeypatch.setattr(kf, "_IS_WINDOWS", False)
    assert kf.known_folder("OneDrive") is None
    assert kf.outcome("OneDrive") == ""


# --- what the store makes of the answers --------------------------------------------------------


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A profile with every known folder where Windows puts it, VOOL home beside it, and a fake
    `known_folder` that notes each answer as the real one does (``answers`` may be edited)."""
    profile = tmp_path / "Users" / "VOOLT"
    folders = {"Profile": profile, **{name: profile / name for name in _USER},
               "LocalAppData": profile / "AppData" / "Local", "RoamingAppData": profile / "AppData" / "Roaming",
               "OneDrive": profile / "OneDrive", "ProgramData": tmp_path / "ProgramData",
               "Windows": tmp_path / "Windows", "ProgramFiles": tmp_path / "Program Files",
               "ProgramFilesX86": tmp_path / "Program Files (x86)"}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    temp = folders["LocalAppData"] / "Temp"
    temp.mkdir()
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    vool_home = tmp_path / "vool-home"
    vool_home.mkdir()
    monkeypatch.setattr("core.runtime_paths.active_vool_home", lambda *a, **k: vool_home)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    for name in ("TEMP", "TMP"):
        monkeypatch.setenv(name, str(temp))
    monkeypatch.setenv("LOCALAPPDATA", str(folders["LocalAppData"]))
    monkeypatch.setattr(store.tempfile, "gettempdir", lambda: str(temp))
    answers: dict[str, object] = {name: str(path) for name, path in folders.items()}
    calls: list[str] = []
    _note = getattr(kf, "_record", lambda name, kind: None)

    def _known_folder(name: str) -> str | None:
        calls.append(name)
        value = answers.get(name, ABSENT)
        if callable(value):
            value = value()
        if isinstance(value, str) and value not in (ABSENT,) and not value.startswith(FAILED):
            _note(name, FOUND)
            return value
        _note(name, str(value))
        return None

    monkeypatch.setattr(kf, "known_folder", _known_folder)
    store._reset_for_tests()
    yield {"profile": profile, "temp": temp, "elsewhere": elsewhere, "vool_home": vool_home,
           "answers": answers, "calls": calls, **{name: Path(path) for name, path in folders.items()}}
    store._reset_for_tests()


def _inside(folder: Path, name: str = "scratch") -> Path:
    path = folder / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _candidates(machine) -> dict[str, Path]:
    """Folders a normal layout lets the owner mark (each is asserted markable in the control row)."""
    return {"control_documents": _inside(machine["Documents"]),
            "inside_TEMP": _inside(machine["temp"], "job-52"),
            "outside_profile": _inside(machine["elsewhere"]),
            "inside_profile": _inside(machine["profile"], "dev-scratch")}


def test_with_every_known_folder_in_place_the_contract_rows_are_marked(machine) -> None:
    for label, folder in _candidates(machine).items():
        assert store.refusal_reason(str(folder)) == "", label
    entry = store.mark(str(_candidates(machine)["control_documents"]))
    store._reset_for_tests()
    assert gate.flagged_roots() == [os.path.normcase(os.path.realpath(entry["path"]))]


def test_a_pc_without_onedrive_can_still_mark(machine) -> None:
    machine["answers"]["OneDrive"] = ABSENT
    for label, folder in _candidates(machine).items():
        assert store.refusal_reason(str(folder)) == "", label
    assert store.mark(str(_candidates(machine)["control_documents"]))["path"]


@pytest.mark.parametrize("failure", ["failed: shell answer 0x80004005", "failed: OSError", FAILED])
def test_a_failed_onedrive_lookup_refuses_every_mark(machine, failure) -> None:
    machine["answers"]["OneDrive"] = failure
    for label, folder in _candidates(machine).items():
        reason = store.refusal_reason(str(folder))
        assert "OneDrive" in reason and "could not say" in reason, (label, reason)
    with pytest.raises(store.MarkRefused):
        store.mark(str(_candidates(machine)["control_documents"]))
    assert not store.store_path().exists()


@pytest.mark.parametrize("name", ["Documents", "Desktop", "LocalAppData", "ProgramData", "Windows"])
def test_a_failed_lookup_of_any_other_known_folder_refuses(machine, name) -> None:
    machine["answers"][name] = "failed: OSError"
    reason = store.refusal_reason(str(_candidates(machine)["outside_profile"]))
    assert name in reason and "could not say" in reason, reason


@pytest.mark.parametrize("name", ["Documents", "Desktop", "LocalAppData"])
def test_only_onedrive_may_be_absent(machine, name) -> None:
    machine["answers"][name] = ABSENT
    assert name in store.refusal_reason(str(_candidates(machine)["outside_profile"]))


@pytest.mark.parametrize("how", ["moved_outside_the_profile", "onedrive_backup", "junction"])
def test_a_redirected_documents_refuses_every_mark(machine, how) -> None:
    candidates = _candidates(machine)
    if how == "moved_outside_the_profile":
        moved = _inside(machine["elsewhere"].parent, "D-Documents")
    elif how == "onedrive_backup":
        moved = _inside(machine["profile"] / "OneDrive", "Documents")
    else:
        # Documents in its usual spelling, but a junction (a link here) to another folder.
        target = _inside(machine["elsewhere"].parent, "real-documents")
        for child in machine["Documents"].iterdir():
            child.rename(target / child.name)
        machine["Documents"].rmdir()
        machine["Documents"].symlink_to(target, target_is_directory=True)
        moved = machine["Documents"]
        candidates.pop("control_documents")  # a folder through a link is refused for that already
    machine["answers"]["Documents"] = str(moved)
    for label, folder in candidates.items():
        reason = store.refusal_reason(str(folder))
        assert "Documents folder is redirected" in reason, (label, reason)
        with pytest.raises(store.MarkRefused):
            store.mark(str(folder))
    assert not store.store_path().exists()


@pytest.mark.parametrize("name", ["Desktop", "Downloads", "Pictures", "Music", "Videos", "RoamingAppData"])
def test_any_redirected_user_folder_refuses_every_mark(machine, name) -> None:
    machine["answers"][name] = str(_inside(machine["elsewhere"].parent, f"moved-{name}"))
    reason = store.refusal_reason(str(_candidates(machine)["outside_profile"]))
    assert f"{name} folder is redirected" in reason, reason


def test_an_entry_already_listed_stops_counting_while_a_folder_is_redirected(machine) -> None:
    folder = _candidates(machine)["outside_profile"]
    store.mark(str(folder))
    store._reset_for_tests()
    assert gate.flagged_roots()
    machine["answers"]["Documents"] = str(_inside(machine["elsewhere"].parent, "D-Documents"))
    store._reset_for_tests()
    assert gate.flagged_roots() == []
    shown = store.load()["folders"]
    assert len(shown) == 1 and "redirected" in shown[0]["refused"] and shown[0]["active"] is False


def test_each_known_folder_is_asked_once_per_check(machine) -> None:
    store.refusal_reason(str(_candidates(machine)["inside_TEMP"]))
    asked = machine["calls"]
    assert asked and len(asked) == len(set(asked)), asked


def test_a_profile_that_answers_once_then_fails_still_seals_its_private_folders(machine) -> None:
    keys = _inside(machine["profile"] / ".ssh", "keys")
    answered: list[int] = []
    profile = str(machine["profile"])

    def _flaky() -> str:
        answered.append(1)
        return profile if len(answered) == 1 else "failed: OSError"

    machine["answers"]["Profile"] = _flaky
    reason = store.refusal_reason(str(keys))
    assert ".ssh" in reason, reason


def test_a_real_lookup_that_returns_none_unexplained_is_failed_and_refuses(machine, monkeypatch) -> None:
    """The real `known_folder`, with its shell call cut short: None comes back before any step
    noted why. That None counts as a failed lookup, so OneDrive is not taken as simply absent."""
    monkeypatch.setattr(kf, "_IS_WINDOWS", True)
    monkeypatch.setattr(kf, "_shell_known_folder", lambda name: None)  # records no outcome
    assert _REAL_KNOWN_FOLDER("OneDrive") is None
    assert kf.outcome("OneDrive") == FAILED

    fake = kf.known_folder
    monkeypatch.setattr(kf, "known_folder", lambda name: _REAL_KNOWN_FOLDER(name) if name == "OneDrive" else fake(name))
    folder = _candidates(machine)["control_documents"]
    reason = store.refusal_reason(str(folder))
    assert "OneDrive" in reason and "could not say" in reason, reason
    with pytest.raises(store.MarkRefused):
        store.mark(str(folder))
    assert not store.store_path().exists()
