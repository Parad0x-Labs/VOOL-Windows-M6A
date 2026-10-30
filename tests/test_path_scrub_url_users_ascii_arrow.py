"""A URL path named ``/users/<id>`` followed by an ASCII arrow (``GET /users/42 -> 200 OK``).

The lowercase ``/users/`` rule took ``42 -`` as a two-word spaced account (``-`` is a name
character and ``>`` closes a value), so session export turned the line into
``GET [redacted-home]> 200 OK`` and every other redactor ate the arrow's ``-``. A word of a spaced
name after a lowercase ``/users/`` now starts with a letter.

Inputs are plain strings, so the checks do not depend on the host OS.
"""
from __future__ import annotations

import pytest

from tests.test_path_scrub_review_leaks import EVERY_REDACTOR

URL_PROSE = [
    "GET /users/42 -> 200 OK in 3ms",
    "GET /users/42 => 200 OK",
    "PATCH /users/7 - 409 Conflict",
]


@pytest.mark.parametrize("text", URL_PROSE)
def test_session_export_leaves_a_url_path_with_an_ascii_arrow_alone(text):
    assert EVERY_REDACTOR["session_portability"](text) == text


@pytest.mark.parametrize("text", URL_PROSE)
@pytest.mark.parametrize("name", list(EVERY_REDACTOR))
def test_no_redactor_eats_the_arrow_after_a_url_path_named_users(name, text):
    tail = text.split("/users/", 1)[1].split(" ", 1)[1]
    assert tail in EVERY_REDACTOR[name](text)


@pytest.mark.parametrize("name", list(EVERY_REDACTOR))
def test_a_lowercase_users_spaced_name_is_still_masked(name):
    out = EVERY_REDACTOR[name]("open /users/zqa smithq/f.py failed")
    assert "zqa" not in out and "smithq" not in out, f"{name}: {out!r}"
