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
- What is not refused: build, skill and memory messages that only mention a route, a folder or a
  share (the build lane refuses only a share or device it is asked to write); a full drive path the
  input normalizer split (`C: /Users/you/x.txt`), which keeps its drive; a backslash-relative PDF
  and a forward-slash drive path in a skill import or a listing; and a folder typed as `/notes` in a
  write, which is still the workspace folder `notes`, as on base.
- A refused listing of a share is the answer: no other folder is listed in its place.
- A "reply HEARTBEAT_OK" poll that names a share is refused before its file is looked up, in the
  lane and through every chat door, while a local heartbeat file is still read.

The Windows rule is switched on only through `windows_path_policy._IS_WINDOWS` (the `windows_host`
fixture), and only after local setup, because with it on a POSIX `tmp_path` counts as
root-relative; the controls therefore use relative or `~` paths. On a Windows host the Windows write
gate is on as well and answers some writes first: a move whose paths are only in its quoted text is
refused there before the move handler reads them, and the move control and the `/notes` write mark
their folder for it. Literal sentences are asserted, never `refusal_text`, so this file runs against
base 9f88889 given only `core/windows_path_policy.py`, with no guard wired.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import runtime_paths
from core import windows_write_gate as gate
from tests._nonlocal_touch_recorder import MARKER_FORMS, NETWORK_FORMS, MarkerForm, recorder, windows_host
from tests.asgi_harness import asgi_request

STATUS = "non_local_path_refused"
NETWORK = "is a network or device path"
PARTIAL = "is not a full local path"
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
    # A share or a device: no folder at all, rather than the share re-rooted into the workspace
    # ("attkr9/s/x/repo"). A partial path is placed as base places it once it is outside the workspace,
    # by the lexical clean, without resolving it ("/attkr9/x/repo" is the workspace folder "attkr9/x/repo").
    expected: dict[str, Any] = {form.id: "" for form in NETWORK_FORMS}
    expected.update({
        "root-relative-backslash": "\\attkr9\\x\\repo",
        "root-relative-forward": "attkr9/x/repo",
        "drive-relative": "C:attkr9\\x\\repo",
    })
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
            if case == "move, quoted only" and gate._IS_WINDOWS:
                # On a Windows host the write gate answers first: a move that names no source path of its
                # own cannot be placed, so it is refused before the handler reads the quoted text.
                expected[f"{form.id} {case}"] = (False, gate.STATUS, False)
    # Control: a local move is still checked on disk and proposed for approval. Off Windows the paths
    # are relative, because the handler expands `~` into this host's POSIX home, which the Windows rule
    # reads as root-relative. On a Windows host the write gate places only a path that is absolute once
    # `~` is expanded, inside a folder marked for it, so there they are `~` paths in a marked folder.
    if gate._IS_WINDOWS:
        (Path.home() / "a.txt").write_text("a\n", encoding="utf-8")
        local_move = 'move "~\\a.txt" to "~\\archive"'
    else:
        (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
        local_move = 'move "a.txt" to "archive"'
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(gate.ENV_VAR, str(tmp_path))
        ok, status, _text = run(parse_operator_action_intent(local_move))
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


def _not_refused(reply: str) -> bool:
    """No Windows refusal of any kind: neither sentence, nor the raw status a lane might relay."""
    return NETWORK not in reply and PARTIAL not in reply and STATUS not in reply


def test_build_skill_and_memory_messages_that_only_mention_a_path_are_not_refused(monkeypatch, recorder, windows_host) -> None:
    # On base these turns are taken by the builder, the skill lane and memory, which come before the
    # write-root and PDF lanes. A route, a folder named in prose or a fact to remember is no path the
    # turn opens, so the Windows rule does not answer them first.
    chat = _served_chat(monkeypatch)
    windows_host.on()
    builds = [
        "create a flask app with a /api/users endpoint that returns json",
        "add a route /api/login to server.py",
        "make a website with pages /about and /contact",
    ]
    skill = "create a skill called backup that saves files to /backups"
    memories = [
        "remember that my tax return is at /Users/me/tax.pdf",
        "remember that I put my projects in /code",
        "remember that I save my invoices to //attkr9/s/Invoices",
        "remember that my tax return is at //attkr9/s/tax.pdf",
    ]
    # A build asked to write to a share is still answered first: the builder joins the paths a build
    # names onto the workspace and looks them up.
    builds_to_a_share = {
        "create a flask app that saves uploads to //attkr9/s/uploads": "//attkr9/s/uploads",
        "create a python app in //attkr9/s/app and build it in the workspace": "//attkr9/s/app",
    }

    replies, touched = _served_turns(chat, recorder, [*builds, skill, *memories, *builds_to_a_share])

    assert touched == {}, touched
    assert all(status == 200 and reply.strip() for status, reply in replies.values()), replies
    for text in (*builds, skill, *memories):
        assert _not_refused(replies[text][1]), (text, replies[text])
    # Each was answered by its own lane, as on base.
    assert all("I couldn't build that in the workspace" in replies[text][1] for text in builds), replies
    assert "I drafted the skill `backup`" in replies[skill][1], replies[skill]
    assert all("I’ll remember that" in replies[text][1] for text in memories), replies
    for text, typed in builds_to_a_share.items():
        assert _refused_as_typed(replies[text][1], typed), (text, replies[text])


def test_a_code_comment_in_a_write_or_build_message_is_not_a_path(monkeypatch, recorder, windows_host) -> None:
    # `//` and `/*` open a comment in a file's text. They name no host and no folder, and no lane sees
    # them as a path (the normalizer spaces them out), so the Windows rule does not answer these turns:
    # each goes to the lane that takes it on base.
    chat = _served_chat(monkeypatch)
    windows_host.on()
    messages = [
        "create main.js with: // entry point",
        "create style.css with /* reset */ body { margin: 0 }",
        "add a // TODO comment to server.js",
        "write hello to out.txt // no trailing newline",
    ]

    replies, touched = _served_turns(chat, recorder, messages)

    assert touched == {}, touched
    assert all(status == 200 and reply.strip() for status, reply in replies.values()), replies
    refused = {text: reply for text, (_status, reply) in replies.items() if not _not_refused(reply)}
    assert refused == {}, refused


def _approval_asks(tmp_path: Path, monkeypatch, recorder, windows_host, planned: dict[str, str], others: list[str]):
    """Served replies with the write gate on and the workspace marked, as on a Windows host, so a write that
    goes ahead reaches the approval ask naming where it would land."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    replies, touched = _served_turns(chat, recorder, [*planned, *others])
    assert touched == {}, touched
    asked = {text: replies[text] for text in planned}
    missed = {
        text: reply
        for text, (status, reply) in asked.items()
        if status != 200 or f"Affected: {workspace}/{planned[text]}." not in reply
    }
    assert missed == {}, missed
    return replies


def test_a_block_comment_attached_to_its_text_reaches_the_write_approval(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # A block comment opened against its text (`/*eslint-disable*/`, `a{color:red}/*x*/`) is root-relative
    # only by its leading `/`, and `*` is never part of a Windows name, so no lane can open it: the write
    # goes ahead to the approval ask for the workspace file, as on base, and is not refused as "not a full
    # local path". A question about such a comment is no write, and is not refused either.
    planned = {
        "create a.js with: /*eslint-disable*/ const x = 1;": "a.js",
        "create style.css with /*reset*/ body { margin: 0 }": "style.css",
        "create app.css with: a{color:red}/*x*/": "app.css",
        "create a.ts with: /**@type {number}*/ let n = 1": "a.ts",
    }
    question = "why does adding /*x*/ to my css break the minifier?"

    replies = _approval_asks(tmp_path, monkeypatch, recorder, windows_host, planned, [question])

    status, reply = replies[question]
    assert status == 200 and reply.strip() and _not_refused(reply), reply


def test_doc_comment_markers_in_a_write_message_reach_the_write_approval(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # `///` (Rust, C#) and `/**`, `/**/` (JSDoc, C) are, like `//` and `/*`, only separators and asterisks:
    # they name no host and no folder, so the write goes ahead to the approval ask. The two messages in
    # `others` are taken by other lanes, and are not refused either.
    planned = {
        "create lib.rs with: /// Adds two numbers": "lib.rs",
        "create a.ts with: /** Adds two numbers */ export const x = 1;": "a.ts",
        "create a.c with: /**/ int x;": "a.c",
    }
    others = ["write hello to out.txt /// banner", "create a.ts with: /** docs */ export {}"]

    replies = _approval_asks(tmp_path, monkeypatch, recorder, windows_host, planned, others)

    refused = {text: replies[text] for text in others if replies[text][0] != 200 or not _not_refused(replies[text][1])}
    assert refused == {}, refused


def test_a_share_with_a_wildcard_or_a_comment_beside_it_is_still_refused_as_typed(
    monkeypatch, recorder, windows_host
) -> None:
    # A `*` in a share, device or NT path, or a block comment typed next to one, never lets it through: the
    # write and build lanes refuse it as typed, before anything is looked up.
    chat = _served_chat(monkeypatch)
    windows_host.on()
    shares = {
        "write hello to //attkr9/s/*": "//attkr9/s/*",
        "write hello to //attkr9/s/a*b": "//attkr9/s/a*b",
        "write hello to //*/s/x.txt": "//*/s/x.txt",
        "write hello to ~///attkr9/s/*": "~///attkr9/s/*",
        "write hello to //?/UNC/attkr9/s/*": "//?/UNC/attkr9/s/*",
        "write hello to /??/UNC/attkr9/s/*": "/??/UNC/attkr9/s/*",
        "create a flask app that saves uploads to //attkr9/s/*": "//attkr9/s/*",
        "create a flask app that saves uploads to /*x*/ //attkr9/s/uploads": "//attkr9/s/uploads",
    }

    replies, touched = _served_turns(chat, recorder, list(shares))

    assert touched == {}, touched
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    wrong = {text: replies[text] for text, typed in shares.items() if not _refused_as_typed(replies[text][1], typed)}
    assert wrong == {}, wrong


def test_a_block_comment_before_a_share_does_not_hide_the_share(monkeypatch, recorder, windows_host) -> None:
    # The comment is passed over, so the answer is about the share the turn names as its destination, as
    # typed, and not about the comment.
    chat = _served_chat(monkeypatch)
    windows_host.on()
    shares = {
        "create a.js with: /*x*/ and save it to //attkr9/s/a.js": "//attkr9/s/a.js",
        "create /*x*/ //attkr9/s/y.txt": "//attkr9/s/y.txt",
    }

    replies, touched = _served_turns(chat, recorder, list(shares))

    assert touched == {}, touched
    wrong = {text: replies[text] for text, typed in shares.items() if not _refused_as_typed(replies[text][1], typed)}
    assert wrong == {}, wrong


def test_the_write_finder_passes_over_comment_markers_and_never_over_a_share(recorder, windows_host) -> None:
    from core.agent_runtime.write_root_honesty import non_local_write_target

    windows_host.on()
    comments = [
        "create a.js with: /*eslint-disable*/ const x = 1;",
        "create app.css with: a{color:red}/*x*/",
        "create a.js with: /*global $*/",
        "create a.ts with: /**@type {number}*/ let n = 1",
        "create lib.rs with: /// Adds two numbers",
        "create a.ts with: /** docs */ export {}",
        "create a.c with: /*** banner ***/",
    ]
    found = {
        "write hello to //attkr9/s/*": ("//attkr9/s/*", "unc"),
        "write hello to //attkr9/s/a*b": ("//attkr9/s/a*b", "unc"),
        "write hello to //*/s/x.txt": ("//*/s/x.txt", "unc"),
        "write hello to ///attkr9/s/*": ("///attkr9/s/*", "unc"),
        "write hello to ~///attkr9/s/*": ("~///attkr9/s/*", "unc"),
        "write hello to //?/UNC/attkr9/s/*": ("//?/UNC/attkr9/s/*", "device"),
        "write hello to //./pipe/*": ("//./pipe/*", "device"),
        "write hello to /??/UNC/attkr9/s/*": ("/??/UNC/attkr9/s/*", "nt_namespace"),
        "create a.js with: /*x*/ and save it to //attkr9/s/a.js": ("//attkr9/s/a.js", "unc"),
        # Not a comment: a folder typed without its drive is still answered as typed.
        "write hello to /tmp/*.log": ("/tmp/*.log", "root_relative"),
        "create todo.txt in /notes": ("/notes", "root_relative"),
        # A share, a device or a URL can start inside these: each is answered as typed, as on base.
        "write hello to /*x*///attkr9/s/x.txt": ("/*x*///attkr9/s/x.txt", "root_relative"),
        "write hello to /*x*/\\\\attkr9\\s\\x.txt": ("/*x*/\\\\attkr9\\s\\x.txt", "root_relative"),
        "write hello to /*x*/://?/UNC/attkr9/s/x.txt": ("/*x*/://?/UNC/attkr9/s/x.txt", "root_relative"),
        "write hello to /*x*/??/UNC/attkr9/s/x.txt": ("/*x*/??/UNC/attkr9/s/x.txt", "root_relative"),
        "write hello to /*x*/file://attkr9/s/x.txt": ("/*x*/file://attkr9/s/x.txt", "root_relative"),
    }
    # The verb reaches the comment but not the share after it: the share is answered in its place.
    far = "write hello to /*" + "x" * 70 + "*/ //attkr9/s/x.txt"

    assert {text: non_local_write_target(text) for text in comments} == dict.fromkeys(comments, ("", ""))
    assert {text: non_local_write_target(text) for text in found} == found
    assert non_local_write_target(far) == ("//attkr9/s/x.txt", "unc")
    # The build lane's network tier finds the same shares and devices.
    network = {text: path for text, path in found.items() if path[1] != "root_relative"}
    assert {text: non_local_write_target(text, network_only=True) for text in network} == network
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


def test_a_share_inside_or_after_a_block_comment_is_still_refused_as_typed(monkeypatch, recorder, windows_host) -> None:
    # A comment a share, a device or a URL starts inside (`/*x*///host/s`) is refused whole, as typed, as
    # on base; a share typed after a comment the write verb reaches, past the verb's own reach, is refused
    # as typed. Nothing names the marker.
    chat = _served_chat(monkeypatch)
    windows_host.on()
    glued = [
        "/*x*///attkr9/s/x.txt",
        "/*x*/\\\\attkr9\\s\\x.txt",
        "/*x*/://?/UNC/attkr9/s/x.txt",
        "/*x*/??/UNC/attkr9/s/x.txt",
        "/*x*/file://attkr9/s/x.txt",
    ]
    far = "write hello to /*" + "x" * 70 + "*/ //attkr9/s/x.txt"

    replies, touched = _served_turns(chat, recorder, [*(f"write hello to {path}" for path in glued), far])

    assert touched == {}, touched
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    wrong = {
        path: replies[f"write hello to {path}"]
        for path in glued
        if not (PARTIAL in replies[f"write hello to {path}"][1] and f"`{path}`" in replies[f"write hello to {path}"][1])
    }
    assert wrong == {}, wrong
    assert _refused_as_typed(replies[far][1], "//attkr9/s/x.txt"), replies[far]


def test_a_build_whose_file_is_typed_as_a_share_reads_no_file_by_that_name(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # A file a build names as a share (quoted, backticked or as a `file://` URL, which the write check
    # does not read as a destination), with or without a block comment beside it, is not the build's
    # subject: the builder neither opens the share nor joins `attkr9/s/a.js` onto the workspace and reads
    # that. A file named relative to the workspace is still read there, and only there.
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    shares = [
        'create a.js and save it to "//attkr9/s/a.js"',
        'create a.js and save it to "//attkr9/s/a.js" /*x*/',
        'create a.js /*x*/ and save it to "//attkr9/s/a.js"',
        'create a.js and save it to /*x*/ "//attkr9/s/a.js"',
        "create a.js and save it to `//attkr9/s/a.js` /*x*/",
        "create a.js and save it to file://attkr9/s/a.js",
        "create a.js and save it to file://attkr9/s/a.js /*x*/",
    ]
    local = ["create a.js and save it to 'attkr9/s/a.js' /*x*/", "create attkr9.js /*x*/"]

    replies, touched = _served_turns(chat, recorder, [*shares, *local])

    assert all(status == 200 and reply.strip() for status, reply in replies.values()), replies
    assert {text: found for text, found in touched.items() if text in shares} == {}, touched
    # The local controls may read `<workspace>/attkr9/s/a.js` and `<workspace>/attkr9.js`: nothing else.
    inside = os.path.normcase(str(workspace)) + os.sep
    outside = [
        touch for touch in recorder.touches if not os.path.normcase(os.path.normpath(touch.path)).startswith(inside)
    ]
    assert outside == [] and recorder.marker_popen == [], recorder.report()


def test_the_build_subject_is_never_a_file_the_message_typed_as_a_share(windows_host) -> None:
    # The builder reads its subject from the newest user message that names a file, joined onto the
    # workspace. On Windows a file that message typed as a share or a device path is no subject, and no
    # older message stands in for it; a file named relative to the workspace, or none at all, is read as
    # before. Off Windows the subject is the file named, as on base. Nothing here looks anything up.
    from core.agent_runtime.builder.controller import _audited_subject_paths

    def subject(*messages: str) -> tuple[str, ...]:
        history = [{"role": "user", "content": text} for text in messages]
        return _audited_subject_paths({"conversation_history": history})

    shares = [
        'create a.js and save it to "//attkr9/s/a.js"',
        "create a.js and save it to `//attkr9/s/a.js` /*x*/",
        "create a.js and save it to file://attkr9/s/a.js",
        'create a.js and save it to "file:////attkr9/s/a.js"',
        'create a.js and save it to "///attkr9/s/a.js"',
        'create a.js and save it to "//./UNC/attkr9/s/a.js"',
    ]
    windows_host.on()

    assert {text: subject("audit app.py", text) for text in shares} == dict.fromkeys(shares, ())
    assert subject("audit app.py", "create a.js and save it to 'attkr9/s/a.js'") == ("attkr9/s/a.js",)
    assert subject("audit app.py", "create attkr9.js") == ("attkr9.js",)
    assert subject("audit app.py", "prove the bug you found") == ("app.py",)
    windows_host.off()
    assert subject("audit app.py", shares[0]) == ("attkr9/s/a.js",)


def test_a_build_subject_typed_inside_a_device_or_nt_path_is_no_subject(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # The file the builder takes can start inside a device or NT path typed with mixed separators
    # (`\\?\UNC/host/s/a.js` gives `UNC/host/s/a.js`), after no separator of its own. The whole word it
    # ends is read, so that is no subject either, and a build naming one, with a block comment or the
    # workspace folder ahead of it, reads no file by that name.
    from core.agent_runtime.builder.controller import _audited_subject_paths

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    typed = os.path.splitdrive(str(workspace))[1].replace("\\", "/")
    devices = [
        'create a.js and save it to "\\\\?\\UNC/attkr9/s/a.js"',
        'create a.js and save it to "\\??\\UNC/attkr9/s/a.js"',
        'create a.js and save it to "//?/UNC/attkr9/s/a.js"',
        'create a.js and save it to "//-attkr9/s/a.js"',
    ]
    builds = [
        "create a.js /*x*/ and save it to \\\\?\\UNC/attkr9/s/x.txt",
        f"create todo.txt in {typed}/notes and save a copy to \\\\?\\UNC/attkr9/s/x.txt",
    ]

    subjects = {
        text: _audited_subject_paths({"conversation_history": [{"role": "user", "content": text}]}) for text in devices
    }
    replies, touched = _served_turns(chat, recorder, builds)

    assert subjects == dict.fromkeys(devices, ()), subjects
    assert touched == {}, touched
    assert all(status == 200 and reply.strip() for status, reply in replies.values()), replies


def test_a_full_drive_path_split_by_the_normalizer_keeps_its_drive(monkeypatch, recorder, windows_host) -> None:
    # The input normalizer turns `C:/Users/you/x.txt` into `C: /Users/you/x.txt`. This test's home
    # stands in for `C:/Users/you`: its rooted half exists, as `\\Users\\you` does on the current drive
    # on Windows, so the lanes really reach the read door with what they kept. On POSIX the drive path
    # is not one of the home folders, so the lanes then answer that; on Windows they read it.
    home = Path.home()
    (home / "Documents" / "notes.txt").write_text("hello notes\n", encoding="utf-8")
    (home / "Documents" / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    drive, rooted = os.path.splitdrive(str(home))
    typed_home = (drive or "C:") + rooted.replace("\\", "/")
    chat = _served_chat(monkeypatch)
    windows_host.on()
    reads = [f"open {typed_home}/Documents/notes.txt", f"what does {typed_home}/Documents/notes.txt say"]
    pdfs = [f"read the pdf {typed_home}/Documents/report.pdf", f"summarize {typed_home}/Documents/report.pdf"]

    replies, touched = _served_turns(chat, recorder, [*reads, *pdfs])

    assert touched == {}, touched
    assert all(status == 200 and reply.strip() for status, reply in replies.values()), replies
    for text in (*reads, *pdfs):
        assert _not_refused(replies[text][1]), (text, replies[text])
    # The read is done, or names the path as typed, drive included, and not the rooted half alone (the
    # reply clips a long path, so the name it shows is the start of the typed one).
    typed = f"{typed_home}/Documents/notes.txt"
    for text in reads:
        named = re.findall(r"`([^`]+)`", replies[text][1])
        drive_kept = bool(named) and named[0][1:3] == ":/" and typed.startswith(named[0])
        assert "hello notes" in replies[text][1] or drive_kept, (text, replies[text])


def test_relative_and_forward_slash_drive_paths_are_not_refused(monkeypatch, recorder, windows_host) -> None:
    chat = _served_chat(monkeypatch)
    windows_host.on()
    messages = [
        # The normalizer splits `docs\report.pdf` into `docs\ report.pdf`: the `\ report.pdf` it leaves
        # is debris of a relative path, not a root-relative one.
        "read the pdf docs\\report.pdf",
        "summarize docs\\report.pdf",
        # A skill package and a listing keep the drive the normalizer split off.
        "import this skill C:/Users/you/pkg.zip",
        "what files are in C:/Users/you/Desktop",
        "what is in the folder C:/Users/you/Documents/Vool testing",
    ]

    replies, touched = _served_turns(chat, recorder, messages)

    assert touched == {}, touched
    assert all(status == 200 and reply.strip() for status, reply in replies.values()), replies
    refused = {text: reply for text, (_status, reply) in replies.items() if not _not_refused(reply)}
    assert refused == {}, refused


def test_a_file_created_in_a_typed_folder_stays_in_that_workspace_folder(tmp_path: Path, monkeypatch, recorder, windows_host) -> None:
    # `/notes` names no full local path on Windows, and it is not resolved; as on base it is the
    # workspace folder `notes`, not dropped so that the file silently lands at the workspace root.
    workspace = tmp_path / "workspace"
    # As on a Windows host: the write gate is on and the chat's workspace is a marked disposable folder,
    # so the write reaches the approval ask, which names where it would land.
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    planned = {
        "in /notes create todo.txt with text: hi": f"Affected: {workspace}/notes/todo.txt.",
        "inside /docs create readme.md with text: hello": f"Affected: {workspace}/docs/readme.md.",
    }

    replies, touched = _served_turns(chat, recorder, list(planned))

    assert touched == {}, touched
    for text, affected in planned.items():
        status, reply = replies[text]
        assert status == 200 and affected in reply, (text, reply)


def test_a_typed_folder_inside_the_workspace_is_that_workspace_folder(tmp_path: Path, monkeypatch, recorder, windows_host) -> None:
    # Without its drive, the workspace's own folder (`\Users\you\ws\notes` for workspace `C:\Users\you\ws`)
    # is still the workspace folder `notes`, as base found it by resolving on the workspace's drive. Read
    # as typed, not resolved, it must not be nested under the workspace a second time.
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    chat = _served_chat(monkeypatch)
    windows_host.on()
    typed = workspace.as_posix()
    planned = {
        f"in {typed}/notes create todo.txt with text: hi": f"Affected: {workspace}/notes/todo.txt.",
        f"in {typed.upper()}/Docs create readme.md with text: hello": f"Affected: {workspace}/Docs/readme.md.",
    }

    replies, touched = _served_turns(chat, recorder, list(planned))

    assert touched == {}, touched
    for text, affected in planned.items():
        status, reply = replies[text]
        assert status == 200 and affected in reply, (text, reply)


@pytest.mark.parametrize(
    "text",
    ["//attkr9/s/docs what files are in it", "what files are in //attkr9/s/docs"],
    ids=["share-first", "share-last"],
)
def test_a_refused_listing_is_the_answer_and_no_other_folder_is_listed(text: str, monkeypatch, recorder, windows_host) -> None:
    # "docs" is also a home-folder word: a lane that took the refused listing as no answer would list
    # ~/Documents instead and present it as the share's contents.
    (Path.home() / "Documents" / "sentinel-doc.txt").write_text("x\n", encoding="utf-8")
    chat = _served_chat(monkeypatch)
    windows_host.on()

    replies, touched = _served_turns(chat, recorder, [text])

    assert touched == {}, touched
    status, reply = replies[text]
    assert status == 200 and _refused_as_typed(reply, "//attkr9/s/docs"), reply
    assert "sentinel-doc.txt" not in reply, reply


def test_the_planner_lists_a_split_drive_path_with_its_drive(windows_host) -> None:
    from core.execution.planner import plan_tool_workflow

    def planned(text: str) -> tuple[str, str, str]:
        decision = plan_tool_workflow(user_text=text, task_class="unknown", executed_steps=[], source_context={})
        payload = dict(decision.next_payload or {})
        return decision.reason, str(payload.get("intent")), str(dict(payload.get("arguments") or {}).get("path"))

    texts = ("list the files in C: /Users/you/Desktop/notes", "show me what is in C: /Users/you/Desktop/notes")
    # Off Windows the drive is not re-attached: the plan is base's, byte for byte.
    windows_host.off()
    assert {text: planned(text) for text in texts} == {
        text: ("planned_safe_machine_directory_list", "machine.list_directory", "/Users/you/Desktop/notes") for text in texts
    }
    # On Windows the listing keeps the drive the normalizer split off; without it the list tool refuses
    # `/Users/you/Desktop/notes` as no full local path.
    windows_host.on()
    assert {text: planned(text) for text in texts} == {
        text: ("planned_safe_machine_directory_list", "machine.list_directory", "C:/Users/you/Desktop/notes") for text in texts
    }


def test_a_drive_split_pdf_in_a_folder_named_scan_is_read_from_its_text(windows_host) -> None:
    # The OCR words are read with the file's name taken out, as it stands in the text. A drive the
    # normalizer split off is not in that spelling, so taking out the drive-joined name would leave
    # `old scan` behind and send a text PDF to OCR.
    from core.agent_runtime.fast_paths_pdf import pdf_read_request
    from core.input_normalizer import normalize_user_text

    home = Path.home()
    (home / "Documents" / "old scan").mkdir(parents=True, exist_ok=True)
    (home / "Documents" / "old scan" / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    drive, rooted = os.path.splitdrive(str(home))
    typed_home = (drive or "C:") + rooted.replace("\\", "/")
    windows_host.on()

    text = normalize_user_text(f"read the pdf {typed_home}/Documents/old scan/report.pdf").normalized_text
    request = pdf_read_request(str(text))

    assert request is not None and request["named"] == f"{typed_home}/Documents/old scan/report.pdf", (text, request)
    assert request["spelled"] != request["named"] and request["wants_ocr"] is False, (text, request)


def test_the_drive_check_reads_only_the_few_characters_before_each_path(monkeypatch, windows_host) -> None:
    # A drive the normalizer split off sits at most `C: ` before its path, so only that much is read.
    # Copying and rescanning the whole message up to each path made a pasted message of many rooted
    # tokens take minutes.
    from core.agent_runtime.write_root_honesty import non_local_write_target
    from core.execution import constants

    real = constants._DRIVE_LEFT_BEHIND_RE
    spans: list[tuple[int, int]] = []

    class _Spy:
        def search(self, string: str, pos: int = 0, endpos: int | None = None):
            end = len(string) if endpos is None else min(endpos, len(string))
            spans.append((pos, end))
            return real.search(string, pos, end)

    monkeypatch.setattr(constants, "_DRIVE_LEFT_BEHIND_RE", _Spy())
    windows_host.on()

    assert non_local_write_target(" ".join(f"/d{n}" for n in range(2000))) == ("", "")
    assert len(spans) >= 2000 and max(end - pos for pos, end in spans) <= 4, (len(spans), max(spans, key=lambda s: s[1] - s[0]))


# The heartbeat poll lane answers an idle "reply HEARTBEAT_OK" poll from the file it names, and it runs
# twice on every turn, before every other lane: once in the closed-contract preflight and once at the
# top of the front door. It reads the raw message, so the normalizer never splits the share, and a URL
# or a `C://` path is read from its first `/`.
HEARTBEAT_SHARE_POLLS = [
    "Read //attkr9/s/HEARTBEAT.md if it exists. If nothing needs attention, reply HEARTBEAT_OK.",
    "Read /\\attkr9\\s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
    'Read "//attkr9/s/HEARTBEAT.md". If nothing needs attention, reply HEARTBEAT_OK.',
    "Read //?/UNC/attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
    "Read file://attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
    "Check https://attkr9.example/s/HEARTBEAT.md and reply HEARTBEAT_OK if nothing needs attention.",
    "Read C://attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
    # POSIX reads this as `/attkr9/s/...`; Windows as the share. The reply names it as typed.
    "Read ///attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
]


def test_a_heartbeat_poll_naming_a_share_is_refused_before_it_is_looked_up(monkeypatch, recorder, windows_host) -> None:
    # A "check https://..." poll is answered 500 on every host, before this change and after it: the
    # URL grounding check refuses any reply about a URL that was not fetched. The lane test below
    # covers that spelling.
    polls = [text for text in HEARTBEAT_SHARE_POLLS if "https://" not in text]
    chat = _served_chat(monkeypatch)
    windows_host.on()

    replies, touched = _served_turns(chat, recorder, polls)

    assert touched == {}, "\n".join(f"{text!r}:\n  " + "\n  ".join(found) for text, found in touched.items())
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    # The poll is told the share was not opened, not HEARTBEAT_OK as if the file were checked and empty.
    unrefused = {text: reply for text, reply in replies.items() if reply[0] != 200 or NETWORK not in reply[1]}
    assert unrefused == {}, unrefused
    tripled = replies[HEARTBEAT_SHARE_POLLS[-1]][1]
    assert f"`///attkr9/s/HEARTBEAT.md` {NETWORK}" in tripled, tripled


@pytest.mark.parametrize("text", HEARTBEAT_SHARE_POLLS)
def test_the_heartbeat_lane_refuses_a_share_without_looking_it_up(text: str, recorder, windows_host) -> None:
    from core.agent_runtime.fast_paths_utility import heartbeat_poll_fast_path

    windows_host.on()

    reply = heartbeat_poll_fast_path(text, source_context={})

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert reply is not None and NETWORK in reply, reply


@pytest.mark.parametrize("where", ["workspace", "home"])
def test_a_heartbeat_poll_whose_file_would_open_on_a_share_is_refused(where: str, monkeypatch, recorder, windows_host) -> None:
    # The typed text names no share: the file it names would open on one through the chat's workspace
    # or the home folder, so the path about to be looked up is judged too.
    from core.agent_runtime.fast_paths_utility import heartbeat_poll_fast_path

    share = "//attkr9/s/beat"
    if where == "home":
        monkeypatch.setenv("HOME", share)
        monkeypatch.setenv("USERPROFILE", share)
        text, context = "Read ~/HEARTBEAT.md if it exists. If nothing needs attention, reply HEARTBEAT_OK.", {}
    else:
        text, context = "Read HEARTBEAT.md if it exists. If nothing needs attention, reply HEARTBEAT_OK.", {"workspace": share}
    windows_host.on()

    reply = heartbeat_poll_fast_path(text, source_context=context)

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert reply is not None and NETWORK in reply, reply


@pytest.mark.parametrize("door", ["/api/generate", "/api/null", "/v1/chat/completions", "discord"])
def test_a_heartbeat_poll_naming_a_share_touches_nothing_through_any_chat_door(door: str, monkeypatch, recorder, windows_host) -> None:
    # The same turn reaches the lane through the other served chat routes and through the channel
    # gateway, where any author in a channel the Discord bridge watches can send it.
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from core.channel_gateway import ChannelRequest, process_channel_request
    from core.web.api.runtime import RuntimeServices

    text = HEARTBEAT_SHARE_POLLS[0]
    agent = VoolAgent(backend_name="test-backend", device="nonlocal-test", persona_id="default")
    monkeypatch.setattr(vool_api_server, "_agent", agent)
    app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))
    bodies = {
        "/api/generate": {"prompt": text, "stream": False},
        "/api/null": {"uri": "null://svc/x", "prompt": text},
        "/v1/chat/completions": {"messages": [{"role": "user", "content": text}], "stream": False},
    }
    windows_host.on()

    if door == "discord":
        answer = process_channel_request(
            agent,
            ChannelRequest(
                platform="discord", user_id="stranger-1", channel_id="c1", text=text,
                device_hint="channel", surface="discord_bot",
            ),
        ).response_text
    else:
        status, _headers, raw = asgi_request(
            app, method="POST", path=door, headers=JSON_HEADERS, body=json.dumps(bodies[door]).encode("utf-8")
        )
        answer = raw.decode("utf-8", "replace")
        assert status == 200, (status, answer)

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert NETWORK in answer, answer


def test_a_heartbeat_poll_of_a_local_file_still_reads_it(tmp_path: Path, windows_host) -> None:
    # Only a share or a device is refused here. A `~` path and a rooted local path are still read, as
    # on base: a file with an open item makes the poll stand aside for the turn, and one with only
    # headings is answered HEARTBEAT_OK, so each pair shows the file itself was read.
    from core.agent_runtime.fast_paths_utility import heartbeat_poll_fast_path

    home = tmp_path / "user"
    for name, content in (("busy", "# Heartbeat\n- rotate the logs\n"), ("quiet", "# Heartbeat\n")):
        (home / name).mkdir()
        (home / name / "HEARTBEAT.md").write_text(content, encoding="utf-8")
    # The home folder as a rooted path without its drive (the lane reads a path from its first `/`);
    # the working directory is this test's folder, so on Windows it lands on the same drive.
    rooted = os.path.splitdrive(str(home))[1].replace("\\", "/")

    def poll(folder: str) -> str:
        return f"Read {folder}/HEARTBEAT.md if it exists. If nothing needs attention, reply HEARTBEAT_OK."

    windows_host.on()

    assert heartbeat_poll_fast_path(poll("~/quiet"), source_context={}) == "HEARTBEAT_OK"
    assert heartbeat_poll_fast_path(poll("~/busy"), source_context={}) is None
    assert heartbeat_poll_fast_path(poll(f"{rooted}/quiet"), source_context={}) == "HEARTBEAT_OK"
    assert heartbeat_poll_fast_path(poll(f"{rooted}/busy"), source_context={}) is None
