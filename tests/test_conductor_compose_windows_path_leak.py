"""Windows filesystem paths must be treated as leaked internals, independent of the host OS."""

from __future__ import annotations

import pytest

from core.conductor.compose import _looks_like_leaked_internals, reader_facing_reason

LEAKING = (
    r"C:\Users\Satoshi\secret.txt",
    "C:/Users/x",
    r"D:\data\x",
    r"\\server\share\x",
    r"\\?\C:\Users\x",
    r"C:x\secret.txt",
    r"cannot open C:\Users\Satoshi\secret.txt for reading",
    r"missing file ..\x\y.py",
    "failed at c:/work/cfg.json today",
    # Escaped spellings, as an OSError repr or a JSON string carries them.
    r"PermissionError(13, 'Access is denied', '\\\\fileserv\\finance\\q3.xlsx')",
    r'{"path": "\\\\fileserv\\finance\\q3.xlsx"}',
    r"[WinError 3] '\\\\?\\C:\\Users\\x'",
    r"FileNotFoundError: '..\\x\\y.py'",
    r"'C:\\Users\\Satoshi\\secret.txt'",
)

CLEAN = (
    "the C: drive was mentioned",
    "C:",
    "Note: nothing to report",
    "ratio 3:1 at 10:30",
    "Option A: yes",
)


@pytest.mark.parametrize("text", LEAKING)
def test_windows_path_is_detected_as_leak(text: str) -> None:
    assert _looks_like_leaked_internals(text)


@pytest.mark.parametrize("text", LEAKING)
def test_windows_path_never_reaches_reader(text: str) -> None:
    assert reader_facing_reason(text) == "could not be completed"


@pytest.mark.parametrize("text", CLEAN)
def test_ordinary_prose_is_not_flagged(text: str) -> None:
    assert not _looks_like_leaked_internals(text)
    assert reader_facing_reason(text) == text
