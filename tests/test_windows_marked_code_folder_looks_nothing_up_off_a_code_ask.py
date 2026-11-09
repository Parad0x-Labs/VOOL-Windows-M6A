"""Windows: a turn that is not a Python or pytest ask is not looked up to place it in a marked folder.

CI run 42676239859 (windows/train-captest 18d2de4a): the served heartbeat poll
"Read C://attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK." still got the share
refusal, but `windows_marked_code_folder.is_code_ask` (asked on every owner-local unbound turn by the
M4 turn rebind) had first stat'ed `C://attkr9/s/HEARTBEAT.md. If` and listed `C://attkr9/s/` while
reading the paths out of the text, before it found no code words at all. The heartbeat lane reads
that spelling as the share `//attkr9/s`; nothing it names may be looked up first.

On a real Windows host three seams are on that `windows_host` leaves alone: the write gate's
`_IS_WINDOWS`, write-root honesty's `_ON_WINDOWS`, and `_drive_is_remote("C:")` answering False.
They are set here as CI has them.
"""
from __future__ import annotations

import pytest

from core import windows_known_folders
from core import windows_write_gate as gate
from core.agent_runtime import write_root_honesty
from tests._nonlocal_touch_recorder import recorder, windows_host  # noqa: F401
from tests.test_windows_nonlocal_path_message_lanes import NETWORK, _served_chat, _served_turns

POLL = "Read C://attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK."


@pytest.fixture
def real_windows_seams(monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: False)


def test_is_code_ask_looks_nothing_up_for_a_message_with_no_code_words(recorder, windows_host, real_windows_seams) -> None:
    from core import windows_marked_code_folder as placed

    windows_host.on()

    assert placed.is_code_ask(POLL) is False
    assert placed.turn_folder(POLL) == ""
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


def test_the_served_drive_spelled_share_poll_is_refused_before_anything_is_looked_up(
    monkeypatch, recorder, windows_host, real_windows_seams
) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()

    replies, touched = _served_turns(chat, recorder, [POLL])

    assert touched == {}, touched
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    status, reply = replies[POLL]
    assert status == 200 and NETWORK in reply, reply
    assert "HEARTBEAT_OK" not in reply, reply
