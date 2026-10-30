"""Leaks and an over-redaction a second review of the shared path scrubber found.

- A Windows account the word rules could not take (a lowercase word after the first, as in
  ``ana de la cruz``, or a name over 64 characters) lost only its first word. A Windows account
  now runs to the next separator or stop, whatever its words look like.
- ``SPACED_HOME_RE`` took ``/users/42 404 Not Found`` (a URL path, lowercase ``users``, no drive)
  as a home, so session export masked the rest of the line.
- Deep on a share, ``li.wang`` looked like a file with a ``.wang`` extension and was given back as
  the basename. A share path now never gives back its last segment.
- A non-ASCII name in a bytes repr (``b'C:\\\\Users\\\\\\xc5\\x81ukasz'``) or in JSON's ASCII escapes
  (``\\u0141ukasz``): the escape's backslash was read as a separator, so ``x81ukasz`` came back.
- Acceptance masked a URL's ``/home/`` segment.

Inputs are plain strings, so the checks do not depend on the host OS.
"""
from __future__ import annotations

import json

import pytest

from core.path_scrub import path_basename, scrub_windows_paths
from tests.test_path_scrub_review_leaks import EVERY_REDACTOR

LEAKS = [
    ("lowercase_particles", "open C:\\Users\\anaq de la cruzq\\f.py failed", ("anaq", "cruzq", " de la ")),
    ("all_lowercase_prose_after", "cwd is C:\\Users\\johnq smithq and it failed", ("johnq", "smithq")),
    ("particle_prose_after", "cwd is C:\\Users\\Zqa van Smithq and it failed", ("Zqa", "Smithq", " van ")),
    ("over_64_chars", "open C:\\Users\\Zqa " + "q" * 70 + " smithq\\f.py failed", ("Zqa", "qqqq", "smithq")),
    ("lowercase_doubled", "open C:\\\\Users\\\\anaq de la cruzq\\\\f.py failed", ("anaq", "cruzq", " de la ")),
    ("posix_home_lowercase", "open /home/anaq de la cruzq/f.py failed", ("anaq", "cruzq", " de la ")),
    ("bytes_repr", "No such file: " + repr(b"C:\\Users\\\xc5\x81ukaszq"), ("ukaszq", "x81")),
    ("bytes_repr_deeper", "No such file: " + repr(b"C:\\Users\\\xc5\x81ukaszq\\f.py"), ("ukaszq", "x81")),
    ("json_ascii_escape", "open " + json.dumps("C:\\Users\\\u0141ukaszq\\f.py") + " failed", ("ukaszq", "u0141")),
]


@pytest.mark.parametrize("text, needles", [(t, n) for _, t, n in LEAKS], ids=[i for i, _, _ in LEAKS])
@pytest.mark.parametrize("name", list(EVERY_REDACTOR))
def test_no_redactor_keeps_any_part_of_the_account(name, text, needles):
    out = EVERY_REDACTOR[name](text)
    for needle in needles:
        assert needle not in out, f"{name} kept {needle!r} from {text!r}: {out!r}"


@pytest.mark.parametrize(
    "path", ["\\\\fs01\\dept\\team\\li.wangq", "\\\\fs01\\a\\b\\c\\jane.doeq", "\\\\fs01\\dept\\x\\y\\plan.txt"]
)
@pytest.mark.parametrize("name", ["error_surface", "faults", "task_event_model"])
def test_a_share_path_never_gives_back_its_last_segment(name, path):
    out = EVERY_REDACTOR[name](f"open {path} failed")
    last = path.rsplit("\\", 1)[1]
    assert last not in out, f"{name}: {out!r}"


def test_the_shared_scrubber_masks_a_whole_share_path():
    assert scrub_windows_paths("open \\\\fs01\\dept\\team\\li.wangq now", path_basename) == "open <path> now"


URL_PROSE = [("GET /users/42 404 Not Found", "404 Not Found"), ("GET /users/42 \u2192 200 OK in 3ms", "200 OK in 3ms")]


@pytest.mark.parametrize("text, _tail", URL_PROSE)
def test_session_export_leaves_a_url_path_named_users_alone(text, _tail):
    assert EVERY_REDACTOR["session_portability"](text) == text


@pytest.mark.parametrize("text, tail", URL_PROSE)
@pytest.mark.parametrize("name", ["proof_projection", "tool_certification"])
def test_the_prose_after_a_url_path_named_users_survives(name, text, tail):
    # These two have always masked ``/users/<id>`` itself; the words after it must stay.
    assert tail in EVERY_REDACTOR[name](text)


def test_acceptance_leaves_a_url_home_segment_alone():
    assert EVERY_REDACTOR["acceptance"]("see https://example.com/home/index") == "see https://example.com/home/index"


def test_a_windows_path_after_the_account_still_reads_as_its_own_path():
    out = scrub_windows_paths("copied C:\\Users\\bob to the share and then D:\\out\\f.txt", path_basename)
    assert "bob" not in out
    assert out.endswith("f.txt"), out
