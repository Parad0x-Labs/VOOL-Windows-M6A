"""A spaced account name that a quote, a bracket or a full stop closes must not leak its surname.

A JSON ``cwd`` or ``path`` that ends at the account folder (``{"cwd": "C:\\\\Users\\\\Bob Smith"}``)
is a common shape. The spaced account could end only at a separator, ``,`` ``;``, a line end or the
end of the text, so a closing ``"`` ``'`` ``)`` or ``.`` made it fall back to the strict segment, and
"Smith" was left in every redactor's output. On POSIX the error-surface and task-event rules then
published ``/Users/Bob Smith`` (and ``/Users/bob``) as the path's "basename", the account itself.
Inputs are plain strings, so the checks do not depend on the host OS.
"""
from __future__ import annotations

import pytest

from tests.test_spaced_account_names_scrub import REDACTORS


def _proof_projection(text: str) -> str:
    from core.proof_projection import _redact_text

    return _redact_text(text)


def _tool_certification(text: str) -> str:
    from core.local_model_tool_certification import _redact_text

    return _redact_text(text)


def _fact_extractor(text: str) -> str:
    from core.fact_extractor import _redact_sensitive_text

    return _redact_sensitive_text(text)


ALL_REDACTORS = {
    **REDACTORS,
    "proof_projection": _proof_projection,
    "tool_certification": _tool_certification,
    "fact_extractor": _fact_extractor,
}

SPACED_INPUTS = [
    ("json_doubled", '{"cwd": "C:\\\\Users\\\\Bob Smith"}'),
    ("json_single", '{"cwd": "C:\\Users\\Bob Smith"}'),
    ("json_forward", '{"cwd": "C:/Users/Bob Smith"}'),
    ("single_quoted", "open 'C:\\Users\\Bob Smith' failed"),
    ("parenthesised", "open (C:\\Users\\Bob Smith) failed"),
    ("full_stop", "Nothing in C:\\Users\\Bob Smith. Retry later"),
    ("posix_json", '{"cwd": "/Users/Bob Smith"}'),
    ("posix_parenthesised", "open (/home/Bob Smith) failed"),
    ("posix_full_stop", "Nothing in /Users/Bob Smith. Retry later"),
]


@pytest.mark.parametrize("text", [text for _, text in SPACED_INPUTS], ids=[i for i, _ in SPACED_INPUTS])
@pytest.mark.parametrize("name", list(ALL_REDACTORS))
def test_a_closed_spaced_account_leaks_no_part_of_the_name(name, text):
    out = ALL_REDACTORS[name](text)
    for needle in ("Bob", "Smith"):
        assert needle not in out, f"{name} kept {needle!r} from {text!r}: {out!r}"


# The error-surface and task-event POSIX rules reduce a path to its last segment. A path that ends
# AT the account folder has the account as its last segment, so that case gives the placeholder.
@pytest.mark.parametrize("name", ["error_surface", "faults", "task_event_model"])
@pytest.mark.parametrize("text", ['{"cwd": "/Users/bob"}', "open /home/bob failed", "open /Users/bob/ failed"])
def test_a_posix_path_ending_at_the_account_never_names_it(name, text):
    out = REDACTORS[name](text)
    assert "bob" not in out, f"{name} published the account from {text!r}: {out!r}"


# What these rules did before still holds: a file keeps its name, and prose after the path survives.
@pytest.mark.parametrize("name", ["error_surface", "faults", "task_event_model"])
def test_a_posix_file_still_keeps_its_name(name):
    out = REDACTORS[name]("could not read /Users/bob/x/notes.txt today")
    assert "notes.txt" in out and "bob" not in out and "today" in out, out


@pytest.mark.parametrize("name", list(REDACTORS))
@pytest.mark.parametrize(
    "text",
    ["Bob's laptop is fine.", "it's John Smith's file (see notes).", 'He said "Users can\'t log in."'],
)
def test_prose_with_quotes_and_full_stops_stays_readable(name, text):
    assert text in REDACTORS[name](text)
