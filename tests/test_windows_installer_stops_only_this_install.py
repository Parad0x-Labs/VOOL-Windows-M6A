"""The installer, Stop button and bundle supervisor stop only this installation's Python."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
    inno_escaped_root = str(install_root).replace("'", "''")
    return "$r = '" + inno_escaped_root + "'" + production_tail


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
