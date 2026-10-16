"""An incomplete recovery cannot enable checks or become a successful CLI result."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from core.updater import runtime, service
from core.updater.feed import FeedConfig
from core.updater.service import UserGesture
from core.updater.status import UpdatePhase
from core.updater.transaction import RecoveryResult
from core.updater.trust import TrustedPublishers
from installer import update_cli

from .helpers import generate_publisher_keypair

MANUAL_DETAIL = "Startup recovery is incomplete; manual recovery is required"


@pytest.fixture()
def subsystem(tmp_path, monkeypatch):
    _, public_hex = generate_publisher_keypair()
    owner = runtime.UpdateSubsystem(
        feed=FeedConfig(manifest_url="https://updates.example.invalid/m.json",
                        app_path=str(tmp_path / "test-app")),
        trust=TrustedPublishers(pinned_keys={"release-2026-01": public_hex}),
        data_dir=tmp_path / "data", installed_version="0.5.0", platform="macos-arm64",
    )
    calls = []
    monkeypatch.setattr(owner.service, "start", lambda: calls.append("start"))
    monkeypatch.setattr(owner.service, "check_once", lambda: calls.append("check"))
    return owner, calls


@pytest.mark.parametrize("failure", ["result", "mixed", "unknown", "exception"])
def test_unresolved_recovery_blocks_check_install_and_restart(subsystem, monkeypatch, failure):
    owner, calls = subsystem

    def recover(**kwargs):
        if failure == "exception":
            raise OSError("synthetic recovery failure")
        action = "recovery_failed" if failure == "result" else "unknown_recovery_action"
        if failure == "mixed":
            return [RecoveryResult("test-tx", "recovery_failed", MANUAL_DETAIL),
                    RecoveryResult("later-tx", "finalized")]
        return [RecoveryResult("test-tx", action, MANUAL_DETAIL)]

    monkeypatch.setattr(runtime, "recover_interrupted_update", recover)
    assert owner.start() is owner
    assert not owner.configured
    payload = owner.status_payload()
    assert payload["phase"] == "unavailable"
    assert payload["unavailable_reason"] == "recovery_failed"
    assert "manual" in payload["unavailable_plain"].lower()
    assert "manual" in payload["recovery_action"].lower()
    assert not owner.trigger_check()["check_ok"]
    gesture = UserGesture(pressed_at=1.0, origin="test")
    assert not owner.press_install(gesture).accepted
    assert not owner.press_restart(gesture).accepted
    assert owner.start() is owner
    assert calls == []

    # A late recovery worker must not remove the manual-recovery latch.
    owner.status.publish(UpdatePhase.DONE, version="0.6.0")
    assert owner.status_payload()["phase"] == "unavailable"
    assert owner.status_payload()["configured"] is False
    assert owner.status_payload()["unavailable_reason"] == "recovery_failed"
    assert owner.status_payload()["fault_code"] == "unexpected_failure"
    assert "manual" in owner.status_payload()["message"].lower()
    assert "manual" in owner.status_payload()["recovery_action"].lower()
    assert not owner.trigger_check()["check_ok"]
    assert calls == []


def test_bounded_timeout_keeps_a_late_worker_from_reenabling_updates(subsystem, monkeypatch):
    owner, calls = subsystem
    began = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    workers = []

    def blocked_recovery(**kwargs):
        workers.append(threading.current_thread())
        began.set()
        try:
            assert released.wait(timeout=5.0)
            owner.status.publish(UpdatePhase.DONE, version="0.6.0")
            return [RecoveryResult("test-tx", "finalized")]
        finally:
            finished.set()

    monkeypatch.setattr(runtime, "recover_interrupted_update", blocked_recovery)
    monkeypatch.setattr(runtime, "RECOVERY_BOUND_SECONDS", 0.05)
    try:
        started = time.monotonic()
        owner.start()
        assert time.monotonic() - started < 2.0
        assert began.wait(timeout=1.0)
        assert not owner.configured
        assert calls == []
    finally:
        released.set()
        assert finished.wait(timeout=3.0)
        workers[0].join(timeout=3.0)
        assert not workers[0].is_alive()
    payload = owner.status_payload()
    assert payload["phase"] == "unavailable"
    assert payload["unavailable_reason"] == "recovery_failed"
    assert payload["fault_code"] == "unexpected_failure"
    assert "manual" in payload["message"].lower()
    assert "manual" in payload["recovery_action"].lower()
    assert not owner.trigger_check()["check_ok"]
    assert calls == []


@pytest.mark.parametrize("action", ["aborted", "rolled_back", "finalized"])
def test_completed_recovery_still_starts_the_checker(subsystem, monkeypatch, action):
    owner, calls = subsystem
    monkeypatch.setattr(runtime, "recover_interrupted_update",
                        lambda **kwargs: [RecoveryResult("test-tx", action)])
    owner.start()
    assert owner.configured
    assert calls == ["start"]


@pytest.mark.parametrize("apply", [False, True])
def test_vool_cli_reports_latched_recovery_failure(subsystem, monkeypatch, capsys, apply):
    from apps.vool_cli import cmd_update

    owner, calls = subsystem
    monkeypatch.setattr(runtime, "recover_interrupted_update",
                        lambda **kwargs: [RecoveryResult("test-tx", "recovery_failed", MANUAL_DETAIL)])
    owner.start()
    monkeypatch.setattr(runtime, "boot_update_subsystem", lambda **kwargs: owner)
    monkeypatch.setattr(runtime, "get_update_subsystem", lambda: owner)
    assert cmd_update(apply=apply, json_mode=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "unavailable"
    assert "manual" in payload["detail"].lower()
    assert calls == []


@pytest.mark.parametrize("action", ["recovery_failed", "unknown_recovery_action", "left_running"])
def test_recovery_cli_rejects_incomplete_or_unknown_actions(tmp_path, monkeypatch, capsys, action):
    monkeypatch.setattr(update_cli, "recover_interrupted_update",
                        lambda **kwargs: [RecoveryResult("test-tx", action, MANUAL_DETAIL)])
    args = SimpleNamespace(data_dir=str(tmp_path / "data"), app_path=str(tmp_path / "test-app"),
                           health_url="http://127.0.0.1:1/healthz", channel="stable", pid_file="")
    assert update_cli.cmd_recover(args) == 1
    assert MANUAL_DETAIL in capsys.readouterr().out


@pytest.mark.parametrize("action", ["aborted", "rolled_back", "finalized"])
def test_recovery_cli_retains_completed_actions(tmp_path, monkeypatch, capsys, action):
    monkeypatch.setattr(update_cli, "recover_interrupted_update",
                        lambda **kwargs: [RecoveryResult("test-tx", action)])
    args = SimpleNamespace(data_dir=str(tmp_path / "data"), app_path=str(tmp_path / "test-app"),
                           health_url="http://127.0.0.1:1/healthz", channel="stable", pid_file="")
    assert update_cli.cmd_recover(args) == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("failure_position", [0, 1, 2])
def test_recovery_cli_checks_every_result(tmp_path, monkeypatch, capsys, failure_position):
    results = [RecoveryResult(str(index), "finalized") for index in range(3)]
    results[failure_position] = RecoveryResult("failed-tx", "recovery_failed", MANUAL_DETAIL)
    monkeypatch.setattr(update_cli, "recover_interrupted_update", lambda **kwargs: results)
    args = SimpleNamespace(data_dir=str(tmp_path / "data"), app_path=str(tmp_path / "test-app"),
                           health_url="http://127.0.0.1:1/healthz", channel="stable", pid_file="")
    assert update_cli.cmd_recover(args) == 1
    assert MANUAL_DETAIL in capsys.readouterr().out


@pytest.mark.parametrize("failure_position", [0, 1, 2])
def test_exported_startup_hook_checks_every_result(subsystem, monkeypatch, failure_position):
    owner, calls = subsystem
    results = [RecoveryResult(str(index), "finalized") for index in range(3)]
    results[failure_position] = RecoveryResult("failed-tx", "recovery_failed", MANUAL_DETAIL)
    monkeypatch.setattr(service, "recover_interrupted_update", lambda **kwargs: results)
    recovered = service.startup_recover_and_start(
        service=owner.service, data_dir=owner.data_dir, app_path=owner.data_dir / "test-app",
        helper=SimpleNamespace(), health_probe=lambda **kwargs: None,
        installed_version="0.5.0", platform="macos-arm64",
    )
    assert recovered == results
    assert calls == []


@pytest.mark.parametrize("failure", ["result", "unknown", "exception"])
def test_exported_startup_hook_does_not_start_checks_after_failure(subsystem, monkeypatch, failure):
    owner, calls = subsystem

    def recover(**kwargs):
        if failure == "exception":
            raise OSError("synthetic recovery failure")
        action = "unknown_recovery_action" if failure == "unknown" else "recovery_failed"
        return [RecoveryResult("test-tx", action, MANUAL_DETAIL)]

    monkeypatch.setattr(service, "recover_interrupted_update", recover)
    results = service.startup_recover_and_start(
        service=owner.service, data_dir=owner.data_dir, app_path=owner.data_dir / "test-app",
        helper=SimpleNamespace(), health_probe=lambda **kwargs: None,
        installed_version="0.5.0", platform="macos-arm64",
    )
    assert [item.action for item in results] == (["unknown_recovery_action"] if failure == "unknown" else ["recovery_failed"])
    assert calls == []


@pytest.mark.parametrize("action", ["aborted", "rolled_back", "finalized"])
def test_exported_startup_hook_retains_completed_recovery(subsystem, monkeypatch, action):
    owner, calls = subsystem
    monkeypatch.setattr(service, "recover_interrupted_update",
                        lambda **kwargs: [RecoveryResult("test-tx", action)])
    results = service.startup_recover_and_start(
        service=owner.service, data_dir=owner.data_dir, app_path=owner.data_dir / "test-app",
        helper=SimpleNamespace(), health_probe=lambda **kwargs: None,
        installed_version="0.5.0", platform="macos-arm64",
    )
    assert [item.action for item in results] == [action]
    assert calls == ["start"]
