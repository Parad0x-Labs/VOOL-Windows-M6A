"""The installer, Stop button and bundle supervisor stop only this installation's Python."""
from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.process_liveness import pid_alive
from tests.test_windows_stop_root_quoting import installer_escaped_install_dir, installer_stop_parameters

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="the ownership predicates are evaluated by Windows PowerShell",
)

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("VOOL_TEST_SOURCE_ROOT", _REPOSITORY_ROOT))


def _load_source_module(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if ROOT == _REPOSITORY_ROOT:
    from installer import vool_stop
    from installer.bundle import bundle_supervisor
else:
    vool_stop = _load_source_module("job4_source_vool_stop", ROOT / "installer" / "vool_stop.py")
    bundle_supervisor = _load_source_module(
        "job4_source_bundle_supervisor",
        ROOT / "installer" / "bundle" / "bundle_supervisor.py",
    )

INSTALL = r"C:\Program Files\VOOL's [prod]"
SOURCE_CHECKOUT = r"C:\src\VOOL's checkout [dev]"
SUPERVISOR_PID = 60001


def _installer_command(install_root: Path) -> str:
    text = (ROOT / "installer" / "bundle" / "vool.iss").read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "Stop-Process" in line)
    if "StringChangeEx(InstallDir, '''', '''''', True);" not in text:
        command_start = line.index('-Command "') + len('-Command "')
        return line[command_start:line.rindex('"')].replace("''", "'")

    assert "InstallDir := ExpandConstant('{app}');" in text
    root_setup = line.index("; $rootPrefix")
    command_end = line.rindex('"')
    production_tail = line[root_setup:command_end].replace("''", "'")
    return "$r = '" + installer_escaped_install_dir(text, str(install_root)) + "'" + production_tail


def _stop_button_command(project_root: Path) -> str:
    captured: list[list[str]] = []

    def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    if "install_root" in signature(vool_stop.kill_listener).parameters:
        vool_stop.kill_listener(capture, vool_stop.API_PORT, project_root)
    else:
        vool_stop.kill_listener(capture, vool_stop.API_PORT)
    return captured[0][captured[0].index("-Command") + 1]


def _supervisor_command(monkeypatch: pytest.MonkeyPatch) -> str:
    captured: list[list[str]] = []

    def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    if hasattr(bundle_supervisor, "_is_windows_platform"):
        monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    # Replace the module reference so the evaluator keeps the real subprocess.run.
    monkeypatch.setattr(bundle_supervisor, "subprocess", SimpleNamespace(run=capture))
    supervisor = object.__new__(bundle_supervisor.BundleSupervisor)
    supervisor.root = Path(INSTALL)
    supervisor._log = lambda *_: None
    supervisor._clear_stale_api_processes()
    return captured[0][captured[0].index("-Command") + 1]


def _production_setup(command: str, *, supervisor: bool = False) -> str:
    if supervisor:
        return command[: command.index("$supervisorPid =")]
    markers = (
        "$p = Get-NetTCPConnection",
        "$v = Get-CimInstance",
        "Get-CimInstance Win32_Process",
    )
    positions = [command.find(marker) for marker in markers if marker in command]
    assert positions, f"production ownership setup missing from command: {command}"
    return command[: min(positions)]


def _predicate(command: str) -> str:
    matches = list(re.finditer(r"Where-Object\s*\{(.*?)\}\s*(?:\||;|\}|\)|$)", command, flags=re.DOTALL))
    assert matches, f"ownership predicate missing from command: {command}"
    return matches[-1].group(1)


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _evaluate_legacy_process(
    command: str,
    *,
    name: str,
    executable_path: str | None,
    command_line: str | None,
) -> bool:
    process = (
        "[pscustomobject]@{Name=" + _ps_literal(name)
        + ";ExecutablePath=" + ("$null" if executable_path is None else _ps_literal(executable_path))
        + ";CommandLine=" + ("$null" if command_line is None else _ps_literal(command_line)) + "}"
    )
    ps = "$case = " + process + "; [bool]($case | Where-Object {" + _predicate(command) + "})"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr or result.stdout}\nPowerShell={ps}"
    observed = result.stdout.strip().lower()
    assert observed in {"true", "false"}, result.stdout
    return observed == "true"


def _evaluate(
    command: str,
    *,
    project_root: Path,
    layout: str,
    expected_window: bool,
    supervisor: bool = False,
    case_labels: set[str] | None = None,
) -> dict[str, bool]:
    predicate = _predicate(command)
    root = str(project_root).rstrip("\\/")
    api_script = root + (
        r"\apps\vool_api_server.py" if layout in {"source", "bundle"}
        else r"\app\apps\vool_api_server.py"
    )
    root_python = (
        root + r"\.venv\Scripts\python.exe"
        if layout == "source"
        else INSTALL + r"\python\python.exe"
    )
    window_python = INSTALL + r"\python\pythonw.exe"
    cases = [
        (
            "root-exe",
            "python.exe",
            root_python,
            fr'"{root_python}" -m unrelated',
            layout in {"installer", "source"} and not supervisor,
        ),
        (
            "api-external-python",
            "python.exe",
            r"C:\Python\python.exe",
            fr'"C:\Python\python.exe" -B -X utf8 "{api_script}" --port 11435',
            True,
        ),
        (
            "api-external-bare-python",
            "python.exe",
            r"C:\Python\python.exe",
            fr'python.exe "{api_script}" --port 11435',
            True,
        ),
        (
            "bundled-window",
            "pythonw.exe",
            window_python,
            fr'"C:\Python\pythonw.exe" "{INSTALL}\vool_window.py"',
            expected_window,
        ),
        (
            "source-checkout-different-root",
            "python.exe",
            r"C:\Python\python.exe",
            fr'"C:\Python\python.exe" "{SOURCE_CHECKOUT}-different\apps\vool_api_server.py"',
            False,
        ),
        (
            "sibling-folder",
            "python.exe",
            r"C:\Python\python.exe",
            fr'"C:\Python\python.exe" "{INSTALL}-dev\app\apps\vool_api_server.py"',
            False,
        ),
        (
            "wildcard-sibling",
            "python.exe",
            root.replace("[prod]", "p").replace("[dev]", "d") + r"\python.exe",
            "-m unrelated",
            False,
        ),
        (
            "argument-only-mention",
            "python.exe",
            r"C:\Python\python.exe",
            fr'"C:\Python\python.exe" "C:\Other\worker.py" --template "{api_script}"',
            False,
        ),
        (
            "code-mention",
            "python.exe",
            r"C:\Python\python.exe",
            fr'"C:\Python\python.exe" -c "runpy.run_path({api_script})"',
            False,
        ),
        (
            "foreign-filename-mention",
            "python.exe",
            r"C:\Python\python.exe",
            '"C:\\Python\\python.exe" "C:\\Other\\worker.py" --note vool_api_server.py',
            False,
        ),
        (
            "source-module-listener",
            "pythonw.exe",
            r"C:\Python312\pythonw.exe",
            r'"C:\Python312\pythonw.exe" -m apps.vool_api_server --port 11435',
            layout == "source",
        ),
        (
            "source-module-foreign-parent",
            "pythonw.exe",
            r"C:\Python312\pythonw.exe",
            r'"C:\Python312\pythonw.exe" -m apps.vool_api_server --port 11435',
            False,
        ),
        (
            "source-module-missing-parent",
            "pythonw.exe",
            r"C:\Python312\pythonw.exe",
            r'"C:\Python312\pythonw.exe" -m apps.vool_api_server --port 11435',
            False,
        ),
        (
            "module-mentioned-later",
            "pythonw.exe",
            r"C:\Python312\pythonw.exe",
            r'"C:\Python312\pythonw.exe" "C:\Other\worker.py" -m apps.vool_api_server',
            False,
        ),
        ("unknown-command", "python.exe", r"C:\Python\python.exe", None, False),
        ("null-exe-and-command", "python.exe", None, None, False),
        (
            "powershell-self",
            "powershell.exe",
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            fr'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" "{INSTALL}\app\apps\vool_api_server.py"',
            False,
        ),
    ]
    if supervisor:
        cases.extend([
            (
                "supervisor-self",
                "pythonw.exe",
                INSTALL + r"\python\pythonw.exe",
                fr'"{INSTALL}\python\pythonw.exe" "{INSTALL}\bundle_supervisor.py" --root "{INSTALL}"',
                False,
            ),
            (
                "supervisor-parent",
                "python.exe",
                INSTALL + r"\python\python.exe",
                fr'"{INSTALL}\python\python.exe" "{api_script}"',
                False,
            ),
            (
                "supervisor-grandparent",
                "python.exe",
                INSTALL + r"\python\python.exe",
                fr'"{INSTALL}\python\python.exe" "{api_script}"',
                False,
            ),
        ])
    if layout == "installer":
        # Setup stops every matching Python on the machine: only the bundle's own interpreter, by
        # its exact path, is VOOL's, not any Python a user keeps inside the install folder.
        cases.append((
            "users-own-python-in-the-install-folder",
            "python.exe",
            root + r"\Python312\python.exe",
            fr'"{root}\Python312\python.exe" -m jupyter lab',
            False,
        ))
    if case_labels is not None:
        cases = [case for case in cases if case[0] in case_labels]

    if "$rootPrefix" not in command:
        observed = [
            _evaluate_legacy_process(
                command,
                name=name,
                executable_path=exe,
                command_line=cmd,
            )
            for _, name, exe, cmd, _ in cases
        ]
        observed_by_case = dict(zip((case[0] for case in cases), observed, strict=True))
        expected_by_case = {case[0]: case[4] for case in cases}
        assert observed_by_case == expected_by_case, (
            f"observed={observed_by_case}; expected={expected_by_case}"
        )
        return observed_by_case

    process_rows = []
    parent_rows = []
    parent_paths = {
        "source-module-foreign-parent": root + r"-other\.venv\Scripts\pythonw.exe",
    }
    if layout == "source":
        parent_paths["source-module-listener"] = root + r"\.venv\Scripts\pythonw.exe"
    for index, (label, name, exe, cmd, _) in enumerate(cases):
        process_id = {"supervisor-self": SUPERVISOR_PID, "supervisor-parent": SUPERVISOR_PID + 1,
                      "supervisor-grandparent": SUPERVISOR_PID + 2}.get(label, index + 1000)
        if label == "supervisor-self" or label == "supervisor-parent":
            parent_id = process_id + 1
        elif label in parent_paths:
            parent_id = 30000 + index
        elif label == "source-module-missing-parent":
            parent_id = 50000 + index
        else:
            parent_id = 0
        if label in parent_paths:
            parent_rows.append(
                "[pscustomobject]@{Name='pythonw.exe';ExecutablePath="
                + _ps_literal(parent_paths[label])
                + f";CommandLine='';ProcessId={parent_id};ParentProcessId=0}}"
            )
        exe_value = "$null" if exe is None else _ps_literal(exe)
        command_value = "$null" if cmd is None else _ps_literal(cmd)
        process_rows.append(
            "[pscustomobject]@{Label=" + _ps_literal(label)
            + ";Name=" + _ps_literal(name)
            + ";ExecutablePath=" + exe_value
            + ";CommandLine=" + command_value
            + f";ProcessId={process_id};ParentProcessId={parent_id}}}"
        )
    rows = ",".join(process_rows)
    parents = ",".join(parent_rows)
    setup = (
        _production_setup(command, supervisor=supervisor)
        + "$cases = @("
        + rows
        + "); $all = @($cases); $all += @(" + parents + "); "
        + "function Get-CimInstance { [CmdletBinding()] param([string]$ClassName, [string]$Filter); "
        + "if ($Filter -match 'ProcessId=(\\d+)') { $requestedId = [int]$Matches[1]; "
        + "return $all | Where-Object { [int]$_.ProcessId -eq $requestedId } | Select-Object -First 1 }; "
        + "return $all } "
    )
    if supervisor:
        chain_start = command.index("$protectedPids =")
        chain_end = command.rindex("$all | Where-Object")
        setup += f"$supervisorPid = {SUPERVISOR_PID}; " + command[chain_start:chain_end]
    ps = setup + "$cases | ForEach-Object { $_.Label + '=' + [string][bool]($_ | Where-Object {" + predicate + "}) }"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr or result.stdout}\nPowerShell={ps}"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    observed = {
        line.rsplit("=", 1)[0]: line.rsplit("=", 1)[1].lower() == "true"
        for line in lines
    }
    expected = {case[0]: case[4] for case in cases}
    assert len(observed) == len(cases), result.stdout
    assert observed == expected, f"observed={observed}; expected={expected}"
    return observed


def test_installer_stops_only_its_installation_processes() -> None:
    command = _installer_command(Path(INSTALL))
    source = (ROOT / "installer" / "bundle" / "vool.iss").read_text(encoding="utf-8")
    assert "Get-NetTCPConnection" not in command
    assert "OwningProcess" not in command
    assert "-like" not in command
    assert "ExpandConstant('{app}')" in source
    _evaluate(
        command,
        project_root=Path(INSTALL),
        layout="installer",
        expected_window=True,
    )


def test_stop_button_stops_only_its_installation_api() -> None:
    source = (ROOT / "installer" / "vool_stop.py").read_text(encoding="utf-8")
    assert "pid > 0 and pid != os.getpid()" in source
    for layout, project_root in (
        ("source", Path(SOURCE_CHECKOUT)),
        ("bundle", Path(INSTALL) / "app"),
    ):
        command = _stop_button_command(project_root)
        assert "Get-NetTCPConnection" in command
        assert "OwningProcess" in command
        assert "-like" not in command
        _evaluate(
            command,
            project_root=project_root,
            layout=layout,
            expected_window=False,
        )


def test_source_checkout_module_listener_requires_its_venv_parent() -> None:
    command = _stop_button_command(Path(SOURCE_CHECKOUT))
    observed = _evaluate(
        command,
        project_root=Path(SOURCE_CHECKOUT),
        layout="source",
        expected_window=False,
        case_labels={
            "source-module-listener",
            "source-module-foreign-parent",
            "source-module-missing-parent",
            "module-mentioned-later",
        },
    )
    assert observed == {
        "source-module-listener": True,
        "source-module-foreign-parent": False,
        "source-module-missing-parent": False,
        "module-mentioned-later": False,
    }


def test_stop_root_setup_quotes_apostrophes_spaces_and_brackets() -> None:
    source_root = Path(SOURCE_CHECKOUT)
    stop_command = _stop_button_command(source_root)
    stop_literal = vool_stop._windows_install_root_literal(source_root)
    assert "$r = " + stop_literal + ";" in stop_command
    _evaluate(
        stop_command,
        project_root=source_root,
        layout="source",
        expected_window=False,
        case_labels={"root-exe", "wildcard-sibling"},
    )

    installer_command = _installer_command(Path(INSTALL))
    assert "$r = '" + INSTALL.replace("'", "''") + "';" in installer_command
    _evaluate(
        installer_command,
        project_root=Path(INSTALL),
        layout="installer",
        expected_window=True,
        case_labels={"root-exe", "wildcard-sibling", "bundled-window", "sibling-folder"},
    )


def test_stop_button_pidfile_fallback_checks_install_ownership(tmp_path: Path) -> None:
    for layout, project_root in (
        ("source", Path(SOURCE_CHECKOUT)),
        ("bundle", Path(INSTALL) / "app"),
    ):
        pidfile = tmp_path / layout / "vool_api.pid"
        pidfile.parent.mkdir(parents=True)
        pidfile.write_text("4321", encoding="utf-8")
        captured: list[list[str]] = []

        def capture(
            args: list[str],
            *,
            captured_args: list[list[str]] = captured,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            captured_args.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        vool_stop.kill_api_by_pidfile(capture, pidfile, project_root)
        command = captured[0][captured[0].index("-Command") + 1]
        assert "ProcessId=4321" in command
        assert "-like" not in command
        _evaluate(
            command,
            project_root=project_root,
            layout=layout,
            expected_window=False,
        )


def test_bundle_stop_button_selects_api_at_agent_brake_root() -> None:
    project_root = Path(INSTALL) / "app"
    command = _stop_button_command(project_root)
    if "$rootPrefix" in command:
        observed = _evaluate(
            command,
            project_root=project_root,
            layout="bundle",
            expected_window=False,
        )
        assert observed["api-external-python"] is True
        return

    for api_script in (
        SOURCE_CHECKOUT + r"\apps\vool_api_server.py",
        str(project_root) + r"\apps\vool_api_server.py",
    ):
        assert _evaluate_legacy_process(
            command,
            name="python.exe",
            executable_path=r"C:\Python\python.exe",
            command_line=fr'"C:\Python\python.exe" "{api_script}" --port 11435',
        )


def test_bundle_supervisor_does_not_select_its_window_host(monkeypatch: pytest.MonkeyPatch) -> None:
    command = _supervisor_command(monkeypatch)
    if "$scriptArgument" in command:
        observed = _evaluate(
            command,
            project_root=Path(INSTALL),
            layout="supervisor",
            expected_window=False,
            supervisor=True,
        )
        assert observed["bundled-window"] is False
        return

    assert not _evaluate_legacy_process(
        command,
        name="pythonw.exe",
        executable_path=INSTALL + r"\python\pythonw.exe",
        command_line=fr'"{INSTALL}\python\pythonw.exe" "{INSTALL}\vool_window.py"',
    )


def test_supervisor_stops_only_its_installation_api_and_excludes_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _supervisor_command(monkeypatch)
    assert "ParentProcessId" in command
    assert "protectedPids" in command
    assert "-like" not in command
    assert "ExecutablePath" not in _predicate(command)
    _evaluate(
        command,
        project_root=Path(INSTALL),
        layout="supervisor",
        expected_window=False,
        supervisor=True,
    )


# Real processes. The tests above evaluate each predicate against fake process rows. These start
# stand-in VOOL processes in each layout and run the production stop commands for real: the Stop
# button's listener and pidfile kills, the supervisor's stale-API cleanup and the installer's stop.
_FAKE_VOOL_PROCESS = '''\
"""Stands in for one VOOL process: it listens on a free loopback port until it is killed."""
import os
import socket
import time

server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen()
ready = os.environ["VOOL_TEST_READY_FILE"]
with open(ready + ".tmp", "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()} {server.getsockname()[1]}")
os.replace(ready + ".tmp", ready)
while True:
    time.sleep(1)
'''
# A space, [brackets] and every quote PowerShell honours (', U+2018, U+2019, U+201A, U+201B) in
# both roots.
REAL_CHECKOUT_NAME = "VOOL's \u2018checkout\u2019 \u201adev\u201b [x]"
REAL_INSTALL_NAME = "VOOL's \u2018prod\u2019 \u201ax\u201b [x]"


class _Running:
    def __init__(self, label: str, popen: subprocess.Popen[bytes], log: Path) -> None:
        self.label = label
        self.popen = popen
        self.log = log
        self.pid = 0  # the process that listens: a venv launcher's child, not the launcher itself
        self.port = 0


class _FakeVool:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True)
        self.directory = directory
        self.running: list[_Running] = []

    def start(self, label: str, args: list[str], *, cwd: Path, env: dict[str, str]) -> _Running:
        ready = self.directory / f"{len(self.running)}.ready"
        log = self.directory / f"{len(self.running)}.log"
        with log.open("wb") as output:
            popen = subprocess.Popen(
                args,
                cwd=cwd,
                env={**env, "VOOL_TEST_READY_FILE": str(ready)},
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        running = _Running(label, popen, log)
        self.running.append(running)
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert popen.poll() is None, f"{label} exited with {popen.returncode}: {_log_text(log)}"
            assert time.monotonic() < deadline, f"{label} never started listening: {_log_text(log)}"
            time.sleep(0.05)
        running.pid, running.port = (int(part) for part in ready.read_text(encoding="utf-8").split())
        return running

    def close(self) -> None:
        for running in self.running:
            if running.popen.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(running.popen.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            with contextlib.suppress(subprocess.TimeoutExpired):
                running.popen.wait(timeout=15)


@pytest.fixture
def fake_vool(tmp_path: Path):
    processes = _FakeVool(tmp_path / "processes")
    try:
        yield processes
    finally:
        processes.close()


def _log_text(log: Path) -> str:
    return log.read_bytes().decode("utf-8", "replace")[-2000:]


def _environment(**extra: str) -> dict[str, str]:
    inherited = {"PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__"}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in inherited}
    environment.update(extra)
    return environment


def _describe(pid: int) -> str:
    query = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
        "if ($p) { $parent = Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\"; "
        "'exe=' + $p.ExecutablePath + ' | command=' + $p.CommandLine + ' | parent=' + $parent.ExecutablePath } "
        "else { 'no such process' }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return (result.stdout or result.stderr).strip()


def _accepts(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _assert_still_running(*processes: _Running) -> None:
    for running in processes:
        assert pid_alive(running.pid) and _accepts(running.port), f"{running.label} was stopped"


def _assert_stopped(running: _Running) -> None:
    deadline = time.monotonic() + 20
    while pid_alive(running.pid):
        assert time.monotonic() < deadline, f"{running.label} is still running: {_describe(running.pid)}"
        time.sleep(0.1)
    assert not _accepts(running.port), f"{running.label} still listens on {running.port}"


def _pidfile(tmp_path: Path, running: _Running) -> Path:
    pidfile = tmp_path / "pidfiles" / f"{running.pid}.pid"
    pidfile.parent.mkdir(exist_ok=True)
    pidfile.write_text(str(running.pid), encoding="utf-8")
    return pidfile


def _copy_interpreter(destination: Path) -> None:
    # The bundle embeds a real CPython, not a venv launcher: copy the base interpreter and its DLLs.
    base = Path(sys.base_prefix)
    assert (base / "python.exe").is_file() and (base / "pythonw.exe").is_file(), f"no CPython at {base}"
    destination.mkdir(parents=True)
    for source in (base / "python.exe", base / "pythonw.exe", *base.glob("*.dll")):
        shutil.copy2(source, destination / source.name)


def _bundle(root: Path) -> Path:
    _copy_interpreter(root / "python")
    for script in (
        root / "app" / "apps" / "vool_api_server.py",
        root / "vool_window.py",
        root / "bundle_supervisor.py",
    ):
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(_FAKE_VOOL_PROCESS, encoding="utf-8")
    return root


def _start_bundle(fake_vool: _FakeVool, root: Path, role: str, *, label: str) -> _Running:
    # The same command lines as VOOL.cmd and bundle_supervisor._start. The stand-in API listens on
    # a free port, whatever its --port says.
    python = root / "python"
    commands = {
        "supervisor": (
            [str(python / "pythonw.exe"), str(root / "bundle_supervisor.py"), "--root", str(root)],
            root,
        ),
        "api": (
            [str(python / "python.exe"), str(root / "app" / "apps" / "vool_api_server.py"), "--port", "11435"],
            root / "app",
        ),
        "window": ([str(python / "pythonw.exe"), str(root / "vool_window.py")], root),
    }
    args, cwd = commands[role]
    return fake_vool.start(label, args, cwd=cwd, env=_environment(PYTHONHOME=sys.base_prefix))


def _checkout(root: Path) -> Path:
    (root / "apps").mkdir(parents=True)
    (root / "apps" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "vool_api_server.py").write_text(_FAKE_VOOL_PROCESS, encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
        check=True,
        capture_output=True,
        env=_environment(),
        timeout=300,
    )
    return root


def _start_checkout_api(fake_vool: _FakeVool, root: Path, *, label: str) -> _Running:
    # Start_VOOL.bat's start: the venv's pythonw.exe with -m, PYTHONPATH at the checkout.
    running = fake_vool.start(
        label,
        [str(root / ".venv" / "Scripts" / "pythonw.exe"), "-m", "apps.vool_api_server"],
        cwd=root,
        env=_environment(PYTHONPATH=str(root)),
    )
    assert running.pid != running.popen.pid, (
        f"{label}: the venv's pythonw.exe listened itself instead of starting the base interpreter: "
        + _describe(running.pid)
    )
    return running


def _run_installer_stop(install_root: Path) -> None:
    iss = (ROOT / "installer" / "bundle" / "vool.iss").read_text(encoding="utf-8")
    # Setup's Exec hands CreateProcess "<file>" <params>; only Setup's own Pascal is modelled here.
    command_line = '"powershell.exe" ' + installer_stop_parameters(iss, str(install_root))
    result = subprocess.run(
        command_line,
        capture_output=True,
        check=False,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW,  # -WindowStyle Hidden would hide pytest's console
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace") or result.stdout.decode("utf-8", "replace")


def _start_script_api(fake_vool: _FakeVool, root: Path, *, label: str) -> _Running:
    # A Python outside the install running its API script: the script rule alone makes it VOOL's.
    return fake_vool.start(
        label,
        [sys.executable, str(root / "app" / "apps" / "vool_api_server.py"), "--port", "11435"],
        cwd=root / "app",
        env=_environment(),
    )


def _run_supervisor_cleanup(install_root: Path) -> None:
    # In the bundle the supervisor is a console-less pythonw; run its cleanup the same way, so its
    # -WindowStyle Hidden powershell never hides pytest's console.
    helper = (
        "import sys; from pathlib import Path; from installer.bundle import bundle_supervisor; "
        "cleaner = object.__new__(bundle_supervisor.BundleSupervisor); cleaner.root = Path(sys.argv[1]); "
        "cleaner._log = lambda message: sys.exit(message); cleaner._clear_stale_api_processes()"
    )
    result = subprocess.run(
        [sys.executable, "-c", helper, str(install_root)],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        check=False,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_a_checkout_api_started_through_its_venv_launcher_is_stopped_for_real(
    tmp_path: Path,
    fake_vool: _FakeVool,
) -> None:
    checkout = _checkout(tmp_path / REAL_CHECKOUT_NAME)
    sibling = _checkout(tmp_path / (REAL_CHECKOUT_NAME + "-dev"))
    other = _start_checkout_api(fake_vool, sibling, label="sibling checkout api")

    api = _start_checkout_api(fake_vool, checkout, label="checkout api")
    assert vool_stop.kill_listener(subprocess.run, api.port, checkout) == api.pid, _describe(api.pid)
    _assert_stopped(api)
    api.popen.wait(timeout=15)  # the launcher ends with its child
    assert vool_stop.kill_listener(subprocess.run, other.port, checkout) is None
    _assert_still_running(other)

    api = _start_checkout_api(fake_vool, checkout, label="checkout api, pidfile")
    assert vool_stop.kill_api_by_pidfile(subprocess.run, _pidfile(tmp_path, api), checkout) == api.pid, (
        _describe(api.pid)
    )
    _assert_stopped(api)
    assert vool_stop.kill_api_by_pidfile(subprocess.run, _pidfile(tmp_path, other), checkout) is None
    _assert_still_running(other)


def test_every_bundle_stop_stops_only_this_install_for_real(tmp_path: Path, fake_vool: _FakeVool) -> None:
    app = _bundle(tmp_path / "Programs" / REAL_INSTALL_NAME)
    sibling = _bundle(tmp_path / "Programs" / (REAL_INSTALL_NAME + "-dev"))
    worker = tmp_path / "elsewhere" / "worker.py"
    worker.parent.mkdir()
    worker.write_text(_FAKE_VOOL_PROCESS, encoding="utf-8")
    _copy_interpreter(app / "Python312")
    supervisor = _start_bundle(fake_vool, app, "supervisor", label="supervisor")
    window = _start_bundle(fake_vool, app, "window", label="window host")
    sibling_api = _start_bundle(fake_vool, sibling, "api", label="sibling install api")
    others = [
        sibling_api,
        _start_bundle(fake_vool, sibling, "supervisor", label="sibling install supervisor"),
        _start_bundle(fake_vool, sibling, "window", label="sibling install window host"),
        fake_vool.start("unrelated python", [sys.executable, str(worker)], cwd=worker.parent, env=_environment()),
        _start_script_api(fake_vool, sibling, label="a plain python running the sibling install's api script"),
        fake_vool.start(
            "the user's own python inside the install folder",
            [str(app / "Python312" / "python.exe"), str(worker)],
            cwd=worker.parent,
            env=_environment(PYTHONHOME=sys.base_prefix),
        ),
    ]

    # The /stopall brake runs the Stop module at the bundle's code root, {app}\app.
    api = _start_bundle(fake_vool, app, "api", label="api")
    assert vool_stop.kill_listener(subprocess.run, api.port, app / "app") == api.pid, _describe(api.pid)
    _assert_stopped(api)
    assert vool_stop.kill_listener(subprocess.run, sibling_api.port, app / "app") is None
    _assert_still_running(supervisor, window, *others)

    api = _start_bundle(fake_vool, app, "api", label="api, pidfile")
    assert vool_stop.kill_api_by_pidfile(subprocess.run, _pidfile(tmp_path, api), app / "app") == api.pid
    _assert_stopped(api)
    assert vool_stop.kill_api_by_pidfile(subprocess.run, _pidfile(tmp_path, sibling_api), app / "app") is None
    _assert_still_running(supervisor, window, *others)

    # The supervisor clears a stale API before each API start, at its own --root.
    api = _start_bundle(fake_vool, app, "api", label="stale api")
    script_api = _start_script_api(fake_vool, app, label="stale api in a plain python")
    _run_supervisor_cleanup(app)
    _assert_stopped(api)
    _assert_stopped(script_api)
    _assert_still_running(supervisor, window, *others)

    # Setup stops every process of this install before it replaces any file.
    api = _start_bundle(fake_vool, app, "api", label="api, installer")
    script_api = _start_script_api(fake_vool, app, label="api in a plain python, installer")
    _run_installer_stop(app)
    for running in (api, script_api, window, supervisor):
        _assert_stopped(running)
    _assert_still_running(*others)
