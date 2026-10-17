"""Focused cleanup checks for the real first-run pact writer storm."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests.first_run_pact_rig import pact_rig
from tests.test_first_run_pact_restart_multiplicity import _WriterCleanupError
from tests.test_first_run_pact_restart_multiplicity import (
    test_sigkill_between_writes_never_leaves_a_half_written_file as _existing_torn_write_test,
)

_REAL_EVENT_SET = threading.Event.set
_REAL_THREAD_START = threading.Thread.start
_REAL_THREAD_JOIN = threading.Thread.join
_REAL_THREAD_IS_ALIVE = threading.Thread.is_alive
_REAL_PATH_READ_BYTES = Path.read_bytes
_STORM_QUALNAME = f"{_existing_torn_write_test.__qualname__}.<locals>.storm"


def _capture_storm_stop(thread, stop_events):
    target = getattr(thread, "_target", None)
    if getattr(target, "__qualname__", None) != _STORM_QUALNAME:
        return
    closure = dict(zip(target.__code__.co_freevars, target.__closure__ or (), strict=True))
    stop_event = closure["stop"].cell_contents
    if stop_event not in stop_events:
        stop_events.append(stop_event)


def _observe_writer_lifecycle(monkeypatch):
    stop_events = []
    start_attempts = []
    started = []
    join_attempts = []

    def start(thread, *args, **kwargs):
        start_attempts.append(thread)
        _capture_storm_stop(thread, stop_events)
        _REAL_THREAD_START(thread, *args, **kwargs)
        started.append(thread)

    def join(thread, *args, **kwargs):
        if thread in started:
            timeout = kwargs.get("timeout", args[0] if args else None)
            join_attempts.append((thread, timeout))
        return _REAL_THREAD_JOIN(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(threading.Thread, "join", join)
    return stop_events, start_attempts, started, join_attempts


def _rescue_threads(stop_events, start_attempts):
    """Clean up even when the unfixed target returns before its own cleanup."""
    for stop_event in stop_events:
        _REAL_EVENT_SET(stop_event)
    for thread in start_attempts:
        if thread.ident is not None:
            _REAL_THREAD_JOIN(thread, timeout=5)
    leaked = [thread for thread in start_attempts if _REAL_THREAD_IS_ALIVE(thread)]
    assert not leaked, f"writer threads leaked during test rescue: {leaked!r}"


def _leaf_exceptions(error):
    if isinstance(error, _WriterCleanupError):
        if error.__cause__ is not None:
            yield from _leaf_exceptions(error.__cause__)
        for nested in error.errors:
            yield from _leaf_exceptions(nested)
    else:
        yield error


def test_existing_torn_write_test_preserves_main_read_failure_and_stops_all_writers(pact_rig, monkeypatch):
    stop_events, start_attempts, started, join_attempts = _observe_writer_lifecycle(monkeypatch)
    read_error = PermissionError("injected main pact read failure")
    main_ident = threading.get_ident()
    observed_main_reads = []

    def fail_main_read(path, *args, **kwargs):
        if path.name == "first_run_pact_state.json" and threading.get_ident() == main_ident:
            observed_main_reads.append(path)
            raise read_error
        return _REAL_PATH_READ_BYTES(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", fail_main_read)
    try:
        with pytest.raises(PermissionError) as caught:
            _existing_torn_write_test(pact_rig)
        assert caught.value is read_error
        assert observed_main_reads
        assert len(started) == 3
        assert all(not _REAL_THREAD_IS_ALIVE(thread) for thread in started)
        assert len(stop_events) == 1 and stop_events[0].is_set()
        assert len(join_attempts) == len(started) == 3
        assert all(timeout == 5 for _, timeout in join_attempts)
    finally:
        _rescue_threads(stop_events, start_attempts)


def test_existing_torn_write_test_preserves_third_start_failure(pact_rig, monkeypatch):
    stop_events, start_attempts, started, join_attempts = _observe_writer_lifecycle(monkeypatch)
    start_error = RuntimeError("injected third writer start failure")

    def fail_third_start(thread, *args, **kwargs):
        start_attempts.append(thread)
        _capture_storm_stop(thread, stop_events)
        if len(start_attempts) == 3:
            raise start_error
        _REAL_THREAD_START(thread, *args, **kwargs)
        started.append(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_third_start)
    try:
        with pytest.raises(RuntimeError) as caught:
            _existing_torn_write_test(pact_rig)
        assert caught.value is start_error
        assert len(start_attempts) == 3
        assert len(started) == 2
        assert start_attempts[:2] == started
        assert start_attempts[2].ident is None
        assert not _REAL_THREAD_IS_ALIVE(start_attempts[2])
        assert all(not _REAL_THREAD_IS_ALIVE(thread) for thread in started)
        assert len(stop_events) == 1 and stop_events[0].is_set()
        assert len(join_attempts) == len(started) == 2
        assert all(timeout == 5 for _, timeout in join_attempts)
    finally:
        _rescue_threads(stop_events, start_attempts)


def test_existing_torn_write_test_groups_read_and_join_failures_without_leaks(pact_rig, monkeypatch):
    stop_events, start_attempts, started, join_attempts = _observe_writer_lifecycle(monkeypatch)
    read_error = PermissionError("injected main pact read failure")
    join_error = RuntimeError("injected writer join failure")
    main_ident = threading.get_ident()

    def fail_main_read(path, *args, **kwargs):
        if path.name == "first_run_pact_state.json" and threading.get_ident() == main_ident:
            raise read_error
        return _REAL_PATH_READ_BYTES(path, *args, **kwargs)

    def fail_one_join(thread, *args, **kwargs):
        if thread in started:
            timeout = kwargs.get("timeout", args[0] if args else None)
            join_attempts.append((thread, timeout))
            if thread is started[0]:
                raise join_error
        return _REAL_THREAD_JOIN(thread, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", fail_main_read)
    monkeypatch.setattr(threading.Thread, "join", fail_one_join)
    caught = None
    try:
        try:
            _existing_torn_write_test(pact_rig)
        except BaseException as error:
            caught = error
        stop_flags_before_rescue = [event.is_set() for event in stop_events]
        live_unfaulted_writers = [thread for thread in started[1:] if _REAL_THREAD_IS_ALIVE(thread)]
    finally:
        _rescue_threads(stop_events, start_attempts)

    assert stop_flags_before_rescue == [True]
    assert not live_unfaulted_writers
    assert caught is not None
    assert isinstance(caught, _WriterCleanupError)
    leaves = list(_leaf_exceptions(caught))
    assert any(error is read_error for error in leaves)
    assert any(error is join_error for error in leaves)
    assert len(join_attempts) == len(started) == 3
    assert all(timeout == 5 for _, timeout in join_attempts)
    assert all(thread in [item[0] for item in join_attempts] for thread in started)


def test_existing_torn_write_test_reports_bounded_join_timeout_and_attempts_every_join(pact_rig, monkeypatch):
    stop_events, start_attempts, started, join_attempts = _observe_writer_lifecycle(monkeypatch)

    def bounded_join(thread, *args, **kwargs):
        if thread in started:
            timeout = kwargs.get("timeout", args[0] if args else None)
            join_attempts.append((thread, timeout))
            if thread is started[0]:
                return None
        return _REAL_THREAD_JOIN(thread, *args, **kwargs)

    def bounded_is_alive(thread):
        if started and thread is started[0]:
            return True
        return _REAL_THREAD_IS_ALIVE(thread)

    monkeypatch.setattr(threading.Thread, "join", bounded_join)
    monkeypatch.setattr(threading.Thread, "is_alive", bounded_is_alive)
    caught = None
    try:
        try:
            _existing_torn_write_test(pact_rig)
        except BaseException as error:
            caught = error
        stop_flags_before_rescue = [event.is_set() for event in stop_events]
        live_unfaulted_writers = [thread for thread in started[1:] if _REAL_THREAD_IS_ALIVE(thread)]
    finally:
        _rescue_threads(stop_events, start_attempts)

    assert stop_flags_before_rescue == [True]
    assert not live_unfaulted_writers
    assert caught is not None
    assert any(isinstance(error, TimeoutError) for error in _leaf_exceptions(caught))
    assert len(join_attempts) == len(started) == 3
    assert all(timeout == 5 for _, timeout in join_attempts)
    assert all(thread in [item[0] for item in join_attempts] for thread in started)
