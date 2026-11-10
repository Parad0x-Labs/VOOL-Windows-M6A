"""A spaced path on a mapped network drive is never looked up while reading the chat message.

Security Architect, 9 Nov 2026: `_spaces_may_extend` let any `X:\\` path be extended through
`os.path.isdir` / `os.scandir` on the user's typed text. On a mapped network drive
(`Z:\\Team Share\\...`) that lookup is already the SMB connection, made only by reading the message,
which breaks "mapped drives refused before anything opens them". On Windows a drive-letter path is
extended only when the drive is known to be local; a remote drive, or one Windows cannot classify,
is never touched.
"""
from __future__ import annotations

import pytest

from core import windows_known_folders
from core.agent_runtime import write_root_honesty

_TEXT = r"create a.txt in Z:\Some Folder\x please"
_PATH_START = _TEXT.index("Z:")
_PATH_END = _TEXT.index(" Folder")


@pytest.fixture
def no_disk_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    touched: list[str] = []

    def _isdir(path: str) -> bool:
        touched.append(f"isdir {path}")
        return True

    def _exists(path: str) -> bool:
        touched.append(f"exists {path}")
        return True

    def _scandir(path: str):
        touched.append(f"scandir {path}")
        raise AssertionError("scandir on a network drive")

    monkeypatch.setattr(write_root_honesty.os.path, "isdir", _isdir)
    monkeypatch.setattr(write_root_honesty.os.path, "exists", _exists)
    monkeypatch.setattr(write_root_honesty.os, "scandir", _scandir)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    return touched


@pytest.mark.parametrize("drive_kind", [True, None], ids=["remote", "unknown"])
def test_a_spaced_path_on_a_network_drive_is_not_looked_up(
    no_disk_lookups: list[str], monkeypatch: pytest.MonkeyPatch, drive_kind: bool | None
) -> None:
    # GetDriveTypeW == DRIVE_REMOTE (4) is reported as True; an unclassifiable drive as None.
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive_kind)

    assert write_root_honesty._spaces_may_extend(r"Z:\Some") is False
    assert write_root_honesty._extend_through_spaces(_TEXT, _PATH_START, _PATH_END) == _PATH_END
    assert no_disk_lookups == []


def test_a_spaced_path_on_a_local_drive_may_still_be_extended(
    no_disk_lookups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def _local(drive: str) -> bool:
        asked.append(drive)
        return False

    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", _local)

    assert write_root_honesty._spaces_may_extend(r"C:\Users\VOOLT\VOOL") is True
    assert asked == ["C:"]
