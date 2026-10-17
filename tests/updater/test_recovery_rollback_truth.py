"""Crash recovery must prove restoration and restart before claiming rollback."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.updater import transaction
from core.updater.macos import MacOSBundleInstaller
from core.updater.platforms import SwapOutcome
from core.updater.status import StatusStore, UpdateFault, UpdatePhase
from core.updater.transaction import InProcessHelper, SimulatedCrashError, Step, UpdateJournal

from .test_transaction_flow import harness as harness


def _raise_failure(*args, **kwargs):
    raise OSError("synthetic recovery failure")


@pytest.fixture()
def interrupted(harness, monkeypatch):
    (harness.data_dir / "data_schema.json").write_text('{"schema_version":"0.5.0"}', encoding="utf-8")
    flow = harness.make_flow(crash_after=Step.SWAPPED)
    with pytest.raises(SimulatedCrashError):
        flow.run(harness.raw_manifest, harness.manifest, harness.decision)
    journal = flow._journal
    prior = Path(journal.payload_of(Step.SWAPPED)["prior_path"])
    harness.health_forced_fail = True
    settings = harness.data_dir / "config" / "settings.json"
    settings.write_text(json.dumps({"setting": "new-settings"}), encoding="utf-8")
    calls = []
    helper = InProcessHelper(
        shutdown=lambda: calls.append("shutdown"),
        stop_waiter=lambda timeout: (calls.append("wait"), True)[1],
        relauncher=lambda path: (calls.append("relaunch"), True)[1],
    )
    real_restore = MacOSBundleInstaller.restore_prior

    def restore(installer, app, previous, *, txid):
        calls.append("restore")
        return real_restore(installer, app, previous, txid=txid)

    monkeypatch.setattr(MacOSBundleInstaller, "restore_prior", restore)

    def recover(**overrides):
        params = dict(data_dir=harness.data_dir, app_path=harness.app_path,
                      helper=helper, health_probe=harness.health,
                      installed_version="0.5.0", platform="macos-arm64")
        params.update(overrides)
        return transaction.recover_interrupted_update(**params)

    return SimpleNamespace(box=harness, journal=journal, prior=prior, calls=calls,
                           helper=helper, recover=recover, restore=restore,
                           snapshot=journal.dir / "config-snapshot")


def _incomplete(rig, results):
    assert len(results) == 1
    assert results[0].action == "recovery_failed"
    assert not rig.journal.is_terminal()
    assert not rig.journal.has(Step.ROLLED_BACK)
    status = StatusStore(rig.box.data_dir / "update_v2" / "status.json").load()
    assert status.phase is UpdatePhase.UNAVAILABLE
    assert status.fault_code == UpdateFault.UNEXPECTED.value
    assert "manual" in status.recovery_action.lower()
    assert "put back and restarted" not in status.message
    assert "left as it was" not in status.message
    assert "running again" not in status.recovery_action


@pytest.mark.parametrize("platform", ["windows-x64", "linux-x64"])
def test_missing_installer_cannot_claim_rollback(interrupted, platform):
    rig = interrupted
    _incomplete(rig, rig.recover(platform=platform))
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert rig.prior.is_dir()
    assert (rig.snapshot / "config" / "settings.json").is_file()
    assert rig.calls == []


def test_missing_prior_cannot_touch_app_or_settings(interrupted):
    rig = interrupted
    retained = rig.prior.with_name(rig.prior.name + "-retained")
    rig.prior.rename(retained)
    _incomplete(rig, rig.recover())
    assert retained.is_dir()
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert (rig.snapshot / "config" / "settings.json").is_file()
    assert rig.calls == []


@pytest.mark.parametrize("missing", ["directory", "member"])
def test_missing_snapshot_cannot_claim_settings_restored(interrupted, missing):
    rig = interrupted
    source = rig.snapshot if missing == "directory" else rig.snapshot / "config"
    retained = source.with_name(source.name + "-retained")
    source.rename(retained)
    _incomplete(rig, rig.recover())
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert retained.is_dir()
    assert rig.prior.is_dir()
    assert rig.calls == []


def test_legacy_snapshot_without_membership_metadata_requires_manual_recovery(interrupted):
    rig = interrupted
    entries = [json.loads(line) for line in rig.journal.path.read_text(encoding="utf-8").splitlines()]
    for entry in entries:
        if entry["step"] == Step.SNAPSHOT_COMPLETE.value:
            entry.pop("saved_paths", None)
    rig.journal.path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    _incomplete(rig, rig.recover())
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert rig.prior.is_dir()
    assert rig.calls == []


def test_failed_recovery_does_not_advance_another_transaction(interrupted):
    rig = interrupted
    later = UpdateJournal(rig.journal.dir.with_name(rig.journal.dir.name + "-later"))
    later.append(Step.CREATED)
    later.append(Step.DECIDED, rig.journal.payload_of(Step.DECIDED))
    later.append(Step.SWAPPED, rig.journal.payload_of(Step.SWAPPED))
    probes = []

    def unhealthy(**kwargs):
        probes.append(kwargs)
        return rig.box.health(**kwargs)

    _incomplete(rig, rig.recover(platform="windows-x64", health_probe=unhealthy))
    assert len(probes) == 1
    assert later.last_step() is Step.SWAPPED
    assert not later.is_terminal()
    assert rig.calls == []
    assert rig.box.marker() == "new"


@pytest.mark.parametrize("failure", ["shutdown", "wait_false", "wait_raises"])
def test_failed_stop_cannot_restore_or_finalize(interrupted, failure):
    rig = interrupted
    if failure == "shutdown":
        rig.helper.shutdown = _raise_failure
    else:
        rig.helper.stop_waiter = (lambda timeout: False) if failure == "wait_false" else _raise_failure
    _incomplete(rig, rig.recover())
    assert "restore" not in rig.calls
    assert "relaunch" not in rig.calls
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert rig.prior.is_dir()


@pytest.mark.parametrize("failure", ["false", "raises"])
def test_failed_restore_is_not_terminal_and_can_retry(interrupted, monkeypatch, failure):
    rig = interrupted

    def failing_restore(*args, **kwargs):
        rig.calls.append("failed_restore")
        if failure == "raises":
            _raise_failure()
        return SwapOutcome(False, detail="synthetic restore refusal")

    monkeypatch.setattr(MacOSBundleInstaller, "restore_prior", failing_restore)
    _incomplete(rig, rig.recover())
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert rig.prior.is_dir()
    assert (rig.snapshot / "config" / "settings.json").is_file()
    assert rig.calls == ["shutdown", "wait", "failed_restore"]

    monkeypatch.setattr(MacOSBundleInstaller, "restore_prior", rig.restore)
    results = rig.recover()
    assert [result.action for result in results] == ["rolled_back"]
    assert rig.box.marker() == "old"
    assert rig.box.config()["setting"] == "original"
    assert rig.calls.count("restore") == 1
    assert rig.calls.count("relaunch") == 1
    assert sum(entry["step"] is Step.ROLLED_BACK for entry in rig.journal.entries()) == 1
    before = list(rig.calls)
    assert rig.recover() == []
    assert rig.calls == before


@pytest.mark.parametrize("failure", ["missing", "false", "raises"])
def test_delegated_stop_never_invokes_swap_shutdown(interrupted, failure):
    rig = interrupted
    rig.helper.performs_swap = True
    if failure != "missing":
        rig.helper.stop_running_app = (lambda **kwargs: False) if failure == "false" else _raise_failure
    _incomplete(rig, rig.recover())
    assert rig.calls == []
    assert rig.box.marker() == "new"
    assert rig.box.config()["setting"] == "new-settings"
    assert rig.prior.is_dir()


def test_successful_delegated_recovery_uses_only_plain_stop(interrupted):
    rig = interrupted
    rig.helper.performs_swap = True

    def stop(**kwargs):
        rig.calls.append("plain_stop")
        return True

    rig.helper.stop_running_app = stop
    results = rig.recover()
    assert [result.action for result in results] == ["rolled_back"]
    assert rig.calls == ["plain_stop", "restore", "relaunch"]
    assert rig.box.marker() == "old"
    assert rig.box.config()["setting"] == "original"
    assert sum(entry["step"] is Step.ROLLED_BACK for entry in rig.journal.entries()) == 1


def test_partial_settings_restore_is_preserved_on_retry(interrupted, monkeypatch):
    rig = interrupted
    real_restore_tree = transaction.restore_tree
    remaining = rig.snapshot / "data_schema.json"
    assert remaining.read_text(encoding="utf-8") == '{"schema_version":"0.5.0"}'
    restores = []

    def partial_restore(snapshot, destinations):
        restores.append(snapshot)
        real_restore_tree(snapshot, destinations[:1])
        _raise_failure()

    monkeypatch.setattr(transaction, "restore_tree", partial_restore)
    _incomplete(rig, rig.recover())
    assert rig.box.marker() == "old"
    assert rig.box.config()["setting"] == "original"
    assert not rig.prior.exists()
    assert remaining.read_text(encoding="utf-8") == '{"schema_version":"0.5.0"}'
    assert "relaunch" not in rig.calls
    before = list(rig.calls)
    _incomplete(rig, rig.recover())
    assert rig.box.config()["setting"] == "original"
    assert remaining.read_text(encoding="utf-8") == '{"schema_version":"0.5.0"}'
    assert rig.calls == before
    assert len(restores) == 1


@pytest.mark.parametrize("failure", ["false", "raises"])
def test_failed_relaunch_cannot_claim_rollback_or_reconsume_config(interrupted, monkeypatch, failure):
    rig = interrupted
    real_restore_tree = transaction.restore_tree
    restores = []

    def restore(snapshot, destinations):
        restores.append(snapshot)
        real_restore_tree(snapshot, destinations)

    def relaunch(path):
        rig.calls.append("failed_relaunch")
        if failure == "raises":
            _raise_failure()
        return False

    monkeypatch.setattr(transaction, "restore_tree", restore)
    rig.helper.relauncher = relaunch
    _incomplete(rig, rig.recover())
    assert rig.box.marker() == "old"
    assert rig.box.config()["setting"] == "original"
    assert not rig.prior.exists()
    assert rig.calls.count("failed_relaunch") == 1
    before = list(rig.calls)
    _incomplete(rig, rig.recover())
    assert rig.box.config()["setting"] == "original"
    assert rig.calls == before
    assert len(restores) == 1


def test_successful_recovery_is_terminal_once(interrupted):
    rig = interrupted
    results = rig.recover(installed_version="9.9.9")
    assert [result.action for result in results] == ["rolled_back"]
    assert rig.box.marker() == "old"
    assert rig.box.config()["setting"] == "original"
    assert rig.calls == ["shutdown", "wait", "restore", "relaunch"]
    assert sum(entry["step"] is Step.ROLLED_BACK for entry in rig.journal.entries()) == 1
    status = StatusStore(rig.box.data_dir / "update_v2" / "status.json").load()
    assert status.phase is UpdatePhase.ROLLED_BACK
    assert "0.5.0" in status.message
    assert rig.recover() == []
