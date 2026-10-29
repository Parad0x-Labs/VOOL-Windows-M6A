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


# --- prose after a plain account name survives ------------------------------------------------------
# A spaced account is accepted only as at most 3 single-spaced words that end right at a separator
# or at the end of the text; otherwise the strict segment applies and the words stay.
#
# session_portability is the one redactor whose own older rules (``_WINDOWS_HOME_RE`` and
# ``_ESCAPED_WINDOWS_HOME_RE``, class ``[A-Za-z0-9._ -]+``) already ate the words after a
# backslash/doubled-backslash account at base 2572663, so for those spellings only the account's
# absence is asserted; its forward-slash spelling is left to ``_POSIX_HOME_RE`` and keeps the words.
PROSE_AFTER_ACCOUNT = [
    ("failed", "C:\\Users\\bob failed to start the job", "failed to start the job"),
    (
        "opened",
        "C:\\Users\\bob opened the report and then saved /tmp/x.txt",
        "opened the report and then saved",
    ),
    ("then_run", "see C:\\Users\\bob then run python tools/check.py now", "then run python"),
    (
        "to_the_share",
        "copied C:\\Users\\bob to the share and then D:\\out\\f.txt",
        "to the share and then",
    ),
    ("failed_doubled", "C:\\\\Users\\\\bob failed to start the job", "failed to start the job"),
    ("failed_forward", "C:/Users/bob failed to start the job", "failed to start the job"),
]
PROSE_AFTER_IDS = [case_id for case_id, _, _ in PROSE_AFTER_ACCOUNT]
#: Spellings whose words session_portability's pre-existing space-tolerant rules already removed.
SESSION_PORTABILITY_ATE_WORDS = {
    "failed", "opened", "then_run", "to_the_share", "failed_doubled",
}


@pytest.mark.parametrize("case", PROSE_AFTER_ACCOUNT, ids=PROSE_AFTER_IDS)
def test_shared_scrubber_keeps_the_words_after_a_plain_account(case):
    _case_id, text, kept = case
    out = scrub_windows_paths(text, path_basename)
    assert kept in out, f"lost {kept!r} from {text!r}: {out!r}"
    assert "bob" not in out, f"kept the account from {text!r}: {out!r}"


@pytest.mark.parametrize("case", PROSE_AFTER_ACCOUNT, ids=PROSE_AFTER_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_keeps_the_words_after_a_plain_account(name, case):
    case_id, text, kept = case
    out = REDACTORS[name](text)
    assert "bob" not in out, f"{name} kept the account from {text!r}: {out!r}"
    if name == "session_portability" and case_id in SESSION_PORTABILITY_ATE_WORDS:
        return
    assert kept in out, f"{name} lost {kept!r} from {text!r}: {out!r}"


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
BOB_SMITH_CASES = [BOB_SMITH_SINGLE, BOB_SMITH_ESCAPED]
BOB_SMITH_IDS = ["single_backslash", "escaped_double_backslash"]


@pytest.mark.parametrize("text", BOB_SMITH_CASES, ids=BOB_SMITH_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_leaks_no_part_of_bob_smith(name, text):
    out = REDACTORS[name](text)
    assert "Bob" not in out and "Smith" not in out, f"{name} kept part of {text!r}: {out!r}"


@pytest.mark.parametrize("text", BOB_SMITH_CASES, ids=BOB_SMITH_IDS)
def test_shared_scrubber_leaks_no_part_of_bob_smith(text):
    out = scrub_windows_paths(text, path_basename)
    assert "Bob" not in out and "Smith" not in out, f"kept part of {text!r}: {out!r}"


def test_a_four_word_account_is_not_taken():
    out = scrub_windows_paths("C:\\Users\\a b c d\\x\\f.py", "X")
    assert out == "X b c d\\x\\f.py"


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
