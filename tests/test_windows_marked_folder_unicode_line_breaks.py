"""Windows marked-folder lane: a paste joined by any line break Python knows is not a one-line request.

Security Architect nit on 906317db: the one-line check looked for CR and LF only, so a paste joined by U+2028 (or
\\x0b, \\x0c, \\x1c-\\x1e, \\x85, U+2029) was read as one line and its "replace ... in <marked file>" planned an edit.
"""
from __future__ import annotations

import pytest

from tests.test_windows_marked_folder_edit_undo_move import _owner, machine


@pytest.mark.parametrize("separator", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_a_paste_joined_by_a_unicode_line_break_is_left_alone(machine, separator) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    target = machine["first"] / "hello.txt"
    target.write_bytes(b"a\n")
    for request in (f"replace 'a' with 'b' in {target}", f"move {target} to {machine['second']}"):
        assert marked_file_ask(request, source_context=_owner()) is not None, request
        for text in (f"{request}{separator}and then tell me a joke", f"log line one{separator}{request}"):
            assert marked_file_ask(text, source_context=_owner()) is None, repr(text)
    assert target.read_bytes() == b"a\n"
