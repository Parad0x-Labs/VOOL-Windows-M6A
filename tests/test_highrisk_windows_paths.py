"""classify_path must apply the same classification to Windows-style paths on any host."""
from __future__ import annotations

import ntpath
from pathlib import PureWindowsPath

import pytest

from core.blackbox.coverage.highrisk import (
    KIND_CREDENTIALS,
    KIND_GIT_METADATA,
    KIND_RELEASE_KEYS,
    KIND_STARTUP,
    classify_path,
    high_risk_for_paths,
)


@pytest.mark.parametrize(
    "path, kind",
    [
        ("C:\\ws\\.git\\config", KIND_GIT_METADATA),
        ("C:/ws/.git/config", KIND_GIT_METADATA),
        (".git\\config", KIND_GIT_METADATA),
        ("..\\.git\\hooks\\pre-commit", KIND_GIT_METADATA),
        ("C:.git\\config", KIND_GIT_METADATA),
        ("\\\\server\\share\\.git\\config", KIND_GIT_METADATA),
        ("C:\\Users\\me\\.ssh\\id_rsa", KIND_CREDENTIALS),
        ("C:\\Users\\me\\.aws\\whatever.txt", KIND_CREDENTIALS),
        ("C:id_rsa", KIND_CREDENTIALS),
        ("\\\\server\\share\\x\\.env", KIND_CREDENTIALS),
        ("C:\\x\\LaunchAgents\\a.txt", KIND_STARTUP),
        ("C:\\keys\\AuthKey_ABC.p8", KIND_RELEASE_KEYS),
    ],
)
def test_backslash_and_drive_paths_classified(path, kind):
    assert classify_path(path) == kind


def test_pure_windows_path_str_classified():
    assert classify_path(PureWindowsPath("C:/ws/.git/config")) == KIND_GIT_METADATA
    assert classify_path(ntpath.join("C:\\ws", ".git", "config")) == KIND_GIT_METADATA


def test_drive_prefix_is_not_a_component_and_safe_paths_stay_safe():
    assert classify_path("C:\\ws\\src\\main.py") is None
    assert classify_path("C:\\") is None
    assert classify_path("\\\\server\\share\\src\\a.txt") is None


def test_high_risk_for_paths_keeps_raw_key():
    raw = "C:\\ws\\.git\\config"
    assert high_risk_for_paths([raw, "C:\\ws\\a.py"]) == {raw: KIND_GIT_METADATA}
