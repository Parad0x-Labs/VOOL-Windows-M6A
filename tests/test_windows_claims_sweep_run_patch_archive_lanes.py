"""Windows claims sweep: python -c, python -m compileall, a pasted patch and a 7z read reach their real lanes.

Claims sweep on Steve (build 170f6572, a chat bound to a marked folder on Windows):

1. `in <marked folder> run: python -c "print(2+2)"`: the model answered "Let me run that... Output: `4`" with
   no tool call and no approval card. The run-request reader (`core.execution.planner._explicit_command_request`)
   needs "run " right before the command and reads it a word of `[A-Za-z0-9_./:-]` at a time, so
   `run python -c "print(2+2)"` was planned as `python -c` (refused as "'-c' with nothing after it"), and
   "run:", a folder before the run, or a bare command line planned nothing. Those turns were also kept in
   the tools-less chat lane (`explicit_runtime_workflow_request`) and refused at the tool gate
   (`should_attempt_tool_intent`), so the planner was never asked.
2. `python -m compileall .`: made-up "Compiling..." output, no tool, for the same reasons. The front door's
   reader also dropped the ` .` as the sentence's full stop.
3. A unified diff pasted under "apply this patch": "patched", file unchanged. No lane planned
   `workspace.apply_unified_diff` from a chat turn, and the multi-part splitter carved the diff's lines
   into separate requests.
4. A read of a .7z file: "open the 7z archive \"data.7z\"" was read by the operator lane as a move into
   VOOL's own `archive_outbox` (its move words include the noun "archive"); "show me the contents of
   <folder>\\data.7z" got the machine read lane's list of folders (Desktop, Downloads, Documents). VOOL reads
   a 7z only with a confined decoder, and Windows has no decoder confinement.

Each case is driven through the served chat as the VOOL window sends it (/api/chat, the card,
`resolve_approval`, the same turn resent with its token) and the real executor. The python job runs for
real through the job runner's phase-1 argv (`JobRunner._windows_phase1_argv`, the allowlist the python file
lane uses), as a plain child process in place of the VoolConfine launch; the patch runs through the real
patch tool on a real file. The model is never needed: asked, it answers "Done." with no tool call, and every
case asserts it was not asked. Windows is simulated by the write gate's `_IS_WINDOWS` and the job runner's
`_is_windows_platform`, as in tests/test_windows_run_in_folder_served_end_to_end.py, whose fixtures this
file uses.
"""
from __future__ import annotations

import os
import uuid

import pytest

from core import windows_write_gate as gate
from tests.test_windows_run_in_folder_served_end_to_end import CALC, chat, jobs, machine

_SEVEN_Z = b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 40


def _turn() -> str:
    return f"turn-{uuid.uuid4().hex[:10]}"


# --- 1 and 2: python -c and python -m compileall run through the python lane --------------------------------

#: Each ask (in a chat bound to the folder `where` names), the argv the job runner is handed, and what the
#: reply must show from the job's own output.
RUNS = {
    "folder-first-run-colon-c": ('in {hw} run: python -c "print(2+2)"', "hw", ["python", "-c", "print(2+2)"], "4"),
    "run-c": ('run python -c "print(2+2)"', "proj", ["python", "-c", "print(2+2)"], "4"),
    "run-colon-c": ('run: python -c "print(2+2)"', "proj", ["python", "-c", "print(2+2)"], "4"),
    "bare-c": ('python -c "print(2+2)"', "proj", ["python", "-c", "print(2+2)"], "4"),
    "bare-compileall": ("python -m compileall .", "proj", ["python", "-m", "compileall", "."], "Compiling"),
    "run-compileall": ("run python -m compileall .", "proj", ["python", "-m", "compileall", "."], "Compiling"),
    # The control: the python file lane that already worked.
    "control-run-file": ("run python calc.py", "proj", ["python", "calc.py"], "calc ok 5 20"),
}


@pytest.mark.parametrize("case", list(RUNS))
def test_a_python_run_reaches_the_python_lane_and_answers_with_its_output(machine, chat, jobs, case) -> None:
    ask, where, argv, shown = RUNS[case]
    folder = machine[where]
    talk = chat(folder)

    card, reply = talk.ask_allow_resume(ask.format(hw=machine["hw"]))

    assert card["intent"] == "sandbox.run_command", card
    assert talk.model_rounds == 0, reply
    assert len(jobs) == 1, (jobs, reply)
    assert jobs[0]["argv"] == argv, jobs
    assert os.path.samefile(jobs[0]["cwd"], folder), jobs
    assert jobs[0]["returncode"] == 0, (jobs, reply)
    assert "ran and exited 0." in reply and shown in reply, reply
    if "compileall" in argv:
        assert list((folder / "__pycache__").glob("calc.*.pyc")), "compileall wrote no bytecode"


def test_a_folder_first_run_in_an_unmarked_folder_is_refused_before_any_card(machine, chat, jobs) -> None:
    other = machine["home"] / "Other folder"
    other.mkdir()
    talk = chat(machine["proj"])

    reply = talk.say(f'in {other} run: python -c "print(2+2)"', turn_id=_turn())

    assert talk.pending() == [] and jobs == [] and talk.model_rounds == 0, reply
    assert "Nothing was run" in reply and str(other) in reply, reply


@pytest.mark.parametrize(
    "ask",
    ["python -m pip install requests", "run python -m http.server", 'python -i -c "print(1)"'],
)
def test_a_python_command_line_the_allowlist_refuses_is_still_refused_before_any_card(machine, chat, jobs, ask) -> None:
    """The python lane's argv allowlist is not widened: what it refuses is answered with its own sentence,
    before any card and without the model."""
    talk = chat(machine["proj"])

    reply = talk.say(ask, turn_id=_turn())

    assert talk.pending() == [] and jobs == [] and talk.model_rounds == 0, reply
    assert "It was not run." in reply or "Nothing was run." in reply, reply


# --- 3: a pasted unified diff is applied by the patch tool -------------------------------------------------

DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return b + a\n"
PATCHED = CALC.replace("return a + b", "return b + a")
PATCH_ASKS = {
    "fenced": "Apply this patch:\n```diff\n" + DIFF + "```",
    "unfenced": "apply this patch to calc.py\n" + DIFF,
    "folder-first": "In {proj}, apply this unified diff:\n```diff\n" + DIFF + "```",
    "crlf": ("apply the following patch:\n" + DIFF).replace("\n", "\r\n"),
}


@pytest.mark.parametrize("form", list(PATCH_ASKS))
def test_a_pasted_patch_is_applied_through_the_patch_tool(machine, chat, jobs, form) -> None:
    talk = chat(machine["proj"])
    target = machine["proj"] / "calc.py"

    card, reply = talk.ask_allow_resume(PATCH_ASKS[form].replace("{proj}", str(machine["proj"])))

    assert card["intent"] == "workspace.apply_unified_diff", card
    assert talk.model_rounds == 0, reply
    assert target.read_text(encoding="utf-8") == PATCHED, reply
    assert jobs == []


def test_a_patch_that_does_not_match_the_file_changes_nothing_and_says_so(machine, chat) -> None:
    talk = chat(machine["proj"])
    target = machine["proj"] / "calc.py"
    stale = DIFF.replace("-    return a + b", "-    return a - b")

    _card, reply = talk.ask_allow_resume("apply this patch\n" + stale)

    assert talk.model_rounds == 0, reply
    assert target.read_text(encoding="utf-8") == CALC
    assert "patched" not in reply.lower(), reply


def test_a_patch_in_a_chat_bound_to_an_unmarked_folder_is_refused_before_any_card(machine, chat) -> None:
    other = machine["home"] / "Other folder"
    other.mkdir()
    (other / "calc.py").write_text(CALC, encoding="utf-8")
    talk = chat(other)

    reply = talk.say("apply this patch:\n" + DIFF, turn_id=_turn())

    assert talk.pending() == [] and talk.model_rounds == 0, reply
    assert "not inside a folder you marked" in reply and "Nothing was changed." in reply, reply
    assert (other / "calc.py").read_text(encoding="utf-8") == CALC


def test_a_pasted_diff_with_no_request_is_not_applied(machine, chat) -> None:
    """Only a message that asks for the patch on its first line is read as one (in Auto mode it would run
    with no card)."""
    from core import windows_marked_file_ops as marked

    context = {"_owner_local": True, "workspace": str(machine["proj"])}
    assert marked.marked_file_ask(DIFF, source_context=context) is None
    assert marked.marked_file_ask("here is the diff I made yesterday:\n" + DIFF, source_context=context) is None
    assert not marked.carries_a_patch_request(DIFF)


# --- 4: a 7z read gets the readers' plain answer -----------------------------------------------------------

ARCHIVE_ASKS = [
    'open the 7z archive "data.7z"',
    'what is in the archive "data.7z"?',
    "read data.7z",
    "what's in data.7z?",
    "list the files in the archive data.7z",
    "show me the contents of {proj}/data.7z",
    "read {proj}/data.7z",
]


@pytest.mark.parametrize("ask", ARCHIVE_ASKS)
def test_a_7z_read_is_answered_with_the_readers_refusal(machine, chat, jobs, ask, tmp_path) -> None:
    archive = machine["proj"] / "data.7z"
    archive.write_bytes(_SEVEN_Z)
    talk = chat(machine["proj"])

    reply = talk.say(ask.replace("{proj}", str(machine["proj"])), turn_id=_turn())

    assert talk.pending() == [] and jobs == [] and talk.model_rounds == 0, reply
    assert "`data.7z` is 7z archive" in reply and "Nothing was opened." in reply, reply
    assert "move" not in reply.lower() and "archive_outbox" not in reply, reply
    assert "Documents" not in reply, reply
    assert archive.read_bytes() == _SEVEN_Z
    assert not list(tmp_path.rglob("archive_outbox")), "something was moved into VOOL's archive folder"


def test_only_a_read_of_an_archive_is_answered_here(machine) -> None:
    from core.agent_runtime.windows_archive_read import windows_archive_read_refusal

    assert gate.active()
    assert windows_archive_read_refusal("read data.7z")
    for other in ("move data.7z to the Desktop", "delete data.7z", "make a 7z of this folder", "read data.zip",
                  "I sent you data.7z yesterday"):
        assert windows_archive_read_refusal(other) == "", other


# --- off Windows nothing changes ---------------------------------------------------------------------------


def test_off_windows_the_new_readers_say_nothing(machine, monkeypatch) -> None:
    from core import windows_marked_file_ops as marked
    from core.agent_runtime.windows_archive_read import windows_archive_read_refusal
    from core.execution import planner

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert planner._windows_python_command('python -c "print(2+2)"') == ""
    assert planner._windows_command_in_folder(f'in {machine["proj"]} run: python -c "print(2+2)"') == ("", "")
    assert windows_archive_read_refusal("read data.7z") == ""
    assert not marked.carries_a_patch_request("apply this patch\n" + DIFF)
    assert marked.marked_file_ask(
        "apply this patch\n" + DIFF, source_context={"_owner_local": True, "workspace": str(machine["proj"])}
    ) is None
    # The planner reads the run request exactly as on base.
    decision = planner.plan_tool_workflow(
        user_text='run python -c "print(2+2)"', task_class="unknown", executed_steps=[],
        source_context={"workspace": str(machine["proj"])},
    )
    assert dict(decision.next_payload or {}).get("arguments", {}).get("command") == "python -c"
    for bare in ("python -m compileall .", 'python -c "print(2+2)"'):
        assert not planner.should_attempt_tool_intent(bare, task_class="unknown", source_context={"surface": "openclaw"})
        no_plan = planner.plan_tool_workflow(
            user_text=bare, task_class="unknown", executed_steps=[], source_context={"workspace": str(machine["proj"])}
        )
        assert not no_plan.next_payload, no_plan

