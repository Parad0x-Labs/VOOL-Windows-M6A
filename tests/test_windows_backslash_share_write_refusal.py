"""On Windows, a write to a share or a device typed with backslashes gets the plain refusal, first.

Clean-VM capability test B10 (0.7.1-beta): asked to write at `\\\\localhost\\C$\\...`,
`\\\\10.0.2.2\\...`, `\\\\?\\C:\\...` or `\\\\.\\...`, VOOL wrote nothing but answered "I couldn't build
that in the workspace: the model did not return usable file contents" or handed the turn to the model.
The write finder behind the Windows refusal found a path only by its leading `~`, drive or `/`, so a
share typed the way Windows users type it was no path to it, and the turn went on to the builder or the
model. The 0.7.1 notes say files on other machines and device paths are refused; this pins that the
user is told so, in the refusal sentence naming the path as typed, before any builder or model runs.

The marker host `attkr9` is watched by `tests/_nonlocal_touch_recorder.py`, so the served turns also
prove nothing reached for it. The Windows rule is switched on only through the `windows_host` fixture.
"""

from __future__ import annotations

import pytest

from tests._nonlocal_touch_recorder import recorder, windows_host
from tests.test_windows_nonlocal_path_message_lanes import _isolated_home, _served_chat, _served_turns

NETWORK = "is a network or device path"
WROTE_NOTHING = "so nothing was written"

# Each message, with the path the refusal must name and its kind.
TYPED_WRITES = {
    "write hello to \\\\attkr9\\C$\\Users\\Public\\vool_test.txt": ("\\\\attkr9\\C$\\Users\\Public\\vool_test.txt", "unc"),
    "create a file at \\\\attkr9\\share\\a.txt saying hi": ("\\\\attkr9\\share\\a.txt", "unc"),
    "create notes.md at \\\\attkr9\\C$\\Users\\Public with a summary": ("\\\\attkr9\\C$\\Users\\Public", "unc"),
    'write hello to "\\\\attkr9\\C$\\x.txt"': ("\\\\attkr9\\C$\\x.txt", "unc"),
    "write hello to `\\\\attkr9\\s\\x.txt`": ("\\\\attkr9\\s\\x.txt", "unc"),
    "write hi to \\\\?\\UNC\\attkr9\\s\\x.txt": ("\\\\?\\UNC\\attkr9\\s\\x.txt", "device"),
    "write hi to \\\\.\\pipe\\attkr9": ("\\\\.\\pipe\\attkr9", "device"),
    "write hi to \\??\\UNC\\attkr9\\s\\x.txt": ("\\??\\UNC\\attkr9\\s\\x.txt", "nt_namespace"),
}
# A local device path names no host, so it is pinned on the finder alone (the served turn would be the
# same refusal; the recorder watches only the marker host).
LOCAL_DEVICE_WRITES = {
    "write hi to \\\\?\\C:\\Users\\Public\\x.txt": ("\\\\?\\C:\\Users\\Public\\x.txt", "device"),
    "write hi to \\\\.\\C:\\Users\\Public\\x.txt": ("\\\\.\\C:\\Users\\Public\\x.txt", "device"),
    "write hello to \\\\localhost\\C$\\Users\\Public\\x.txt": ("\\\\localhost\\C$\\Users\\Public\\x.txt", "unc"),
    "write hello to \\\\10.0.2.2\\share\\x.txt": ("\\\\10.0.2.2\\share\\x.txt", "unc"),
}
# Backslashes that name no share or device, or a share the turn does not ask to write: not answered here.
NOT_WRITE_TARGETS = [
    'write "\\\\d+" to ./re.txt',
    "add a line \\\\n to ./a.txt",
    "create a.txt with content: \\\\attkr9\\s\\x",
    "create run.sh with:\n```\ncopy a \\\\attkr9\\s\\x\n```",
    "remember my backups are on \\\\attkr9\\s",
    "what is on \\\\attkr9\\s",
    "write hello to C:\\Users\\Public\\x.txt",
    "write hello to S:\\x.txt",
]


def test_the_write_finder_finds_a_share_or_device_typed_with_backslashes(recorder, windows_host) -> None:
    from core.agent_runtime.write_root_honesty import non_local_write_target

    windows_host.on()
    typed = {**TYPED_WRITES, **LOCAL_DEVICE_WRITES}

    assert {text: non_local_write_target(text) for text in typed} == typed
    # The build lane's network tier finds the same shares and devices.
    assert {text: non_local_write_target(text, network_only=True) for text in typed} == typed
    assert {text: non_local_write_target(text) for text in NOT_WRITE_TARGETS} == dict.fromkeys(NOT_WRITE_TARGETS, ("", ""))
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


def test_the_front_door_answers_a_backslash_share_write_before_the_builder_or_model(windows_host) -> None:
    from core.agent_runtime.turn_frontdoor import _non_local_typed_path

    windows_host.on()

    found = {text: _non_local_typed_path(text, "write", network_only=True) for text in TYPED_WRITES}

    assert found == {text: (path, kind, "write") for text, (path, kind) in TYPED_WRITES.items()}


def test_off_windows_a_backslash_share_is_no_write_target() -> None:
    from core.agent_runtime.write_root_honesty import non_local_write_target

    assert {text: non_local_write_target(text) for text in TYPED_WRITES} == dict.fromkeys(TYPED_WRITES, ("", ""))


# Numbered ids: a test id naming the marker reaches the environment of every process the turn starts
# (PYTEST_CURRENT_TEST), which the recorder would count as a process naming it.
@pytest.mark.parametrize("text", list(TYPED_WRITES), ids=[f"write-{n}" for n in range(len(TYPED_WRITES))])
def test_a_served_write_to_a_backslash_share_is_one_plain_refusal(text: str, monkeypatch, recorder, windows_host) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()
    path, _kind = TYPED_WRITES[text]

    replies, touched = _served_turns(chat, recorder, [text])

    status, reply = replies[text]
    assert touched == {}, touched
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert status == 200
    # The refusal sentence about the path as typed, and nothing else: no builder failure, no model reply.
    assert reply == (
        f"`{path}` is a network or device path. On Windows, VOOL only opens files on a local drive "
        "(like C:\\Users\\you\\file.txt) or inside the current folder, so nothing was written."
    ), reply


def test_the_workspace_reachability_check_is_unchanged_for_backslash_shares(tmp_path, windows_host) -> None:
    from core.agent_runtime.write_root_honesty import requested_rooted_paths, unreachable_write_target

    context = {"workspace": str(tmp_path / "workspace")}
    windows_host.on()

    assert [requested_rooted_paths(text) for text in TYPED_WRITES] == [[] for _ in TYPED_WRITES]
    assert [unreachable_write_target(text, source_context=context) for text in TYPED_WRITES] == [""] * len(TYPED_WRITES)


