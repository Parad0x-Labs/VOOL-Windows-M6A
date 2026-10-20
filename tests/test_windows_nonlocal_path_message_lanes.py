"""On Windows, a chat message that names a share or a partial path is answered without anything reaching for it.

The deterministic message lanes read paths straight out of the user's words: the write-root honesty
check, the safe machine-write guard, the PDF fast path, the read/list path probes, the planner's
folder cleaner, the skill package precheck and the operator move/disk-usage lane. On base each of
them resolves, stats or opens what it found before deciding anything. On Windows a stat of
`\\\\host\\share` is an SMB connection (and an NTLM hash sent to whoever answers), so "refused, but
only after a lookup" is still the bug. `tests/_nonlocal_touch_recorder.py` records (and refuses)
every filesystem call or process start that names the marker host `attkr9`.

What this file pins:

- A served `/api/chat` turn, one per lane a message reaches first, touches nothing that names the
  marker and starts no process naming it.
- Each lane, called directly with every marker form, refuses (or stands aside, when the form is not
  a path in its own syntax) without a lookup, and still probes a local relative path.
- What the user is told: the PDF, move and skill messages carry the refusal sentence about the path
  exactly as the user typed it, bare or quoted, even where the input normalizer split it on the way
  to the lane; the write message says nothing was written (the refusal sentence, or the write lane's
  existing "I can't write to ... Nothing was written" reply), naming the path as typed; a quoted
  share in a write is never named any other way; and a full local drive path written with forward
  slashes is not refused, in a read or in a write.

The Windows rule is switched on only through `windows_path_policy._IS_WINDOWS` (the `windows_host`
fixture), and only after local setup, because with it on a POSIX `tmp_path` counts as
root-relative; the controls therefore use relative or `~` paths. Literal sentences are asserted,
never `refusal_text`, so this file runs unchanged against a base that has no such module.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import runtime_paths
from tests._nonlocal_touch_recorder import MARKER_FORMS, NETWORK_FORMS, MarkerForm, recorder, windows_host
from tests.asgi_harness import asgi_request

STATUS = "non_local_path_refused"
NETWORK = "is a network or device path"
# What the page's own fetches send: JSON from the page's loopback origin.
JSON_HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    # The machine lanes look under the user's home folders; give them one of this test's own. `~` is
    # HOME on POSIX and USERPROFILE on Windows, so a lane that writes there never reaches the real one.
    home = tmp_path / "user"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    runtime_paths.configure_runtime_home(tmp_path / "vool")
    # Anything a lane resolves relative to the working directory lands in this test's folder.
    monkeypatch.chdir(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


def _on(form: MarkerForm, name: str) -> str:
    """`name` inside the form's folder, in the form's own separator, so the kind does not change."""
    return form.value + ("\\" if "\\" in form.value else "/") + name


def _slash_typed(form: MarkerForm) -> bool:
    """Whether the message lanes' own path syntax sees this form at all.

    Those lanes find a path by its leading `/` (or `~`); a spelling without one is not a path to
    them and is left to the tool doors, which refuse it there.
    """
    return form.value.startswith("/")


def _served_chat(monkeypatch) -> Any:
    """`/api/chat` on the real served application, with a real test-backend agent behind it."""
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from core.web.api.runtime import RuntimeServices

    agent = VoolAgent(backend_name="test-backend", device="nonlocal-test", persona_id="default")
    # The served app hands each turn to the agent the server booted with.
    monkeypatch.setattr(vool_api_server, "_agent", agent)
    app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))

    def chat(text: str) -> tuple[int, str]:
        body = {
            "messages": [{"role": "user", "content": text}],
            "session_id": f"openclaw:{uuid.uuid4().hex[:20]}",
            "stream": False,
        }
        status, _headers, raw = asgi_request(
            app, method="POST", path="/api/chat", headers=JSON_HEADERS, body=json.dumps(body).encode("utf-8")
        )
        payload = json.loads(raw.decode("utf-8") or "{}")
        return status, str((payload.get("message") or {}).get("content") or "")

    return chat


def _served_turns(chat, recorder, messages: list[str]) -> tuple[dict[str, tuple[int, str]], dict[str, list[str]]]:
    """Each message's (status, reply), and what each one touched or started that names the marker."""
    replies: dict[str, tuple[int, str]] = {}
    touched: dict[str, list[str]] = {}
    for text in messages:
        before_touches, before_spawns = len(recorder.touches), len(recorder.marker_popen)
        replies[text] = chat(text)
        found = [str(touch) for touch in recorder.touches[before_touches:]]
        found += [str(spawn) for spawn in recorder.marker_popen[before_spawns:]]
        if found:
            touched[text] = found
    return replies, touched


# One served message per lane a message reaches first on base (the choke-first probe's first-touch
# sites), plus the backslash and drive-relative spellings of lanes that receive them whole.
#
# Chat text passes `core.input_normalizer` before most lanes see it, and it splits an unquoted path
# at its punctuation: "//attkr9/s/x.txt" reaches those lanes as "/ /attkr9/s/x.txt", and
# "C:attkr9/report.pdf" as "C: attkr9/report.pdf", so no lane ever receives an unquoted drive-relative
# path. A quoted path survives whole, so the PDF and write messages are sent both ways; a quoted share
# in a write also reaches the named-file builder and the literal-write demand, which look a target up
# before deciding it is outside the workspace. And once the doors refuse a listing, the find-folder
# phrase probe looks the whole sentence up as one path: "what is in the folder //attkr9/s/docs" is
# then a local name under the working directory, while a sentence that starts with the share points
# that lookup at the host.
FIRST_TOUCH_MESSAGES = [
    "read the pdf //attkr9/s/report.pdf",  # write-root honesty, the PDF lane and its display label
    'read the pdf "//attkr9/s/report.pdf"',  # the PDF lane, with the share whole
    'move "//attkr9/s/a.txt" to "//attkr9/s/b"',  # safe machine-write token scan, operator move check
    "import this skill //attkr9/s/x.zip",  # skill package precheck, then the permission helpers
    "write hello to //attkr9/s/x.txt",  # write-root honesty
    'write hello to "//attkr9/s/x.txt"',  # the named-file builder's target lookup
    'create "//attkr9/s/notes.md" with hello',  # the literal-write demand's and the builder's lookups
    "show me //attkr9/s/x.txt",  # read-path probes in execution/constants
    "//attkr9/s/docs what files are in it",  # listing probes, then the find-folder phrase probe
    "run the tests in //attkr9/s/repo",  # the planner's folder cleaner
    'how much disk space does "//attkr9/s" use',  # the safe machine-write guard's token scan
    "read the pdf \\\\attkr9\\s\\report.pdf",
    'move "\\\\attkr9\\s\\a.txt" to "\\\\attkr9\\s\\b"',
    'move "C:attkr9\\a.txt" to "C:attkr9\\b"',
]


def test_chat_messages_naming_a_share_touch_nothing(monkeypatch, recorder, windows_host) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()

    replies, touched = _served_turns(chat, recorder, FIRST_TOUCH_MESSAGES)

    assert touched == {}, "\n".join(f"{text!r}:\n  " + "\n  ".join(found) for text, found in touched.items())
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    # Every turn really ran and answered, so the empty record above is about a served turn.
    unanswered = {text: reply for text, reply in replies.items() if reply[0] != 200 or not reply[1].strip()}
    assert unanswered == {}, replies


class _ReplyRecorder:
    """Only what a fast path touches on the agent: the reply it builds. Nothing here can fabricate one."""

    def _fast_path_result(self, *, response: str, reason: str, **_: object) -> dict[str, object]:
        return {"response": response, "reason": reason}


def _write_root_honesty_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.agent_runtime.write_root_honesty import unreachable_write_target

    context = {"workspace": str(tmp_path / "workspace")}
    observed = {
        form.id: unreachable_write_target(f"write hello to {_on(form, 'x.txt')}", source_context=context)
        for form in MARKER_FORMS
    }
    # The lane names every path of its own syntax as unreachable; the others are not paths to it.
    expected = {form.id: _on(form, "x.txt") if _slash_typed(form) else "" for form in MARKER_FORMS}
    # Control: a local `~` path under a safe home folder is still resolved and found reachable.
    observed["control"] = unreachable_write_target("write hello to ~/Documents/notes.txt", source_context=context)
    expected["control"] = ""
    return observed, expected


def _safe_machine_write_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.agent_runtime.fast_paths_machine import safe_machine_write_targets_workspace

    local_workspace = {"workspace": str(Path.home() / "Documents")}
    observed: dict[str, Any] = {
        form.id: safe_machine_write_targets_workspace(
            user_input=f"write hello to {_on(form, 'x.txt')}", source_context=local_workspace
        )
        for form in MARKER_FORMS
    }
    expected: dict[str, Any] = {form.id: False for form in MARKER_FORMS}
    # A workspace on a share is never resolved either (the network tier: it is server-held state).
    for form in NETWORK_FORMS:
        observed[f"workspace {form.id}"] = safe_machine_write_targets_workspace(
            user_input="write hello to ~/Documents/x.txt", source_context={"workspace": form.value}
        )
        expected[f"workspace {form.id}"] = False
    # Control: a local `~` path inside a local workspace still resolves into it.
    observed["control"] = safe_machine_write_targets_workspace(
        user_input="write hello to ~/Documents/x.txt", source_context=local_workspace
    )
    expected["control"] = True
    return observed, expected


def _pdf_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.agent_runtime.fast_paths_pdf import maybe_handle_pdf_read

    def reply(text: str) -> str:
        result = maybe_handle_pdf_read(_ReplyRecorder(), text, session_id="lane-pdf", source_context={})
        return str((result or {}).get("response") or "")

    observed: dict[str, Any] = {}
    expected: dict[str, Any] = {}
    for form in MARKER_FORMS:
        named = _on(form, "report.pdf")
        text = reply(f"read the pdf {named}")
        # The reply names the path the user typed and says why it was not opened.
        observed[form.id] = (form.sentence in text, f"`{named}`" in text)
        expected[form.id] = (True, True)
    # Control: a local `~` path is still looked up, and a missing file keeps its own words.
    observed["control"] = reply("read the pdf ~/Documents/missing.pdf")
    expected["control"] = (
        "There is no file at `~/Documents/missing.pdf`. Check the name and the folder — I looked for "
        "exactly that path and nothing is there."
    )
    return observed, expected


def _read_and_list_probe_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.execution.constants import _typed_directory_content_ask, explicit_path_in, machine_file_read_path

    observed: dict[str, Any] = {}
    expected: dict[str, Any] = {}
    for form in MARKER_FORMS:
        folder = _on(form, "docs")
        observed[form.id] = (
            # The trailing words make the lane try to grow the path over them, one lookup per word.
            explicit_path_in(f"what is in the folder {folder} for me today"),
            _typed_directory_content_ask(f"what is in the folder {folder}"),
            machine_file_read_path(f"show me {_on(form, 'x.txt')}"),
        )
        # The typed path is still handed on (the list/read tool's own door refuses it with the
        # sentence); no lookup decides that it is a folder or a file.
        expected[form.id] = (folder if _slash_typed(form) else "", None, None)
    # Control: a local relative folder with a space in its name is still found on disk, and a
    # local relative file is still read.
    (tmp_path / "my docs").mkdir()
    (tmp_path / "my docs" / "notes.txt").write_text("hello\n", encoding="utf-8")
    observed["control"] = (
        explicit_path_in("what is in the folder ./my docs for me"),
        _typed_directory_content_ask("what is in the folder ./my docs"),
        machine_file_read_path("show me ./my docs/notes.txt"),
    )
    expected["control"] = ("./my docs", "./my docs", "./my docs/notes.txt")
    return observed, expected


def _planner_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.execution.planner import _clean_workspace_directory_path

    workspace = str(tmp_path / "workspace")
    observed: dict[str, Any] = {
        form.id: _clean_workspace_directory_path(_on(form, "repo"), workspace_root=workspace) for form in MARKER_FORMS
    }
    # No folder at all, rather than the share re-rooted into the workspace ("attkr9/s/x/repo").
    expected: dict[str, Any] = {form.id: "" for form in MARKER_FORMS}
    # Control: a local relative folder is still cleaned into a workspace folder.
    observed["control"] = _clean_workspace_directory_path("docs/site", workspace_root=workspace)
    expected["control"] = "docs/site"
    return observed, expected


def _skill_precheck_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.agent_runtime.fast_paths_skill import _precheck_package

    observed: dict[str, Any] = {}
    expected: dict[str, Any] = {}
    for form in MARKER_FORMS:
        package = _on(form, "pkg.zip")
        reason = _precheck_package("lane-holder", {"path": package, "label": package})
        observed[form.id] = (form.sentence in reason, f"`{package}`" in reason)
        expected[form.id] = (True, True)
    # Control: a local package is still read and checked; this one is not a zip at all.
    (Path.home() / "Downloads" / "broken.zip").write_bytes(b"not a zip")
    reason = _precheck_package("lane-holder", {"path": "~/Downloads/broken.zip", "label": "broken.zip"})
    observed["control"] = (bool(reason), NETWORK in reason or "is not a full local path" in reason)
    expected["control"] = (True, False)
    return observed, expected


def _operator_lane(tmp_path: Path) -> tuple[dict, dict]:
    from core.local_operator_actions import dispatch_operator_action, parse_operator_action_intent
    from core.operator.models import OperatorActionIntent

    def run(intent: OperatorActionIntent) -> tuple[bool, str, str]:
        result = dispatch_operator_action(intent, task_id="lane-task", session_id="lane-operator")
        return result.ok, result.status, result.response_text

    observed: dict[str, Any] = {}
    expected: dict[str, Any] = {}
    for form in MARKER_FORMS:
        move = f'move "{_on(form, "a.txt")}" to "{_on(form, "b")}"'
        cases = {
            # What the operator parser makes of the message: the paths are the intent's own.
            "move": parse_operator_action_intent(move),
            "disk usage": parse_operator_action_intent(f'how much disk space does "{form.value}" use'),
            # The paths only in the quoted text, which the move handler parses itself.
            "move, quoted only": OperatorActionIntent(kind="move_path", raw_text=move),
        }
        for case, intent in cases.items():
            assert intent is not None, (form.id, case)
            ok, status, text = run(intent)
            observed[f"{form.id} {case}"] = (ok, status, form.sentence in text)
            expected[f"{form.id} {case}"] = (False, STATUS, True)
    # Control: a local move is still checked on disk and proposed for approval. Relative, because the
    # handler expands `~` into this host's POSIX home, which the Windows rule reads as root-relative.
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    ok, status, _text = run(parse_operator_action_intent('move "a.txt" to "archive"'))
    observed["control"] = (ok, status)
    expected["control"] = (True, "approval_required")
    return observed, expected


LANES = {
    "write_root_honesty": _write_root_honesty_lane,
    "safe_machine_write": _safe_machine_write_lane,
    "pdf_read": _pdf_lane,
    "read_and_list_probes": _read_and_list_probe_lane,
    "planner_folder": _planner_lane,
    "skill_precheck": _skill_precheck_lane,
    "operator_move_and_disk_usage": _operator_lane,
}


@pytest.mark.parametrize("lane", list(LANES))
def test_each_message_lane_refuses_every_form_lexically(tmp_path: Path, lane: str, recorder, windows_host) -> None:
    windows_host.on()

    observed, expected = LANES[lane](tmp_path)

    assert recorder.touches == [] and recorder.marker_popen == [], f"{observed}\n{recorder.report()}"
    assert observed == expected


def _named_as(reply: str) -> list[str]:
    """Each word of the reply that names the marker host, without the quotes or punctuation around it."""
    return [word.strip(".,;:!?()") for word in re.findall(r"[^\s`'\"]*attkr9[^\s`'\"]*", reply, re.IGNORECASE)]


def _refused_as_typed(reply: str, *typed: str) -> bool:
    """The refusal sentence about the first path exactly as the user typed it, naming the share no other way."""
    return NETWORK in reply and f"`{typed[0]}`" in reply and set(_named_as(reply)) <= set(typed)


def _wrote_nothing_as_typed(reply: str, typed: str) -> bool:
    """A write's answer: refused, or nothing written, about the path as the user typed it and no other."""
    honest = f"I can't write to `{typed}`" in reply and "Nothing was written" in reply
    return (NETWORK in reply or honest) and f"`{typed}`" in reply and set(_named_as(reply)) <= {typed}


def test_pdf_move_skill_and_write_messages_say_what_was_refused(monkeypatch, recorder, windows_host) -> None:
    # The recorder still answers any lookup of the host as a missing share would; counting those
    # lookups is the first test's job, what the user is told is this one's.
    chat = _served_chat(monkeypatch)
    windows_host.on()

    replies, _touched = _served_turns(
        chat,
        recorder,
        [
            "read the pdf //attkr9/s/report.pdf",
            'read the pdf "//attkr9/s/report.pdf"',
            'move "//attkr9/s/a" to "//attkr9/s/b"',
            # The skill lane's own import phrasing: "install the skill at <zip>" is not routed to
            # that lane at all, on base or after the fix, so it cannot show what the lane says.
            "import this skill //attkr9/s/pkg.zip",
            "write hello to //attkr9/s/x.txt",
            'write hello to "//attkr9/s/x.txt"',
            # Controls: a full local drive path written with forward slashes is not refused.
            "read the pdf C:/Users/you/report.pdf",
            "write hello to C:/Users/you/x.txt",
        ],
    )

    assert all(status == 200 for status, _reply in replies.values()), replies
    pdf, quoted_pdf, move, skill, write, quoted_write, local_pdf, local_write = (
        reply for _status, reply in replies.values()
    )
    # The refusal sentence about the path exactly as the user typed it, even where the input normalizer
    # split it on the way to the lane ("/ /attkr9/s/report.pdf"), instead of "outside those folders", a
    # move proposal, or an approval ask for a package read.
    assert _refused_as_typed(pdf, "//attkr9/s/report.pdf"), pdf
    assert _refused_as_typed(quoted_pdf, "//attkr9/s/report.pdf"), quoted_pdf
    assert _refused_as_typed(move, "//attkr9/s/a", "//attkr9/s/b"), move
    assert _refused_as_typed(skill, "//attkr9/s/pkg.zip"), skill
    # The write says nothing was written: the refusal sentence, or the write lane's own honest answer,
    # about the path the user typed rather than the normalizer's split of it.
    assert _wrote_nothing_as_typed(write, "//attkr9/s/x.txt"), write
    # No lane answers a quoted share in a write on base: the turn falls through to the model. Whatever
    # the turn ends with, it names the share only in one of those answers, and only as typed.
    assert _named_as(quoted_write) == [] or _wrote_nothing_as_typed(quoted_write, "//attkr9/s/x.txt"), quoted_write
    # The controls are answered as on base, by the PDF lane and the write lane in their own words (on
    # this host, about a path outside the folders they serve), never with the Windows refusal.
    for local in (local_pdf, local_write):
        assert NETWORK not in local and "is not a full local path" not in local, local
    assert "I can't write to `" in local_write and "Nothing was written" in local_write, local_write
