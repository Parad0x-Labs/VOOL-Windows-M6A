"""The Windows stops still own VOOL when the install folder is spelled another way.

The Stop module, its pidfile fallback and the bundle supervisor's stale-API scan decide
"is this process this installation's" by comparing paths as text. The same install folder
reached through a directory junction, an 8.3 short name, a different case or a trailing
separator is the SAME install, but a VOOL process started with that other spelling was
missed and left running, and a Stop invoked through that other spelling spared the API it
should have stopped (docs/WINDOWS_PORT_CONTRACTS.md "Known residuals": "No test covers
it"). These tests cover it: both directions (the root spelled another way, the process
started through another spelling of the same folder) are still owned, a foreign folder in
any of those spellings is still not owned, a root that cannot be canonicalized refuses to
act, and the real Stop button tree-kill really stops the own API across spellings.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.process_liveness import pid_alive
from installer import vool_stop
from installer.bundle import bundle_supervisor
from tests.test_windows_installer_stops_only_this_install import _predicate, _ps_literal

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="the ownership predicates are evaluated by Windows PowerShell",
)


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )


def _captured_runner(captured: list[list[str]]):
    def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    return capture


def _listener_command(project_root: Path) -> str:
    captured: list[list[str]] = []
    vool_stop.kill_listener(_captured_runner(captured), vool_stop.API_PORT, project_root)
    return captured[0][captured[0].index("-Command") + 1]


def _pidfile_command(project_root: Path, pidfile: Path) -> str:
    captured: list[list[str]] = []
    vool_stop.kill_api_by_pidfile(_captured_runner(captured), pidfile, project_root)
    return captured[0][captured[0].index("-Command") + 1]


def _supervisor_command(monkeypatch: pytest.MonkeyPatch, root: Path) -> str:
    captured: list[list[str]] = []
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(bundle_supervisor, "subprocess", SimpleNamespace(run=_captured_runner(captured)))
    supervisor = object.__new__(bundle_supervisor.BundleSupervisor)
    supervisor.root = root
    supervisor._log = lambda *_: None
    supervisor._clear_stale_api_processes()
    return captured[0][captured[0].index("-Command") + 1]


def _owns(
    command: str, *, executable_path: str, command_line: str, parent_path: str = ""
) -> bool:
    """Evaluate a stop command's ownership predicate against one fake process (and parent).

    The process query is replaced with fake rows, exactly like the evaluator in
    test_windows_installer_stops_only_this_install.py; the predicate itself runs for real
    in PowerShell, so any path canonicalization it does is exercised for real.
    """
    markers = (command.find("$p = Get-NetTCPConnection"), command.find("$v = Get-CimInstance"))
    marker = min(found for found in markers if found >= 0)
    assert marker > 0, command
    setup = command[:marker]
    ps = (
        setup
        + "$case = [pscustomobject]@{Name='python.exe';ExecutablePath=" + _ps_literal(executable_path)
        + ";CommandLine=" + _ps_literal(command_line) + ";ProcessId=4100;ParentProcessId=4200}; "
        + "$parent = [pscustomobject]@{Name='pythonw.exe';ExecutablePath=" + _ps_literal(parent_path)
        + ";CommandLine='';ProcessId=4200;ParentProcessId=0}; "
        + "$all = @($case, $parent); "
        + "function Get-CimInstance { [CmdletBinding()] param([string]$ClassName, [string]$Filter); "
        + "if ($Filter -match 'ProcessId=(\\d+)') { $requestedId = [int]$Matches[1]; "
        + "return $all | Where-Object { [int]$_.ProcessId -eq $requestedId } | Select-Object -First 1 }; "
        + "return $all }; "
        + "[bool]($case | Where-Object {" + _predicate(command) + "})"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{result.stderr or result.stdout}\nPowerShell={ps}"
    observed = result.stdout.strip().lower()
    assert observed in {"true", "false"}, result.stdout
    return observed == "true"


def _api_row(script_spelling: str) -> str:
    """A foreign python running the API script of the install spelled ``script_spelling``."""
    return f'"C:\\Python\\python.exe" "{script_spelling}" --port 11435'


def test_a_junction_spelled_root_still_owns_its_long_started_api(tmp_path: Path) -> None:
    root = tmp_path / "VOOL checkout"
    (root / "apps").mkdir(parents=True)
    junction = tmp_path / "junction to vool"
    _junction(junction, root)
    sibling = tmp_path / "VOOL checkout-dev"
    (sibling / "apps").mkdir(parents=True)

    # Stop is invoked through the junction spelling while the API was started with the real
    # folder's long spelling: the same install, so it is owned.
    command = _listener_command(junction)
    assert _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(root / "apps" / "vool_api_server.py")),
    )
    # A genuinely different folder in the same spellings is still not ours.
    assert not _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(sibling / "apps" / "vool_api_server.py")),
    )


def test_an_api_started_through_a_junction_is_still_owned(tmp_path: Path) -> None:
    root = tmp_path / "VOOL checkout"
    (root / "apps").mkdir(parents=True)
    (root / "apps" / "vool_api_server.py").write_text("x = 1\n", encoding="utf-8")
    junction = tmp_path / "junction to vool"
    _junction(junction, root)
    sibling = tmp_path / "VOOL checkout-dev"
    (sibling / "apps").mkdir(parents=True)
    (sibling / "apps" / "vool_api_server.py").write_text("x = 1\n", encoding="utf-8")
    link_to_sibling = tmp_path / "link to the sibling"
    _junction(link_to_sibling, sibling)

    # The API was started through a junction spelling of this install while Stop holds the
    # long spelling: the same install, so it is owned (the script file exists, so the
    # predicate's canonicalization resolves the junction for real).
    command = _listener_command(root)
    assert _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(junction / "apps" / "vool_api_server.py")),
    )
    # A junction that leads to a different install is that install's spelling, not ours.
    assert not _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(link_to_sibling / "apps" / "vool_api_server.py")),
    )


def test_an_8_3_spelled_root_still_owns_the_long_spelling() -> None:
    # C:\PROGRA~1 is the one folder this machine reliably also spells in 8.3; nothing is
    # created there -- it is only used as the root spelling the stop is invoked with.
    short_root = "C:\\PROGRA~1"
    if os.path.realpath(short_root) == short_root:
        pytest.skip("C:\\PROGRA~1 has no 8.3 long form on this machine")
    command = _listener_command(Path(short_root))
    assert _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(r"C:\Program Files\apps\vool_api_server.py"),
    )
    assert not _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(r"C:\Program Files (x86)\apps\vool_api_server.py"),
    )


def test_case_and_trailing_separator_spellings_still_owned(tmp_path: Path) -> None:
    root = tmp_path / "VOOL checkout"
    (root / "apps").mkdir(parents=True)
    junction = tmp_path / "junction to vool"
    _junction(junction, root)

    # Case and a trailing separator already matched; a junction spelling with both on top
    # must match too, and the row's own case difference must not break it.
    command = _listener_command(Path(str(junction).upper() + "\\"))
    assert _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(root / "apps" / "vool_api_server.py").upper()),
    )


def test_the_pidfile_fallback_owns_both_junction_spellings(tmp_path: Path) -> None:
    root = tmp_path / "VOOL checkout"
    (root / "apps").mkdir(parents=True)
    # The running API's script exists, so a junction spelling of it resolves to the same
    # file for the process-side canonicalization.
    (root / "apps" / "vool_api_server.py").write_text("x = 1\n", encoding="utf-8")
    junction = tmp_path / "junction to vool"
    _junction(junction, root)
    pidfile = tmp_path / "vool_api.pid"
    pidfile.write_text("4321", encoding="utf-8")

    command = _pidfile_command(junction, pidfile)
    assert "ProcessId=4321" in command
    assert _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(root / "apps" / "vool_api_server.py")),
    )
    command = _pidfile_command(root, pidfile)
    assert _owns(
        command,
        executable_path=r"C:\Python\python.exe",
        command_line=_api_row(str(junction / "apps" / "vool_api_server.py")),
    )


def _supervisor_owns(command: str, *, command_line: str) -> bool:
    """Run the supervisor's whole stale-API scan against fake rows, harmlessly.

    The scan's process query and its Stop-Process call are replaced textually, so the
    assertion observes WHICH pids the scan decided to stop and nothing is ever signaled.
    """
    ps = (
        "function Get-CimInstance { [CmdletBinding()] param([string]$ClassName, [string]$Filter); "
        + "if ($Filter -match 'ProcessId=(\\d+)') { $requestedId = [int]$Matches[1]; "
        + "return $all | Where-Object { [int]$_.ProcessId -eq $requestedId } | Select-Object -First 1 }; "
        + "return $all }; "
        + "$case = [pscustomobject]@{Name='python.exe';ExecutablePath='C:\\Python\\python.exe';CommandLine="
        + _ps_literal(command_line)
        + ";ProcessId=4100;ParentProcessId=4200}; "
        + "$parent = [pscustomobject]@{Name='pythonw.exe';ExecutablePath='';CommandLine='';ProcessId=4200;ParentProcessId=0}; "
        + "$all = @($case, $parent); "
        + command.replace(
            "@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)", "@($case, $parent)"
        ).replace(
            "Stop-Process -Id $_.ProcessId -Force", "Write-Output ('would-stop:' + [int]$_.ProcessId)"
        )
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{result.stderr or result.stdout}\nPowerShell={ps}"
    return "would-stop:4100" in result.stdout


def test_the_supervisor_scan_owns_a_junction_spelled_stale_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "VOOL bundle"
    (root / "app" / "apps").mkdir(parents=True)
    (root / "app" / "apps" / "vool_api_server.py").write_text("x = 1\n", encoding="utf-8")
    junction = tmp_path / "junction to the bundle"
    _junction(junction, root)
    sibling = tmp_path / "VOOL bundle-dev"
    (sibling / "app" / "apps").mkdir(parents=True)
    (sibling / "app" / "apps" / "vool_api_server.py").write_text("x = 1\n", encoding="utf-8")

    command = _supervisor_command(monkeypatch, root)
    assert _supervisor_owns(
        command,
        command_line=_api_row(str(junction / "app" / "apps" / "vool_api_server.py")),
    )
    assert not _supervisor_owns(
        command,
        command_line=_api_row(str(sibling / "app" / "apps" / "vool_api_server.py")),
    )


_STANDIN = (
    '"""Stands in for one VOOL process: it listens on a free loopback port until killed."""\n'
    "import os, socket, time\n"
    "server = socket.socket()\n"
    'server.bind(("127.0.0.1", 0))\n'
    "server.listen()\n"
    'ready = os.environ["VOOL_TEST_READY_FILE"]\n'
    'with open(ready + ".tmp", "w", encoding="utf-8") as handle:\n'
    '    handle.write(f"{os.getpid()} {server.getsockname()[1]}")\n'
    "os.replace(ready + \".tmp\", ready)\n"
    "while True:\n"
    "    time.sleep(1)\n"
)


def _start_standin(
    script: Path, tag: str, processes: list[subprocess.Popen]
) -> tuple[int, int]:
    # The ready/log names carry a tag: two stand-ins of one test share the script file
    # (through different spellings of it) and must not read each other's ready file.
    ready = script.parent / f"{script.stem}.{tag}.ready"
    log = script.parent / f"{script.stem}.{tag}.log"
    with log.open("wb") as output:
        popen = subprocess.Popen(
            [sys.executable, str(script)], cwd=str(script.parent),
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, "VOOL_TEST_READY_FILE": str(ready)},
        )
    processes.append(popen)
    deadline = time.monotonic() + 60
    while not ready.exists():
        assert popen.poll() is None, f"the stand-in exited {popen.returncode}"
        assert time.monotonic() < deadline, "the stand-in never started listening"
        time.sleep(0.05)
    pid, port = (int(part) for part in ready.read_text(encoding="utf-8").split())
    return pid, port


def test_a_junction_spelled_stop_really_stops_the_own_api(tmp_path: Path) -> None:
    """The real Stop-button tree-kill, both ways around the junction.

    Only processes this test itself started are ever addressed: kill_listener is only
    pointed at the port of a stand-in this test started, and the fallback taskkill in the
    finally block uses those same pids.
    """
    processes: list[subprocess.Popen] = []
    try:
        root = tmp_path / "VOOL checkout"
        (root / "apps").mkdir(parents=True)
        script = root / "apps" / "vool_api_server.py"
        script.write_text(_STANDIN, encoding="utf-8")
        junction = tmp_path / "junction to vool"
        _junction(junction, root)

        # The API started with the long spelling; Stop is invoked through the junction.
        pid, port = _start_standin(script, "long-started", processes=processes)
        assert vool_stop.kill_listener(subprocess.run, port, junction) == pid
        deadline = time.monotonic() + 20
        while pid_alive(pid):
            assert time.monotonic() < deadline, "the junction-spelled stop left the own API running"
            time.sleep(0.1)

        # The API started through the junction spelling; Stop is invoked with the long root.
        pid, port = _start_standin(
            junction / "apps" / "vool_api_server.py", "junction-started", processes=processes
        )
        assert vool_stop.kill_listener(subprocess.run, port, root) == pid
        deadline = time.monotonic() + 20
        while pid_alive(pid):
            assert time.monotonic() < deadline, "the own API started via the junction survived"
            time.sleep(0.1)
    finally:
        for popen in processes:
            if popen.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(popen.pid), "/T", "/F"], check=False
                )


def test_a_root_that_cannot_be_canonicalized_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root whose canonicalization fails owns nothing: no query, no kill, an honest report."""

    def _refuse(_path: str) -> str:
        raise OSError("canonicalization refused by test")

    monkeypatch.setattr(os.path, "realpath", _refuse)
    captured: list[list[str]] = []
    run = _captured_runner(captured)
    assert vool_stop.kill_listener(run, vool_stop.API_PORT, tmp_path) is None

    pidfile = tmp_path / "vool_api.pid"
    pidfile.write_text("4321", encoding="utf-8")
    assert vool_stop.kill_api_by_pidfile(run, pidfile, tmp_path) is None

    logs: list[str] = []
    monkeypatch.setattr(bundle_supervisor, "subprocess", SimpleNamespace(run=_captured_runner([])))
    supervisor = object.__new__(bundle_supervisor.BundleSupervisor)
    supervisor.root = tmp_path
    supervisor._log = logs.append
    supervisor._clear_stale_api_processes()

    assert captured == [], "a refused root must not run any stop command"
    assert not any("taskkill" in " ".join(args) for args in captured)
    assert any("canonical" in message.lower() for message in logs), logs
