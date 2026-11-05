"""A machine probe is shared for a few seconds, never re-run by every caller of one turn.

Steve job 42c (5 Nov 2026) sampled a ~17 s chat turn on Windows and found 3.5 s of it in repeated
``probe_machine`` calls during prompt assembly: every call runs nvidia-smi and a PowerShell CIM query.
One served two-turn chat run counted 37 probes from a dozen callers (provider defaults, install
profiles, capability snapshot, routing budget). Hardware does not change within seconds, so callers
inside a short window share one probe, each gets its own copy, and a probe older than the window or
taken with different probe code (a patched detector) is never reused.
"""
from __future__ import annotations

import threading

import pytest

from core import hardware_tier as ht


@pytest.fixture()
def counted(monkeypatch):
    calls: list[int] = []
    clock = [1000.0]

    def _devices():
        calls.append(1)
        return ()

    monkeypatch.setattr(ht, "detect_gpu_devices", _devices)
    monkeypatch.setattr(ht, "_detect_ram_gb", lambda: 16.0)
    monkeypatch.setattr(ht, "_probe_clock", lambda: clock[0])
    ht.forget_machine_probe()
    yield calls, clock
    ht.forget_machine_probe()


def test_callers_within_the_window_share_one_probe(counted) -> None:
    calls, clock = counted

    first = ht.probe_machine()
    clock[0] += ht.MACHINE_PROBE_TTL_S / 2
    second = ht.probe_machine()

    assert len(calls) == 1
    assert first == second


def test_a_probe_older_than_the_window_is_taken_again(counted) -> None:
    calls, clock = counted

    ht.probe_machine()
    clock[0] += ht.MACHINE_PROBE_TTL_S + 0.01
    ht.probe_machine()

    assert len(calls) == 2


def test_each_caller_gets_its_own_copy(counted) -> None:
    first = ht.probe_machine()
    first.ram_gb = 1.0

    assert ht.probe_machine().ram_gb == 16.0


def test_different_probe_code_is_never_answered_from_the_window(counted, monkeypatch) -> None:
    calls, _clock = counted
    ht.probe_machine()

    monkeypatch.setattr(ht, "_detect_ram_gb", lambda: 64.0)

    assert ht.probe_machine().ram_gb == 64.0
    assert len(calls) == 2


def test_a_changed_probe_setting_is_never_answered_from_the_window(counted, monkeypatch) -> None:
    calls, _clock = counted
    ht.probe_machine()

    monkeypatch.setenv("VOOL_SKIP_TORCH_GPU_PROBE", "1")
    ht.probe_machine()

    assert len(calls) == 2


def test_concurrent_callers_wait_for_one_probe(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def _slow_devices():
        calls.append(1)
        started.set()
        release.wait(5)
        return ()

    monkeypatch.setattr(ht, "detect_gpu_devices", _slow_devices)
    monkeypatch.setattr(ht, "_detect_ram_gb", lambda: 16.0)
    ht.forget_machine_probe()
    try:
        results: list[ht.MachineProbe] = []
        threads = [threading.Thread(target=lambda: results.append(ht.probe_machine())) for _ in range(4)]
        threads[0].start()
        assert started.wait(5)
        for thread in threads[1:]:
            thread.start()
        release.set()
        for thread in threads:
            thread.join(5)

        assert len(results) == 4
        assert len(calls) == 1
    finally:
        ht.forget_machine_probe()


def test_a_failed_probe_is_not_kept(monkeypatch) -> None:
    attempts: list[int] = []

    def _broken():
        attempts.append(1)
        raise RuntimeError("detector broke")

    monkeypatch.setattr(ht, "detect_gpu_devices", _broken)
    monkeypatch.setattr(ht, "_detect_ram_gb", lambda: 16.0)
    ht.forget_machine_probe()
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                ht.probe_machine()
        assert len(attempts) == 2
    finally:
        ht.forget_machine_probe()


@pytest.mark.parametrize(
    "seam",
    ["_parse_compute_cap", "_cuda_device_is_legacy", "_console_encoding", "a_helper_added_later"],
)
def test_any_replaced_module_function_is_measured_afresh(counted, monkeypatch, seam) -> None:
    """Not a hand-picked list: every function of the module, including one added after this test."""
    calls, _clock = counted
    if seam == "a_helper_added_later":
        monkeypatch.setattr(ht, seam, lambda: None, raising=False)
        ht.probe_machine()
        monkeypatch.setattr(ht, seam, lambda: 1)
    else:
        ht.probe_machine()
        monkeypatch.setattr(ht, seam, lambda *a, **k: None)
    ht.probe_machine()

    assert len(calls) == 2


@pytest.mark.parametrize("target", ["platform.machine", "core.process_liveness.system_powershell"])
def test_a_replaced_outside_seam_is_measured_afresh(counted, monkeypatch, target) -> None:
    calls, _clock = counted
    ht.probe_machine()

    monkeypatch.setattr(target, lambda *a, **k: "replaced")
    ht.probe_machine()

    assert len(calls) == 2
