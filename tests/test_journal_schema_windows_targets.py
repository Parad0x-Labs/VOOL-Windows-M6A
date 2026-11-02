"""recorded_target must not treat Windows drive/UNC paths as workspace-relative text."""

from __future__ import annotations

import pytest

from core.code_assistant.journal_schema import recorded_target, upgrade_payload


@pytest.mark.parametrize(
    "root, raw",
    [
        ("/srv/other", "C:/ws/x"),
        ("/srv/other", "C:\\ws\\x"),
        ("C:/ws", "D:/ws/x"),
        ("C:/ws", "C:/elsewhere/x"),
        ("C:/ws", "C:/wsx/x"),
        ("C:/ws", "C:x"),
        ("/srv/other", "C:x"),
        ("C:\\ws", "C:..\\x"),
        ("/srv/other", "\\\\server\\share\\x"),
        ("C:/ws", "\\\\server\\share\\x"),
        ("C:/ws", "//server/share/x"),
        ("", "C:/ws/x"),
    ],
)
def test_drive_and_unc_targets_outside_the_recorded_root_are_refused(root, raw):
    assert recorded_target(root, raw) is None


@pytest.mark.parametrize(
    "root, raw, expected",
    [
        ("C:/ws", "C:/ws/x", "x"),
        ("C:\\ws", "C:/ws/sub/x.py", "sub/x.py"),
        ("C:\\ws", "C:\\ws\\sub\\x.py", "sub/x.py"),
        ("C:\\ws", "c:/WS/x", "x"),
        ("C:/ws", "sub\\x.py", "sub/x.py"),
    ],
)
def test_drive_absolute_target_inside_the_recorded_root_is_classified_as_absolute(root, raw, expected):
    assert recorded_target(root, raw) == expected


def test_drive_absolute_path_never_binds_a_legacy_approval():
    payload = {
        "workspace_root": "/srv/other",
        "proposals": {
            "p1": {
                "intent": "workspace.write_file",
                "arguments": {"path": "C:/ws/x", "expected_hash": "ab" * 32},
                "created_at": "2025-01-01T00:00:00+00:00",
            }
        },
    }
    proposal = upgrade_payload(payload)["proposals"]["p1"]
    assert proposal["targets"] == []
    assert proposal["base"] == {}
    assert proposal["invalidated"]
