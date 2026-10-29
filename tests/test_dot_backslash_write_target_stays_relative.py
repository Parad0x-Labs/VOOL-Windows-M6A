"""A write target written `.\\x.txt` names the current folder, never the root of the drive.

`write_demand._clean_relative_path` stripped the character set "./", so `.\\x.txt` lost its dot and
came out as `\\x.txt`, a path rooted on the current drive that the re-check let through
(`/\\x.txt` the same way). Plain strings, so the result is the same on every host.
"""

from __future__ import annotations

import pytest

from core.execution.write_demand import _clean_relative_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".\\x.txt", "x.txt"),
        (".\\src\\a.py", "src\\a.py"),
        (".\\.\\x.txt", "x.txt"),
        ("./.\\x.txt", "x.txt"),
    ],
)
def test_a_dot_backslash_target_stays_in_the_current_folder(path: str, expected: str) -> None:
    assert _clean_relative_path(path) == expected


@pytest.mark.parametrize("path", ["/\\x.txt", ".\\\\x.txt", "./\\x.txt", ".\\/x.txt", "//\\x.txt"])
def test_a_target_still_rooted_after_the_strip_is_refused(path: str) -> None:
    assert _clean_relative_path(path) == ""


@pytest.mark.parametrize(("path", "expected"), [("./src/a.py", "src/a.py"), ("/src/a.py", "src/a.py")])
def test_the_strip_keeps_its_posix_behaviour(path: str, expected: str) -> None:
    assert _clean_relative_path(path) == expected
