"""One transient storage error in the forget-authority read does not withhold a payload nothing revokes.

Measured 2026-10-27 while root-causing CI run 40441789238 (tests (0)): authorize_payload turned any exception from the
authority read into a refusal, so a single "database schema has changed" from the memory store dropped a served
receipt from the packet. The packet still reported full coverage, and the value that receipt replaced read as current.
"database schema has changed" is now read again once; a read that keeps failing still withholds (fail closed), and a
revoked payload is still withheld. Other storage errors still withhold at once (tests/test_forget_admission_r9.py).
"""
from __future__ import annotations

import sqlite3

import pytest

import core.memory.revocation as revocation
from core.vool_memory import VoolMemory
from tests.test_question_date_time_leg_20261002 import _hash_backend, _profile

pytestmark = pytest.mark.usefixtures("_hash_backend")

CHAT = "transient-authority"
PAYLOAD = "Correction, my longest tunnel route is now 31 km."


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_MEMORY_RECEIPTS", "1")
    memory = VoolMemory(runtime_home=str(_profile(tmp_path)))
    yield memory
    memory.close()


def _failing(mem, monkeypatch, failures: int):
    real = mem.revoked_tokens
    calls = {"n": 0}

    def revoked_tokens(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise sqlite3.OperationalError("database schema has changed")
        return real(*args, **kwargs)

    monkeypatch.setattr(mem, "revoked_tokens", revoked_tokens)
    return calls


def test_one_transient_authority_read_error_does_not_withhold_the_payload(mem, monkeypatch):
    calls = _failing(mem, monkeypatch, failures=1)
    assert revocation.authorize_payload(PAYLOAD, CHAT, mem._authority_home, owner_memory=mem) is True
    assert calls["n"] == 2


def test_an_authority_read_that_keeps_failing_still_withholds(mem, monkeypatch):
    _failing(mem, monkeypatch, failures=10)
    assert revocation.authorize_payload(PAYLOAD, CHAT, mem._authority_home, owner_memory=mem) is False


def test_a_revoked_payload_is_still_withheld_after_a_transient_error(mem, monkeypatch):
    real = mem.revoked_tokens
    calls = {"n": 0}

    def revoked_tokens(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database schema has changed")
        return (*real(*args, **kwargs), "31")

    monkeypatch.setattr(mem, "revoked_tokens", revoked_tokens)
    assert revocation.authorize_payload(PAYLOAD, CHAT, mem._authority_home, owner_memory=mem) is False
