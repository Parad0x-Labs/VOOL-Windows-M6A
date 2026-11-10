"""The follow-up mark (`tool_followup_after_run`) is the server's, and Windows-only at every seam.

Security Architect, GRN on windows/port-captest-followup-plain-text with two conditions:

1. The Ollama lane's prose repair checks the Windows seam itself
   (`execution_gate.operator_approval_spend_applies`), so a stray or forwarded mark can never
   switch it on off Windows: mark set and seam off means no repair, and the missing-tool-call
   refusal stands.
2. No caller can put the mark on a turn. `/api/chat` copies the body's `source_context` dict into
   the turn (core/web/api/service.py `_inbound_source_context`); the key is stripped there with the
   other server-owned keys (`core.request_trust.RESERVED_TRUST_KEYS`), and the tool loop sets or
   clears it itself before every round, so a mark that arrives anyway (a body, a restored
   checkpoint) on a round where no step ran gives no repair.
"""
from __future__ import annotations

import pytest

from core.cloud_tool_call_contract import MalformedToolArgumentsError
from tests.test_the_tool_loop_reports_what_it_actually_ran import _call, _drive
from tests.test_windows_followup_after_a_run_writes_plain_text import (  # `ollama` is a fixture
    FOLLOWUP_KEY,
    PROSE,
    _request,
    _run,
    ollama,
)


@pytest.fixture
def windows_seam(monkeypatch):
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)


@pytest.fixture
def off_windows_seam(monkeypatch):
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)


# --- condition 1: the adapter checks the seam itself -----------------------------------------------


def test_a_marked_round_off_windows_gets_no_repair_and_the_refusal_stands(ollama, off_windows_seam) -> None:
    with pytest.raises(MalformedToolArgumentsError, match="required native tool call is missing"):
        _run(ollama({"role": "assistant", "content": PROSE}), _request(marked=True))


def test_the_same_marked_round_on_windows_is_repaired(ollama, windows_seam) -> None:
    """The control for the row above: only the seam differs."""
    response = _run(ollama({"role": "assistant", "content": PROSE}), _request(marked=True))

    assert [call.intent for call in response.tool_calls] == ["respond.direct"], response.tool_calls


# --- condition 2: no caller can set the mark -------------------------------------------------------


def test_the_mark_is_a_reserved_server_key() -> None:
    from core.request_trust import RESERVED_TRUST_KEYS, strip_reserved_trust_keys

    assert FOLLOWUP_KEY in RESERVED_TRUST_KEYS
    assert FOLLOWUP_KEY not in strip_reserved_trust_keys({FOLLOWUP_KEY: True, "surface": "api"})


def test_a_body_carried_mark_on_a_round_where_nothing_ran_gives_no_repair(windows_seam) -> None:
    """The mark reaches the loop anyway (it is what a forwarded body or a restored checkpoint would
    carry): the first round follows nothing that ran, so the loop clears it before the round."""
    _result, router, _events, _ws = _drive([], extra_context={FOLLOWUP_KEY: True})

    assert router.contexts, "the loop never asked the model"
    assert not router.contexts[0].get(FOLLOWUP_KEY), router.contexts[0].get(FOLLOWUP_KEY)


def test_a_body_carried_mark_off_windows_is_cleared_even_after_a_run(off_windows_seam) -> None:
    _result, router, _events, _ws = _drive(
        [[_call("workspace.read_file", path="f00.py")]], extra_context={FOLLOWUP_KEY: True}
    )

    assert len(router.contexts) == 2, router.calls
    assert not any(context.get(FOLLOWUP_KEY) for context in router.contexts), router.contexts


def test_the_chat_door_strips_a_body_carried_mark() -> None:
    """`/api/chat` reads the body's source_context through `_inbound_source_context` and then
    `strip_reserved_trust_keys`, in that order (core/web/api/service.py)."""
    from core.request_trust import strip_reserved_trust_keys
    from core.web.api.service import _inbound_source_context

    inbound = strip_reserved_trust_keys(_inbound_source_context({"source_context": {FOLLOWUP_KEY: True}}))

    assert FOLLOWUP_KEY not in inbound
