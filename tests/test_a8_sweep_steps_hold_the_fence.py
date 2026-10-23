"""The A8 erase sweep's transcript and session-summary steps read and rewrite inside the forget fence.

Measured 2026-10-21 at port head 6c0d92a: `finalization._sweep_step_conversation_log` and the
summaries/heuristics part of `_sweep_step_memory_jsonl` read their rows under `A8_TRAVERSAL_LOCK`
only and wrote them back through `rewrite_jsonl`, which takes the fence just for the write. In that
window:

- another chat's turn, written under the fence, was lost when the step wrote back its older read;
- another chat's "Forget X" landed, and the write-back of rows still carrying X was refused, so the
  sweep step failed.

Holding the fence across the read and the write orders both wholly before or after the step. The A8
traversal takes `A8_TRAVERSAL_LOCK` first, so the order stays A8, then the fence.

The gate stops the sweep between its read and its write. Without the fence it waits for the other
thread to finish; holding the fence it gives the other thread a moment to block on it, then goes on.
Each case runs on its own copy of the fence's process lock.
"""
from __future__ import annotations

import random
import threading

import pytest

import core.finalization as finalization
import core.memory.files as files
import core.memory.revocation as revocation
from core.context_namespace import ensure_chat_namespace
from core.memory.entries import resolve_memory_access_policy
from core.persistent_memory import append_conversation_event
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend

_WAIT = 10.0
_JOIN = 30.0
_BLOCKED = 0.5
_REQUEST = "req-a8-sweep-window"


def _canonical(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _value(rng: random.Random) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5)) + f"-{rng.randint(1000, 9999)}"


def _seed_swept_rows(chat: str) -> None:
    """One transcript row and one session summary written under the request the sweep erases."""
    files.append_sequenced_jsonl(files.conversation_log_path(), {
        "session_id": chat, "user": "what is on the list", "assistant": "milk and eggs", "request_id": _REQUEST,
    })
    files.append_jsonl(files.session_summaries_path(), {
        "session_id": chat, "summary": "Recent asks: what is on the list.", "request_id": _REQUEST,
    })


def _step(which: str):
    if which == "conversation_log":
        return files.conversation_log_path, lambda: finalization._sweep_step_conversation_log(_REQUEST, "")
    return files.session_summaries_path, lambda: finalization._sweep_step_memory_jsonl(_REQUEST)


def _run_in_window(monkeypatch, which: str, other) -> tuple[dict, dict]:
    """Run the sweep step, and `other` between the step's read and its write."""
    path_of, sweep = _step(which)
    in_window, other_done = threading.Event(), threading.Event()
    seen: dict = {}
    real_rewrite = files.rewrite_jsonl

    def rewrite_jsonl(path, rows):
        if threading.current_thread().name == "sweeper" and path == path_of() and not in_window.is_set():
            in_window.set()  # the step has read its rows and writes them back next
            if revocation._LOCK._is_owned():
                other_done.wait(_BLOCKED)  # the other thread asks for the fence now and must wait
            else:
                other_done.wait(_WAIT)
            seen["other_done_in_window"] = other_done.is_set()
        return real_rewrite(path, rows)

    monkeypatch.setattr(files, "rewrite_jsonl", rewrite_jsonl)
    results: dict = {}

    def run(key, fn, done=None):
        try:
            results[key] = fn()
        except BaseException as exc:  # reported below; a hang is a failure too
            results[key] = exc
        finally:
            if done is not None:
                done.set()

    one = threading.Thread(target=run, args=("sweep", sweep), name="sweeper", daemon=True)
    two = threading.Thread(target=run, args=("other", other, other_done), name="other", daemon=True)
    one.start()
    assert in_window.wait(_WAIT), "the sweep step never reached its write"
    two.start()
    one.join(_JOIN)
    two.join(_JOIN)
    assert not one.is_alive() and not two.is_alive(), "the sweep step and the other thread deadlocked"
    return results, seen


@pytest.mark.parametrize("which", ["conversation_log", "session_summaries"])
def test_another_chats_turn_written_during_the_sweep_step_is_kept(tmp_path, monkeypatch, which):
    monkeypatch.setattr(revocation, "_LOCK", threading.RLock())
    rng = random.Random(7401 if which == "conversation_log" else 7402)
    _profile(tmp_path)
    swept, other = _canonical(rng), _canonical(rng)
    ensure_chat_namespace(other, grant_current_receipts=False)
    policy = resolve_memory_access_policy(chat_id=other)
    _seed_swept_rows(swept)
    marker = _value(rng)

    def turn():
        append_conversation_event(session_id=other, user_input=f"Note the parcel number {marker}.",
                                  assistant_output="Noted.", access_policy=policy)

    results, seen = _run_in_window(monkeypatch, which, turn)
    assert results["sweep"] == finalization._SWEEP_STEP_OK, results
    assert not isinstance(results["other"], BaseException), results
    path_of, _ = _step(which)
    rows = files.load_jsonl(path_of())
    assert not [row for row in rows if row.get("request_id") == _REQUEST], rows
    assert [row for row in rows if row.get("session_id") == other and marker in str(row)], (
        "the other chat's turn, written while the sweep step was between its read and its write, was lost")
    assert not seen["other_done_in_window"], "the other chat's write ran inside the sweep step's read and write"


@pytest.mark.parametrize("which", ["conversation_log", "session_summaries"])
def test_another_chats_forget_during_the_sweep_step_leaves_the_step_completed(tmp_path, monkeypatch, which):
    monkeypatch.setattr(revocation, "_LOCK", threading.RLock())
    rng = random.Random(7411 if which == "conversation_log" else 7412)
    profile = _profile(tmp_path)
    swept, other = _canonical(rng), _canonical(rng)
    value = _value(rng)
    _ingest(profile, other, [(_ts("2026-03-04T09:00:00"), f"The gate code is {value}.")])
    _seed_swept_rows(swept)
    path_of, _ = _step(which)
    assert any(value in str(row) for row in files.load_jsonl(path_of())), "the other chat left nothing in this file"

    results, seen = _run_in_window(monkeypatch, which, lambda: _forget_command(profile, other, f"Forget {value}"))
    assert results["sweep"] == finalization._SWEEP_STEP_OK, results
    handled, reply = results["other"]
    assert handled and reply.startswith("Forget applied"), reply
    rows = files.load_jsonl(path_of())
    assert not [row for row in rows if row.get("request_id") == _REQUEST], rows
    assert not [row for row in rows if value in str(row)], rows
    assert not seen["other_done_in_window"], "the forget ran inside the sweep step's read and write"
