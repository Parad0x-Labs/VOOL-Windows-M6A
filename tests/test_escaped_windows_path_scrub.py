"""Escaped Windows paths (doubled backslashes, UNC, \\\\?\\ device forms) never survive a redactor.

A path that went through a Python ``repr`` or JSON carries doubled backslashes. The redactors'
own patterns knew a single backslash or a POSIX slash only, so ``C:\\\\Users\\\\alice\\\\x\\\\f.py`` kept
the account name and folders. Every input below is a plain string, so these tests do not depend on
the host OS. Imports of the shared helper are inside the tests that use it, so on code without it
the redactor tests still fail by assertion on the leak, not by a collection error.
"""
from __future__ import annotations

import time

import pytest

# --- the forms: (id, text). The account name "alice" must not survive any of them. ------------------
FORMS = [
    ("single", "open C:\\Users\\alice\\x\\f.py"),
    ("doubled", "open C:\\\\Users\\\\alice\\\\x\\\\f.py"),
    ("forward", "open C:/Users/alice/x/f.py"),
    ("unc_single", "open \\\\fileserver\\share\\alice\\f.py"),
    ("unc_doubled", "open \\\\\\\\fileserver\\\\share\\\\alice\\\\f.py"),
    ("long_drive", "open \\\\?\\C:\\Users\\alice\\x\\f.py"),
    ("long_drive_doubled", "open \\\\\\\\?\\\\C:\\\\Users\\\\alice\\\\x\\\\f.py"),
    ("long_unc", "open \\\\?\\UNC\\fileserver\\share\\alice\\f.py"),
    ("device", "open \\\\.\\GLOBALROOT\\alice\\f.py"),
    ("rooted_single", "open \\Users\\alice\\x\\f.py"),
    ("rooted_doubled", "open \\\\Users\\\\alice\\\\x\\\\f.py"),
]
FORM_IDS = [form_id for form_id, _ in FORMS]
FORM_TEXTS = [text for _, text in FORMS]


def _error_surface(text: str) -> str:
    from core.error_surface import safe_error_text

    return safe_error_text(OSError(text))


def _faults(text: str) -> str:
    from core.faults.redaction import redact_text

    return redact_text(text)


def _bug_report(text: str) -> str:
    from core.bug_report.redaction import redact_text

    return redact_text(text)[0]


def _session_portability(text: str) -> str:
    from core.session_portability.redaction import RedactionReport, redact_value

    return redact_value(text, RedactionReport(), [])


def _task_event_model(text: str) -> str:
    from core.task_event_model import _safe_diagnostic_text

    return _safe_diagnostic_text(text, limit=200)


REDACTORS = {
    "error_surface": _error_surface,
    "faults": _faults,
    "bug_report": _bug_report,
    "session_portability": _session_portability,
    "task_event_model": _task_event_model,
}


# --- the exact reported leak ----------------------------------------------------------------------
def test_error_surface_doubled_backslash_path_keeps_neither_user_nor_folders():
    out = _error_surface("open C:\\\\Users\\\\alice\\\\x\\\\f.py")
    assert "alice" not in out
    assert "Users" not in out
    assert "x\\" not in out
    assert "open f.py" in out


def test_error_surface_path_ending_at_the_account_folder_does_not_name_it():
    out = _error_surface("open C:\\\\Users\\\\alice")
    assert "alice" not in out


# --- every redactor, every form ----------------------------------------------------------------------
@pytest.mark.parametrize("text", FORM_TEXTS, ids=FORM_IDS)
@pytest.mark.parametrize("name", list(REDACTORS))
def test_redactor_scrubs_every_path_spelling(name, text):
    out = REDACTORS[name](text)
    assert "alice" not in out, f"{name} leaked the account name from {text!r}: {out!r}"


# error_surface wraps its detail in a fixed sentence, so feeding its output back in is not a
# redaction-idempotence question; the other four return the redacted text itself.
@pytest.mark.parametrize("name", [n for n in REDACTORS if n != "error_surface"])
def test_redactor_scrub_is_idempotent_on_escaped_paths(name):
    text = "open C:\\\\Users\\\\alice\\\\x\\\\f.py and \\\\\\\\fileserver\\\\share\\\\alice\\\\g.py"
    once = REDACTORS[name](text)
    assert "alice" not in once
    assert REDACTORS[name](once) == once


def test_bug_report_keeps_the_project_relative_remainder_of_an_escaped_home_path():
    out = _bug_report("open C:\\\\Users\\\\alice\\\\proj\\\\core\\\\f.py")
    assert "[redacted-user]" in out
    assert "alice" not in out
    assert "f.py" in out
    assert "[redacted-home]" not in out


def test_bug_report_single_backslash_home_path_output_is_unchanged_by_the_extra_pass():
    out = _bug_report("open C:\\Users\\bob\\AppData\\Local\\f.py")
    assert out == "open C:/Users/[redacted-user]\\AppData\\Local\\f.py"


def test_bug_report_unc_and_device_paths_take_the_home_marker():
    for text in (
        "see \\\\fileserver\\share\\alice\\f.py now",
        "see \\\\\\\\fileserver\\\\share\\\\alice\\\\f.py now",
        "see \\\\?\\UNC\\fileserver\\share\\alice\\f.py now",
    ):
        assert _bug_report(text) == "see [redacted-home] now"


def test_session_portability_escaped_home_keeps_the_remainder_and_uses_its_marker():
    out = _session_portability("open C:\\\\Users\\\\alice\\\\x\\\\f.py")
    assert out == "open [redacted-home]\\\\x\\\\f.py"


def test_session_portability_unc_takes_the_home_marker():
    assert _session_portability("open \\\\fileserver\\share\\alice\\f.py") == "open [redacted-home]"


# --- the bug-report scanner post-check ---------------------------------------------------------------
def _scan_rules(text: str) -> set[str]:
    from core.bug_report.scanner import scan_text

    return {finding.rule for finding in scan_text(text)}


def test_scanner_flags_a_doubled_backslash_home_path():
    assert "home_path" in _scan_rules("open C:\\\\Users\\\\alice\\\\x\\\\f.py")


def test_scanner_flags_a_doubled_rooted_home_path():
    assert "home_path" in _scan_rules("open \\\\Users\\\\alice\\\\x\\\\f.py")


def test_scanner_flags_a_device_path():
    assert "home_path" in _scan_rules("open \\\\?\\C:\\proj\\f.py")
    assert "home_path" in _scan_rules("open \\\\\\\\?\\\\C:\\\\proj\\\\f.py")


@pytest.mark.parametrize("text", FORM_TEXTS, ids=FORM_IDS)
def test_scanner_finds_no_home_path_in_redacted_output(text):
    assert "home_path" not in _scan_rules(_bug_report(text))


def test_scanner_leaves_a_json_encoded_relative_path_alone():
    # JSON doubles the single backslashes of "\foo\bar"; that is not a UNC share.
    assert "home_path" not in _scan_rules('{"note": "see \\\\foo\\\\bar"}')


# --- monotonicity: nothing redacted before may come back ----------------------------------------------
# (text, substrings that must NOT appear in any redactor's output). Taken from the existing
# error_surface / bug_report / fault / task-event tests plus POSIX /home/alice/x and /Users/alice/x.
#
# Every needle below is a substring the surface's UNCHANGED code already removes (traced from its
# base patterns), so each case passes on base and can only fail if the new shared pass un-redacts.
# Per surface: the inputs its own existing tests use, plus single-backslash, forward-slash and
# POSIX home forms. The surface is in the test id.
_COMMON_HOME_FORMS = [
    ("C:\\Users\\bob\\AppData\\Local\\VOOL\\vool.db", ("bob",)),  # single backslash
    ("C:/Users/carol/Documents/settings.yaml", ("carol",)),  # forward slash
    ("/home/alice/x", ("alice",)),  # POSIX
    ("/Users/alice/x", ("alice",)),  # POSIX (macOS)
]
MONOTONIC_BY_SURFACE = {
    # tests/test_error_surface.py
    "error_surface": [
        ("cannot open /Users/example-user/Desktop/web0/secrets.env", ("example-user",)),
        ("failed on C:\\Users\\kas\\AppData\\vool\\model.bin", ("kas", "AppData")),
        *_COMMON_HOME_FORMS,
    ],
    # tests/test_fault_catalog.py, tests/test_fault_producers.py
    "faults": [
        ("boom at /Users/example-user/desktop/secret_loader.py", ("example-user",)),
        ("Bearer abc123def456 at /Users/example-user/x/y.py", ("example-user",)),
        ("/home/alice/whatever", ("alice",)),
        ("/Users/example-user/secret-folder/creds.txt", ("example-user",)),
        *_COMMON_HOME_FORMS,
    ],
    # tests/test_bug_report_redaction.py, tests/test_bug_report_pipeline.py
    "bug_report": [
        ("/Users/fixtureuser/Desktop/vool-checkout/core/foo.py", ("fixtureuser",)),
        ('  File "/Users/fixtureuser/vool/core/conductor.py", line 88, in plan', ("fixtureuser",)),
        ("/home/dave/", ("dave",)),
        ("C:\\Users\\kas\\AppData\\vool\\model.bin", ("kas",)),
        *_COMMON_HOME_FORMS,
    ],
    # tests/session_portability/test_redaction.py (PLANTED_PRIVATE_PATH)
    "session_portability": [
        ("notes live in /Users/example-user/secret-notes/tokens.txt", ("example-user",)),
        ("my key is x and notes live in /home/alice/notes.txt", ("alice",)),
        ("C:\\Users\\kas\\AppData\\vool\\model.bin", ("kas",)),
        *_COMMON_HOME_FORMS,
    ],
    # tests/test_task_event_diagnostics.py
    "task_event_model": [
        ("FileNotFoundError: /Users/operator/Desktop/vool/models/qwen3.gguf", ("operator",)),
        ("OSError: cannot open C:\\Users\\kas\\AppData\\vool\\model.bin", ("kas", "AppData")),
        *_COMMON_HOME_FORMS,
    ],
}
assert set(MONOTONIC_BY_SURFACE) == set(REDACTORS)
MONOTONIC = [
    pytest.param(surface, text, sensitive, id=f"{surface}-{index}")
    for surface, cases in MONOTONIC_BY_SURFACE.items()
    for index, (text, sensitive) in enumerate(cases)
]


@pytest.mark.parametrize(("name", "text", "sensitive"), MONOTONIC)
def test_no_previously_redacted_substring_comes_back(name, text, sensitive):
    out = REDACTORS[name](text)
    for needle in sensitive:
        assert needle not in out, f"{name}: {needle!r} survived in {out!r}"


# The bug-report scanner post-check over the same inputs. On the raw text it reports a home path
# exactly where the base _HOME_SEGMENT_RE does (a single separator before and after users/home --
# every input above); the doubled-only spellings are new (see test_scanner_flags_*). On the
# bug_report redactor's output it must report none.
_SCANNER_INPUTS = sorted({text for cases in MONOTONIC_BY_SURFACE.values() for text, _ in cases})


@pytest.mark.parametrize("text", _SCANNER_INPUTS, ids=[f"bug_report_scanner-{i}" for i in range(len(_SCANNER_INPUTS))])
def test_bug_report_scanner_flags_raw_and_clears_redacted_home_paths(text):
    assert "home_path" in _scan_rules(text), f"scanner missed a home path in the raw {text!r}"
    redacted = _bug_report(text)
    assert "home_path" not in _scan_rules(redacted), f"scanner still sees a home path in {redacted!r}"


# --- ordinary text stays readable -------------------------------------------------------------------
PROSE = [
    "Meeting at 10:30 today",
    "Option A: text",
    "see https://host/a/b for details",
    "pattern [A-Za-z]:\\s*\\d+ matches",
    "re: \\\\d+\\\\s*\\\\w+ is a regex",
    "re.compile('\\\\\\\\d+')",
    "model openrouter/anthropic/claude-x",
    "^\\d{3}-\\d{4}$",
    "a:b, c:d and x:y",
]


@pytest.mark.parametrize("text", PROSE)
def test_shared_scrubber_leaves_ordinary_text_alone(text):
    from core.path_scrub import scrub_windows_paths

    assert scrub_windows_paths(text, "X") == text


@pytest.mark.parametrize("name", list(REDACTORS))
@pytest.mark.parametrize("text", ["Meeting at 10:30 today", "Option A: text"])
def test_redactors_leave_times_and_labels_readable(name, text):
    assert text in REDACTORS[name](text)


# --- the shared helper itself ---------------------------------------------------------------------------
def test_shared_scrubber_replaces_every_form_with_the_callers_token():
    from core.path_scrub import scrub_windows_paths

    for _form_id, text in FORMS:
        out = scrub_windows_paths(text, "<P>")
        assert "alice" not in out
        assert "<P>" in out


def test_shared_scrubber_token_is_literal_not_a_regex_template():
    from core.path_scrub import scrub_windows_paths

    assert scrub_windows_paths("open C:\\\\a\\\\b", "\\1[x]\\n") == "open \\1[x]\\n"


def test_shared_scrubber_basename_callable():
    from core.path_scrub import path_basename, scrub_windows_paths

    assert scrub_windows_paths("open C:\\\\Users\\\\alice\\\\x\\\\f.py!", path_basename) == "open f.py!"
    assert scrub_windows_paths("open C:\\Users\\alice\\x\\f.py", path_basename) == "open f.py"
    assert scrub_windows_paths("at C:/Users/alice/x/f.py", path_basename) == "at f.py"
    assert scrub_windows_paths("at C:\\\\Users\\\\alice", path_basename) == "at <path>"


@pytest.mark.parametrize(
    "text",
    [
        "\\" * 100_000,
        "C:" + "\\" * 100_000,
        "\\\\" + "a" * 100_000,
        "\\\\" + "a" * 100_000 + "\\",
        "C:\\" * 30_000,
        "\\\\?\\" * 25_000,
        "\\\\ab\\" * 20_000,
        "C:\\\\" + "a\\\\" * 40_000,
        "a\\ " * 30_000,
    ],
    ids=[
        "backslashes",
        "drive_then_backslashes",
        "unc_prefix_long_server",
        "unc_prefix_long_server_sep",
        "repeated_drive",
        "repeated_device_prefix",
        "repeated_unc",
        "long_doubled_path",
        "spaced_segments",
    ],
)
def test_shared_scrubber_is_linear_on_hostile_lines(text):
    from core.path_scrub import scrub_windows_paths

    started = time.perf_counter()
    scrub_windows_paths(text, "X")
    assert time.perf_counter() - started < 5.0
