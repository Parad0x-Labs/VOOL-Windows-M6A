"""A test run's "ran and exited 0" and a patch's "Applied unified diff" are claims: backed when the tool ran.

Steve consistency check (12 Nov 2026, ec7c3205): a sandbox run and a file write were signed `clean`, but
"run pytest in <folder>" (workspace.run_tests, reply "`pytest` ran and exited 0.") and a pasted patch
(workspace.apply_unified_diff, reply "Applied unified diff:") were signed `no_action_claimed` although
each turn had its tool's executed receipt: the shared claim reader (`completion_claim_kind`) did not
read the tools' own result lines as claims. It now does. With the receipt the turn is `clean`; the same
words with no receipt are a claim the blocker refuses and the ledger signs `blocked_false_claim`.

The served rows drive the real lanes as the VOOL window does, with the fixtures of
tests/test_windows_run_in_folder_served_end_to_end.py.
"""
from __future__ import annotations

import pytest

from core.agent_runtime.action_honesty_validator import (
    completion_claim_kind,
    emit_turn_honesty_receipt,
    enforce_final_action_honesty,
)
from core.honesty_receipt import VERDICT_BLOCKED, VERDICT_CLEAN, list_honesty_receipts
from tests.test_windows_claims_sweep_run_patch_archive_lanes import PATCH_ASKS, PATCHED
from tests.test_windows_run_in_folder_served_end_to_end import CALC, chat, jobs, machine

__all__ = ["chat", "jobs", "machine"]

RUN_TESTS_REPLY = "`python -m pytest -q` ran and exited 0.\n\nCommand output:\n2 passed in 0.03s"
PATCH_REPLY = "Applied unified diff:\n- calc.py"


def _verdicts(session: str) -> list[str]:
    return [receipt["verdict"] for receipt in list_honesty_receipts(session)]


def _tools(session: str) -> set[str]:
    return {tool["tool"] for receipt in list_honesty_receipts(session) for tool in receipt["executed_tools"]}


def test_a_served_test_run_is_signed_clean(machine, chat, jobs) -> None:
    talk = chat(machine["desktop"])
    card, reply = talk.ask_allow_resume(f"run pytest in {machine['proj']}")
    assert card["intent"] == "workspace.run_tests", card
    assert "ran and exited 0." in reply, reply
    assert _verdicts(talk.session)[-1] == VERDICT_CLEAN, list_honesty_receipts(talk.session)
    assert "workspace.run_tests" in _tools(talk.session)


def test_a_served_patch_is_signed_clean(machine, chat, jobs) -> None:
    talk = chat(machine["proj"])
    target = machine["proj"] / "calc.py"
    target.write_bytes(CALC.encode("utf-8"))
    card, reply = talk.ask_allow_resume(PATCH_ASKS["fenced"])
    assert card["intent"] == "workspace.apply_unified_diff", card
    assert reply.startswith("Applied unified diff:"), reply
    assert target.read_text(encoding="utf-8") == PATCHED
    assert _verdicts(talk.session)[-1] == VERDICT_CLEAN, list_honesty_receipts(talk.session)
    assert "workspace.apply_unified_diff" in _tools(talk.session)


@pytest.fixture
def receipts_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    monkeypatch.setenv("VOOL_HONESTY_RECEIPTS", "1")


@pytest.mark.parametrize(
    ("reply", "request_text"),
    [(RUN_TESTS_REPLY, "run pytest"), (PATCH_REPLY, "apply this diff:\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-x\n+y")],
    ids=["run_tests", "patch"],
)
def test_the_same_words_with_no_receipt_are_blocked(receipts_home, reply, request_text) -> None:
    session = "no-receipt-" + str(abs(hash(reply)))
    result = enforce_final_action_honesty(
        {"response": reply, "details": {}}, user_input=request_text, effective_input=request_text,
        session_id=session, source_context={},
    )
    out = emit_turn_honesty_receipt(
        result, user_input=request_text, effective_input=request_text, session_id=session, source_context={},
    )
    assert out["action_honesty_validator"]["applied"] is True, out
    assert out["action_honesty_validator"]["claim_kind"] == "execution"
    assert "ran and exited" not in out["response"] and "Applied unified diff" not in out["response"], out["response"]
    assert out["honesty_receipt"]["verdict"] == VERDICT_BLOCKED
    assert list_honesty_receipts(session)[-1]["executed_tools"] == []


@pytest.mark.parametrize("reply", [RUN_TESTS_REPLY, PATCH_REPLY, "`py calc.py` ran and exited 0."])
def test_the_tools_result_lines_are_execution_claims(reply) -> None:
    assert completion_claim_kind(reply) == "execution"


@pytest.mark.parametrize("reply", [
    "If `pytest` ran and exited 0, you are done.",
    "You said `pytest` ran and exited 0.",
    "To apply a unified diff, use git apply.",
    "What does an applied unified diff look like?",
])
def test_explanations_and_quotes_are_not_claims(reply) -> None:
    assert completion_claim_kind(reply) == ""
