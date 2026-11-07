"""Windows one-press updater: payload shape, in-folder journaled swap, process helper, wiring.

Runs on every lane. The swap uses real directories and real renames; process work goes
through injected tables so no real process is touched.
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.updater import windows as win
from core.updater.decision import UpdateDecisionReason, decide_update
from core.updater.download import StagedDownloader
from core.updater.health import HealthResult
from core.updater.manifest import parse_and_verify_manifest
from core.updater.migrations import MigrationCatalog
from core.updater.platforms import installer_for, supported_platforms
from core.updater.status import StatusStore
from core.updater.transaction import InProcessHelper, Step, UpdateFlow, UpdateJournal, recover_interrupted_update
from core.updater.trust import TrustedPublishers
from core.updater.windows import MANAGED_ENTRIES, WindowsBundleInstaller, WindowsProcessHelper
from core.updater.work import WorkCoordinator

from .helpers import build_manifest, generate_publisher_keypair, manifest_bytes

COMMIT = "a" * 40


def make_bundle(root: Path, *, version: str, marker: str, commit: str = COMMIT, dirty: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative in win._REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker}:{relative}", encoding="utf-8")
    for name in MANAGED_ENTRIES:
        path = root / name
        if name in win._MANAGED_DIRECTORIES:
            path.mkdir(exist_ok=True)
        elif name == "bundle_manifest.json":
            # PowerShell writes it with a BOM.
            path.write_text("\ufeff" + json.dumps({
                "schema": "vool.bundle_manifest.v1", "app_version": version,
                "source_commit": commit, "source_dirty_state": dirty,
            }), encoding="utf-8")
        else:
            path.write_text(f"{marker}:{name}", encoding="utf-8")
    (root / "app" / "marker.txt").write_text(marker, encoding="utf-8")
    return root


def make_install(tmp_path: Path, *, version: str = "0.7.1") -> Path:
    app = make_bundle(tmp_path / "Programs" / "VOOL", version=version, marker="old")
    (app / "unins000.exe").write_text("uninstaller", encoding="utf-8")
    (app / "unins000.dat").write_text("uninstall log", encoding="utf-8")
    return app


def zip_bundle(bundle: Path, output: Path) -> bytes:
    with zipfile.ZipFile(output, "w") as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, "VOOL/" + path.relative_to(bundle).as_posix())
    return output.read_bytes()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_windows_x64_has_a_real_installer():
    installer = installer_for("windows-x64")
    assert isinstance(installer, WindowsBundleInstaller)
    assert "windows-x64" in supported_platforms()
    assert installer_for("windows-arm64") is None


# --------------------------------------------------------------------------- #
# Payload verification
# --------------------------------------------------------------------------- #

class TestVerifyBundle:
    def test_complete_payload_is_accepted_and_says_it_is_not_authenticode_checked(self, tmp_path):
        result = WindowsBundleInstaller().verify_bundle(make_bundle(tmp_path / "p", version="0.7.2", marker="new"))
        assert result.ok, result.detail
        assert result.codesigned is False
        assert "Authenticode" in result.detail

    @pytest.mark.parametrize("name", ["python", "app", "vool.vbs", "bundle_manifest.json"])
    def test_missing_entry_is_refused(self, tmp_path, name):
        bundle = make_bundle(tmp_path / "p", version="0.7.2", marker="new")
        target = bundle / name
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()
        assert not WindowsBundleInstaller().verify_bundle(bundle).ok

    @pytest.mark.parametrize("name", ["models", "unins000.exe", "update_backups", "Python"])
    def test_extra_or_miscased_top_level_entry_is_refused(self, tmp_path, name):
        bundle = make_bundle(tmp_path / "p", version="0.7.2", marker="new")
        if name == "Python":
            os.rename(bundle / "python", bundle / "Python")
        else:
            (bundle / name).mkdir()
        result = WindowsBundleInstaller().verify_bundle(bundle)
        assert not result.ok
        assert name in result.detail

    def test_missing_required_file_is_refused(self, tmp_path):
        bundle = make_bundle(tmp_path / "p", version="0.7.2", marker="new")
        (bundle / "python" / "pythonw.exe").unlink()
        result = WindowsBundleInstaller().verify_bundle(bundle)
        assert not result.ok and "pythonw.exe" in result.detail

    def test_link_inside_payload_is_refused(self, tmp_path):
        bundle = make_bundle(tmp_path / "p", version="0.7.2", marker="new")
        outside = tmp_path / "outside.txt"
        outside.write_text("user data", encoding="utf-8")
        try:
            (bundle / "app" / "escape.txt").symlink_to(outside)
        except OSError:
            pytest.skip("this account cannot create symlinks")
        result = WindowsBundleInstaller().verify_bundle(bundle)
        assert not result.ok and "link" in result.detail

    def test_wrong_bundle_manifest_schema_is_refused(self, tmp_path):
        bundle = make_bundle(tmp_path / "p", version="0.7.2", marker="new")
        (bundle / "bundle_manifest.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
        assert not WindowsBundleInstaller().verify_bundle(bundle).ok

    def test_payload_identity(self, tmp_path):
        bundle = make_bundle(tmp_path / "p", version="0.7.2", marker="new")
        assert win.payload_identity_problem(bundle, version="0.7.2", build_commit=COMMIT) == ""
        assert "build commit" in win.payload_identity_problem(bundle, version="0.7.2")
        assert "0.7.3" in win.payload_identity_problem(bundle, version="0.7.3", build_commit=COMMIT)
        assert "commit" in win.payload_identity_problem(bundle, version="0.7.2", build_commit="b" * 40)
        dirty = make_bundle(tmp_path / "d", version="0.7.2", marker="new", dirty=True)
        assert "clean" in win.payload_identity_problem(dirty, version="0.7.2", build_commit=COMMIT)


# --------------------------------------------------------------------------- #
# Swap, restore, prune
# --------------------------------------------------------------------------- #

class TestSwap:
    def test_staging_and_backups_stay_inside_the_program_folder(self, tmp_path):
        app = make_install(tmp_path)
        installer = WindowsBundleInstaller()
        assert installer.staging_root(app).parent == app
        assert installer.prior_path(app, "tx1").is_relative_to(app)

    def test_swap_replaces_only_managed_entries_and_keeps_a_runnable_prior(self, tmp_path):
        app = make_install(tmp_path)
        staged = make_bundle(app / ".vool-update" / "stage", version="0.7.2", marker="new")
        installer = WindowsBundleInstaller()
        outcome = installer.atomic_swap(staged, app, txid="tx1")
        assert outcome.ok, outcome.detail
        assert (app / "app" / "marker.txt").read_text() == "new"
        assert (app / "unins000.exe").read_text() == "uninstaller"
        assert (app / "unins000.dat").read_text() == "uninstall log"
        prior = outcome.prior_path
        assert prior == app / "update_backups" / "windows-tx1"
        assert (prior / "app" / "marker.txt").read_text() == "old"
        assert installer.verify_bundle(prior).ok is False  # holds its journal too
        assert sorted(json.loads((prior / "_replaced.json").read_text())) == sorted(MANAGED_ENTRIES)

        restored = installer.restore_prior(app, prior, txid="tx1")
        assert restored.ok, restored.detail
        assert (app / "app" / "marker.txt").read_text() == "old"
        assert (app / "unins000.exe").read_text() == "uninstaller"

    def test_a_refused_rename_leaves_the_old_version_in_place(self, tmp_path, monkeypatch):
        app = make_install(tmp_path)
        staged = make_bundle(app / ".vool-update" / "stage", version="0.7.2", marker="new")
        real_rename = os.rename

        def rename(source, destination):
            if Path(source) == app / "python":
                raise PermissionError(32, "The process cannot access the file because it is being used")
            return real_rename(source, destination)

        monkeypatch.setattr(win.os, "rename", rename)
        outcome = WindowsBundleInstaller().atomic_swap(staged, app, txid="tx1")
        assert not outcome.ok
        assert "being used" in outcome.detail
        assert (app / "app" / "marker.txt").read_text() == "old"
        assert all((app / name).exists() for name in MANAGED_ENTRIES)
        assert not (app / "update_backups" / "windows-tx1").exists()

    def test_swap_refuses_a_folder_that_is_not_an_installed_bundle(self, tmp_path):
        target = tmp_path / "Documents"
        target.mkdir()
        (target / "notes.txt").write_text("mine", encoding="utf-8")
        staged = make_bundle(tmp_path / "stage", version="0.7.2", marker="new")
        outcome = WindowsBundleInstaller().atomic_swap(staged, target, txid="tx1")
        assert not outcome.ok
        assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]

    def test_interrupted_swap_is_undone_from_its_journal(self, tmp_path, monkeypatch):
        app = make_install(tmp_path)
        staged = make_bundle(app / ".vool-update" / "stage", version="0.7.2", marker="new")
        real_rename = os.rename
        moves = []

        def rename(source, destination):
            moves.append(source)
            if len(moves) == 7:
                raise KeyboardInterrupt  # the process died mid-swap
            return real_rename(source, destination)

        monkeypatch.setattr(win.os, "rename", rename)
        with pytest.raises(KeyboardInterrupt):
            WindowsBundleInstaller().atomic_swap(staged, app, txid="tx1")
        monkeypatch.setattr(win.os, "rename", real_rename)
        restored = WindowsBundleInstaller().restore_prior(app, app / "update_backups" / "windows-tx1", txid="tx1")
        assert restored.ok, restored.detail
        assert (app / "app" / "marker.txt").read_text() == "old"
        assert all((app / name).exists() for name in MANAGED_ENTRIES)

    def _swapped(self, tmp_path):
        app = make_install(tmp_path)
        staged = make_bundle(app / ".vool-update" / "stage", version="0.7.2", marker="new")
        outcome = WindowsBundleInstaller().atomic_swap(staged, app, txid="tx1")
        assert outcome.ok, outcome.detail
        return app, outcome.prior_path

    def test_restore_never_deletes_in_place_and_rides_out_a_brief_lock(self, tmp_path, monkeypatch):
        app, prior = self._swapped(tmp_path)
        monkeypatch.setattr(win, "RENAME_RETRY_SECONDS", 0)
        real_rename, real_rmtree = os.rename, win.shutil.rmtree
        refusals = {"left": 2}

        def rename(source, destination):
            if Path(source) == app / "python" and refusals["left"]:
                refusals["left"] -= 1
                raise PermissionError(32, "in use by a scanner")
            return real_rename(source, destination)

        def rmtree(path, *args, **kwargs):
            assert Path(path).is_relative_to(prior / "_discarded"), f"deleted in place: {path}"
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(win.os, "rename", rename)
        monkeypatch.setattr(win.shutil, "rmtree", rmtree)
        outcome = WindowsBundleInstaller().restore_prior(app, prior, txid="tx1")
        assert outcome.ok, outcome.detail
        assert refusals["left"] == 0
        assert (app / "app" / "marker.txt").read_text() == "old"
        assert (app / "python" / "python.exe").read_text() == "old:python/python.exe"
        assert not (prior / "_discarded").exists()

    def test_a_lasting_lock_leaves_every_entry_whole_and_a_replay_finishes(self, tmp_path, monkeypatch):
        app, prior = self._swapped(tmp_path)
        monkeypatch.setattr(win, "RENAME_RETRY_SECONDS", 0)
        real_rename = os.rename
        locked = {"on": True}

        def rename(source, destination):
            if Path(source) == app / "python" and locked["on"]:
                raise PermissionError(32, "held open")
            return real_rename(source, destination)

        monkeypatch.setattr(win.os, "rename", rename)
        outcome = WindowsBundleInstaller().restore_prior(app, prior, txid="tx1")
        assert not outcome.ok and "held open" in outcome.detail
        # Nothing half-deleted: the locked entry is still the complete new one.
        assert (app / "python" / "python.exe").read_text() == "new:python/python.exe"
        assert (app / "python" / "pythonw.exe").read_text() == "new:python/pythonw.exe"
        locked["on"] = False
        assert WindowsBundleInstaller().restore_prior(app, prior, txid="tx1").ok
        for name in MANAGED_ENTRIES:
            assert (app / name).exists(), name
        assert (app / "app" / "marker.txt").read_text() == "old"
        assert (app / "python" / "python.exe").read_text() == "old:python/python.exe"

    def test_restoring_an_unstarted_swap_changes_nothing(self, tmp_path):
        app = make_install(tmp_path)
        prior = app / "update_backups" / "windows-tx1"
        prior.mkdir(parents=True)
        assert WindowsBundleInstaller().restore_prior(app, prior, txid="tx1").ok
        assert (app / "app" / "marker.txt").read_text() == "old"

    def test_prune_keeps_the_newest_complete_priors_and_drops_spent_journals(self, tmp_path):
        app = make_install(tmp_path)
        root = app / "update_backups"
        for index in range(4):
            prior = make_bundle(root / f"windows-tx{index}", version="0.7.0", marker=str(index))
            os.utime(prior, (1000 + index, 1000 + index))
        spent = root / "windows-spent"
        spent.mkdir()
        (spent / "_replaced.json").write_text("[]", encoding="utf-8")
        linux_style = root / "signed-keep"
        linux_style.mkdir()
        removed = WindowsBundleInstaller().prune_priors(app, keep=2)
        assert sorted(p.name for p in removed) == ["windows-spent", "windows-tx0", "windows-tx1"]
        assert sorted(p.name for p in root.iterdir()) == ["signed-keep", "windows-tx2", "windows-tx3"]


# --------------------------------------------------------------------------- #
# Process helper: only VOOL's own processes
# --------------------------------------------------------------------------- #

class TestProcessHelper:
    def _table(self, app: Path):
        return [
            (101, str(app / "python" / "pythonw.exe")),          # supervisor / API / window
            (102, str(app / "ollama" / "ollama.exe")),
            (103, str(app / "ollama" / "lib" / "ollama" / "llama-server.exe")),
            (104, str(app / "broker" / "VoolBroker.exe")),
            (201, str(app / "tools" / "python.exe")),            # inside {app} but not ours
            (202, str(app.parent / "VOOL-other" / "python" / "python.exe")),  # sibling prefix
            (203, r"C:\Python312\python.exe"),
            (204, ""),                                             # exe unreadable
            (os.getpid(), str(app / "python" / "python.exe")),    # never itself
        ]

    def test_only_program_folder_runtimes_are_owned(self, tmp_path):
        app = make_install(tmp_path)
        helper = WindowsProcessHelper(app_path=app, vool_home=tmp_path / "home",
                                      process_table=lambda: self._table(app))
        assert helper.owned_pids() == [101, 102, 103, 104]

    def test_shutdown_asks_the_supervisor_then_ends_only_owned_survivors(self, tmp_path):
        app = make_install(tmp_path)
        home = tmp_path / "home"
        alive = dict(self._table(app))
        clock = {"now": 0.0}
        killed = []

        def kill(pid):
            killed.append(pid)
            alive.pop(pid, None)

        helper = WindowsProcessHelper(app_path=app, vool_home=home,
                                      process_table=lambda: list(alive.items()), kill=kill,
                                      clock=lambda: clock["now"],
                                      sleeper=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                                      grace_seconds=5.0, poll_interval=1.0)
        helper.shutdown_app()
        assert (home / "run" / "stop").read_text().strip() == "stop"
        assert (app / ".vool_updating.lock").is_file()
        assert helper.wait_until_stopped(timeout=30.0)
        assert sorted(killed) == [101, 102, 103, 104]
        assert clock["now"] >= 5.0  # the supervisor got its grace first
        assert 201 in alive and 203 in alive

    def test_kill_goes_through_the_enumerated_process_object(self, tmp_path):
        app = make_install(tmp_path)
        handle = object()
        alive = {"on": True}
        killed = []
        clock = {"now": 0.0}

        def table():
            return [(101, str(app / "python" / "python.exe"), handle)] if alive["on"] else []

        def kill(target):
            killed.append(target)
            alive["on"] = False

        helper = WindowsProcessHelper(app_path=app, vool_home=None, process_table=table, kill=kill,
                                      clock=lambda: clock["now"],
                                      sleeper=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                                      grace_seconds=0.0, poll_interval=1.0)
        assert helper.wait_until_stopped(timeout=5.0)
        assert killed == [handle]  # never a bare pid that the OS may have reused

    def test_wait_reports_false_when_an_owned_process_will_not_end(self, tmp_path):
        app = make_install(tmp_path)
        clock = {"now": 0.0}
        helper = WindowsProcessHelper(app_path=app, vool_home=None,
                                      process_table=lambda: [(101, str(app / "python" / "python.exe"))],
                                      kill=lambda pid: (_ for _ in ()).throw(PermissionError("denied")),
                                      clock=lambda: clock["now"],
                                      sleeper=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                                      grace_seconds=1.0, poll_interval=1.0)
        assert helper.wait_until_stopped(timeout=5.0) is False

    def test_relaunch_starts_the_installed_launcher_detached_with_a_clean_environment(self, tmp_path, monkeypatch):
        app = make_install(tmp_path)
        home = tmp_path / "home"
        (home / "run").mkdir(parents=True)
        (home / "run" / "stop").write_text("stop\n")
        (app / ".vool_updating.lock").write_text("x")
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "frozen-worker-code"))
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        calls = []
        helper = WindowsProcessHelper(app_path=app, vool_home=home,
                                      popen=lambda command, **options: calls.append((command, options)))
        assert helper.relaunch(app)
        (command, options), = calls
        assert command[0].lower().endswith("wscript.exe") and command[1] == str(app / "vool.vbs")
        assert options["creationflags"] & win.DETACHED_PROCESS
        assert options["creationflags"] & win.CREATE_NO_WINDOW
        assert "PYTHONPATH" not in options["env"]
        assert not (app / ".vool_updating.lock").exists()
        assert not (home / "run" / "stop").exists()


def test_pid_probe_never_signals_on_windows(monkeypatch):
    from core.updater import macos

    monkeypatch.setattr(macos, "_is_windows", lambda: True)
    monkeypatch.setattr(macos.os, "kill", lambda *args: pytest.fail("os.kill is TerminateProcess on Windows"))
    import core.process_liveness as liveness

    monkeypatch.setattr(liveness, "pid_alive", lambda pid: pid == 4242)
    assert macos._pid_has_exited(4242) is False
    assert macos._pid_has_exited(4243) is True


# --------------------------------------------------------------------------- #
# Detached worker on Windows
# --------------------------------------------------------------------------- #

def test_worker_alive_reads_the_command_line_with_psutil_on_windows(monkeypatch, tmp_path):
    from core.updater import worker

    monkeypatch.setattr(worker, "_is_windows", lambda: True)

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return [r"C:\VOOL home\python\python.exe", "-m", "core.updater.worker", r"C:\a b\plan.json"]

    import psutil

    monkeypatch.setattr(psutil, "Process", FakeProcess)
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: pytest.fail("no ps on Windows"))
    assert r"C:\a b\plan.json" in worker._process_command_line(77)


def _worker_job(home: Path, app: Path, txid: str = "tx1") -> Path:
    job = home / "data" / "update_v2" / "workers" / txid
    job.mkdir(parents=True)
    (job / "plan.json").write_text(json.dumps({"txid": txid, "platform": "windows-x64",
                                               "feed": {"app_path": str(app)}, "keys": {}}))
    return job


def _program(tmp_path: Path) -> Path:
    app = make_install(tmp_path)
    (app / "python" / "python313._pth").write_text("python313.zip\n.\nimport site\nLib\\site-packages\n..\\app\n")
    for name in ("core", "installer", "storage", "network", "config"):
        (app / "app" / name).mkdir(parents=True, exist_ok=True)
        (app / "app" / name / "shipped.py").write_text("# shipped\n")
    return app


class _Child:
    pid = 9001


def _request(home: Path, body: dict) -> None:
    (home / "run").mkdir(parents=True, exist_ok=True)
    (home / "run" / win.WORKER_REQUEST_NAME).write_text(json.dumps(body))


def test_supervisor_builds_the_frozen_copy_from_the_program_folder_and_starts_it_outside_the_job(
        tmp_path, monkeypatch):
    app = _program(tmp_path)
    home = tmp_path / "home"
    job = _worker_job(home, app)
    _request(home, {"txid": "tx1"})
    monkeypatch.setenv("PYTHONHOME", "C:/elsewhere")
    monkeypatch.setenv("VOOL_UPDATE_TRUSTED_KEYS_JSON", "C:/planted-keys.json")
    calls = []
    pid = win.consume_worker_request(home, app, popen=lambda c, **o: (calls.append((c, o)), _Child())[1])
    assert pid == 9001
    job = job.resolve()
    assert (job / "python" / "pythonw.exe").read_text() == "old:python/pythonw.exe"
    assert (job / "app" / "core" / "shipped.py").read_text() == "# shipped\n"
    assert (job / "python" / "python313._pth").read_bytes() == (app / "python" / "python313._pth").read_bytes()
    (command, options), = calls
    assert command == [str(job / "python" / "pythonw.exe"), "-m", "core.updater.worker", str(job / "plan.json")]
    assert options["cwd"] == str(job / "app")
    assert options["env"]["VOOL_UPDATE_WORKER_APP"] == str(app.resolve())
    assert "PYTHONHOME" not in options["env"] and "VOOL_UPDATE_TRUSTED_KEYS_JSON" not in options["env"]
    assert options["creationflags"] & win.DETACHED_PROCESS
    assert not options["creationflags"] & win.CREATE_BREAKAWAY_FROM_JOB  # the supervisor is not in its job
    lease = json.loads((home / "data" / "update_v2" / "worker.json").read_text())
    assert lease["pid"] == 9001 and lease["launcher"] == "supervisor" and lease["txid"] == "tx1"
    assert not (home / "run" / win.WORKER_REQUEST_NAME).exists()


@pytest.mark.parametrize("planted", ["python/pythonw.exe", "app/core/evil.py", "code/core/evil.py"])
def test_supervisor_refuses_a_planted_interpreter_or_code_tree(tmp_path, planted):
    app = _program(tmp_path)
    home = tmp_path / "home"
    job = _worker_job(home, app)
    (job / planted).parent.mkdir(parents=True)
    (job / planted).write_text("planted")
    _request(home, {"txid": "tx1"})
    assert win.consume_worker_request(home, app, popen=lambda c, **o: pytest.fail("must not start")) is None
    assert (job / planted).read_text() == "planted"  # left for diagnosis, never run


def test_supervisor_refuses_a_frozen_interpreter_whose_pth_differs(tmp_path):
    app = _program(tmp_path)
    home = tmp_path / "home"
    _worker_job(home, app)
    _request(home, {"txid": "tx1"})
    real = win.shutil.copytree

    def tampering_copy(source, destination, **options):
        real(source, destination, **options)
        if Path(source).name == "python":
            (Path(destination) / "python313._pth").write_text("..\\elsewhere\n")

    assert win.consume_worker_request(home, app, copytree=tampering_copy,
                                      popen=lambda c, **o: pytest.fail("must not start")) is None


@pytest.mark.parametrize("request_body", [
    {"txid": "../../evil"}, {"txid": ".."}, {"txid": ""}, {"txid": "tx2"}, {"command": ["calc.exe"]},
])
def test_supervisor_refuses_any_other_request(tmp_path, request_body):
    app = _program(tmp_path)
    home = tmp_path / "home"
    _worker_job(home, app)
    _request(home, request_body)
    assert win.consume_worker_request(home, app, popen=lambda c, **o: pytest.fail("must not start")) is None
    assert not (home / "run" / win.WORKER_REQUEST_NAME).exists()


@pytest.mark.parametrize("plan", [
    {"txid": "other", "platform": "windows-x64"},
    {"txid": "tx1", "platform": "macos-arm64"},
    {"txid": "tx1", "platform": "windows-x64", "feed": {"app_path": "C:/Users/me/Documents"}},
])
def test_supervisor_refuses_a_plan_for_another_transaction_platform_or_folder(tmp_path, plan):
    app = _program(tmp_path)
    home = tmp_path / "home"
    job = _worker_job(home, app)
    plan.setdefault("feed", {"app_path": str(app)})
    (job / "plan.json").write_text(json.dumps(plan))
    _request(home, {"txid": "tx1"})
    assert win.consume_worker_request(home, app, popen=lambda c, **o: pytest.fail("must not start")) is None


def test_windows_worker_never_takes_trust_keys_or_target_from_the_plan(tmp_path, monkeypatch):
    from core.updater import worker
    from core.updater.runtime import UpdateSubsystem

    shipped = TrustedPublishers(pinned_keys={"vool-release-2026": "11" * 32})
    monkeypatch.setattr(UpdateSubsystem, "_load_trust", staticmethod(lambda: shipped))
    app = tmp_path / "Programs" / "VOOL"
    plan = {"platform": "windows-x64", "feed": {"app_path": str(app)}, "keys": {"attacker": "22" * 32}}
    trust = worker.plan_trust(plan, env={"VOOL_UPDATE_WORKER_APP": str(app)})
    assert trust is shipped and "attacker" not in trust.key_ids
    with pytest.raises(ValueError, match="program folder"):
        worker.plan_trust({**plan, "feed": {"app_path": str(tmp_path / "other")}},
                          env={"VOOL_UPDATE_WORKER_APP": str(app)})
    with pytest.raises(ValueError, match="program folder"):
        worker.plan_trust(plan, env={})
    # Mac and Linux keep their plan-carried keys (their worker is not outside any job).
    assert "attacker" in worker.plan_trust({**plan, "platform": "linux-x64"}).key_ids


def test_api_side_waits_for_the_supervisor_lease_and_fails_closed(tmp_path):
    from core.updater import worker

    app = _program(tmp_path)
    home = tmp_path / "home"
    job = _worker_job(home, app)
    root = home / "data" / "update_v2"

    def supervisor_answers(_seconds):
        win.consume_worker_request(home, app, popen=lambda c, **o: _Child())

    handle = worker.request_supervisor_start(root, "tx1", job / "plan.json", sleeper=supervisor_answers)
    assert handle.pid == 9001

    clock = {"now": 0.0}
    job2 = _worker_job(home, app, "tx2")
    with pytest.raises(RuntimeError, match="nothing was installed"):
        worker.request_supervisor_start(root, "tx2", job2 / "plan.json", clock=lambda: clock["now"],
                                        sleeper=lambda s: clock.__setitem__("now", clock["now"] + 1), timeout=3)
    assert not (home / "run" / win.WORKER_REQUEST_NAME).exists()


def test_bundle_supervisor_loop_consumes_update_requests():
    source = (Path(__file__).resolve().parents[2] / "installer" / "bundle" / "bundle_supervisor.py").read_text()
    assert "self._consume_update_worker_request()" in source
    assert "allow_breakaway" not in source and "BREAKAWAY" not in source  # the job stays closed


# --------------------------------------------------------------------------- #
# Feed and runtime wiring
# --------------------------------------------------------------------------- #

def test_windows_reads_its_own_feed_block(tmp_path):
    from core.updater.feed import load_feed_config

    config = tmp_path / "update_feed.json"
    config.write_text(json.dumps({
        "manifest_url": "https://mac.example/manifest.json", "channel": "beta",
        "allow_publisher_verified_adhoc": True,
        "platforms": {"windows-x64": {"manifest_url": "https://win.example/manifest.json",
                                      "allow_publisher_verified_adhoc": False, "app_path": "C:/evil"}},
    }))
    windows = load_feed_config(config_path=config, env={}, platform="windows-x64")
    assert windows.manifest_url == "https://win.example/manifest.json"
    assert windows.allow_publisher_verified_adhoc is False
    assert windows.app_path == ""  # a platform block cannot name the swap target
    assert windows.channel == "beta"
    mac = load_feed_config(config_path=config, env={}, platform="macos-arm64")
    assert mac.manifest_url == "https://mac.example/manifest.json"


def test_shipped_feed_points_windows_at_its_own_release():
    from core.updater.feed import load_feed_config

    feed = load_feed_config(env={}, platform="windows-x64")
    assert feed.manifest_url.startswith("https://github.com/Parad0x-Labs/VOOL-Windows-M6A/releases/")
    assert feed.allow_publisher_verified_adhoc is False


def test_windows_source_checkout_reports_it_cannot_update(tmp_path):
    from core.updater.feed import FeedConfig
    from core.updater.runtime import UnavailableReason, UpdateSubsystem

    subsystem = UpdateSubsystem(feed=FeedConfig(manifest_url="https://win.example/m.json"),
                                trust=TrustedPublishers(pinned_keys={"k": "00" * 32}),
                                data_dir=tmp_path / "home" / "data", installed_version="0.7.1",
                                platform="windows-x64")
    assert subsystem.unavailable_reason is UnavailableReason.NOT_INSTALLED_BUNDLE
    assert "VOOL-Setup.exe" in subsystem.unavailable_reason.plain


def test_windows_install_gets_the_windows_helper(tmp_path):
    from core.updater.feed import FeedConfig
    from core.updater.runtime import UpdateSubsystem

    app = make_install(tmp_path)
    subsystem = UpdateSubsystem(feed=FeedConfig(manifest_url="https://win.example/m.json", app_path=str(app)),
                                trust=TrustedPublishers(pinned_keys={"k": "00" * 32}),
                                data_dir=tmp_path / "home" / "data", installed_version="0.7.1",
                                platform="windows-x64")
    assert subsystem.configured, subsystem.unavailable_reason
    helper = subsystem._make_helper()
    assert isinstance(helper, WindowsProcessHelper)
    assert helper.app_path == app
    assert isinstance(subsystem.make_flow().installer, WindowsBundleInstaller)


# --------------------------------------------------------------------------- #
# The whole transaction on a Windows-shaped install
# --------------------------------------------------------------------------- #

class Box:
    def __init__(self, tmp_path: Path, *, payload_version: str = "0.7.2", commit: str = COMMIT,
                 signed_commit: str = COMMIT):
        self.private_hex, self.public_hex = generate_publisher_keypair()
        self.data_dir = tmp_path / "home" / "data"
        (self.data_dir / "config").mkdir(parents=True)
        (self.data_dir / "config" / "settings.json").write_text('{"setting": "mine"}', encoding="utf-8")
        self.app = make_install(tmp_path)
        new = make_bundle(tmp_path / "build" / "VOOL", version=payload_version, marker="new", commit=commit)
        blob = zip_bundle(new, tmp_path / "build" / "update.zip")
        manifest, _ = build_manifest(version="0.7.2", artifacts={"windows-x64": blob},
                                     private_hex=self.private_hex, minimum_compatible="0.7.0",
                                     build_commit=signed_commit)
        self.raw = manifest_bytes(manifest)
        self.trust = TrustedPublishers(pinned_keys={"release-2026-01": self.public_hex})
        verification = parse_and_verify_manifest(self.raw, self.trust,
                                                 now=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc))
        assert verification.ok, verification.reason
        self.manifest = verification.manifest
        self.decision = decide_update(self.manifest, installed_version="0.7.1", channel="stable",
                                      platform_key="windows-x64", high_water={"sequence": 0},
                                      installer_available=True)
        assert self.decision.reason is UpdateDecisionReason.UPDATE_AVAILABLE
        self.blob = blob
        self.unhealthy = False
        self.relaunches = []

    def marker(self) -> str:
        return (self.app / "app" / "marker.txt").read_text(encoding="utf-8")

    def health(self, *, timeout, expected_version=None):
        reported = COMMIT if self.marker() == "new" else "0.7.1"
        if self.unhealthy and self.marker() == "new":
            return HealthResult(False, "forced", reported)
        return HealthResult(expected_version in (None, reported), "probe", reported)

    def flow(self, **overrides) -> UpdateFlow:
        from .test_transaction_flow import FakeServer

        params = dict(
            data_dir=self.data_dir, app_path=self.app, platform="windows-x64", installed_version="0.7.1",
            channel="stable", trust=self.trust,
            downloader=StagedDownloader(fetch=FakeServer(self.blob).open, chunk_size=4096),
            coordinator=WorkCoordinator(), migration_catalog=MigrationCatalog(),
            helper=InProcessHelper(shutdown=lambda: None,
                                   relauncher=lambda path: (self.relaunches.append(path), True)[1]),
            health_probe=self.health, installer=WindowsBundleInstaller(),
            status_store=StatusStore(self.data_dir / "update_v2" / "status.json"),
        )
        params.update(overrides)
        return UpdateFlow(**params)


def _program_folder_outside_entries(app: Path) -> list[str]:
    return sorted(p.name for p in app.parent.iterdir())


def test_one_press_installs_inside_the_program_folder_only(tmp_path):
    box = Box(tmp_path)
    result = box.flow().run(box.raw, box.manifest, box.decision)
    assert result.ok, result.detail
    assert box.marker() == "new"
    assert (box.app / "unins000.exe").read_text() == "uninstaller"
    assert _program_folder_outside_entries(box.app) == ["VOOL"]  # nothing written beside the install
    journal = UpdateJournal(box.data_dir / "update_v2" / "transactions" / result.txid)
    assert journal.payload_of(Step.SWAP_PLANNED)["prior_path"] == journal.payload_of(Step.SWAPPED)["prior_path"]
    assert (box.app / "update_backups" / f"windows-{result.txid}" / "app" / "marker.txt").read_text() == "old"
    assert not any((box.app / ".vool-update").iterdir())
    assert json.loads((box.data_dir / "config" / "settings.json").read_text()) == {"setting": "mine"}


def test_unhealthy_new_version_is_rolled_back_to_the_previous_one(tmp_path):
    box = Box(tmp_path)
    box.unhealthy = True
    result = box.flow().run(box.raw, box.manifest, box.decision)
    assert not result.ok
    assert box.marker() == "old"
    assert all((box.app / name).exists() for name in MANAGED_ENTRIES)
    assert _program_folder_outside_entries(box.app) == ["VOOL"]


def test_payload_for_another_version_is_refused_before_anything_stops(tmp_path):
    box = Box(tmp_path, payload_version="0.7.9")
    stops = []
    result = box.flow(helper=InProcessHelper(shutdown=lambda: stops.append(1),
                                             relauncher=lambda path: True)).run(box.raw, box.manifest, box.decision)
    assert not result.ok and "Windows payload" in result.detail
    assert stops == [] and box.marker() == "old"


def test_payload_from_another_commit_is_refused(tmp_path):
    box = Box(tmp_path, commit="c" * 40)
    result = box.flow().run(box.raw, box.manifest, box.decision)
    assert not result.ok and "commit" in result.detail
    assert box.marker() == "old"


def test_windows_offer_without_a_pinned_build_commit_is_refused(tmp_path):
    box = Box(tmp_path, signed_commit="")
    box.health = lambda *, timeout, expected_version=None: HealthResult(True, "probe", expected_version)
    result = box.flow().run(box.raw, box.manifest, box.decision)
    assert not result.ok and "build commit" in result.detail
    assert box.marker() == "old"


def test_crash_in_the_middle_of_the_swap_is_recovered_at_next_start(tmp_path, monkeypatch):
    box = Box(tmp_path)
    real_rename = os.rename
    moves = []

    def rename(source, destination):
        moves.append(source)
        if len(moves) == 5:
            raise SystemExit("power cut")
        return real_rename(source, destination)

    monkeypatch.setattr(win.os, "rename", rename)
    with pytest.raises(SystemExit):
        box.flow().run(box.raw, box.manifest, box.decision)
    monkeypatch.setattr(win.os, "rename", real_rename)
    results = recover_interrupted_update(
        data_dir=box.data_dir, app_path=box.app, platform="windows-x64",
        helper=InProcessHelper(shutdown=lambda: None, relauncher=lambda path: True),
        health_probe=box.health, installed_version="0.7.1", channel="stable",
    )
    assert [r.action for r in results] == ["rolled_back"], results
    assert box.marker() == "old"
    assert all((box.app / name).exists() for name in MANAGED_ENTRIES)


# --------------------------------------------------------------------------- #
# Release payload builder
# --------------------------------------------------------------------------- #

def test_windows_payload_builder_ships_exactly_the_program_files(tmp_path, monkeypatch):
    from installer import build_update_payload as builder

    stage = make_bundle(tmp_path / "stage", version="0.7.2", marker="new")
    (stage / "models").mkdir()
    (stage / "models" / "seed.gguf").write_bytes(b"model")
    (stage / "python-embed.zip").write_bytes(b"zip")
    monkeypatch.setattr(builder, "_validate_source", lambda source, version: None)
    output = builder.build_payload(stage, tmp_path / "out", platform="windows-x64", version="0.7.2")
    assert output.name == "VOOL-0.7.2-windows-x64-update.zip"
    with zipfile.ZipFile(output) as archive:
        tops = {name.split("/")[1] for name in archive.namelist() if name.count("/") >= 1 and name.split("/")[1]}
    assert tops == set(MANAGED_ENTRIES)


def test_windows_payload_builder_refuses_a_dirty_or_unpinned_stage(tmp_path, monkeypatch):
    from installer import build_update_payload as builder

    monkeypatch.setattr(builder, "_validate_source", lambda source, version: None)
    dirty = make_bundle(tmp_path / "dirty", version="0.7.2", marker="new", dirty=True)
    with pytest.raises(ValueError, match="clean"):
        builder.build_payload(dirty, tmp_path / "out", platform="windows-x64", version="0.7.2")
    unpinned = make_bundle(tmp_path / "unpinned", version="0.7.2", marker="new", commit="")
    with pytest.raises(ValueError, match="commit"):
        builder.build_payload(unpinned, tmp_path / "out2", platform="windows-x64", version="0.7.2")
