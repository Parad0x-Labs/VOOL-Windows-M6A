"""Credential transactions serialize contention without weakening publication locks."""
from __future__ import annotations

import errno
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.credential_intelligence import _state_lock as locks
from core.cross_process_lock import LockUnavailable, PublicationLock


def test_credential_transaction_waits_for_a_contending_thread(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    contended = threading.Event()
    entered = threading.Event()
    errors = []

    class ObservedLock(PublicationLock):
        def __enter__(self):
            try:
                return super().__enter__()
            except LockUnavailable:
                contended.set()
                raise

    monkeypatch.setattr(locks, "PublicationLock", ObservedLock)

    def mutate():
        try:
            with locks.state_lock(state):
                entered.set()
                state.write_text(state.read_text() + ",second")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=mutate)
    try:
        with PublicationLock(state.with_name("state.json.lock")):
            state.write_text("first")
            worker.start()
            assert contended.wait(10), "the competing transaction never reached the lock"
            assert not entered.is_set(), "the transaction entered without the lock"
    finally:
        if worker.ident is not None:
            worker.join(15)
    assert not worker.is_alive(), "the transaction did not finish after release"
    assert not errors, errors
    assert entered.is_set()
    assert state.read_text() == "first,second"


_PROCESS_DRIVER = """
import sys
from pathlib import Path
from core.credential_intelligence import _state_lock as locks
from core.cross_process_lock import LockUnavailable, PublicationLock
state, ready = map(Path, sys.argv[1:])
class ObservedLock(PublicationLock):
    def __enter__(self):
        try:
            return super().__enter__()
        except LockUnavailable:
            ready.write_text('contended')
            raise
locks.PublicationLock = ObservedLock
with locks.state_lock(state):
    state.write_text(state.read_text() + ',second')
"""


def test_credential_transaction_waits_for_a_contending_process(tmp_path):
    state = tmp_path / "state.json"
    ready = tmp_path / "contended"
    child = None
    try:
        with PublicationLock(state.with_name("state.json.lock")):
            state.write_text("first")
            child = subprocess.Popen(
                [sys.executable, "-c", _PROCESS_DRIVER, str(state), str(ready)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "the competing process never reached the lock"
            assert state.read_text() == "first", "the child entered without the lock"
        output, error = child.communicate(timeout=15)
        assert child.returncode == 0, (output, error)
        assert state.read_text() == "first,second"
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_shared_publication_lock_still_refuses_contention(tmp_path):
    path = tmp_path / "state.lock"
    with PublicationLock(path):
        with pytest.raises(LockUnavailable):
            with PublicationLock(path):
                pytest.fail("the shared non-blocking lock yielded without exclusion")


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EAGAIN, errno.EDEADLK])
def test_credential_lock_retries_only_acquisition(error_number, monkeypatch, tmp_path):
    calls = []

    class BusyOnce:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            calls.append("enter")
            if len(calls) == 1:
                raise LockUnavailable(self.path) from OSError(error_number, "busy")
            return self

        def __exit__(self, *exc):
            calls.append("exit")

    monkeypatch.setattr(locks, "PublicationLock", BusyOnce)
    with locks.state_lock(tmp_path / "state.json"):
        calls.append("body")
    assert calls == ["enter", "enter", "body", "exit"]


@pytest.mark.parametrize("cause", [OSError(errno.EIO, "disk fault"), ImportError("unsupported"), None])
def test_non_contention_failure_is_not_retried(cause, monkeypatch, tmp_path):
    failure = LockUnavailable(tmp_path / "state.json.lock", "unavailable")
    calls = []

    class Unavailable:
        def __init__(self, path):
            pass

        def __enter__(self):
            calls.append("enter")
            raise failure from cause

        def __exit__(self, *exc):
            pytest.fail("an acquisition failure has no acquired lock to release")

    monkeypatch.setattr(locks, "PublicationLock", Unavailable)
    with pytest.raises(LockUnavailable) as caught:
        with locks.state_lock(tmp_path / "state.json"):
            pytest.fail("an unavailable lock admitted a mutation")
    assert caught.value is failure
    assert calls == ["enter"]


def test_lock_failure_in_the_body_is_not_retried_and_releases_the_lock(tmp_path):
    state = tmp_path / "state.json"
    failure = LockUnavailable(state, "body failure")
    calls = []
    with pytest.raises(LockUnavailable) as caught:
        with locks.state_lock(state):
            calls.append("body")
            raise failure from OSError(errno.EAGAIN, "not acquisition")
    assert caught.value is failure
    assert calls == ["body"]
    with PublicationLock(state.with_name("state.json.lock")):
        pass


def test_contended_credential_lock_has_a_bounded_fail_closed_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "_LOCK_TIMEOUT_S", 0.03, raising=False)
    state = tmp_path / "state.json"
    started = time.monotonic()
    with PublicationLock(state.with_name("state.json.lock")):
        with pytest.raises(LockUnavailable) as caught:
            with locks.state_lock(state):
                pytest.fail("the timeout admitted an unlocked mutation")
    elapsed = time.monotonic() - started
    assert 0.025 <= elapsed < 2, elapsed
    assert isinstance(caught.value.__cause__, OSError)
    assert not state.exists()
