"""A command line is split by the rules of the platform that runs it.

On Windows the sandbox split every command with POSIX shell rules, so a backslash was an escape: an
unquoted interpreter path like C:\\hostedtoolcache\\...\\python.exe lost its separators and was
refused as 'hostedtoolcachewindows...python', a program that does not exist. Windows now uses the
CommandLineToArgvW rules. A cmd.exe metacharacter outside double quotes, a line break, or a program
that names a path but is not an absolute .exe is refused, never guessed at. POSIX is unchanged.
These tests drive the Windows rules on any host through network_guard._IS_WINDOWS.
"""
from __future__ import annotations

import pytest

from sandbox import network_guard
from sandbox.network_guard import CommandLineRefused, join_command, parse_command

PYTHON = r"C:\hostedtoolcache\windows\Python\3.12.10\x64\python.exe"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True, raising=False)


def test_an_unquoted_windows_interpreter_path_keeps_its_separators(windows):
    assert parse_command(f"{PYTHON} -m pytest -q tests/test_pack.py") == [
        PYTHON, "-m", "pytest", "-q", "tests/test_pack.py",
    ]


def test_the_whitelist_sees_the_interpreter_by_its_case_folded_name_without_exe(windows, monkeypatch):
    from sandbox import sandbox_runner

    monkeypatch.setattr(sandbox_runner.os, "name", "nt")
    argv = parse_command(r'"C:\Program Files\Python312\PYTHON.EXE" -c "print(1)"')
    assert argv[0] == r"C:\Program Files\Python312\PYTHON.EXE"
    assert sandbox_runner._base_command_name(argv[0]) == "python"


@pytest.mark.parametrize(
    ("line", "argv"),
    [
        (r'git commit -m "a \"b\" c"', ["git", "commit", "-m", 'a "b" c']),
        (r'tool a\\"b c" d', ["tool", r"a\b c", "d"]),
        (r"tool a\b\c", ["tool", r"a\b\c"]),
        ('tool "say ""hi"""', ["tool", 'say "hi"']),
        ('python -m pytest -k "a and (b)"', ["python", "-m", "pytest", "-k", "a and (b)"]),
        ('tool "a & b" "x|y" "<z>"', ["tool", "a & b", "x|y", "<z>"]),
        ("   ", []),
    ],
)
def test_windows_arguments_follow_the_commandlinetoargvw_rules(windows, line, argv):
    assert parse_command(line) == argv


@pytest.mark.parametrize("line", ["echo a > b", "tool a && b", "tool a|b", "tool ^x", "tool %PATH%", "tool !x!", "tool (a)"])
def test_a_cmd_metacharacter_outside_quotes_is_refused(windows, line):
    with pytest.raises(CommandLineRefused, match="metacharacter"):
        parse_command(line)


@pytest.mark.parametrize("line", ["tool a\nrm b", "tool a\r\nb"])
def test_a_command_line_that_spans_lines_is_refused(windows, line):
    with pytest.raises(CommandLineRefused):
        parse_command(line)


@pytest.mark.parametrize("program", [r".\tool.exe", r"tools\tool.exe", r"C:\x\tool.bat", r"C:tool.exe", r"\\srv\share\tool.exe", "/usr/bin/python"])
def test_a_program_that_names_a_path_must_be_an_absolute_exe(windows, program):
    with pytest.raises(CommandLineRefused, match=r"absolute path to an \.exe"):
        parse_command(f'"{program}" -V')


def test_an_unbalanced_quote_is_refused(windows):
    with pytest.raises(CommandLineRefused):
        parse_command('tool "a b')


def test_join_is_the_inverse_of_parse_on_windows(windows):
    argv = [r"C:\Program Files\x\p.exe", "a b", 'q"x', "tr\\", "", r"C:\a\b"]
    assert parse_command(join_command(argv)) == argv


def test_the_sandbox_refuses_a_command_line_it_cannot_read_with_the_reason(windows, tmp_path):
    from core.execution_gate import (
        ExecutionGate,
        reset_request_autonomy_override,
        set_request_autonomy_override,
    )
    from core.remote_fetch_policy import remote_fetch_policy_scope
    from sandbox.sandbox_runner import SandboxRunner

    # The gate consults the turn's ledger, as it does in production (every turn is scoped). With
    # autonomy on, the gate lets the command through, so the refusal is the command line's own.
    token = set_request_autonomy_override("auto")
    try:
        with remote_fetch_policy_scope({"surface": "openclaw"}):
            result = SandboxRunner(ExecutionGate(), str(tmp_path)).run_command(
                "git status %CD%", cwd=str(tmp_path)
            )
    finally:
        reset_request_autonomy_override(token)
    assert result["status"] == "blocked_by_policy"
    assert result["error"].startswith("Command line refused:"), result
    assert "metacharacter" in result["error"], result


def test_off_windows_commands_are_still_shell_words(monkeypatch):
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", False, raising=False)
    assert parse_command(r"tool a\b 'c d' > e") == ["tool", "ab", "c d", ">", "e"]
    assert join_command(["a b", "c"]) == "'a b' c"
