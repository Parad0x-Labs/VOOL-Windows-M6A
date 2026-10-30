"""Leaks a review of the shared path scrubber still found, through every redactor.

- Nested escaping (a JSON string inside a JSON string: four backslashes) and doubled forward
  slashes (``C://Users//x``) got past every separator pattern.
- ``\\\\wsl$\\...``: the ``$`` in the server name stopped the share match.
- A share or rooted ``\\Users`` path right after a ``:`` (``denied:\\\\fs01\\...``) was not a path.
- NT object paths (``\\Device\\HarddiskVolume3\\Users\\...``, ``\\??\\C:\\...``) were not paths.
- A spaced account the three-word rule does not take (four words, a double space, a no-break
  space, prose after the name) lost only its first word.
- ``+`` and ``&``, which Windows allows in an account name, ended the bug-report and session
  export name class.
- A share path four or more folders deep gave the person's folder as its "basename".
- The bug-report post-check scans ``json.dumps(payload)``, which doubles the backslashes again, so
  an escaped path in a field reached it with four and passed.
- The conductor's leak filter did not see a rooted ``\\Users`` path.
- faults redaction returned the unscrubbed text when a masking pass raised.

Inputs are plain strings, so the checks do not depend on the host OS.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.path_scrub import ends_at_home_account, path_basename, scrub_windows_paths
from tests.test_spaced_account_closed_by_punctuation import ALL_REDACTORS


def _acceptance(text: str) -> str:
    from ops import run_local_acceptance

    return run_local_acceptance._sanitize_text(text, repo_root=Path("/nonexistent-repo-root"))


EVERY_REDACTOR = {**ALL_REDACTORS, "acceptance": _acceptance}

ACCOUNT_INPUTS = [
    ("nested_escape", "open C:\\\\\\\\Users\\\\\\\\zqalice\\\\\\\\f.py failed"),
    ("json_in_json", "open " + json.dumps(json.dumps("C:\\Users\\zqalice\\f.py")) + " failed"),
    ("doubled_forward", "open C://Users//zqalice//f.py failed"),
    ("wsl_share", "open \\\\wsl$\\Ubuntu-22.04\\home\\zqalice\\proj\\a\\b.py failed"),
    ("wsl_share_doubled", "open \\\\\\\\wsl$\\\\Ubuntu-22.04\\\\home\\\\zqalice\\\\b.py failed"),
    ("share_after_colon", "denied:\\\\fs01\\users\\zqalice\\a\\b\\c.txt"),
    ("rooted_after_colon", "path:\\Users\\zqalice\\a.txt"),
    ("nt_device", "open \\Device\\HarddiskVolume3\\Users\\zqalice\\f.py failed"),
    ("nt_object", "open \\??\\C:\\Users\\zqalice\\f.py failed"),
    ("plus_name", "open C:\\Users\\zq+zqalice\\f.py failed"),
    ("plus_name_posix", "open /home/zq+zqalice/f.py failed"),
]


@pytest.mark.parametrize("text", [t for _, t in ACCOUNT_INPUTS], ids=[i for i, _ in ACCOUNT_INPUTS])
@pytest.mark.parametrize("name", list(EVERY_REDACTOR))
def test_no_redactor_keeps_the_account_name(name, text):
    out = EVERY_REDACTOR[name](text)
    assert "zqalice" not in out, f"{name}: {out!r}"


SPACED_NEEDLES = ("Zqa", "Alw", "Bov", "Smithq")
SPACED_INPUTS = [
    ("four_words", "open C:\\Users\\Zqa Alw Bov Smithq\\f.py failed"),
    ("four_words_doubled", "open C:\\\\Users\\\\Zqa Alw Bov Smithq\\\\f.py failed"),
    ("double_space", "open C:\\Users\\Zqa  Smithq\\f.py failed"),
    ("no_break_space", "open C:\\Users\\Zqa\u00a0Smithq\\f.py failed"),
    ("prose_after_name", "cwd is C:\\Users\\Zqa Smithq and it failed"),
    ("ampersand", "open C:\\Users\\Zqa&Smithq\\f.py failed"),
    ("posix_four_words", "open /home/Zqa Alw Bov Smithq/f.py failed"),
]


@pytest.mark.parametrize("text", [t for _, t in SPACED_INPUTS], ids=[i for i, _ in SPACED_INPUTS])
@pytest.mark.parametrize("name", list(EVERY_REDACTOR))
def test_no_redactor_keeps_any_word_of_an_unusual_account_name(name, text):
    out = EVERY_REDACTOR[name](text)
    for needle in SPACED_NEEDLES:
        assert needle not in out, f"{name} kept {needle!r} from {text!r}: {out!r}"


def test_prose_after_an_unusual_name_survives_where_it_can():
    out = scrub_windows_paths("cwd is C:\\Users\\Zqa Smithq and it failed", "X")
    assert out == "cwd is X and it failed"


@pytest.mark.parametrize("text", ["access denied: retry later", "Error: \\n was unexpected"])
def test_a_colon_before_prose_is_still_prose(text):
    assert scrub_windows_paths(text, "X") == text


# --- share paths deep enough to pass the depth guard ------------------------------------------------
@pytest.mark.parametrize(
    "path", ["\\\\fs01\\dept\\team\\zqalice", "\\\\fs01\\share\\folder\\zqalice$", "\\\\fs01\\a\\b\\c\\zqalice"]
)
@pytest.mark.parametrize("name", ["error_surface", "faults", "task_event_model"])
def test_a_deep_share_folder_is_not_given_as_a_basename(name, path):
    out = EVERY_REDACTOR[name](f"open {path} failed")
    assert "zqalice" not in out, f"{name}: {out!r}"


def test_a_file_deep_on_a_share_keeps_its_name():
    assert scrub_windows_paths("open \\\\fs01\\dept\\team\\x\\plan.txt now", path_basename) == "open plan.txt now"


def test_a_shallow_share_path_is_masked_even_when_it_ends_in_a_file_name():
    # Only the depth guard catches this one: server, share and one item could all be a person.
    assert scrub_windows_paths("open \\\\fs01\\share\\notes.txt now", path_basename) == "open <path> now"


@pytest.mark.parametrize("home", ["users", "home", "homes", "profiles", "Users$", "profiles$"])
def test_each_profile_share_folder_counts_as_a_home(home):
    assert ends_at_home_account(["fs01", "dept", home, "zqalice"])


# --- the bug-report post-check sees what json.dumps hands it ---------------------------------------
@pytest.mark.parametrize(
    "note",
    [
        "C:\\\\Users\\\\zqalice\\\\f.py",
        "\\\\\\\\fs01\\\\users\\\\zqalice\\\\f.py",
        "\\\\\\\\wsl$\\\\Ubuntu\\\\home\\\\zqalice\\\\f.py",
    ],
)
def test_bug_report_post_check_refuses_an_escaped_path_in_a_field(note):
    from core.bug_report.schema import SanitizedMaterial, UnsafeReportContentError

    with pytest.raises(UnsafeReportContentError):
        SanitizedMaterial.from_payload({"note": note})


# --- conductor leak filter ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["failed at \\Users\\zqalice\\f.py", "failed at \\\\Users\\\\zqalice\\\\f.py"])
def test_conductor_sees_a_rooted_users_path(text):
    from core.conductor.compose import _looks_like_leaked_internals

    assert _looks_like_leaked_internals(text)


# --- faults redaction fails closed -------------------------------------------------------------------
@pytest.mark.parametrize("target", ["redact_secrets", "_strip_paths", "scrub_windows_paths"])
def test_faults_redaction_never_returns_unscrubbed_text_when_a_pass_raises(monkeypatch, target):
    from core.faults import redaction

    def boom(*_args, **_kwargs):
        raise RuntimeError("scrub pass broke")

    monkeypatch.setattr(redaction, target, boom)
    out = redaction.redact_text("open C:\\Users\\zqalice\\f.py with token sk-ant-zzzzzzzzzzzzzzzzzzzzzzzz")
    assert "zqalice" not in out, out
    assert "sk-ant-" not in out, out


def test_faults_second_windows_pass_catches_what_the_first_misses(monkeypatch):
    from core.faults import redaction

    monkeypatch.setattr(redaction, "_strip_paths", lambda text: text)
    out = redaction.redact_text("open C:\\\\Users\\\\zqalice\\\\f.py failed")
    assert "zqalice" not in out, out
