"""Windows claims recheck on Steve (round 18, build 578d185f): a patch to a named full path, python -c in a
marked folder whose path holds "claims" or "Documents", and the 7z refusal's words.

1. "apply this patch to C:\\...\\proj\\notes.txt" with a unified diff under it, in an unbound chat (its folder is
   the Desktop): refused as "`...\\Desktop` is not inside a folder you marked". The patch lane placed the patch in
   the chat's own folder and never read the full path the request named. A patch that did not match the file got
   the same wrong-folder reason.
2. 'in C:\\Users\\Satoshi\\Documents\\VOOL Windows\\missions\\capability-test\\claims\\marked run: python -c
   "print(2+2)"' went to the model. Two readers took words of the path as words of the request:
   - the semantic preflight read the folder `claims` as "the quoted text is what someone claims", held the
     run as a quoted example and forbade every action;
   - the planner read the code's `print` as a listing verb and the path's `Documents` as the folder, and
     planned a listing of ~/Documents ahead of the run.
   `python -m compileall .` has neither `print` nor quotes, so it ran.
3. The 7z refusal told a Windows owner that confinement "is implemented for macOS only".

Each case is driven through the served chat as the VOOL window sends it, with the fixtures of
tests/test_windows_run_in_folder_served_end_to_end.py (Windows simulated by the write gate's `_IS_WINDOWS` and
the job runner's `_is_windows_platform`; the python job runs for real through the phase-1 argv).
"""
from __future__ import annotations

import os
import platform
import uuid

import pytest

from core import windows_write_gate as gate
from tests.test_windows_claims_sweep_run_patch_archive_lanes import DIFF, PATCHED
from tests.test_windows_run_in_folder_served_end_to_end import CALC, chat, jobs, machine

_STEVE_MARK = ("Documents", "VOOL Windows", "missions", "capability-test", "claims", "marked")


def _turn() -> str:
    return f"turn-{uuid.uuid4().hex[:10]}"


# --- 1: a patch to a full path goes where the path says ---------------------------------------------------


@pytest.mark.parametrize("target", ["file", "folder", "quoted-folder"])
def test_a_patch_to_a_full_path_is_applied_there_from_an_unbound_chat(machine, chat, jobs, target) -> None:
    calc = machine["proj"] / "calc.py"
    calc.write_bytes(CALC.encode("utf-8"))  # LF on every host: the patch tool refuses a file with CRs
    named = {"file": str(calc), "folder": str(machine["proj"]), "quoted-folder": f'"{machine["proj"]}"'}[target]
    talk = chat(machine["desktop"])

    card, reply = talk.ask_allow_resume(f"apply this patch to {named}\n" + DIFF)

    assert card["intent"] == "workspace.apply_unified_diff", card
    assert talk.model_rounds == 0, reply
    assert calc.read_text(encoding="utf-8") == PATCHED, reply
    assert "Desktop" not in reply, reply


def test_a_patch_that_does_not_match_the_named_file_says_why_not_which_folder(machine, chat) -> None:
    calc = machine["proj"] / "calc.py"
    calc.write_bytes(CALC.encode("utf-8"))
    stale = DIFF.replace("-    return a + b", "-    return a - b")
    talk = chat(machine["desktop"])

    _card, reply = talk.ask_allow_resume(f"apply this patch to {calc}\n" + stale)

    assert talk.model_rounds == 0, reply
    assert calc.read_text(encoding="utf-8") == CALC
    assert "not inside a folder you marked" not in reply and "Desktop" not in reply, reply
    assert "mismatch" in reply.lower() and "patched" not in reply.lower(), reply


def test_a_patch_to_a_full_path_outside_the_marked_folders_is_refused_by_that_path(machine, chat) -> None:
    other = machine["home"] / "Other folder"
    other.mkdir()
    (other / "calc.py").write_bytes(CALC.encode("utf-8"))
    talk = chat(machine["desktop"])

    reply = talk.say(f"apply this patch to {other / 'calc.py'}\n" + DIFF, turn_id=_turn())

    assert talk.pending() == [] and talk.model_rounds == 0, reply
    assert f"`{other / 'calc.py'}` is not inside a folder you marked" in reply and "Nothing was changed." in reply, reply
    assert (other / "calc.py").read_bytes() == CALC.encode("utf-8")


def test_a_patch_to_a_full_path_that_is_not_there_says_so(machine, chat) -> None:
    missing = machine["proj"] / "nope" / "calc.py"
    talk = chat(machine["desktop"])

    reply = talk.say(f"apply this patch to {missing}\n" + DIFF, turn_id=_turn())

    assert talk.pending() == [] and talk.model_rounds == 0, reply
    assert f"There is no file or folder at `{missing}`." in reply and "Nothing was changed." in reply, reply


# --- 2: python -c in a marked folder whose path holds "claims" or "Documents" -----------------------------


@pytest.mark.parametrize(
    "parts",
    [_STEVE_MARK, ("a", "claims", "marked"), ("Documents", "marked")],
    ids=["steve-mark", "claims", "documents"],
)
@pytest.mark.parametrize("binding", ["unbound", "bound"])
def test_python_c_in_such_a_marked_folder_runs_and_answers_with_its_output(
    machine, chat, jobs, monkeypatch, parts, binding
) -> None:
    mark = machine["home"].joinpath(*parts)
    mark.mkdir(parents=True)
    monkeypatch.setenv(gate.ENV_VAR, str(mark))
    talk = chat(machine["desktop"] if binding == "unbound" else mark)

    card, reply = talk.ask_allow_resume(f'in {mark} run: python -c "print(2+2)"')

    assert card["intent"] == "sandbox.run_command", card
    assert talk.model_rounds == 0, reply
    assert len(jobs) == 1 and jobs[0]["argv"] == ["python", "-c", "print(2+2)"], jobs
    assert os.path.samefile(jobs[0]["cwd"], mark), jobs
    assert "ran and exited 0." in reply and "4" in reply, reply


def test_a_folder_word_is_not_discourse_but_a_sentence_word_still_is(machine, monkeypatch) -> None:
    from core.semantic.preflight import SemanticFrame, semantic_preflight

    steve = "in C:\\Users\\Satoshi\\" + "\\".join(_STEVE_MARK) + ' run: python -c "print(2+2)"'
    assert semantic_preflight(steve).effect_routes_safe
    assert semantic_preflight('in /srv/a/claims/m run: python -c "print(2+2)"').effect_routes_safe
    # A sentence about what someone claims is still about the quoted text.
    said = semantic_preflight('what does he mean when he claims "the sky is green"?')
    assert said.dominant_frame is SemanticFrame.QUOTED and not said.effect_routes_safe
    # Off Windows the preflight reads the path's words as before.
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert semantic_preflight(steve).dominant_frame is SemanticFrame.QUOTED


def test_a_python_run_is_not_planned_as_a_listing_but_a_listing_still_is(machine) -> None:
    from core.execution import planner

    documents = machine["home"] / "Documents"
    documents.mkdir()
    run = planner.plan_tool_workflow(
        user_text=f'in {documents} run: python -c "print(2+2)"', task_class="unknown", executed_steps=[],
        source_context={},
    )
    assert dict(run.next_payload or {}).get("intent") == "sandbox.run_command", run
    listing = planner.plan_tool_workflow(
        user_text="list the files in my Documents", task_class="unknown", executed_steps=[], source_context={}
    )
    assert dict(listing.next_payload or {}).get("intent") == "machine.list_directory", listing


# --- 3: the 7z refusal names Windows, not macOS ------------------------------------------------------------


def test_the_7z_refusal_on_windows_does_not_mention_macos(machine, chat, jobs, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    archive = machine["proj"] / "data.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 40)
    talk = chat(machine["proj"])

    reply = talk.say("read data.7z", turn_id=_turn())

    assert talk.model_rounds == 0 and jobs == [], reply
    assert "`data.7z` is 7z archive" in reply and "Nothing was opened." in reply, reply
    assert "macos" not in reply.lower() and "mac " not in reply.lower(), reply


def test_off_windows_the_confinement_reason_keeps_its_words(monkeypatch) -> None:
    from core.artifact_readers import _sandbox

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert _sandbox.confinement_unavailable_reason() == (
        "process confinement for untrusted decoders is implemented for macOS only; this host is Linux"
    )


# --- nit: the rules hint names the Remove button ---------------------------------------------------------


def test_the_rules_hint_names_the_remove_button_in_every_language() -> None:
    import json
    from pathlib import Path

    from core import settings_extras_fragment

    catalogs = Path(settings_extras_fragment.__file__).parent / "i18n" / "catalogs"
    for catalog in sorted(catalogs.glob("*.json")):
        strings = json.loads(catalog.read_text(encoding="utf-8"))
        flat = strings["messages"]
        hint, button = flat["vx.memory.rules_hint"], flat["vx.memory.rule_remove"]
        assert button in hint, (catalog.name, hint, button)
