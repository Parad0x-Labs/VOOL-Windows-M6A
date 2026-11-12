"""A rule take-back by chat never leaves memory locked, and recovery only ever rolls a proven take-back forward.

Measured on Steve (12 Nov, train 8810bbf8): "forget the rule about OK-TEST" (11 Nov) and "... CONS-TEST" (today)
each left a forget admission pending in .forget-authority/ledger.json. The forget grammar admits the turn, the
rules lane answers it (no memory forget is owed), and only a verified memory-forget outcome ever completed an
admission. Every memory read in its scope then failed closed: 583 "authorize_payload failed" lines an hour, and
Settings > Memory took 46 to 147 s and showed nothing.

An admission's token is a marker, sha256 of (chat, sorted forget targets), not a forget word: completing one
revokes and restores nothing. So the take-back completes its own admission once the rules lane has answered
that turn without an error, and startup recovery completes a stuck one only when the chat's own transcript
proves it was such a take-back (see core/persistent_memory.py _recover_admissions).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

import pytest

from core import runtime_paths

REPO = Path(__file__).resolve().parents[1]
CHAT = "openclaw:" + "a1" * 10
OTHER = "openclaw:" + "b2" * 10
TAKE_BACK = "forget the rule about OK-TEST"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    profile = tmp_path / "home"
    profile.mkdir()
    monkeypatch.setenv("VOOL_HOME", str(profile))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(profile / "workspace"))
    runtime_paths.configure_runtime_home(profile)
    from core.context_namespace import ensure_chat_namespace
    from core.memory.revocation import prepare

    for chat in (CHAT, OTHER):
        ensure_chat_namespace(chat, grant_current_receipts=False)
    prepare(profile, allow_initialize=True, arm=False)
    try:
        yield profile
    finally:
        runtime_paths.configure_runtime_home(None)


def _marker(chat: str, text: str) -> str:
    from core.memory.command_result import forget_demand_targets

    return sha256(repr((chat, sorted(forget_demand_targets(text)))).encode("utf-8")).hexdigest()


_RUN = __import__("uuid").uuid4().hex[:8]


def _id(request: str) -> str:
    """Turn ids unique to this run and test: the execution-fact store is shared by the whole test session."""
    import os

    test = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]
    return sha256(f"{_RUN}|{test}|{request}".encode("utf-8")).hexdigest()[:32]


def _turn(chat: str, text: str, request: str) -> None:
    """One transcript row as a served turn writes it (request id with its ingress prefix)."""
    from core.memory.files import conversation_log_path

    path = conversation_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"session_id": chat, "request_id": "req:http:" + _id(request), "user": text,
                                 "assistant": "ok"}) + "\n")


def _rules_fact(chat: str, request: str, *, name: str = "rules.remove", ok: bool = True) -> None:
    from core.execution_truth import KIND_TOOL, record_execution

    record_execution(session_id=chat, turn_key=_id(request), kind=KIND_TOOL, name=name, ok=ok,
                     status="executed" if ok else "failed", detail={"kind": "taken_back"}, dedupe=name + _id(request))


def _db_path():
    from storage.db import active_default_db_path

    return active_default_db_path()


def _admission(home: Path, chat: str, text: str) -> str:
    from core.memory.revocation import begin

    return begin(chat, _marker(chat, text), home, _kind="admission")


def _ledger(home: Path) -> dict:
    return json.loads((home / ".forget-authority" / "ledger.json").read_text(encoding="utf-8"))["operations"]


def _recover(home: Path) -> dict:
    from core.persistent_memory import _recover_admissions

    return _recover_admissions(home)


# --- startup recovery ---------------------------------------------------------------------------------------


def test_recovery_completes_a_finished_rule_take_back_and_nothing_else(home, caplog) -> None:
    from core.memory.revocation import begin, complete

    _turn(CHAT, TAKE_BACK, "t1")
    _rules_fact(CHAT, "t1")
    op = _admission(home, CHAT, TAKE_BACK)
    # A forget that is still pending, and one that completed: recovery leaves both exactly as they were.
    pending_forget = begin(OTHER, "pending-word", home)
    done_forget = begin(OTHER, "QUILL-77", home)
    complete(done_forget, home)
    before = _ledger(home)

    with caplog.at_level(logging.WARNING):
        assert _recover(home) == {"completed": 1, "kept_pending": 0}
    after = _ledger(home)
    assert after[op]["status"] == "complete"
    assert after[pending_forget] == before[pending_forget]
    assert after[done_forget] == before[done_forget]
    assert list(after) == list(before)
    # Never the user's words or the targets in the log.
    assert "OK-TEST" not in caplog.text and "ok test" not in caplog.text
    # Running it again changes nothing.
    assert _recover(home) == {"completed": 0, "kept_pending": 0}
    assert _ledger(home) == after


def test_a_forgotten_memory_stays_withheld_after_recovery(home) -> None:
    from core.memory.revocation import begin, complete
    from core.persistent_memory import append_conversation_event, list_conversation_sessions

    append_conversation_event(session_id=OTHER, user_input="My locker code is QUILL-77.", assistant_output="ok")
    complete(begin(OTHER, "QUILL-77", home), home)
    _turn(CHAT, TAKE_BACK, "t1")
    _rules_fact(CHAT, "t1")
    _admission(home, CHAT, TAKE_BACK)
    assert _recover(home)["completed"] == 1
    assert "QUILL-77" not in json.dumps(list_conversation_sessions(limit=100))


@pytest.mark.parametrize("case", ["no_turn", "memory_forget", "fact_on_another_turn", "fact_not_ok",
                                  "two_turns", "no_fact"])
def test_an_admission_that_cannot_be_proven_a_take_back_stays_pending(home, caplog, case) -> None:
    text = TAKE_BACK
    if case == "memory_forget":
        text = "Forget QUILL-77."
        _turn(CHAT, text, "t1")
        _rules_fact(CHAT, "t1")
    elif case == "no_turn":
        _rules_fact(CHAT, "t1")
    elif case == "fact_on_another_turn":
        # One take-back answered by the rules lane, and a second identical request that never was.
        _turn(CHAT, "forget the rule about units", "t0")
        _rules_fact(CHAT, "t0")
        _turn(CHAT, text, "t1")
    elif case == "fact_not_ok":
        _turn(CHAT, text, "t1")
        _rules_fact(CHAT, "t1", ok=False)
    elif case == "two_turns":
        _turn(CHAT, text, "t1")
        _rules_fact(CHAT, "t1")
        _turn(CHAT, text, "t2")
        _rules_fact(CHAT, "t2")
    elif case == "no_fact":
        _turn(CHAT, text, "t1")
    op = _admission(home, CHAT, text)
    before = _ledger(home)
    with caplog.at_level(logging.WARNING):
        assert _recover(home) == {"completed": 0, "kept_pending": 1}
    assert _ledger(home) == before and before[op]["status"] == "pending"
    assert "stays pending" in caplog.text
    assert "QUILL" not in caplog.text and "OK-TEST" not in caplog.text


def test_an_admission_with_no_targets_is_never_matched(home) -> None:
    from core.memory.revocation import begin

    # Turns with no forget targets all share one marker; such an op must never be completed from them.
    _turn(CHAT, "hello there", "t1")
    _rules_fact(CHAT, "t1")
    op = begin(CHAT, sha256(repr((CHAT, [])).encode("utf-8")).hexdigest(), home, _kind="admission")
    assert _recover(home)["completed"] == 0
    assert _ledger(home)[op]["status"] == "pending"


def test_an_unverifiable_authority_stops_recovery_and_completes_nothing(home, caplog) -> None:
    _turn(CHAT, TAKE_BACK, "t1")
    _rules_fact(CHAT, "t1")
    op = _admission(home, CHAT, TAKE_BACK)
    (home / ".forget-authority" / "lease-0000").write_bytes(b"not armed\n")
    with caplog.at_level(logging.WARNING):
        assert _recover(home) == {"completed": 0, "kept_pending": 0}
    assert _ledger(home)[op]["status"] == "pending"
    assert "forget authority lease is invalid" in caplog.text


_CRASH = """
import os, sys
sys.path.insert(0, {repo!r})
from core import runtime_paths
runtime_paths.configure_runtime_home({home!r})
from storage.db import configure_default_db_path
configure_default_db_path({db!r})
from core.memory import revocation
def die(*args, **kwargs):
    os._exit(9)
revocation.complete = die
from core.persistent_memory import _recover_admissions
_recover_admissions({home!r})
"""


def test_a_kill_during_recovery_leaves_the_op_pending_and_the_next_start_completes_it(home) -> None:
    _turn(CHAT, TAKE_BACK, "t1")
    _rules_fact(CHAT, "t1")
    op = _admission(home, CHAT, TAKE_BACK)
    done = subprocess.run([sys.executable, "-c", _CRASH.format(repo=str(REPO), home=str(home), db=str(_db_path()))],
                          env={**__import__("os").environ, "VOOL_HOME": str(home)}, timeout=180)
    assert done.returncode == 9
    assert _ledger(home)[op]["status"] == "pending"
    assert _recover(home)["completed"] == 1
    assert _ledger(home)[op]["status"] == "complete"


# --- the take-back finishing its own admission ----------------------------------------------------------------


def _admitted(home: Path, turn: str):
    from core.persistent_memory import _forget_outcomes

    context = {"turn_key": _id(turn)}
    op = _admission(home, CHAT, TAKE_BACK)
    _forget_outcomes.admit([(home, op)], request=TAKE_BACK, session=CHAT, source_context=context)
    return op, context


def test_a_take_back_the_rules_lane_answered_completes_its_admission(home) -> None:
    from core.persistent_memory import _finish_forget_turn_admission

    op, context = _admitted(home, "t9")
    _rules_fact(CHAT, "t9")
    result = {"response": "Took back your standing instruction."}
    assert _finish_forget_turn_admission(result, user_text=TAKE_BACK, session_id=CHAT,
                                         source_context=context) == result
    assert _ledger(home)[op]["status"] == "complete"


@pytest.mark.parametrize("fact", ["none", "failed", "other_turn"])
def test_a_take_back_that_errored_leaves_its_admission_pending(home, fact) -> None:
    from core.persistent_memory import _finish_forget_turn_admission, _forget_outcomes

    op, context = _admitted(home, "t9")
    if fact == "failed":
        _rules_fact(CHAT, "t9", ok=False)
    elif fact == "other_turn":
        _rules_fact(CHAT, "t8")
    _finish_forget_turn_admission({"response": "Something went wrong."}, user_text=TAKE_BACK, session_id=CHAT,
                                  source_context=context)
    assert _ledger(home)[op]["status"] == "pending"
    _forget_outcomes.release_admission(request=TAKE_BACK, session=CHAT, source_context=context)


# --- served -------------------------------------------------------------------------------------------------


def test_served_a_rule_take_back_leaves_no_pending_op_and_memory_answers_quickly(tmp_path) -> None:
    from tests._blackbox_served_rig import ServedDaemon, run_in_home
    from tests._served_sufficiency_provider import SEED
    from tests.test_private_chat_served_workflow import MODEL, _certify, _Provider, _stream

    profile = tmp_path / "home"
    provider = _Provider({MODEL: "Got it."})
    with provider:
        run_in_home(profile, SEED.format(root=REPO, base_url=provider.base_url, models=[(MODEL, {})]))
        with ServedDaemon(profile, env_extra={"VOOL_MODEL_LOAD_FLOOR_GB": "0"}) as daemon:
            assert _certify(daemon).get("state") == "verified"
            assert "Locked in" in _stream(daemon, "Remember that my boat is called Juniper.", OTHER)[0]
            _stream(daemon, "From now on always end with a haiku about OK-TEST.", CHAT)
            answer = _stream(daemon, TAKE_BACK, CHAT)[0]
            assert "Took back" in answer, answer
            operations = _ledger(profile)
            assert operations and all(row["status"] == "complete" for row in operations.values()), operations
            started = time.monotonic()
            with urlopen(f"{daemon.base_url}/api/memory/entries?limit=100", timeout=300) as response:
                facts = [str(e.get("fact") or "") for e in json.loads(response.read().decode())["entries"]]
            took = time.monotonic() - started
            assert any("Juniper" in fact for fact in facts), facts
            assert took < 3.0, took
            # The rule stays taken back.
            assert "Took back" not in _stream(daemon, TAKE_BACK, CHAT)[0]
    log = (profile / "daemon.log").read_text(encoding="utf-8", errors="replace")
    assert "authorize_payload failed" not in log
