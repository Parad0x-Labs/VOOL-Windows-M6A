"""Backslash and forward-slash spellings of one path must normalise identically.

Pure string inputs, so the result does not depend on the host OS.
"""
from __future__ import annotations

import pytest

from core import answer_binder


@pytest.mark.parametrize(
    "backslash, forward",
    [
        (r"C:\Users\me\Desktop\x", "C:/Users/me/Desktop/x"),
        (r"C:x\y", "C:x/y"),
        (r"\\server\share\x", "//server/share/x"),
        (r"..\x", "../x"),
        (r"~\Desktop\x", "~/Desktop/x"),
        ("`C:\\Users\\me\\x\\`", "`C:/Users/me/x/`"),
        (r"C:\Users/me\x", "C:/Users/me/x"),
    ],
)
def test_backslash_and_forward_slash_forms_normalise_identically(backslash, forward):
    assert answer_binder._normalise_path(backslash) == answer_binder._normalise_path(forward)
    assert answer_binder._segments(backslash) == answer_binder._segments(forward)


def test_backslash_path_splits_into_segments():
    assert answer_binder._segments(r"C:\Users\me\x") == ("C:", "Users", "me", "x")
    assert answer_binder._segments(r"..\x") == ("..", "x")


def test_trailing_backslash_is_stripped():
    assert answer_binder._segments("C:\\Users\\me\\") == ("C:", "Users", "me")


def test_backslash_path_is_related_to_forward_slash_target():
    assert answer_binder._is_related(r"C:\Users\me\Desktop", "C:/Users/me/Desktop/x")
    assert answer_binder._is_related("C:/Users/me/Desktop/x", r"C:\Users\me\Desktop")
    assert answer_binder._is_related(r"~\Desktop\x", "C:/Users/me/Desktop/x")


def test_different_backslash_paths_are_not_related():
    assert not answer_binder._is_related(r"C:\Users\me\Other", "C:/Users/me/Desktop/x")


def test_case_handling_is_unchanged():
    assert not answer_binder._is_related(r"C:\Users\ME\Desktop", "C:/Users/me/Desktop")
