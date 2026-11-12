"""Windows: a read the user types against a share or a device path gets the refusal sentence.

Claims sweep on Windows 11 (11 Nov 2026, 170f6572): "read \\\\localhost\\c$\\Windows\\win.ini", "show me
\\\\.\\PhysicalDrive0" and "open \\\\?\\C:\\" each got a vague model answer, because no message lane claimed
a read of a share or a device. The front door now answers such a read with the same sentence the
tool door gives, naming the path as typed, before any model or tool runs and without looking it up.

The Windows rule is switched on through `windows_path_policy._IS_WINDOWS` (the `windows_host`
fixture) after local setup, as in `test_windows_nonlocal_path_message_lanes.py`, whose served-chat
helpers this file reuses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._nonlocal_touch_recorder import recorder, windows_host
from tests.test_windows_nonlocal_path_message_lanes import (
    NETWORK,
    _isolated_home,
    _served_chat,
    _served_turns,
)

_SHARE_AND_DEVICE_READS = {
    "read \\\\attkr9\\c$\\Windows\\win.ini": "\\\\attkr9\\c$\\Windows\\win.ini",
    "read `\\\\attkr9\\c$\\Windows\\win.ini`": "\\\\attkr9\\c$\\Windows\\win.ini",
    "show me \\\\.\\PhysicalDrive0": "\\\\.\\PhysicalDrive0",
    "open \\\\?\\C:\\": "\\\\?\\C:\\",
    'what is in "//attkr9/s/notes.txt"': "//attkr9/s/notes.txt",
}


def test_on_windows_a_typed_share_or_device_read_is_refused_by_name(monkeypatch, recorder, windows_host) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()
    replies, touched = _served_turns(chat, recorder, list(_SHARE_AND_DEVICE_READS))
    assert touched == {}, touched
    for text, typed in _SHARE_AND_DEVICE_READS.items():
        status, reply = replies[text]
        assert status == 200, (text, reply)
        assert NETWORK in reply, (text, reply)
        assert f"`{typed}`" in reply, (text, reply)
        assert "nothing was opened" in reply, (text, reply)


def test_on_windows_a_local_read_is_still_read(monkeypatch, recorder, windows_host, tmp_path: Path) -> None:
    (tmp_path / "user" / "Documents" / "notes.txt").write_text("local notes 4821\n", encoding="utf-8")
    chat = _served_chat(monkeypatch)
    windows_host.on()
    replies, _touched = _served_turns(chat, recorder, ["show me ~/Documents/notes.txt"])
    status, reply = replies["show me ~/Documents/notes.txt"]
    assert status == 200
    assert NETWORK not in reply
    assert "4821" in reply, reply


@pytest.mark.parametrize("text", [
    "remember that my tax return is at //attkr9/s/tax.pdf",
    "delete //attkr9/s/x.txt",
    "read C:\\Users\\you\\notes.txt",
    "what is a UNC path like \\\\server\\share?",
])
def test_on_windows_the_read_finder_leaves_other_messages_alone(windows_host, text) -> None:
    from core.agent_runtime.read_path_honesty import non_local_read_target

    windows_host.on()
    assert non_local_read_target(text) == ("", ""), text


def test_off_windows_the_read_finder_stands_down(windows_host) -> None:
    from core.agent_runtime.read_path_honesty import non_local_read_target

    windows_host.off()  # a real Windows runner has the seam on

    for text in _SHARE_AND_DEVICE_READS:
        assert non_local_read_target(text) == ("", "")


def test_sabotage_without_the_read_finder_the_share_read_falls_through(monkeypatch, recorder, windows_host) -> None:
    from core.agent_runtime import read_path_honesty

    monkeypatch.setattr(read_path_honesty, "non_local_read_target", lambda text: ("", ""))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    replies, _touched = _served_turns(chat, recorder, ["show me \\\\.\\PhysicalDrive0"])
    _status, reply = replies["show me \\\\.\\PhysicalDrive0"]
    assert "`\\\\.\\PhysicalDrive0` is a network or device path" not in reply, reply
