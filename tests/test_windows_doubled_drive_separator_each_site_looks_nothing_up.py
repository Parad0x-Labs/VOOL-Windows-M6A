"""Windows: each site that asks the doubled-separator rule refuses on its own, before any lookup.

One row per site changed in a7c92431, called directly, so no other site's refusal can stand in for
it: `write_root_honesty.typed_path_kind`, `write_root_honesty._spaces_may_extend`,
`windows_marked_file_ops._local`, M4's `windows_marked_code_folder._on_a_local_drive` and the
planner's `_clean_workspace_directory_path`. Each row spies on os.stat, os.scandir, os.path.exists,
os.path.isdir, os.path.isfile and os.path.realpath (the `lookups` fixture), and the touch recorder
watches the rest.
"""
from __future__ import annotations

import pytest

from tests._nonlocal_touch_recorder import recorder, windows_host  # noqa: F401
from tests.test_windows_doubled_drive_separator_is_refused_like_a_share import (  # noqa: F401
    DOUBLED,
    lookups,
    real_windows_seams,
)


def _typed_path_kind(path: str, tmp_path) -> bool:
    from core.agent_runtime import write_root_honesty

    text = f"create a.txt in {path}"
    return write_root_honesty.typed_path_kind(text, text.index(path), path) == "unc"


def _spaces_may_extend(path: str, tmp_path) -> bool:
    from core.agent_runtime import write_root_honesty

    return write_root_honesty._spaces_may_extend(path) is False


def _marked_file_ops_local(path: str, tmp_path) -> bool:
    from core import windows_marked_file_ops

    text = f"replace 'a' with 'b' in {path}"
    return windows_marked_file_ops._local(text, text.index(path), path) is False


def _on_a_local_drive(path: str, tmp_path) -> bool:
    from core import windows_marked_code_folder

    return windows_marked_code_folder._on_a_local_drive(path) is False


def _clean_workspace_directory_path(path: str, tmp_path) -> bool:
    from core.execution import planner

    return planner._clean_workspace_directory_path(path, workspace_root=str(tmp_path)) == ""


SITES = {
    "typed_path_kind": _typed_path_kind,
    "_spaces_may_extend": _spaces_may_extend,
    "marked_file_ops._local": _marked_file_ops_local,
    "M4 _on_a_local_drive": _on_a_local_drive,
    "planner._clean_workspace_directory_path": _clean_workspace_directory_path,
}


@pytest.mark.parametrize("path", DOUBLED)
@pytest.mark.parametrize("site", list(SITES))
def test_the_site_refuses_a_doubled_separator_drive_path_without_a_lookup(
    site: str, path: str, tmp_path, recorder, lookups, windows_host, real_windows_seams
) -> None:
    windows_host.on()

    assert SITES[site](path, tmp_path), (site, path)

    assert lookups == [], (site, lookups)
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
