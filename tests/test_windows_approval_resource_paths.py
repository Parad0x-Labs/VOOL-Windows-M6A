"""The approval ask names a workspace write's destination in the host's native path form.

`affected_resources` is the one place the operator sees where a write will land before
allowing it. On Windows that destination is "C:\\ws\\repo\\calc.py"; the ask string-joined
the workspace root onto the relative resource with a literal "/" and printed
"C:\\ws\\repo/calc.py" instead (Windows sweep 2026-10-28,
tests/test_code_step_argument_boundary.py:49). POSIX bytes stay exactly as they were:
the two pinned-regression tests at the bottom are green on POSIX at both heads.
"""

from pathlib import Path


def test_the_approval_ask_joins_the_file_destination_in_the_hosts_native_form(tmp_path):
    from core.mode_permission_policy import _affected_resources_display

    workspace = tmp_path / "ws"
    displayed = _affected_resources_display(
        {"path": "finalbot/README.md", "content": "x"},
        {"workspace": str(workspace)},
    )
    assert displayed == [str(workspace / "finalbot" / "README.md")], displayed


def test_the_approval_ask_joins_each_listed_path_in_the_hosts_native_form(tmp_path):
    from core.mode_permission_policy import _affected_resources_display

    workspace = tmp_path / "ws"
    displayed = _affected_resources_display(
        {"paths": ["src/one.py", "docs/two.md"]},
        {"workspace": str(workspace)},
    )
    assert displayed == [
        str(workspace / "src" / "one.py"),
        str(workspace / "docs" / "two.md"),
    ], displayed


def test_the_approval_ask_joins_the_working_directory_in_the_hosts_native_form(tmp_path):
    from core.mode_permission_policy import _affected_resources_display

    workspace = tmp_path / "ws"
    displayed = _affected_resources_display(
        {"cwd": "build/pkg"},
        {"workspace_root": str(workspace)},
    )
    assert displayed == [str(workspace / "build" / "pkg")], displayed


def test_a_windows_root_recorded_with_a_trailing_backslash_names_the_real_destination(tmp_path):
    from core.mode_permission_policy import _affected_resources_display

    root = str(tmp_path / "ws") + "\\"
    displayed = _affected_resources_display(
        {"path": "pkg", "content": "x"},
        {"workspace": root},
    )
    # The destination the writer's own pathlib join names: on Windows the trailing
    # backslash is a separator, and the old f-string printed "…ws\/pkg" for it; on POSIX
    # a backslash is an ordinary character, so the old and new bytes agree there.
    assert displayed == [str(Path(root) / "pkg")], displayed


def test_posix_keeps_the_exact_slash_joined_bytes(monkeypatch):
    """Off Windows the join branch must produce the old `root + "/" + value` bytes verbatim.

    Forced through the rule's documented test hook (`windows_path_policy._IS_WINDOWS`),
    so the same proof runs on Windows here and naturally on POSIX CI.
    """
    import core.windows_path_policy as windows_path_policy
    from core.mode_permission_policy import _affected_resources_display

    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", False)
    for root, value in [
        ("/srv/ws", "finalbot/README.md"),
        ("/srv/ws/", "a/b"),
        ("//srv/share", "pkg/mod.py"),
        ("/srv/ws", "a//b"),
        ("/srv/ws", "a/./b"),
        ("/srv/ws", "a/../b"),
        ("/srv/ws", "notes."),
    ]:
        displayed = _affected_resources_display(
            {"path": value, "content": "x"},
            {"workspace": root},
        )
        assert displayed == [f"{root.rstrip('/')}/{value}"], (root, value, displayed)


def test_absolute_and_home_prefixed_resources_are_never_rerooted(tmp_path):
    """An absolute or home-relative resource is passed through untouched, never silently
    re-rooted under the workspace (which would name a destination the write cannot land at)."""
    from core.mode_permission_policy import _affected_resources_display

    displayed = _affected_resources_display(
        {"path": "/etc/passwd", "content": "x"},
        {"workspace": str(tmp_path / "ws")},
    )
    assert displayed == ["/etc/passwd"], displayed

    displayed = _affected_resources_display(
        {"cwd": "~/build"},
        {"workspace_root": str(tmp_path / "ws")},
    )
    assert displayed == ["~/build"], displayed
