"""``relative_path_escapes`` must refuse Windows drive and UNC forms on every host.

On Windows ``root / "C:/x"`` replaces ``root`` and ``root / "C:x"`` re-roots on the drive's
current directory, so a journal-supplied path of that shape escapes the workspace. The check is
lexical, so these cases are host-independent.
"""
from __future__ import annotations

import pytest

from core.blackbox.snapshot import relative_path_escapes


@pytest.mark.parametrize(
    "relative",
    [
        "C:/x",
        "C:\\x",
        "C:x",
        "c:x",
        "C:",
        "C:/",
        "Z:/a/b",
        "C:a/../b",
        "\\\\server\\share\\x",
        "//server/share/x",
        "\\\\?\\C:\\x",
        "\\\\.\\C:\\x",
        "/x",
        "\\x",
        "..\\x",
        "../x",
        "a/../../x",
        "a\\..\\..\\x",
        "",
        ".",
        "./",
    ],
)
def test_escaping_forms_are_refused(relative: str) -> None:
    assert relative_path_escapes(relative) is True


@pytest.mark.parametrize(
    "relative",
    [
        "x",
        "a/b.txt",
        "a\\b.txt",
        "./a/b",
        "dir/file.name",
        "a..b/c",
    ],
)
def test_ordinary_relative_paths_are_allowed(relative: str) -> None:
    assert relative_path_escapes(relative) is False
