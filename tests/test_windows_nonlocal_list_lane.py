"""Windows: a listing the user types against a share or a device path gets the refusal sentence.

Steve round 18 (Windows 11, 12 Nov 2026, 578d185f): "read", "show me" and "open" of a share or device
path were refused plainly, but "list \\\\?\\C:\\" still went to the model. The front door's read finder now
takes a listing verb (list, ls, dir, browse) too, and answers with the same sentence, before any model or
tool runs and without looking the path up.

The served-chat helpers and the `windows_host` seam are the ones `test_windows_nonlocal_read_lane.py` uses.
"""
from __future__ import annotations

import pytest

from tests._nonlocal_touch_recorder import recorder, windows_host
from tests.test_windows_nonlocal_path_message_lanes import NETWORK, _isolated_home, _served_chat, _served_turns

__all__ = ["_isolated_home", "recorder", "windows_host"]

_SHARE_AND_DEVICE_LISTINGS = {
    "list \\\\?\\C:\\": "\\\\?\\C:\\",
    "list the files in \\\\attkr9\\c$\\Windows": "\\\\attkr9\\c$\\Windows",
    "ls \\\\attkr9\\s": "\\\\attkr9\\s",
    "dir \\\\.\\PhysicalDrive0": "\\\\.\\PhysicalDrive0",
    "browse `\\\\?\\UNC\\attkr9\\s`": "\\\\?\\UNC\\attkr9\\s",
}


def test_on_windows_a_typed_share_or_device_listing_is_refused_by_name(monkeypatch, recorder, windows_host) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()
    replies, touched = _served_turns(chat, recorder, list(_SHARE_AND_DEVICE_LISTINGS))
    assert touched == {}, touched
    for text, typed in _SHARE_AND_DEVICE_LISTINGS.items():
        status, reply = replies[text]
        assert status == 200, (text, reply)
        assert NETWORK in reply, (text, reply)
        assert f"`{typed}`" in reply, (text, reply)
        assert "nothing was opened" in reply, (text, reply)


@pytest.mark.parametrize("text", [
    "list C:\\Users\\you\\Documents",
    "list my notes",
    "delete \\\\attkr9\\s\\x.txt",
    "copy the list to \\\\attkr9\\s",
    "what is a UNC path like \\\\server\\share?",
])
def test_on_windows_the_listing_cue_leaves_other_messages_alone(windows_host, text) -> None:
    from core.agent_runtime.read_path_honesty import non_local_read_target

    windows_host.on()
    assert non_local_read_target(text) == ("", ""), text


def test_off_windows_the_listing_cue_stands_down(windows_host) -> None:
    from core.agent_runtime.read_path_honesty import non_local_read_target

    windows_host.off()  # a real Windows runner has the seam on

    for text in _SHARE_AND_DEVICE_LISTINGS:
        assert non_local_read_target(text) == ("", "")


def test_sabotage_without_the_listing_cue_the_device_listing_is_not_refused(monkeypatch, windows_host) -> None:
    from core.agent_runtime import read_path_honesty

    monkeypatch.setattr(read_path_honesty, "_asks_to_list", lambda body: False)
    windows_host.on()
    assert read_path_honesty.non_local_read_target("list \\\\?\\C:\\") == ("", "")
