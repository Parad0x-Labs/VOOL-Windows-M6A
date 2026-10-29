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
