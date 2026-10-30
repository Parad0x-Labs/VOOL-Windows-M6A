"""Account names with a space or an apostrophe must not leak their tail through any redactor.

``C:\\Users\\John Smith\\x\\f.py`` used to lose only "John" (the strict path segment stops at the
space) and keep "Smith\\x\\f.py"; ``al-ice.o'neil`` kept "neil\\x\\f.py". Inputs are plain strings, so
the checks do not depend on the host OS.
"""
from __future__ import annotations

import time

import pytest

from core.bug_report.redaction import redact_text as _br_redact_text
from core.error_surface import safe_error_text
from core.faults.redaction import redact_text as _faults_redact_text
from core.path_scrub import path_basename, scrub_windows_paths
from core.session_portability.redaction import RedactionReport, redact_value
from core.task_event_model import _safe_diagnostic_text

REDACTORS = {
    "error_surface": lambda text: safe_error_text(OSError(text)),
    "faults": lambda text: _faults_redact_text(text),
    "bug_report": lambda text: _br_redact_text(text)[0],
    "session_portability": lambda text: redact_value(text, RedactionReport(), []),
    "task_event_model": lambda text: _safe_diagnostic_text(text, limit=200),
}

NEEDLES = ("John", "Smith", "al-ice", "o'neil", "neil")

INPUTS = [
    ("spaced_single", "open C:\\Users\\John Smith\\x\\f.py"),
    ("spaced_doubled", "open C:\\\\Users\\\\John Smith\\\\x\\\\f.py"),
    ("spaced_forward", "open C:/Users/John Smith/x/f.py"),
    ("apostrophe_single", "open C:\\Users\\al-ice.o'neil\\x\\f.py"),
    ("apostrophe_doubled", "open C:\\\\Users\\\\al-ice.o'neil\\\\x\\\\f.py"),
    ("apostrophe_forward", "open C:/Users/al-ice.o'neil/x/f.py"),
    ("posix_home_spaced", "open /home/John Smith/x/f.py"),
    ("posix_users_apostrophe", "open /Users/al-ice.o'neil/x/f.py"),
]
INPUT_IDS = [case_id for case_id, _ in INPUTS]
INPUT_TEXTS = [text for _, text in INPUTS]


@pytest.mark.parametrize("text", INPUT_TEXTS, ids=INPUT_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_leaks_no_part_of_a_spaced_or_apostrophe_account_name(name, text):
    out = REDACTORS[name](text)
    for needle in NEEDLES:
        assert needle not in out, f"{name} kept {needle!r} from {text!r}: {out!r}"


@pytest.mark.parametrize("text", INPUT_TEXTS, ids=INPUT_IDS)
def test_shared_scrubber_takes_the_whole_account_segment(text):
    out = scrub_windows_paths(text, path_basename)
    for needle in NEEDLES:
        assert needle not in out, f"kept {needle!r} from {text!r}: {out!r}"
    assert out == "open f.py"


# --- ordinary text stays readable ------------------------------------------------------------------
# A space must still end an ordinary path segment, so none of these is read as a path. The URL line
# is checked on the shared scrubber only: error_surface's own POSIX rule has always trimmed a URL's
# path to its last segment, which is not what this change is about.
PROSE = [
    "Meeting at 10:30 today",
    "Option A: text",
    "see https://host/a/b for details",
    "it's John Smith's file",
    "Users can't log in",
]
REDACTOR_PROSE = [text for text in PROSE if "https://" not in text]


@pytest.mark.parametrize("text", PROSE)
def test_shared_scrubber_leaves_prose_alone(text):
    assert scrub_windows_paths(text, "X") == text


@pytest.mark.parametrize("name", list(REDACTORS))
@pytest.mark.parametrize("text", REDACTOR_PROSE)
def test_redactor_leaves_prose_readable(name, text):
    assert text in REDACTORS[name](text)


def test_a_space_still_ends_an_ordinary_path_segment():
    assert scrub_windows_paths("open C:\\proj\\a b\\c", "X") == "open X b\\c"
    assert scrub_windows_paths("open C:\\Users\\alice\\x f.py", path_basename) == "open x f.py"


# --- prose after a path survives; prose right after a bare account goes with it ------------------------
# Once a separator follows the account, the path ends at its last segment and the words after it stay.
# With no separator after the account, nothing tells ``bob failed to start`` from ``ana de la cruz``,
# so the scrubber fails closed and masks the words with the account (second review of #121).
PROSE_AFTER_PATH = [
    ("failed", "C:\\Users\\bob\\job.txt failed to start the job", "failed to start the job"),
    ("failed_doubled", "C:\\\\Users\\\\bob\\\\job.txt failed to start the job", "failed to start the job"),
    ("failed_forward", "C:/Users/bob/job.txt failed to start the job", "failed to start the job"),
    ("then_run", "see C:\\Users\\bob\\notes.txt then run python tools/check.py now", "then run python"),
]
PROSE_AFTER_IDS = [case_id for case_id, _, _ in PROSE_AFTER_PATH]


@pytest.mark.parametrize("case", PROSE_AFTER_PATH, ids=PROSE_AFTER_IDS)
def test_shared_scrubber_keeps_the_words_after_a_path(case):
    _case_id, text, kept = case
    out = scrub_windows_paths(text, path_basename)
    assert kept in out, f"lost {kept!r} from {text!r}: {out!r}"
    assert "bob" not in out, f"kept the account from {text!r}: {out!r}"


@pytest.mark.parametrize("case", PROSE_AFTER_PATH, ids=PROSE_AFTER_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_keeps_the_words_after_a_path(name, case):
    _case_id, text, kept = case
    out = REDACTORS[name](text)
    assert "bob" not in out, f"{name} kept the account from {text!r}: {out!r}"
    assert kept in out, f"{name} lost {kept!r} from {text!r}: {out!r}"


@pytest.mark.parametrize(
    "text",
    [
        "C:\\Users\\bob failed to start the job",
        "C:\\\\Users\\\\bob failed to start the job",
        "C:/Users/bob failed to start the job",
        "copied C:\\Users\\bob to the share and then D:\\out\\f.txt",
    ],
)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_masks_the_words_right_after_a_bare_account(name, text):
    out = REDACTORS[name](text)
    assert "bob" not in out, f"{name} kept the account from {text!r}: {out!r}"
    assert "failed to start" not in out and "to the share" not in out, f"{name}: {out!r}"


def test_a_drive_path_after_a_bare_account_is_still_its_own_path():
    out = scrub_windows_paths("copied C:\\Users\\bob to the share and then D:\\out\\f.txt", path_basename)
    assert out == "copied <path> f.txt", out


# --- an account that runs to the end of the text ------------------------------------------------------
@pytest.mark.parametrize("text", ["open C:\\Users\\John Smith", "open C:/Users/John Smith"])
def test_shared_scrubber_takes_a_spaced_account_at_end_of_text(text):
    out = scrub_windows_paths(text, path_basename)
    assert "John" not in out and "Smith" not in out, out


@pytest.mark.parametrize("text", ["open C:\\Users\\John Smith", "open C:/Users/John Smith"])
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_takes_a_spaced_account_at_end_of_text(name, text):
    out = REDACTORS[name](text)
    assert "John" not in out and "Smith" not in out, f"{name}: {out!r}"


# --- reviewer-requested leak checks: a bare spaced account with one trailing folder ----------------------
BOB_SMITH_SINGLE = "C:\\Users\\Bob Smith\\x"
BOB_SMITH_ESCAPED = "C:\\\\Users\\\\Bob Smith\\\\x"
#: The account as the last thing on a line or before clause punctuation, as log lines and error
#: messages put it.
BOB_SMITH_AT_LINE_END = [
    "C:\\Users\\Bob Smith, then it stopped",
    "C:\\Users\\Bob Smith; then it stopped",
    "C:\\Users\\Bob Smith\nnext line",
    "C:\\Users\\Bob Smith\r\nnext line",
    "C:\\\\Users\\\\Bob Smith\nnext line",
]
BOB_SMITH_CASES = [BOB_SMITH_SINGLE, BOB_SMITH_ESCAPED, *BOB_SMITH_AT_LINE_END]
BOB_SMITH_IDS = ["single_backslash", "escaped_double_backslash", "comma", "semicolon", "newline", "crlf",
                 "escaped_newline"]


@pytest.mark.parametrize("text", BOB_SMITH_CASES, ids=BOB_SMITH_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_leaks_no_part_of_bob_smith(name, text):
    out = REDACTORS[name](text)
    assert "Bob" not in out and "Smith" not in out, f"{name} kept part of {text!r}: {out!r}"


@pytest.mark.parametrize("text", BOB_SMITH_CASES, ids=BOB_SMITH_IDS)
def test_shared_scrubber_leaks_no_part_of_bob_smith(text):
    out = scrub_windows_paths(text, path_basename)
    assert "Bob" not in out and "Smith" not in out, f"kept part of {text!r}: {out!r}"


def test_a_four_word_account_is_taken_whole():
    out = scrub_windows_paths("C:\\Users\\a b c d\\x\\f.py", "X")
    assert out == "X"


# --- linear time ---------------------------------------------------------------------------------------
HOSTILE = [
    "C:\\Users\\" + "a " * 50_000,
    "C:\\Users\\" + "'" * 100_000,
    ("C:\\Users\\a b" + "'") * 20_000,
    "/home/" + "a " * 50_000,
]
HOSTILE_IDS = ["spaced_run", "apostrophe_run", "repeated_spaced_account", "posix_spaced_run"]


@pytest.mark.parametrize("text", HOSTILE, ids=HOSTILE_IDS)
def test_shared_scrubber_is_linear_on_hostile_account_lines(text):
    started = time.perf_counter()
    scrub_windows_paths(text, "X")
    assert time.perf_counter() - started < 5.0


@pytest.mark.parametrize("text", HOSTILE, ids=HOSTILE_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_is_linear_on_hostile_account_lines(name, text):
    started = time.perf_counter()
    REDACTORS[name](text)
    assert time.perf_counter() - started < 5.0
