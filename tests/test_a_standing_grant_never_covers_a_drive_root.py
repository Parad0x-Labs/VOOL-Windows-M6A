"""A standing grant is refused for any filesystem root and for the home directory, on every OS.

On Windows "/" resolves to the current drive's root (for example D:\\), never to "/", and every
drive is its own root, so a literal "/" check lets C:\\ or D:\\ become a standing workspace whose
edit scope covers VOOL's own authority stores whenever they live on that drive. The home
directory is matched the way the filesystem matches names, so a different spelling of its case
on Windows is still the home directory.
"""
import os
import tempfile
from pathlib import Path

from core.mode_permission_policy import _workspace_root_confined
from core.runtime_paths import active_data_dir

_ROOT_REFUSAL = "the filesystem root and the home directory cannot be standing workspaces"


def _anchors() -> set[str]:
    places = (Path.cwd(), Path(tempfile.gettempdir()), Path.home(), active_data_dir())
    return {Path(place).resolve().anchor for place in places}


def test_every_drive_root_this_host_uses_is_refused() -> None:
    anchors = _anchors()
    assert anchors, "no filesystem root was found"
    for anchor in anchors:
        assert _workspace_root_confined(anchor) == _ROOT_REFUSAL, anchor


def test_a_bare_slash_is_refused_whatever_drive_it_resolves_to() -> None:
    assert _workspace_root_confined("/") == _ROOT_REFUSAL


def test_the_home_directory_is_refused_in_the_case_the_filesystem_folds() -> None:
    home = str(Path.home().resolve())
    spelled = home.upper() if os.name == "nt" else home
    assert _workspace_root_confined(spelled) == _ROOT_REFUSAL, spelled


def test_an_ordinary_folder_is_still_a_standing_workspace(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    assert _workspace_root_confined(str(workspace)) == ""
