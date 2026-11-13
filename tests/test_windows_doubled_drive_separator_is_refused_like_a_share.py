"""Windows: a drive path with a doubled separator right after the colon is refused like a share.

`C://host/s/x`, `C:\\\\host\\s\\x`, `C:/\\host` and `C:\\/host` are full local paths to
`windows_path_policy.classify`, but the heartbeat lane reads `C://attkr9/s/...` from its first `/` as
the share `//attkr9/s` (CI run 42676239859). Security Architect, 9 Nov 2026: fail closed. Every lane
that reads a path out of a message refuses one of these before it stats, lists or resolves it:
write-root honesty's extension through spaces, the write lane, the marked-folder edit and move lane
(core/windows_marked_file_ops.py), M4's code-ask placement (core/windows_marked_code_folder.py) and
the planner's folder cleaner.

On a real Windows host three seams are on that `windows_host` leaves alone: the write gate's
`_IS_WINDOWS`, write-root honesty's and the marked-file lane's `_ON_WINDOWS`, and
`_drive_is_remote("C:")` answering False. They are set here as CI has them.
"""
from __future__ import annotations

import os

import pytest

from core import windows_known_folders, windows_marked_file_ops, windows_path_policy
from core import windows_write_gate as gate
from core.agent_runtime import write_root_honesty
from tests._nonlocal_touch_recorder import recorder, windows_host
from tests.test_windows_nonlocal_path_message_lanes import NETWORK, _served_chat, _served_turns

DOUBLED = ["C://attkr9/s/x", "C:\\\\attkr9\\s\\x", "C:/\\attkr9/s/x", "C:\\/attkr9\\s\\x"]


@pytest.fixture
def real_windows_seams(monkeypatch, tmp_path):
    marked = tmp_path / "marked"
    marked.mkdir()
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    monkeypatch.setattr(windows_marked_file_ops, "_ON_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: False)


@pytest.fixture
def lookups(monkeypatch) -> list[str]:
    """Every stat, exists, isdir, isfile, scandir or realpath whose argument names `attkr9`."""
    seen: list[str] = []
    for owner, name in ((os, "stat"), (os, "scandir"), (os.path, "exists"), (os.path, "isdir"),
                        (os.path, "isfile"), (os.path, "realpath")):
        real = getattr(owner, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            if args and "attkr9" in str(os.fspath(args[0]) if isinstance(args[0], os.PathLike) else args[0]).lower():
                seen.append(f"{_name} {args[0]!r}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)
    return seen


@pytest.mark.parametrize("path", DOUBLED)
def test_the_rule_names_each_spelling_on_windows_only(path: str, windows_host) -> None:
    if not windows_path_policy._IS_WINDOWS:
        # Off Windows the rule is off. On a Windows host it is already on, so this half does not apply.
        assert windows_path_policy.refused_doubled_drive(path) == ""
    windows_host.on()
    assert windows_path_policy.refused_doubled_drive(path) == "unc"
    for local in ("C:/attkr9/s/x", "C:\\attkr9\\s", "C:attkr9", "//attkr9/s"):
        assert windows_path_policy.refused_doubled_drive(local) == "", local


@pytest.mark.parametrize("path", DOUBLED)
def test_reading_the_path_out_of_a_message_looks_nothing_up(path: str, recorder, lookups, windows_host, real_windows_seams) -> None:
    from core import windows_marked_code_folder as placed

    windows_host.on()
    text = f"run calc.py in {path} y now"

    assert write_root_honesty._spaces_may_extend(path) is False
    write_root_honesty._rooted_candidates(text)
    assert write_root_honesty.typed_path_kind(text, text.index(path), path)
    assert placed.turn_folder(text) == ""
    assert windows_marked_file_ops._local(text, text.index(path), path) is False
    assert lookups == [], lookups
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


def _not_local(path: str, tail: str) -> str:
    return f"`{path}` is not a full path on a local drive (a share, a device path or a mapped network drive), so I don't look it up. {tail}"


def _not_written(path: str) -> str:
    return (f"`{path}` {NETWORK}. On Windows, VOOL only opens files on a local drive (like "
            "C:\\Users\\you\\file.txt) or inside the current folder, so nothing was written.")


# One row per lane: (message, the plain refusal it gets, or None for "the same reply as the share").
ROWS = [
    ("run the tests in C://attkr9/s/x", None),
    ("run the tests in C:\\\\attkr9\\s\\x", None),
    ("create a.txt in C://attkr9/s/x", _not_written("C://attkr9/s/x")),
    ("create a.txt in C:/\\attkr9/s/x", _not_written("C:/\\attkr9/s/x")),
    ("create a.txt in C:\\/attkr9\\s\\x", _not_written("C:\\/attkr9\\s\\x")),
    ("replace 'a' with 'b' in C://attkr9/s/x.txt", _not_local("C://attkr9/s/x.txt", "Nothing was changed.")),
    ("move C:\\\\attkr9\\s\\a.txt to C:\\\\attkr9\\s\\b", _not_local("C:\\\\attkr9\\s\\a.txt", "Nothing was moved.")),
    ("run calc.py in C://attkr9/s/x y", None),
]


def test_each_lane_refuses_a_doubled_separator_drive_path_before_looking_it_up(
    monkeypatch, recorder, lookups, windows_host, real_windows_seams
) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()
    share_reply = {"run the tests": chat("run the tests in //attkr9/s/x"), "run calc.py": chat("run calc.py in //attkr9/s/x y")}
    recorder.touches.clear()
    lookups.clear()

    replies, touched = _served_turns(chat, recorder, [text for text, _want in ROWS])

    assert touched == {}, touched
    assert lookups == [], lookups
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    for text, want in ROWS:
        status, reply = replies[text]
        if want is None:
            expected = share_reply["run calc.py" if text.startswith("run calc.py") else "run the tests"]
            assert (status, reply) == expected, (text, reply)
        else:
            assert status == 200 and reply == want, (text, reply)
