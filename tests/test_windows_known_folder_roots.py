"""On Windows the safe machine folders are the ones Windows reports (FOLDERID_Desktop, _Documents,
_Downloads), so a Desktop that OneDrive or a policy moved is the one VOOL uses. A known folder on a
share or a network drive is never used, and nor is the shell's answer when the home was redirected
(HOME/USERPROFILE pointed elsewhere, as tests and portable setups do): then the profile spelling
stays, as before. Off Windows nothing changes."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import windows_known_folders as kf

PROFILE = "C:\\Users\\me"
MOVED = {
    "Desktop": "C:\\Users\\me\\OneDrive\\Desktop",
    "Documents": "C:\\Users\\me\\OneDrive\\Documents",
    "Downloads": "D:\\Downloads",
}


def _shell(answers: dict[str, str | None]):
    def fake(name: str) -> str | None:
        return answers.get(name)

    return fake


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(kf, "_IS_WINDOWS", True)
    monkeypatch.setattr(kf, "_drive_is_remote", lambda drive: False)
    monkeypatch.setattr(kf, "_resolved", lambda value: value)
    return monkeypatch


@pytest.mark.parametrize("name", ["Desktop", "Documents", "Downloads"])
def test_windows_uses_the_folder_the_shell_reports(on_windows, name) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, **MOVED}))
    assert str(kf.safe_folder(name, Path(PROFILE))) == MOVED[name]


def test_off_windows_the_home_spelling_is_kept(monkeypatch) -> None:
    monkeypatch.setattr(kf, "_IS_WINDOWS", False)
    monkeypatch.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, **MOVED}))
    assert kf.safe_folder("Desktop", Path("/home/me")) == Path("/home/me") / "Desktop"


def test_a_redirected_home_keeps_its_own_spelling(on_windows) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, **MOVED}))
    home = Path("C:\\tmp\\disposable-home")
    assert kf.safe_folder("Desktop", home) == home / "Desktop"


@pytest.mark.parametrize(
    "reported",
    [
        "\\\\server\\share\\Desktop",
        "//server/share/Desktop",
        "\\\\?\\UNC\\server\\share\\Desktop",
        "\\\\.\\pipe\\x",
        "%LOGONSERVER%\\share\\Desktop",
        "Desktop",
        "\\Desktop",
        "C:Desktop",
        "",
        None,
    ],
)
def test_a_share_device_or_partial_answer_is_never_used(on_windows, reported) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, "Desktop": reported}))
    assert kf.safe_folder("Desktop", Path(PROFILE)) == Path(PROFILE) / "Desktop"


def test_a_known_folder_on_a_network_drive_is_never_used(on_windows) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, "Desktop": "Z:\\Desktop"}))
    on_windows.setattr(kf, "_drive_is_remote", lambda drive: drive.upper().startswith("Z:"))
    assert kf.safe_folder("Desktop", Path(PROFILE)) == Path(PROFILE) / "Desktop"


def test_a_drive_whose_type_cannot_be_read_is_never_used(on_windows) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, "Desktop": "E:\\Desktop"}))
    on_windows.setattr(kf, "_drive_is_remote", lambda drive: None)
    assert kf.safe_folder("Desktop", Path(PROFILE)) == Path(PROFILE) / "Desktop"


@pytest.mark.parametrize("reported", ["C:\\", "D:\\", "C:\\Users\\me", "c:\\users\\ME\\", "C:\\Users", "C:\\Users\\me\\."])
def test_a_drive_root_or_a_folder_holding_the_profile_is_never_used(on_windows, reported) -> None:
    """Documents redirected to the drive or to the profile itself would make the whole drive or
    profile a write root."""
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, "Documents": reported}))
    assert kf.safe_folder("Documents", Path(PROFILE)) == Path(PROFILE) / "Documents"


@pytest.mark.parametrize("target", ["\\\\srv\\share\\Desktop", "C:\\", "C:\\Users\\me", "Z:\\Desktop"])
def test_a_junction_to_a_share_the_drive_or_the_profile_is_never_used(on_windows, target) -> None:
    """The callers resolve the folder, so a junction (`mklink /J`, no admin needed) is judged by where
    it leads, not by its own spelling."""
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, **MOVED}))
    on_windows.setattr(kf, "_resolved", lambda value: target if value == MOVED["Desktop"] else value)
    on_windows.setattr(kf, "_drive_is_remote", lambda drive: drive.upper().startswith("Z:"))
    assert kf.safe_folder("Desktop", Path(PROFILE)) == Path(PROFILE) / "Desktop"


def test_the_resolved_folder_is_what_is_used(on_windows) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, **MOVED}))
    on_windows.setattr(kf, "_resolved", lambda value: "C:\\Users\\me\\OneDrive - Org\\Desktop" if value == MOVED["Desktop"] else value)
    assert str(kf.safe_folder("Desktop", Path(PROFILE))) == "C:\\Users\\me\\OneDrive - Org\\Desktop"


def test_no_profile_answer_keeps_the_home_spelling(on_windows) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell(MOVED))
    assert kf.safe_folder("Desktop", Path(PROFILE)) == Path(PROFILE) / "Desktop"


def test_an_unknown_folder_name_keeps_the_home_spelling(on_windows) -> None:
    on_windows.setattr(kf, "_shell_known_folder", _shell({"Profile": PROFILE, "Music": "C:\\Music"}))
    assert kf.safe_folder("Music", Path(PROFILE)) == Path(PROFILE) / "Music"


# The machine lane, driven end to end on this host: the shell's answers are real folders under
# tmp_path, and the spelling rule is the host's own absolute-path rule.
@pytest.fixture
def moved_desktop(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    moved = tmp_path / "profile" / "OneDrive" / "Desktop"
    moved.mkdir(parents=True)
    for name in ("Downloads", "Documents"):
        (home / name).mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(kf, "_IS_WINDOWS", True)
    monkeypatch.setattr(kf, "_full_local_path", os.path.isabs)
    monkeypatch.setattr(kf, "_shell_known_folder", _shell({"Profile": str(home), "Desktop": str(moved)}))
    return home, moved.resolve()


def test_the_machine_roots_follow_the_moved_desktop(moved_desktop) -> None:
    from core.runtime_execution_tools import _safe_machine_roots

    home, moved = moved_desktop
    roots = _safe_machine_roots()
    assert moved in roots
    assert (home / "Desktop").resolve() not in roots
    assert (home / "Documents").resolve() in roots


@pytest.mark.parametrize("raw", ["desktop", "my desktop", "~/desktop", "Desktop/notes.txt", "~/Desktop/notes.txt"])
def test_machine_paths_naming_the_desktop_land_in_the_moved_desktop(moved_desktop, raw) -> None:
    from core.runtime_execution_tools import _resolve_safe_machine_directory, anchor_machine_path

    _home, moved = moved_desktop
    placed = anchor_machine_path(raw)
    assert placed == moved or moved in placed.parents, placed
    assert _resolve_safe_machine_directory(raw) == placed


def test_the_unmoved_profile_desktop_is_no_longer_a_safe_root(moved_desktop) -> None:
    from core.runtime_execution_tools import _resolve_safe_machine_directory

    home, _moved = moved_desktop
    with pytest.raises(ValueError):
        _resolve_safe_machine_directory(str(home / "Desktop" / "x.txt"))


def test_the_folder_lookup_fast_path_uses_the_moved_desktop(moved_desktop) -> None:
    from core.agent_runtime.fast_paths_machine import _resolve_safe_machine_root_directory

    _home, moved = moved_desktop
    (moved / "ledger").mkdir()
    assert _resolve_safe_machine_root_directory(folder="", root_label="Desktop").resolve() == moved
    assert _resolve_safe_machine_root_directory(folder="ledger", root_label="").resolve() == moved / "ledger"


def test_write_root_honesty_counts_the_moved_desktop_as_reachable(moved_desktop) -> None:
    from core.agent_runtime import write_root_honesty

    _home, moved = moved_desktop
    roots = [Path(root).resolve() for root in write_root_honesty.allowed_write_roots({})]
    assert moved in roots
