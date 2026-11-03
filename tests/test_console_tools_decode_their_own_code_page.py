"""Windows console tools write their own (OEM) code page; VOOL decodes them with it.

subprocess's text=True decodes with the ANSI code page (cp1252 on a Western Windows), strictly,
so a localized PowerShell or tasklist line holding a byte cp1252 leaves undefined (0x81 is "u"
with an umlaut in cp850) raises UnicodeDecodeError inside subprocess.run. vool_stop swallowed
that and skipped the kill. These tests replay that decode on any host: the runner decodes the
bytes the way subprocess would from the arguments it was given, with cp1252 standing in for the
ANSI code page and cp850 for the console's OEM code page (the "oem" codec exists only on Windows).
"""
from __future__ import annotations

import csv
import types

import pytest

import core.process_liveness as process_liveness
from tests._windows_system32 import trust_system32

ANSI = "cp1252"
OEM = "cp850"


@pytest.fixture(autouse=True)
def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    trust_system32(monkeypatch)
    monkeypatch.setattr(process_liveness, "CONSOLE_ENCODING", OEM, raising=False)


class _ConsoleRun:
    """subprocess.run as a Windows host would decode a tool's OEM bytes."""

    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.raw_out = stdout.encode(OEM)
        self.raw_err = stderr.encode(OEM)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        encoding, errors = kwargs.get("encoding"), kwargs.get("errors")
        if kwargs.get("text") or encoding or errors:
            decode = lambda raw: raw.decode(encoding or ANSI, errors or "strict")  # noqa: E731
            return types.SimpleNamespace(stdout=decode(self.raw_out), stderr=decode(self.raw_err), returncode=0)
        return types.SimpleNamespace(stdout=self.raw_out, stderr=self.raw_err, returncode=0)


GERMAN_WARNING = "WARNUNG: Zugriff für den Benutzer verweigert.\r\n"


def test_vool_stop_kills_the_api_when_powershell_warns_in_the_console_code_page(tmp_path):
    from installer import vool_stop

    run = _ConsoleRun("4242\r\n", GERMAN_WARNING)
    assert vool_stop.kill_listener(run, 11435, tmp_path) == 4242
    assert any("taskkill" in c[0].lower() and "4242" in c for c in run.calls)


def test_operator_process_list_reads_a_localized_tasklist():
    from core.operator.system import inspect_processes

    run = _ConsoleRun('"Büro.exe","812","Console","1","4.096 K"\r\n')
    rows = inspect_processes(os_name="nt", subprocess_run=run, csv_module=csv)
    assert rows == [{"pid": "812", "name": "Büro.exe", "cpu_percent": 0.0, "mem_percent": 0.0}]


def test_machine_diagnostics_reads_a_localized_tasklist():
    from core import machine_diagnostics

    run = _ConsoleRun('"Büro.exe","812","Console","1","4.096 K"\r\n')
    result = machine_diagnostics._run(["tasklist", "/fo", "csv", "/nh"], run)
    assert "Büro.exe" in result.stdout


def test_hardware_tier_cim_query_survives_a_localized_warning(monkeypatch):
    from core import hardware_tier

    run = _ConsoleRun("17179869184\r\n", GERMAN_WARNING)
    monkeypatch.setattr(hardware_tier.subprocess, "run", run)
    assert hardware_tier._cim_query("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory").strip() == "17179869184"
