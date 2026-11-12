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


def _journal(home: Path) -> dict:
    path = home / ".forget-authority" / "take-back-forgets.json"
    return json.loads(path.read_text(encoding="utf-8"))["ops"] if path.exists() else {}


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
    # Every earlier row is kept, in order; what is added is the take-back's own forget, complete, in its chat.
    assert list(after)[: len(before)] == list(before)
    added = [after[key] for key in list(after)[len(before):]]
    assert added and all(row.get("kind", "forget") == "forget" and row["chat"] == CHAT
                         and row["status"] == "complete" for row in added), added
    assert _journal(home) == {}
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
real = revocation.complete
def die(operation_id, runtime_home=None):
    if {stage!r} == "forget" or operation_id == {admission!r}:
        os._exit(9)
    return real(operation_id, runtime_home)
revocation.complete = die
from core.persistent_memory import _recover_admissions
_recover_admissions({home!r})
"""


@pytest.mark.parametrize("stage", ["forget", "admission"])
def test_a_kill_during_recovery_leaves_the_op_pending_and_the_next_start_completes_it(home, stage) -> None:
    from core.persistent_memory import append_conversation_event, list_conversation_sessions

    append_conversation_event(session_id=CHAT, user_input="From now on mention OK-TEST.", assistant_output="ok")
    _turn(CHAT, TAKE_BACK, "t1")
    _rules_fact(CHAT, "t1")
    op = _admission(home, CHAT, TAKE_BACK)
    done = subprocess.run([sys.executable, "-c", _CRASH.format(repo=str(REPO), home=str(home), db=str(_db_path()),
                                                                stage=stage, admission=op)],
                          env={**__import__("os").environ, "VOOL_HOME": str(home)}, timeout=180)
    assert done.returncode == 9
    assert _ledger(home)[op]["status"] == "pending"
    # The proof was written down before the forget ran, so the next start finishes it even though the forget
    # may already have withheld the turn that proved it.
    assert op in _journal(home)
    assert _recover(home)["completed"] == 1
    assert _ledger(home)[op]["status"] == "complete"
    assert _journal(home) == {}
    assert "OK-TEST" not in json.dumps(list_conversation_sessions(limit=100))
    assert _recover(home) == {"completed": 0, "kept_pending": 0}


def test_an_admission_stays_pending_when_its_forget_cannot_be_verified(home, monkeypatch) -> None:
    import core.persistent_memory as pm

    _turn(CHAT, TAKE_BACK, "t1")
    _rules_fact(CHAT, "t1")
    op = _admission(home, CHAT, TAKE_BACK)

    def broken(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(pm, "maybe_handle_memory_command", broken)
    assert _recover(home) == {"completed": 0, "kept_pending": 1}
    assert _ledger(home)[op]["status"] == "pending"


def test_a_take_back_by_rule_number_names_no_words_and_stays_pending(home) -> None:
    text = "forget rule 2"
    _turn(CHAT, text, "t1")
    _rules_fact(CHAT, "t1")
    op = _admission(home, CHAT, text)
    _recover(home)
    assert _ledger(home)[op]["status"] == "pending"


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
    forgets = [row for row in _ledger(home).values() if row.get("kind", "forget") == "forget"]
    assert forgets and all(row["chat"] == CHAT and row["status"] == "complete" for row in forgets)
    assert _journal(home) == {}


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


_OLD_BUILD = """
# The build before this fix, for the restart rows: a take-back leaves its admission pending.
import core.persistent_memory as _pm
_pm._admission_recovery_enabled = lambda: False
"""


def _served(tmp_path: Path):
    from tests._blackbox_served_rig import ServedDaemon, run_in_home
    from tests._served_sufficiency_provider import SEED
    from tests.test_user_rules_served_workflow import MODEL, _Provider

    profile = tmp_path / "home"
    folder = tmp_path / "bakery"
    folder.mkdir(exist_ok=True)
    provider = _Provider({MODEL: "Try Crumb and Kettle."})
    return profile, folder, provider, lambda: run_in_home(
        profile, SEED.format(root=REPO, base_url=provider.base_url, models=[(MODEL, {})])), ServedDaemon


def _say(daemon, provider, folder, text: str, session: str) -> tuple[str, str]:
    from tests.test_user_rules_served_workflow import MODEL, _answer

    before = len(provider.calls)
    answer = _answer(daemon.chat(text, session_id=session, model=MODEL, workspace=str(folder), timeout=600.0))
    sent = "\n".join(f"{c.get('system', '')}\n{c.get('full_prompt', '')}" for c in provider.calls[before:])
    return answer, sent


def _memory_list(daemon) -> tuple[float, list[str]]:
    started = time.monotonic()
    with urlopen(f"{daemon.base_url}/api/memory/entries?limit=100", timeout=300) as response:
        facts = [str(e.get("fact") or "") for e in json.loads(response.read().decode())["entries"]]
    return time.monotonic() - started, facts


CUPCAKE_TAKE_BACK = "forget the rule about cupcake"


def _logs(profile: Path) -> str:
    """The daemon's console and its runtime log (data/logs/vool_api.log, where Steve's lines come from)."""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in (profile / "daemon.log", profile / "data" / "logs" / "vool_api.log") if path.exists())


def test_served_a_rule_take_back_keeps_its_words_from_the_model_and_memory_answers_quickly(tmp_path) -> None:
    from tests.test_user_rules_served_workflow import MODEL, PLAIN_TASK, _certify

    profile, folder, provider, seed, ServedDaemon = _served(tmp_path)
    with provider:
        seed()
        with ServedDaemon(profile, env_extra={"VOOL_MODEL_LOAD_FLOOR_GB": "0"}) as daemon:
            assert _certify(daemon).get("state") == "verified"
            assert "Locked in" in _say(daemon, provider, folder, "Remember that my boat is called Juniper.", OTHER)[0]
            assert "Saved rule" in _say(daemon, provider, folder, "add to chat rules: never use the word cupcake", CHAT)[0]
            assert "cupcake" in _say(daemon, provider, folder, PLAIN_TASK, CHAT)[1]
            removed = _say(daemon, provider, folder, CUPCAKE_TAKE_BACK, CHAT)[0]
            assert "Removed" in removed, removed
            operations = _ledger(profile)
            assert not [row for row in operations.values() if row["status"] == "pending"], operations
            # While the take-back turn ran, its admission withheld reads in its scope (by design); after it, none.
            settled = len(_logs(profile))
            # History, retrieved context, standing instructions and recall: the word reaches the model nowhere.
            sent_after = _say(daemon, provider, folder, PLAIN_TASK, CHAT)[1]
            assert sent_after and "cupcake" not in sent_after.lower()
            took, facts = _memory_list(daemon)
            assert any("Juniper" in fact for fact in facts), facts
            assert took < 3.0, took
    assert "authorize_payload failed" not in _logs(profile)[settled:]
    assert MODEL


def test_served_a_stuck_take_back_is_finished_at_restart_and_its_words_stay_from_the_model(tmp_path) -> None:
    from tests.test_user_rules_served_workflow import PLAIN_TASK, _certify

    profile, folder, provider, seed, ServedDaemon = _served(tmp_path)
    old_build = tmp_path / "old-build"
    old_build.mkdir()
    (old_build / "sitecustomize.py").write_text(_OLD_BUILD, encoding="utf-8")
    import os

    with provider:
        seed()
        old = {"VOOL_MODEL_LOAD_FLOOR_GB": "0", "PYTHONPATH": f"{old_build}{os.pathsep}{REPO}"}
        with ServedDaemon(profile, env_extra=old) as daemon:
            assert _certify(daemon).get("state") == "verified"
            assert "Locked in" in _say(daemon, provider, folder, "Remember that my boat is called Juniper.", OTHER)[0]
            assert "Saved rule" in _say(daemon, provider, folder, "add to chat rules: never use the word cupcake", CHAT)[0]
            assert "Removed" in _say(daemon, provider, folder, CUPCAKE_TAKE_BACK, CHAT)[0]
        stuck = [row for row in _ledger(profile).values() if row["status"] == "pending"]
        assert [row.get("kind") for row in stuck] == ["admission"], stuck   # the Steve state

        with ServedDaemon(profile, env_extra={"VOOL_MODEL_LOAD_FLOOR_GB": "0"}) as daemon:
            operations = _ledger(profile)
            assert not [row for row in operations.values() if row["status"] == "pending"], operations
            sent_after = _say(daemon, provider, folder, PLAIN_TASK, CHAT)[1]
            assert sent_after and "cupcake" not in sent_after.lower()
            took, facts = _memory_list(daemon)
            assert any("Juniper" in fact for fact in facts), facts
            assert took < 3.0, took
    log = _logs(profile)
    assert "completed: a finished rule take-back, its words forgotten" in log
    assert "cupcake" not in "\n".join(line for line in log.splitlines() if "forget admission" in line)
    restart_log = log.rsplit("completed: a finished rule take-back", 1)[-1]
    assert "authorize_payload failed" not in restart_log
