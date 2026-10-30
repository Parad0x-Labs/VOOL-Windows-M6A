"""A folder name with any PowerShell quote in it stays one string in VOOL's Windows stop commands.

The Stop button, the bundle supervisor and the installer each put their root folder into a
single-quoted PowerShell string. PowerShell ends that string at the ASCII apostrophe and at the
curly quotes U+2018, U+2019, U+201A and U+201B alike, and reads two of them in a row as the second
one (Tokenizer.ScanStringLiteral). With only the ASCII apostrophe doubled, a folder such as
``C:\\Users\\D’Angelo`` ended the string early: the rest of the name ran as PowerShell code and the
stop failed to parse. These pin the quoting where CI runs; the Windows-only tests in
test_windows_installer_stops_only_this_install.py run the same commands in real PowerShell.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import vool_stop
from installer.bundle import bundle_supervisor
from tests._windows_system32 import trust_system32
from tests.test_windows_installer_setup_initializes import scan_code

ISS = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"
POWERSHELL_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"
# Every quote PowerShell honours, beside a space, brackets and text that would run as code if the
# root string ended early, with a quote last so the closing quote follows an escaped one.
TRICKY_ROOT = "C:\\Users\\D\u2019Angelo\\VOOL's \u2018dev\u2019 \u201alab\u201b [x]; Write-Output escaped; '"
_PASCAL_STRING = r"(?:'(?:[^']|'')*'|#(?:\$[0-9A-Fa-f]+|[0-9]+))+"


def read_powershell_single_quoted(text: str, start: int) -> tuple[str, int]:
    """Return the value of the single-quoted string opening at ``text[start]`` and the index after it."""
    assert text[start] in POWERSHELL_SINGLE_QUOTES, text[start:]
    value: list[str] = []
    index = start + 1
    while index < len(text):
        if text[index] in POWERSHELL_SINGLE_QUOTES:
            if index + 1 < len(text) and text[index + 1] in POWERSHELL_SINGLE_QUOTES:
                value.append(text[index + 1])
                index += 2
                continue
            return "".join(value), index + 1
        value.append(text[index])
        index += 1
    raise AssertionError(f"unterminated PowerShell string: {text[start:]}")


def pascal_string_value(expression: str) -> str:
    """Return the value of an Inno Setup Pascal string constant such as ``''''`` or ``#$2018#$2018``."""
    assert re.fullmatch(_PASCAL_STRING, expression), expression
    value: list[str] = []
    for match in re.finditer(r"'((?:[^']|'')*)'|#(\$[0-9A-Fa-f]+|[0-9]+)", expression):
        code = match.group(2)
        if code is None:
            value.append(match.group(1).replace("''", "'"))
        else:
            value.append(chr(int(code[1:], 16) if code.startswith("$") else int(code)))
    return "".join(value)


def installer_escaped_install_dir(iss_text: str, install_dir: str) -> str:
    """Return ``install_dir`` after StopNulla's StringChangeEx calls, applied in order as Setup runs them."""
    code, _ = scan_code(iss_text)  # comments blanked: Setup never runs a call that is commented out
    calls = re.findall(
        r"StringChangeEx\(InstallDir,\s*(" + _PASCAL_STRING + r"),\s*(" + _PASCAL_STRING + r"),\s*True\);",
        code,
    )
    assert calls, "StopNulla no longer escapes the install folder"
    for old, new in calls:
        install_dir = install_dir.replace(pascal_string_value(old), pascal_string_value(new))
    return install_dir


def installer_stop_parameters(iss_text: str, install_dir: str) -> str:
    """Return the parameters StopNulla's Exec hands powershell.exe when Setup installs to ``install_dir``."""
    code, _ = scan_code(iss_text)
    line = next(line for line in code.splitlines() if "Stop-Process" in line).strip()
    assert line.endswith("',"), line
    before, after = line[:-1].split(" + InstallDir + ")
    return (
        pascal_string_value(before)
        + installer_escaped_install_dir(iss_text, install_dir)
        + pascal_string_value(after)
    )


def _root_value(command: str) -> str:
    start = command.index("$r = ") + len("$r = ")
    value, end = read_powershell_single_quoted(command, start)
    assert command[end] == ";", command[end:end + 60]
    return value


def _captured_runner(captured: list[list[str]]):
    def capture(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    return capture


def test_installer_stop_keeps_an_install_folder_with_any_quote_whole() -> None:
    parameters = installer_stop_parameters(ISS.read_text(encoding="utf-8"), TRICKY_ROOT)
    command = parameters[parameters.index('-Command "') + len('-Command "'):]
    assert _root_value(command) == TRICKY_ROOT


def test_stop_button_keeps_a_root_with_any_quote_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trust_system32(monkeypatch)
    captured: list[list[str]] = []
    run = _captured_runner(captured)
    vool_stop.kill_listener(run, vool_stop.API_PORT, Path(TRICKY_ROOT))
    pidfile = tmp_path / "vool_api.pid"
    pidfile.write_text("4321", encoding="utf-8")
    vool_stop.kill_api_by_pidfile(run, pidfile, Path(TRICKY_ROOT))
    assert len(captured) == 2
    for args in captured:
        assert _root_value(args[args.index("-Command") + 1]) == TRICKY_ROOT


def test_supervisor_cleanup_keeps_a_root_with_any_quote_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    trust_system32(monkeypatch)
    captured: list[list[str]] = []
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(bundle_supervisor, "subprocess", SimpleNamespace(run=_captured_runner(captured)))
    supervisor = object.__new__(bundle_supervisor.BundleSupervisor)
    supervisor.root = Path(TRICKY_ROOT)
    supervisor._log = lambda *_: None
    supervisor._clear_stale_api_processes()
    assert len(captured) == 1
    assert _root_value(captured[0][captured[0].index("-Command") + 1]) == TRICKY_ROOT
