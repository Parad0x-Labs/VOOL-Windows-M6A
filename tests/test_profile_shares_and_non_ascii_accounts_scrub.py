"""Profile shares, non-ASCII account names and ordinary `/users/` prose, through every redactor.

- A UNC or device path that ends at a profile share's account folder (``\\\\fs01\\users$\\alice``,
  ``\\\\?\\UNC\\fs01\\profiles\\jsmith``) gave the account as its "basename".
- The bug-report and session-export home patterns took only ASCII letters, so ``Łukasz`` passed
  whole and ``José`` lost only ``Jos``.
- The session export's escaped-home pattern took a forward slash, any case and a space, so
  ``GET /users/123 returned 404`` lost the rest of the line.

Inputs are plain strings, so the checks do not depend on the host OS.
"""
from __future__ import annotations

import pytest

from core.path_scrub import path_basename, scrub_windows_paths
from tests.test_spaced_account_names_scrub import REDACTORS


@pytest.mark.parametrize(
    "path",
    [
        "\\\\fs01\\users$\\alice",
        "\\\\fs01\\profiles\\alice",
        "\\\\?\\UNC\\fs01\\profiles\\alice",
        "\\\\fs01\\home$\\alice\\",
        "\\\\\\\\fs01\\\\users$\\\\alice",
    ],
)
@pytest.mark.parametrize("name", ["error_surface", "faults", "task_event_model"])
def test_a_profile_share_path_never_names_its_account(name, path):
    out = REDACTORS[name](f"open {path} failed")
    assert "alice" not in out, f"{name}: {out!r}"


def test_a_file_on_a_share_gives_back_nothing():
    assert scrub_windows_paths("open \\\\fs01\\users$\\alice\\docs\\plan.txt now", path_basename) == (
        "open <path> now"
    )


NON_ASCII = [
    ("single", "open C:\\Users\\Łukasz\\x\\f.py"),
    ("doubled", "open C:\\\\Users\\\\Łukasz\\\\x\\\\f.py"),
    ("forward", "open C:/Users/José/x/f.py"),
    ("accent_inside", "open C:\\Users\\José\\x\\f.py"),
    ("posix", "open /Users/José/x/f.py"),
]


@pytest.mark.parametrize("text", [t for _, t in NON_ASCII], ids=[i for i, _ in NON_ASCII])
@pytest.mark.parametrize("name", list(REDACTORS))
def test_a_non_ascii_account_name_is_redacted_whole(name, text):
    out = REDACTORS[name](text)
    for needle in ("Łukasz", "ukasz", "José", "Jos", "é"):
        assert needle not in out, f"{name} kept {needle!r} from {text!r}: {out!r}"


@pytest.mark.parametrize(
    "text",
    ["GET /users/123 returned 404 for bob", "see /users/me for the list"],
)
def test_session_export_leaves_url_paths_named_users_readable(text):
    out = REDACTORS["session_portability"](text)
    assert text == out, out
