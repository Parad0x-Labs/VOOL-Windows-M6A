"""The installer, Stop button and bundle supervisor stop only this installation's Python."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import vool_stop
from installer.bundle import bundle_supervisor

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="the ownership predicates are evaluated by Windows PowerShell",
)

ROOT = Path(__file__).resolve().parent.parent
INSTALL = r"C:\Programs\VOOL[prod]"
SOURCE_CHECKOUT = r"C:\src\VOOL"
SUPERVISOR_PID = 60001


def _installer_command() -> str:
    text = (ROOT / "installer" / "bundle" / "vool.iss").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if "Stop-Process" in line).replace("''", "'")


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


def _assignment(command: str, name: str) -> str:
    match = re.search(rf"\${name}\s*=\s*(.*?);", command, flags=re.DOTALL)
    assert match, f"${name} assignment missing from command: {command}"
    return match.group(1)


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
    executable_path: str,
    command_line: str,
) -> bool:
    process = (
        "[pscustomobject]@{Name=" + _ps_literal(name)
        + ";ExecutablePath=" + _ps_literal(executable_path)
        + ";CommandLine=" + _ps_literal(command_line) + "}"
    )
    ps = "$case = " + process + "; [bool]($case | Where-Object {" + _predicate(command) + "})"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
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
) -> list[bool]:
    predicate = _predicate(command)
    root_prefix = _assignment(command, "rootPrefix")
    script = _assignment(command, "script")
    script_argument = _assignment(command, "scriptArgument")
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
        ("wildcard-sibling", "python.exe", r"C:\Programs\VOOLp\python.exe", "-m unrelated", False),
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

    process_rows = []
    for index, (label, name, exe, cmd, _) in enumerate(cases):
        process_id = {"supervisor-self": SUPERVISOR_PID, "supervisor-parent": SUPERVISOR_PID + 1,
                      "supervisor-grandparent": SUPERVISOR_PID + 2}.get(label, index + 1000)
        parent_id = process_id + 1 if label == "supervisor-self" else (
            process_id + 1 if label == "supervisor-parent" else 0
        )
        exe_value = "$null" if exe is None else _ps_literal(exe)
        command_value = "$null" if cmd is None else _ps_literal(cmd)
        process_rows.append(
            "[pscustomobject]@{Name=" + _ps_literal(name)
            + ";ExecutablePath=" + exe_value
            + ";CommandLine=" + command_value
            + f";ProcessId={process_id};ParentProcessId={parent_id}}}"
        )
    rows = ",".join(process_rows)
    setup = (
        "$r = " + _ps_literal(root) + "; "
        + "$rootPrefix = " + root_prefix + "; $script = " + script + "; $scriptArgument = " + script_argument + "; "
        + "$cases = @(" + rows + "); $all = @($cases); "
    )
    if supervisor:
        chain_start = command.index("$protectedPids =")
        chain_end = command.rindex("$all | Where-Object")
        setup += f"$supervisorPid = {SUPERVISOR_PID}; " + command[chain_start:chain_end]
    ps = setup + "$cases | ForEach-Object { [bool]($_ | Where-Object {" + predicate + "}) }"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    observed = [line.strip().lower() == "true" for line in result.stdout.splitlines() if line.strip()]
    expected = [case[4] for case in cases]
    assert len(observed) == len(cases), result.stdout
    assert observed == expected, result.stdout
    return observed


def test_installer_stops_only_its_installation_processes() -> None:
    command = _installer_command()
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
        assert observed[1] is True
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
        assert observed[3] is False
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
