"""installer/vool_stop.py -- hard-stop every VOOL process and keep it stopped.

Both the in-chat ``/stopall`` brake and the desktop "Stop VOOL" button run this. Order matters:

  1. disable + end the VOOL_Daemon scheduled task, so the watchdog can neither respawn the API
     on its restart loop nor relaunch it at the next logon,
  2. signal any bundle supervisor to stop before its children are killed,
  3. kill the watchdog processes already running (wscript/cmd on vool_background),
  4. kill the OpenClaw gateway (whatever listens on 18789),
  5. kill the VOOL API server on 11435 with a tree-kill so its child workers/agents die too
     (with the pidfile as a fallback if the port lookup fails),
  6. write a .vool_stopped marker.

Relaunching VOOL (desktop shortcut -> OpenClaw_VOOL.bat) re-enables the task and clears the marker.

Every process action goes through an injected ``run`` callable, so the exact command sequence is
unit-testable without touching real processes. Ollama (the shared model server on 11434) is left
running on purpose -- it is not VOOL-specific and idles harmlessly.

On macOS/Linux the same entrypoint takes the POSIX path (``hard_stop_posix``): it boots the
``ai.vool.runtime`` launchd KeepAlive agent out of the GUI domain first (so launchd stops
respawning the API), then frees ports 18789 and 11435 with ``lsof`` + ``kill`` (SIGTERM, then
SIGKILL for any survivor), with the pidfile as a fallback. ``main`` dispatches by platform, so
the Windows ``hard_stop`` sequence above is unchanged.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

WATCHDOG_TASK = "VOOL_Daemon"
API_PORT = 11435
GATEWAY_PORT = 18789
STOPPED_MARKER = ".vool_stopped"
_WINDOWS_PYTHON_ARGUMENT_PREFIX = (
    r'^(?:\x22(?:[^\x22]*\\)?pythonw?\.exe\x22|(?:[^\s\x22]*\\)?pythonw?\.exe)'
    r'(?:\s+-[bBdEhIiOPqRsSuvVx]+'
    r'|\s+-W(?:\s+\x22[^\x22]*\x22|\s+\S+|[^\s\x22]+)'
    r'|\s+-X(?:\s+\x22[^\x22]*\x22|\s+\S+|[^\s\x22]+)'
    r'|\s+--check-hash-based-pycs\s+(?:\x22[^\x22]*\x22|\S+))*'
)
_WINDOWS_SCRIPT_ARGUMENT_PREFIX = _WINDOWS_PYTHON_ARGUMENT_PREFIX + r'\s+\x22?'
_WINDOWS_MODULE_ARGUMENT_PREFIX = _WINDOWS_PYTHON_ARGUMENT_PREFIX + r'\s+-m\s+apps\.vool_api_server(?:\s|$)'

Runner = Callable[..., Any]


# Windows' console tools open no console window of their own; 0 (no flag) off Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(run: Runner, cmd: list[str]) -> Any:
    try:
        # Windows' own taskkill, schtasks and PowerShell by absolute path, never a copy found on
        # PATH; when one can't be trusted the step is skipped like any other command that could not run.
        from core.process_liveness import SYSTEM32_TOOLS, system_powershell, system_tool

        if cmd[0] == "powershell":
            cmd = [system_powershell(), *cmd[1:]]
        elif cmd[0] in SYSTEM32_TOOLS:
            cmd = [system_tool(cmd[0]), *cmd[1:]]
        if _NO_WINDOW:
            return run(cmd, capture_output=True, text=True, check=False, creationflags=_NO_WINDOW)
        return run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        return None


def _stdout(res: Any) -> str:
    return (getattr(res, "stdout", "") or "") if res is not None else ""


def _pidfile(project_root: Path, vool_home: Path | None) -> Path:
    data = (vool_home / "data") if vool_home else (project_root / "data")
    return data / "vool_api.pid"


def disable_watchdog(run: Runner, *, task: str = WATCHDOG_TASK) -> None:
    _run(run, ["schtasks", "/end", "/tn", task])
    _run(run, ["schtasks", "/change", "/tn", task, "/disable"])


def enable_watchdog(run: Runner, *, task: str = WATCHDOG_TASK) -> None:
    _run(run, ["schtasks", "/change", "/tn", task, "/enable"])


def signal_bundle_supervisor(vool_home: Path | None) -> bool:
    """Ask the bundle supervisor to stop before the port kill below."""
    if vool_home is None:
        return False
    run_dir = Path(vool_home) / "run"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stop").write_text("stop\n", encoding="utf-8")
        return True
    except OSError:
        return False


def kill_watchdog_processes(run: Runner) -> None:
    _run(run, [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='wscript.exe' OR Name='cmd.exe'\" "
        "| Where-Object { $_.CommandLine -like '*vool_background*' } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
    ])


# PowerShell ends a single-quoted string at the curly quotes U+2018, U+2019, U+201A and U+201B as
# well as at the ASCII apostrophe, and reads any of them doubled as that one character.
_POWERSHELL_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"


def _windows_install_root_literal(install_root: Path) -> str:
    """Return a PowerShell single-quoted path literal for this VOOL installation."""
    text = "".join(c + c if c in _POWERSHELL_SINGLE_QUOTES else c for c in str(install_root))
    return "'" + text + "'"


def kill_listener(run: Runner, port: int, install_root: Path) -> int | None:
    """Tree-kill the VOOL API process listening on ``port``. Returns the killed pid, or None.

    Only this installation's Python is VOOL's: a python/pythonw whose exe lives under the install
    root, or that runs the root's API script, or that runs ``-m apps.vool_api_server`` with a
    parent under the root (a source checkout's venv launcher). Anything else holding the port -- a
    user's own OpenClaw gateway on 18789 or an unrelated local server -- is left running.
    """
    res = _run(run, [
        "powershell", "-NoProfile", "-Command",
        f"$r = {_windows_install_root_literal(Path(install_root))}; "
        "$rootPrefix = $r.TrimEnd([char[]]@(92,47)) + [char]92; "
        "$script = [regex]::Escape($rootPrefix + 'apps\\vool_api_server.py'); "
        f"$scriptArgument = '{_WINDOWS_SCRIPT_ARGUMENT_PREFIX}' + $script + '(?:\\x22|\\s|$)'; "
        f"$moduleCommand = '{_WINDOWS_MODULE_ARGUMENT_PREFIX}'; "
        f"$p = Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -First 1 -ExpandProperty OwningProcess; "
        "$v = if ($p) { Get-CimInstance Win32_Process -Filter \"ProcessId=$p\" -ErrorAction SilentlyContinue "
        "| Where-Object { ($_.Name -ieq 'python.exe' -or $_.Name -ieq 'pythonw.exe') "
        "-and (([string]$_.ExecutablePath).StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) "
        "-or ([string]$_.CommandLine -match $scriptArgument) "
        "-or (([string]$_.CommandLine -match $moduleCommand) "
        "-and ([string](Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.ParentProcessId)\" "
        "-ErrorAction SilentlyContinue).ExecutablePath).StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase))) } }; "
        "if ($v) { $p } else { '' }",
    ])
    out = _stdout(res).strip()
    pid = int(out) if out.isdigit() else 0
    if pid > 0 and pid != os.getpid():
        _run(run, ["taskkill", "/PID", str(pid), "/T", "/F"])
        return pid
    return None


def kill_api_by_pidfile(run: Runner, pidfile: Path, install_root: Path) -> int | None:
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    command = (
        f"$r = {_windows_install_root_literal(Path(install_root))}; "
        "$rootPrefix = $r.TrimEnd([char[]]@(92,47)) + [char]92; "
        "$script = [regex]::Escape($rootPrefix + 'apps\\vool_api_server.py'); "
        f"$scriptArgument = '{_WINDOWS_SCRIPT_ARGUMENT_PREFIX}' + $script + '(?:\\x22|\\s|$)'; "
        f"$moduleCommand = '{_WINDOWS_MODULE_ARGUMENT_PREFIX}'; "
        f"$v = Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" -ErrorAction SilentlyContinue "
        "| Where-Object { ($_.Name -ieq 'python.exe' -or $_.Name -ieq 'pythonw.exe') "
        "-and (([string]$_.ExecutablePath).StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) "
        "-or ([string]$_.CommandLine -match $scriptArgument) "
        "-or (([string]$_.CommandLine -match $moduleCommand) "
        "-and ([string](Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.ParentProcessId)\" "
        "-ErrorAction SilentlyContinue).ExecutablePath).StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase))) }; "
        "if ($v) { 'owned' } else { '' }"
    )
    result = _run(run, ["powershell", "-NoProfile", "-Command", command])
    if _stdout(result).strip() != "owned":
        return None  # stale/reused pid or foreign process -> never kill it
    _run(run, ["taskkill", "/PID", str(pid), "/T", "/F"])
    return pid


def clear_stopped_marker(project_root: Path) -> None:
    with contextlib.suppress(Exception):
        (Path(project_root) / STOPPED_MARKER).unlink(missing_ok=True)


def hard_stop(project_root: Path, vool_home: Path | None = None, *, run: Runner = subprocess.run) -> dict[str, Any]:
    project_root = Path(project_root)
    vool_home = Path(vool_home) if vool_home else None

    # 1) stop the watchdog FIRST so nothing respawns mid-stop.
    disable_watchdog(run)
    bundle_supervisor_signaled = signal_bundle_supervisor(vool_home)
    kill_watchdog_processes(run)

    # 2) gateway, then the API (tree-kill takes child agent workers with it).
    gateway_pid = kill_listener(run, GATEWAY_PORT, project_root)
    api_pid = kill_listener(run, API_PORT, project_root)
    if api_pid is None:
        api_pid = kill_api_by_pidfile(run, _pidfile(project_root, vool_home), project_root)

    # 3) marker (informational; a relaunch clears it).
    marker = project_root / STOPPED_MARKER
    with contextlib.suppress(Exception):
        marker.write_text("stopped\n", encoding="utf-8")

    return {
        "watchdog_disabled": True,
        "bundle_supervisor_signaled": bundle_supervisor_signaled,
        "gateway_pid": gateway_pid,
        "api_pid": api_pid,
        "marker": str(marker),
    }


# ---------------------------------------------------------------------------
# macOS / Linux path. On POSIX the watchdog is a launchd KeepAlive agent
# (installer/install_vool.sh install_macos_launch_agent), not a schtasks task,
# and processes are addressed with lsof + kill instead of Get-NetTCPConnection/taskkill.
# ---------------------------------------------------------------------------

LAUNCH_AGENT_LABEL = "ai.vool.runtime"
SYSTEMD_UNIT = "vool-runtime.service"


def launch_agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def disable_systemd_service(run: Runner) -> bool:
    """Linux: stop + disable the systemd --user keep-alive unit so it stops respawning the API.

    The installer registers vool-runtime.service with Restart=always (install_vool.sh
    install_linux_keepalive_service), so a bare port-kill is respawned within ~5s. Stopping the
    unit (and disabling it, so it stays down across logins) is the Linux analog of the macOS
    launchctl bootout and the Windows schtasks disable -- OpenClaw_VOOL.sh re-enables it on the
    next launch. No-op off Linux, without systemctl, or when the unit isn't installed (False).
    """
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which("systemctl") is None:
        return False
    res = _run(run, ["systemctl", "--user", "list-unit-files", SYSTEMD_UNIT])
    if SYSTEMD_UNIT not in _stdout(res):
        return False  # VOOL's unit isn't installed on this box; nothing to tear down.
    _run(run, ["systemctl", "--user", "stop", SYSTEMD_UNIT])
    _run(run, ["systemctl", "--user", "disable", SYSTEMD_UNIT])
    return True


def disable_launch_agent(run: Runner) -> bool:
    """macOS: boot the KeepAlive LaunchAgent out of the GUI domain so launchd stops respawning.

    Booting the job out unloads it now and drops KeepAlive supervision, so a manual port-kill
    sticks. The plist stays on disk, so RunAtLoad reloads it at the next login -- matching the
    Windows onlogon task, which also re-arms at logon. No-op off macOS or when the agent was
    never installed (returns False).
    """
    if sys.platform != "darwin":
        return False
    plist = launch_agent_plist_path()
    if not plist.exists():
        return False
    _run(run, ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
    return True


def _pids_listening(run: Runner, port: int) -> list[int]:
    res = _run(run, ["lsof", "-ti", f"tcp:{int(port)}", "-sTCP:LISTEN"])
    pids: list[int] = []
    for tok in _stdout(res).split():
        tok = tok.strip()
        if tok.isdigit():
            pid = int(tok)
            if pid > 0 and pid != os.getpid():
                pids.append(pid)
    return pids


def kill_listener_posix(run: Runner, port: int, *, sleep: Callable[[float], Any] = time.sleep) -> int | None:
    """SIGTERM every listener on ``port``, then SIGKILL any survivor. Returns the first pid, or None."""
    pids = _pids_listening(run, port)
    if not pids:
        return None
    for pid in pids:
        _run(run, ["kill", "-TERM", str(pid)])
    sleep(1.0)
    for pid in _pids_listening(run, port):
        _run(run, ["kill", "-KILL", str(pid)])
    return pids[0]


def _pid_is_python_posix(run: Runner, pid: int) -> bool:
    res = _run(run, ["ps", "-p", str(int(pid)), "-o", "comm="])
    return "python" in _stdout(res).lower()


def kill_api_by_pidfile_posix(run: Runner, pidfile: Path) -> int | None:
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    if not _pid_is_python_posix(run, pid):
        return None  # stale/reused pid -> never kill an unrelated process
    _run(run, ["kill", "-TERM", str(pid)])
    _run(run, ["kill", "-KILL", str(pid)])
    return pid


def hard_stop_posix(
    project_root: Path,
    vool_home: Path | None = None,
    *,
    run: Runner = subprocess.run,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    project_root = Path(project_root)
    vool_home = Path(vool_home) if vool_home else None

    # 1) tear down the keep-alive supervisor FIRST so nothing respawns mid-stop:
    #    launchd (macOS) or the systemd --user Restart=always unit (Linux).
    watchdog_disabled = disable_launch_agent(run) or disable_systemd_service(run)

    # 2) gateway, then the API -- frees the ports whether launchd- or nohup-spawned.
    gateway_pid = kill_listener_posix(run, GATEWAY_PORT, sleep=sleep)
    api_pid = kill_listener_posix(run, API_PORT, sleep=sleep)
    if api_pid is None:
        api_pid = kill_api_by_pidfile_posix(run, _pidfile(project_root, vool_home))

    # 3) marker (informational; a relaunch clears it).
    marker = project_root / STOPPED_MARKER
    with contextlib.suppress(Exception):
        marker.write_text("stopped\n", encoding="utf-8")

    return {
        "watchdog_disabled": watchdog_disabled,
        "gateway_pid": gateway_pid,
        "api_pid": api_pid,
        "marker": str(marker),
    }


def spawn_detached_stop(*, project_root: Path, vool_home: Path | None) -> bool:
    """Launch this stopper DETACHED so it survives the API tree-kill it performs.

    Mirrors installer.self_update.spawn_detached_update: the stopper must not be a child of the
    API server, or the tree-kill that stops the API would also kill the stopper mid-way. The
    intermediate start_windows_detached breaks out of the server's job and orphans the stopper.
    """
    root = Path(project_root).resolve()
    stop_cmd = [sys.executable, "-m", "installer.vool_stop", "--project-root", str(root)]
    if vool_home:
        stop_cmd += ["--vool-home", str(Path(vool_home).resolve())]
    data_dir = (Path(vool_home).resolve() / "data") if vool_home else (root / "data")
    with contextlib.suppress(Exception):
        data_dir.mkdir(parents=True, exist_ok=True)
    launcher_cmd = [
        sys.executable, "-m", "installer.start_windows_detached",
        "--cwd", str(root),
        "--stdout", str(data_dir / "vool_stop.out.log"),
        "--stderr", str(data_dir / "vool_stop.err.log"),
        "--", *stop_cmd,
    ]
    try:
        if sys.platform == "win32":
            # DETACHED | NEW_GROUP | NO_WINDOW | BREAKAWAY_FROM_JOB
            flags = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
            subprocess.Popen(launcher_cmd, cwd=str(root), creationflags=flags, close_fds=True)
        else:
            subprocess.Popen(launcher_cmd, cwd=str(root), start_new_session=True)
        return True
    except Exception:
        return False


def _resolve_vool_home(explicit: str) -> Path | None:
    if explicit:
        return Path(explicit)
    with contextlib.suppress(Exception):
        from core.runtime_paths import active_vool_home
        home = active_vool_home()
        return Path(home) if home else None
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hard-stop all VOOL processes.")
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--vool-home", default="")
    args = ap.parse_args(argv)
    project_root = Path(args.project_root)
    vool_home = _resolve_vool_home(args.vool_home)
    if sys.platform == "win32":
        result = hard_stop(project_root, vool_home)
    else:
        result = hard_stop_posix(project_root, vool_home)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
