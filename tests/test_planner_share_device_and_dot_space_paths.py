"""Share, device and dot-space path forms through the planner's and write_demand's cleaners.

- The planner refused only drive paths (`C:\\x`), so a share (`\\\\srv\\share\\x.txt`), a device path
  (`\\\\?\\C:\\x`, `\\\\.\\pipe\\x`) or an NT object path (`\\??\\C:\\x.txt`) came back as a "workspace folder",
  and `/\\srv\\share\\x.txt` (a share on Windows) came back rooted as `\\srv\\share\\x.txt`.
- A segment of dots and spaces (`.. `, `. `) passed the `..` check, then `lstrip("./")` turned
  `.. \\x.txt` into ` \\x.txt` in both cleaners. Windows trims trailing dots and spaces from a segment,
  so `.. ` can read as `..`.

Plain strings are used, so these cases fail on the unfixed code on every host.
"""

from __future__ import annotations

import pytest

from core.execution.planner import _clean_workspace_path
from core.execution.write_demand import _clean_relative_path

_SHARE_AND_DEVICE = [
    r"\\srv\share\x.txt",
    r"\\?\C:\x.txt",
    r"\\.\pipe\x.txt",
    r"\??\C:\x.txt",
    r"/\srv\share\x.txt",
    r"//srv/share/x.txt",
    r"/./\\srv\share\x.txt",
]

_DOT_SPACE = [
    r".. \x.txt",
    r"../.. /x.txt",
    r"a\.. \..\x.txt",
    r". \x.txt",
    r"... \x.txt",
]


@pytest.mark.parametrize("path", _SHARE_AND_DEVICE)
def test_planner_never_takes_a_share_or_device_path_as_a_workspace_folder(path):
    assert _clean_workspace_path(path) == ""


@pytest.mark.parametrize("path", _DOT_SPACE)
def test_planner_refuses_a_dot_space_segment(path):
    assert _clean_workspace_path(path) == ""


@pytest.mark.parametrize("path", _DOT_SPACE)
def test_write_demand_refuses_a_dot_space_segment(path):
    assert _clean_relative_path(path) == ""


@pytest.mark.parametrize("path", ["notes", "docs/plan", r"src\app", "./notes", "a.b/c"])
def test_planner_keeps_an_ordinary_relative_folder(path):
    assert _clean_workspace_path(path)


@pytest.mark.parametrize("path", ["x.txt", "docs/x.txt", r".\x.txt", "a. b/x.txt"])
def test_write_demand_keeps_an_ordinary_relative_file(path):
    assert _clean_relative_path(path)
