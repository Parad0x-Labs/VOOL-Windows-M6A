"""Detached continuation of a prepared, signed transaction across app shutdown."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from core.updater.macos import _pid_has_exited
from core.updater.state import UpdaterPaths, _atomic_write_json, _read_json


def _cgroup_units(path: Path = Path("/proc/self/cgroup")) -> set[str]:
    return {part for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            for part in line.split(":", 2)[-1].split("/") if part}


def _systemd_scope_prefix(txid: str, *, platform: str | None = None,
                          cgroup_path: Path = Path("/proc/self/cgroup")) -> list[str]:
    if (platform or sys.platform) != "linux" or "vool-runtime.service" not in _cgroup_units(cgroup_path):
        return []
    binary = shutil.which("systemd-run")
    if not binary:
        raise ValueError("the updater cannot leave its runtime service; systemd-run is unavailable")
    unit = "vool-update-" + hashlib.sha256(txid.encode()).hexdigest()[:24]
    command = [binary, "--user", "--scope", "--quiet", "--collect", "--unit=" + unit]
    help_result = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=5, check=False)
    if "--expand-environment=" in help_result.stdout:
        command.append("--expand-environment=no")
    return [*command, "--"]


def prune_finished_workers(data_dir: Path) -> None:
    """Retain journals/logs and incomplete recoveries; release dead frozen runtimes."""
    from core.updater.transaction import UpdateJournal

    paths = UpdaterPaths.for_data_dir(data_dir)
    root = paths.root / "workers"
    for job in root.iterdir() if root.is_dir() else ():
        if job.is_symlink() or not job.is_dir():
            continue
        lease = _read_json(job / "process.json")
        if not isinstance(lease, dict) or int(lease.get("pid") or 0) <= 1:
            continue
        if not _pid_has_exited(int(lease["pid"])):
            continue
        finished = _read_json(job / "recovery-result.json") or _read_json(job / 'rollback-result.json') or {}
        if job.name.startswith("recovery-"):
            if not finished.get("ok"):
                continue
        elif job.name.startswith('rollback-'):
            if not (finished.get('ok') or finished.get('restored_current')):
                continue
        elif not UpdateJournal(paths.transaction_dir(job.name)).is_terminal():
            continue
        for name in ("code", "app", "python"):
            directory = job / name
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)


#: The code trees a detached worker runs from (a frozen copy, outside the replacement target).
FROZEN_CODE_TREES = ("core", "installer", "storage", "network", "config")


def _is_windows() -> bool:
    """Indirection so tests can take the Windows branch without patching os.name."""
    return os.name == "nt"


def _process_command_line(pid: int) -> str:
    """The whole command line of ``pid``, or "" when it cannot be read."""
    if _is_windows():
        try:
            import psutil

            return subprocess.list2cmdline(psutil.Process(pid).cmdline())
        except Exception:
            return ""
    # `-ww` asks for the whole command line (macOS ps and Linux procps both take it):
    # without it ps may cut `args=` to the terminal width, and a live worker whose plan
    # path sits past that width would read as dead.
    command = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, check=False)
    return command.stdout if command.returncode == 0 else ""


def worker_alive(data_dir: Path) -> bool:
    lease = _read_json(UpdaterPaths.for_data_dir(data_dir).root / "worker.json")
    if not isinstance(lease, dict):
        return False
    pid = int(lease.get("pid") or 0)
    marker = str(lease.get("plan") or "")
    if pid <= 1 or not marker or _pid_has_exited(pid):
        return False
    # A stale/reused pid is insufficient. It must still run this exact plan.
    return marker in _process_command_line(pid)


def start_worker(subsystem, offer, txid: str, *, recovery: bool = False, rollback: dict | None = None,
                 rollback_recovery: bool = False) -> subprocess.Popen:
    """Freeze code (and a bundled interpreter) OUTSIDE the replacement target.

    No private publisher material or user profile is copied. Parent already owns
    the work gate, active-state receipt, DB/config snapshot and signed download.
    The child re-verifies both signatures before proceeding with that journal.
    """
    paths = subsystem.paths
    prune_finished_workers(subsystem.data_dir)
    root = paths.root / "workers" / txid
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    code = root / "code"
    source = Path(__file__).resolve().parents[2]
    windows = str(subsystem.platform).startswith("windows-")
    # On Windows the bundle supervisor builds the frozen code and interpreter itself from the
    # program folder when it starts the worker (core.updater.windows.consume_worker_request).
    for name in () if windows else FROZEN_CODE_TREES:
        shutil.copytree(source / name, code / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    executable = Path(sys.executable).resolve()
    app = Path(subsystem.feed.app_path).resolve()
    if executable.is_relative_to(app) and not windows:
        # The worker must not run from the folder it replaces: Windows refuses to rename a
        # directory while a process holds its interpreter open.
        runtime = app / "Contents/Resources/python"
        if not executable.is_relative_to(runtime):
            raise ValueError("bundled updater interpreter is outside the shipped runtime")
        shutil.copytree(runtime, root / "python", symlinks=True)
        executable = root / "python" / executable.relative_to(runtime)
    feed = subsystem.feed
    if feed.helper_script:
        frozen_helper = root / "mac_update_helper.sh"
        shutil.copy2(feed.helper_script, frozen_helper)
        feed = replace(feed, helper_script=str(frozen_helper))
    plan = root / "plan.json"
    _atomic_write_json(plan, {
        "txid": txid, "feed": asdict(feed), "data_dir": str(subsystem.data_dir),
        "installed_version": subsystem.installed_version, "platform": subsystem.platform,
        "health_timeout": subsystem.health_timeout,
        "notarization_waiver_reason": subsystem.notarization_waiver_reason,
        "recovery": recovery,
        "rollback": rollback, "rollback_recovery": rollback_recovery,
        "keys": {key: subsystem.trust.public_key_hex(key) for key in subsystem.trust.key_ids},
    })
    if not recovery and not rollback and not rollback_recovery:
        (root / "manifest.json").write_bytes(offer.raw)
    env = dict(os.environ, PYTHONPATH=str(code), PYTHONDONTWRITEBYTECODE="1",
               VOOL_HOME=str(subsystem.data_dir.resolve().parent))
    log = root / "worker.log"
    if windows:
        # The press arrives in the API, inside the bundle supervisor's kill-on-close job and
        # under its process tree, both of which the swap must stop. The supervisor itself is
        # outside the job: it starts the worker from the frozen copy named by this request.
        return request_supervisor_start(paths.root, txid, plan)
    prefix = _systemd_scope_prefix(txid)
    with log.open("ab") as output:
        process = subprocess.Popen([*prefix, str(executable), "-m", "core.updater.worker", str(plan)],
                                   cwd=code, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, start_new_session=True, close_fds=True)
    lease = {
        "pid": process.pid, "plan": str(plan), "txid": txid, "started_at": time.time(),
        "scope": next((arg.split("=", 1)[1] + ".scope" for arg in prefix if arg.startswith("--unit=")), ""),
    }
    _atomic_write_json(root / "process.json", lease)
    _atomic_write_json(paths.root / "worker.json", lease)
    return process


WINDOWS_WORKER_APP_ENV = "VOOL_UPDATE_WORKER_APP"


class SupervisorStartedWorker:
    """Popen-shaped handle for a worker the bundle supervisor started (Windows)."""

    def __init__(self, pid: int):
        self.pid = int(pid)

    def poll(self):
        from core.process_liveness import pid_alive

        return None if pid_alive(self.pid) else 0

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("vool-update-worker", timeout)
            time.sleep(0.2)
        return 0


def request_supervisor_start(updater_root: Path, txid: str, plan: Path, *, clock=time.monotonic,
                             sleeper=time.sleep, timeout: float = 180.0) -> SupervisorStartedWorker:
    """Ask the bundle supervisor to start this transaction's worker; wait for its lease.

    The request names only the transaction. The supervisor copies the interpreter and code
    from the program folder itself (core.updater.windows.consume_worker_request), which takes
    a while for the interpreter, hence the long bound. No answer in time fails closed with
    nothing installed.
    """
    from core.updater.windows import WORKER_REQUEST_NAME

    request = updater_root.resolve().parent.parent / "run" / WORKER_REQUEST_NAME
    request.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(request, {"txid": txid})
    deadline = clock() + timeout
    while clock() < deadline:
        lease = _read_json(updater_root / "worker.json") or {}
        if (lease.get("txid") == txid and lease.get("launcher") == "supervisor"
                and Path(str(lease.get("plan") or "")).resolve() == plan.resolve() and int(lease.get("pid") or 0) > 1):
            return SupervisorStartedWorker(int(lease["pid"]))
        sleeper(0.05)
    request.unlink(missing_ok=True)
    raise RuntimeError("the VOOL supervisor did not start the updater in time; nothing was installed")


def plan_trust(params: dict, *, env=None):
    """The publisher keys a detached worker verifies with.

    The plan lives in the user-writable VOOL home. On Windows, where the worker runs outside the
    supervisor's job, neither its keys nor its target are trusted: keys come from this frozen
    code copy (which the supervisor built from the program folder), and the plan's target must
    be the program folder the supervisor serves.
    """
    from core.updater.runtime import UpdateSubsystem
    from core.updater.trust import TrustedPublishers

    if not str(params.get("platform") or "").startswith("windows-"):
        return TrustedPublishers(params["keys"])
    environ = os.environ if env is None else env
    served = str(environ.get(WINDOWS_WORKER_APP_ENV) or "")
    target = str((params.get("feed") or {}).get("app_path") or "")
    if not served or not target or os.path.normcase(os.path.abspath(served)) != os.path.normcase(os.path.abspath(target)):
        raise ValueError("the update plan does not name the program folder this worker was started for")
    return UpdateSubsystem._load_trust()


def main() -> int:
    from core.updater.decision import decide_update
    from core.updater.feed import FeedConfig
    from core.updater.manifest import parse_and_verify_manifest
    from core.updater.runtime import UpdateSubsystem
    from core.updater.status import UpdateFault, UpdatePhase

    if sys.argv[1] == "--recover-startup":
        subsystem = UpdateSubsystem()
        if not subsystem.feed.has_target:
            return 2
        subsystem._recover_bounded()
        if not subsystem._recovering and not subsystem._worker_alive():
            if subsystem.status.load() and subsystem.status.load().fault_code == "rollback_failed":
                return 1
            (Path(subsystem.feed.app_path) / ".vool_updating.lock").unlink(missing_ok=True)
        return 0

    plan = Path(sys.argv[1])
    params = json.loads(plan.read_text(encoding="utf-8"))
    trust = plan_trust(params)
    subsystem = UpdateSubsystem(feed=FeedConfig(**params["feed"]),
                                trust=trust,
                                data_dir=Path(params["data_dir"]),
                                installed_version=params["installed_version"], platform=params["platform"],
                                notarization_waiver_reason=params["notarization_waiver_reason"],
                                health_timeout=params["health_timeout"])
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            lease = _read_json(subsystem.paths.root / "worker.json") or {}
            if lease.get("txid") == params["txid"] and lease.get("pid") == os.getpid():
                if lease.get("scope") and lease["scope"] not in _cgroup_units():
                    raise ValueError("the updater did not enter its independent service scope")
                break
            if lease.get("txid") == params["txid"] and lease.get("scope") and lease.get("pid") == os.getppid():
                if lease["scope"] not in _cgroup_units():
                    raise ValueError("the updater did not enter its independent service scope")
                lease.update(launcher_pid=lease["pid"], pid=os.getpid())
                _atomic_write_json(plan.with_name("process.json"), lease)
                _atomic_write_json(subsystem.paths.root / "worker.json", lease)
                break
            time.sleep(.02)
        else:
            raise ValueError("the parent did not publish the detached transaction lease; nothing was installed")
        if params.get('rollback') or params.get('rollback_recovery'):
            from core.updater.rollback import recover_manual_rollbacks, run_manual_rollback

            receipt = recover_manual_rollbacks(subsystem) if params.get('rollback_recovery') else run_manual_rollback(
                subsystem, params['rollback'], params['txid'])
            _atomic_write_json(plan.with_name('rollback-result.json'), receipt)
            print(json.dumps(receipt), flush=True)
            return 0 if receipt.get('ok') else 1
        if params.get("recovery") is True:
            from core.updater.transaction import recover_interrupted_update

            results = recover_interrupted_update(
                data_dir=subsystem.data_dir, app_path=Path(subsystem.feed.app_path),
                helper=subsystem._make_helper(), health_probe=subsystem._make_probe(),
                installed_version=subsystem.installed_version, channel=subsystem.feed.channel,
                platform=subsystem.platform, status_store=subsystem.status,
            )
            ok = all(result.action != "rollback_failed" for result in results)
            receipt = {"ok": ok, "actions": [{"txid": result.txid, "action": result.action} for result in results]}
            _atomic_write_json(plan.with_name("recovery-result.json"), receipt)
            print(json.dumps(receipt), flush=True)
            return 0 if ok else 1
        raw = plan.with_name("manifest.json").read_bytes()
        verification = parse_and_verify_manifest(raw, subsystem.trust)
        if not verification.ok or verification.manifest is None:
            raise ValueError("prepared manifest failed detached re-verification")
        manifest = verification.manifest
        decision = decide_update(manifest, installed_version=subsystem.installed_version,
                                 channel=subsystem.feed.channel, platform_key=subsystem.platform,
                                 high_water=subsystem.high_water.load(subsystem.feed.channel),
                                 installer_available=subsystem.make_flow().installer is not None)
        if not decision.should_install:
            raise ValueError(f"prepared update no longer applies: {decision.reason.value}")
        result = subsystem.make_flow().resume(params["txid"], raw, manifest, decision)
        print(json.dumps({"ok": result.ok, "step": result.terminal.value,
                          "detail": result.detail, "txid": result.txid}), flush=True)
        return 0 if result.ok else 1
    except Exception as exc:
        subsystem.status.publish(UpdatePhase.FAILED, detail=f"detached update failed: {exc}",
                                 fault=UpdateFault.ROLLBACK_FAILED if any(params.get(key) for key in
                                 ('recovery', 'rollback', 'rollback_recovery')) else UpdateFault.INSTALL_FAILED)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
