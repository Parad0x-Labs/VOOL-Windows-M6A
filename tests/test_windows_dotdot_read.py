"""Windows (owner decision D4, 11 Nov 2026): a read whose relative path climbs out of the chat's folder is refused.

Claims sweep on Windows 11 (170f6572): a write or a move typed as "..\\y.txt" already gets a plain refusal
naming where it leads (`test_windows_dotdot_and_run_script.py`). A read typed the same way ("show me
..\\secret.txt") did not. The front door now answers it in one plain sentence, naming the path as typed
and where it leads, placed by text from the chat's own folder, before any model or tool runs. A climb
that stays inside the chat's folder ("sub\\..\\x.txt") is left to the read lane.

The served chat and its folder (`<tmp>/workspace`) come from `test_windows_nonlocal_path_message_lanes.py`,
the marked-folder machine and the `\\` separator from `test_windows_dotdot_and_run_script.py`.
The read rule is switched on through `windows_path_policy._IS_WINDOWS` (the `windows_host` fixture), and in
the served rows also the write gate's seam, which keeps a `..\\` name inside one request on Windows.
"""
from __future__ import annotations

import pytest

from core import windows_write_gate as gate
from core.agent_runtime import read_path_honesty
from tests._nonlocal_touch_recorder import recorder, windows_host
from tests.test_windows_dotdot_and_run_script import backslash, machine
from tests.test_windows_nonlocal_path_message_lanes import _isolated_home, _served_chat, _served_turns

__all__ = ["_isolated_home", "backslash", "machine", "recorder", "windows_host"]


@pytest.mark.parametrize("separator", ["/", "\\"], ids=["slash", "backslash"])
@pytest.mark.parametrize("verb", ["show me", "read", "what is in"])
def test_on_windows_a_relative_read_that_climbs_out_is_refused(monkeypatch, recorder, windows_host, backslash, tmp_path, separator, verb) -> None:
    """The chat's own folder is `<tmp>/workspace`; "..\\secret.txt" leads to `<tmp>/secret.txt`."""
    (tmp_path / "secret.txt").write_text("outside 7731\n", encoding="utf-8")
    chat = _served_chat(monkeypatch)
    windows_host.on()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)  # on Windows the turn splitter keeps `..\\` whole too
    typed = f"..{separator}secret.txt"
    replies, touched = _served_turns(chat, recorder, [f"{verb} {typed}"])
    status, reply = replies[f"{verb} {typed}"]
    leads_to = tmp_path / "secret.txt"
    assert status == 200, reply
    assert reply == read_path_honesty.climbing_relative_read_response(typed, str(leads_to)), reply
    assert reply.startswith(f"I can't open `{typed}`: its `..` climbs out of this chat's folder"), reply
    assert reply.endswith("Nothing was opened."), reply
    assert "7731" not in reply
    assert touched == {}, touched


def test_on_windows_a_climb_that_stays_inside_the_chats_folder_is_left_alone(machine, windows_host) -> None:
    sub = machine["marked"] / "sub"
    sub.mkdir()
    context = {"workspace": str(machine["marked"])}
    windows_host.on()
    assert read_path_honesty.climbing_relative_read("show me sub/../x.txt", source_context=context) == ("", "")
    assert read_path_honesty.climbing_relative_read("show me ../sub/x.txt", source_context={"workspace": str(sub)}) == ("", "")
    path, leads_to = read_path_honesty.climbing_relative_read("show me ../../x.txt", source_context={"workspace": str(sub)})
    assert (path, leads_to) == ("../../x.txt", str(machine["marked"].parent / "x.txt"))


@pytest.mark.parametrize("text", [
    "write hi to ../y.txt",
    "the file sits at ../y.txt, I think",
    "show me notes.txt",
    "show me C:\\Users\\you\\notes.txt",
])
def test_on_windows_the_climb_finder_leaves_other_messages_alone(machine, windows_host, text) -> None:
    windows_host.on()
    context = {"workspace": str(machine["marked"])}
    assert read_path_honesty.climbing_relative_read(text, source_context=context) == ("", ""), text


def test_off_windows_the_climb_finder_stands_down(machine, windows_host) -> None:
    windows_host.off()  # a real Windows runner has the seam on
    context = {"workspace": str(machine["marked"])}
    assert read_path_honesty.climbing_relative_read("show me ../secret.txt", source_context=context) == ("", "")


def test_sabotage_without_the_climb_finder_the_read_is_not_refused(monkeypatch, recorder, windows_host, tmp_path) -> None:
    (tmp_path / "secret.txt").write_text("outside 7731\n", encoding="utf-8")
    monkeypatch.setattr(read_path_honesty, "climbing_relative_read", lambda text, source_context=None: ("", ""))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    replies, _touched = _served_turns(chat, recorder, ["show me ../secret.txt"])
    _status, reply = replies["show me ../secret.txt"]
    assert "climbs out of this chat's folder" not in reply, reply
