"""`forget_profile_derivatives` takes the forget fence before `_MEMORY_ENTRY_LOCK`, whoever calls it.

Measured 2026-10-21 at port head 6c0d92a: it took `_MEMORY_ENTRY_LOCK` and reached the fence inside it
(`_forget_session_metadata` -> `revocation.serialized`), the reverse of the order every forget and
memory writer uses (fence, then `_MEMORY_ENTRY_LOCK`). Its one caller, the "Forget X" branch of
`persistent_memory`, already holds the command fence, so that path was safe; a caller without the fence
deadlocked against any thread holding the fence and asking for the entry lock.

`add_memory_fact` rewrites the MEMORY.md projection under `_MEMORY_ENTRY_LOCK` after its fenced
`record_memory_entry` returns; that rewrite never enters the fence, which the second test pins.

The gates only order the two threads so each holds its first lock when the other asks for it. Each
case runs on its own copies of the process locks, so a case that deadlocks leaves its threads stuck on
those copies and the next case still runs.
"""
from __future__ import annotations

import random
import threading

import pytest

import core.memory.entries as entries
import core.memory.revocation as revocation
from core.context_namespace import ensure_chat_namespace
from tests.test_question_date_time_leg_20261002 import _hash_backend, _profile

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend

_WAIT = 10.0
_JOIN = 30.0


def _canonical(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _fresh_locks(monkeypatch) -> None:
    # The case's own process locks: a deadlock here never reaches the next case.
    monkeypatch.setattr(entries, "_MEMORY_ENTRY_LOCK", threading.Lock())
    monkeypatch.setattr(revocation, "_LOCK", threading.RLock())


def _run_pair(first, second, *, first_ready: threading.Event) -> dict:
    results: dict = {}

    def run(key, fn):
        try:
            results[key] = fn()
        except BaseException as exc:  # reported below; a hang is the failure under test
            results[key] = exc

    one = threading.Thread(target=run, args=("first", first), name="first", daemon=True)
    two = threading.Thread(target=run, args=("second", second), name="second", daemon=True)
    one.start()
    assert first_ready.wait(_WAIT), "the first thread never reached its gate"
    two.start()
    one.join(_JOIN)
    two.join(_JOIN)
    assert not one.is_alive() and not two.is_alive(), "the two threads deadlocked"
    return results


def _fence_then_entry_lock(in_fence: threading.Event) -> str:
    """What a forget and every memory writer do: the fence, then the entry lock."""
    with revocation.serialized():
        in_fence.set()
        with entries._MEMORY_ENTRY_LOCK:
            return "done"


def test_forget_profile_derivatives_without_the_command_fence_and_a_fenced_writer_both_finish(tmp_path, monkeypatch):
    _fresh_locks(monkeypatch)
    rng = random.Random(7301)
    _profile(tmp_path)
    chat = _canonical(rng)
    ensure_chat_namespace(chat, grant_current_receipts=False)
    policy = entries.resolve_memory_access_policy(chat_id=chat)

    in_entry_lock, in_fence = threading.Event(), threading.Event()
    real_metadata = entries._forget_session_metadata

    def forget_session_metadata(*args, **kwargs):
        if threading.current_thread().name == "first" and not in_entry_lock.is_set():
            in_entry_lock.set()  # it holds _MEMORY_ENTRY_LOCK and enters the fence next
            if not revocation._LOCK._is_owned():
                # Let the other thread take the fence first. A caller already holding the fence has nothing to wait for.
                in_fence.wait(_WAIT)
        return real_metadata(*args, **kwargs)

    monkeypatch.setattr(entries, "_forget_session_metadata", forget_session_metadata)
    erasures: dict = {}
    results = _run_pair(
        lambda: entries.forget_profile_derivatives("LOCKER-4821", access_policy=policy, erasures=erasures),
        lambda: _fence_then_entry_lock(in_fence),
        first_ready=in_entry_lock,
    )
    assert results["first"] is None, results
    assert results["second"] == "done", results
    assert erasures["stores"]["chat_session_metadata"]["status"] == "verified", erasures


def test_add_memory_facts_mirror_rewrite_never_waits_for_the_fence(tmp_path, monkeypatch):
    _fresh_locks(monkeypatch)
    rng = random.Random(7302)
    _profile(tmp_path)
    chat = _canonical(rng)
    ensure_chat_namespace(chat, grant_current_receipts=False)

    in_mirror, in_fence = threading.Event(), threading.Event()
    real_mirror = entries._rewrite_memory_mirror_locked

    def rewrite_memory_mirror_locked():
        if threading.current_thread().name == "first" and not in_mirror.is_set():
            in_mirror.set()  # add_memory_fact holds _MEMORY_ENTRY_LOCK for the projection rewrite
            in_fence.wait(_WAIT)  # the other thread now holds the fence and asks for the entry lock
        return real_mirror()

    monkeypatch.setattr(entries, "_rewrite_memory_mirror_locked", rewrite_memory_mirror_locked)
    results = _run_pair(
        lambda: entries.add_memory_fact("The shed key hangs by the door.", session_id=chat, scope="chat"),
        lambda: _fence_then_entry_lock(in_fence),
        first_ready=in_mirror,
    )
    assert results["first"] is True, results
    assert results["second"] == "done", results
