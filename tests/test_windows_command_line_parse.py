"""A command line is split by the rules of the platform that runs it.

On Windows the sandbox split every command with POSIX shell rules, so a backslash was an escape: an
unquoted interpreter path like C:\\hostedtoolcache\\...\\python.exe lost its separators and was
refused as 'hostedtoolcachewindows...python', a program that does not exist. Windows now uses the
CommandLineToArgvW rules. A cmd.exe metacharacter outside double quotes, a line break, or a program
that names a path but is not an absolute .exe is refused, never guessed at. POSIX is unchanged.
These tests drive the Windows rules on any host through network_guard._IS_WINDOWS.
"""
from __future__ import annotations

import os

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


def test_the_whitelist_sees_the_interpreter_by_its_case_folded_name_without_exe(windows):
    from sandbox import sandbox_runner

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


def _program(folder, name: str):
    # Named in PATHEXT's case: Windows matches extensions case-blind, a POSIX test host does not.
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(b"MZ")
    return target


def test_a_bare_name_that_resolves_to_a_cmd_or_bat_is_refused(windows, tmp_path):
    from sandbox.network_guard import require_windows_exe

    bin_dir = tmp_path / "bin"
    _program(bin_dir, "npm.CMD")
    _program(bin_dir, "x.BAT")
    for name in ("npm", "x"):
        with pytest.raises(CommandLineRefused, match=r"not an \.exe"):
            require_windows_exe(name, path=str(bin_dir), pathext=".COM;.EXE;.BAT;.CMD")
    with pytest.raises(CommandLineRefused, match="was not found"):
        require_windows_exe("nothing-here", path=str(bin_dir), pathext=".EXE")


def test_a_bare_name_that_resolves_to_an_exe_is_allowed_as_its_absolute_path(windows, tmp_path):
    from sandbox.network_guard import require_windows_exe

    git = _program(tmp_path / "Git" / "cmd", "git.EXE")
    assert require_windows_exe("git", path=str(git.parent), pathext=".EXE") == str(git)


def test_a_program_planted_in_the_current_folder_or_workspace_is_never_chosen(windows, tmp_path, monkeypatch):
    from sandbox.network_guard import require_windows_exe

    workspace = tmp_path / "workspace"
    _program(workspace, "git.EXE")
    real = _program(tmp_path / "Git" / "cmd", "git.EXE")
    monkeypatch.chdir(workspace)
    # The current folder is never searched, and a PATH entry inside it (relative or absolute) is skipped.
    search = os.pathsep.join([".", str(workspace), str(real.parent)])
    assert require_windows_exe("git", refuse_under=(str(workspace),), path=search, pathext=".EXE") == str(real)
    # With nothing else on PATH, the planted copy is refused, not run.
    with pytest.raises(CommandLineRefused):
        require_windows_exe("git", refuse_under=(str(workspace),), path=str(workspace), pathext=".EXE")
    # An absolute path into the workspace is refused too.
    with pytest.raises(CommandLineRefused, match="inside the workspace"):
        require_windows_exe(str(workspace / "git.EXE"), refuse_under=(str(workspace),))


def _run_in_sandbox(tmp_path, command: str):
    from core.execution_gate import (
        ExecutionGate,
        reset_request_autonomy_override,
        set_request_autonomy_override,
    )
    from core.remote_fetch_policy import remote_fetch_policy_scope
    from sandbox.sandbox_runner import SandboxRunner

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    token = set_request_autonomy_override("auto")
    try:
        with remote_fetch_policy_scope({"surface": "openclaw"}):
            runner = SandboxRunner(ExecutionGate(), str(workspace))
            return runner, runner.run_command(command, cwd=str(workspace))
    finally:
        reset_request_autonomy_override(token)


def test_the_sandbox_refuses_a_program_that_would_run_through_cmd(windows, monkeypatch, tmp_path):
    shims = tmp_path / "shims"
    _program(shims, "git.CMD")
    monkeypatch.setenv("PATH", str(shims))
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    _runner, result = _run_in_sandbox(tmp_path, "git status")
    assert result["status"] == "blocked_by_policy", result
    assert "not an .exe" in result["error"], result


def test_the_sandbox_spawns_the_absolute_path_it_checked(windows, monkeypatch, tmp_path):
    from sandbox.job_runner import JobRunner

    real = _program(tmp_path / "Git" / "cmd", "git.EXE")
    monkeypatch.setenv("PATH", str(real.parent))
    monkeypatch.setenv("PATHEXT", ".EXE")
    spawned: list[list[str]] = []

    def fake_run(self, argv, *args, **kwargs):
        spawned.append(list(argv))
        return {"status": "success", "stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(JobRunner, "run", fake_run)
    _runner, result = _run_in_sandbox(tmp_path, "git status")
    assert spawned, f"the command never reached the job runner: {result}"
    assert spawned[0][0] == str(real)
    assert spawned[0][1:] == ["status"]


def test_an_env_wrapped_program_is_refused_on_windows(windows, monkeypatch, tmp_path):
    from sandbox.job_runner import JobRunner

    env = _program(tmp_path / "Git" / "usr" / "bin", "env.EXE")
    planted = _program(tmp_path / "workspace", "git.EXE")
    monkeypatch.setenv("PATH", str(env.parent))
    monkeypatch.setenv("PATHEXT", ".EXE")
    spawned: list[list[str]] = []
    monkeypatch.setattr(JobRunner, "run", lambda self, argv, *a, **k: spawned.append(list(argv)) or {})
    # The runner's own check is under test, so the gate lets every line through.
    from core.execution_gate import ExecutionGate

    monkeypatch.setattr(ExecutionGate, "evaluate_command", lambda self, cmd, **k: {"decision": "allowed", "reason": ""})
    for command in (f"env {planted} status", "env git status", "env FOO=1 git status"):
        _runner, result = _run_in_sandbox(tmp_path, command)
        assert result["status"] == "blocked_by_policy", (command, result)
        assert "env is not used on Windows" in result["error"], (command, result)
    assert spawned == []


@pytest.mark.parametrize(
    "command",
    ["find . -exec git status ;", "find . -execdir x.exe {} +", "find . -ok x.exe {} ;", "rg --pre=x.exe foo", "rg --pre x.exe foo", "rg --hostname-bin=x.exe foo"],
)
def test_a_whitelisted_tool_that_would_start_another_program_is_refused_on_windows(windows, monkeypatch, tmp_path, command):
    from core.execution_gate import ExecutionGate
    from sandbox.job_runner import JobRunner

    bin_dir = tmp_path / "bin"
    _program(bin_dir, "find.EXE")
    _program(bin_dir, "rg.EXE")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".EXE")
    spawned: list[list[str]] = []
    monkeypatch.setattr(JobRunner, "run", lambda self, argv, *a, **k: spawned.append(list(argv)) or {})
    monkeypatch.setattr(ExecutionGate, "evaluate_command", lambda self, cmd, **k: {"decision": "allowed", "reason": ""})
    _runner, result = _run_in_sandbox(tmp_path, command)
    assert result["status"] == "blocked_by_policy", result
    assert "starts another program" in result["error"], result
    assert spawned == []


def test_the_gate_and_the_runner_read_the_same_argv(windows, monkeypatch):
    """A backslash before a quote: POSIX words would give the gate C:a"b, Windows gives C:\\a"b."""
    import shlex

    from core.execution_gate import ExecutionGate
    from core.remote_fetch_policy import remote_fetch_policy_scope

    line = 'git log C:\\a\\"b'
    seen: list[list[str]] = []
    real = network_guard.parse_command

    def recording(cmd):
        argv = real(cmd)
        seen.append(argv)
        return argv

    monkeypatch.setattr(network_guard, "parse_command", recording)
    with remote_fetch_policy_scope({"surface": "openclaw"}):
        ExecutionGate().evaluate_command(line)
    assert seen, "the gate did not split the line with the runner's parser"
    assert seen[0] == real(line) == ["git", "log", 'C:\\a"b']
    assert seen[0] != shlex.split(line)


def test_the_gate_blocks_a_command_line_it_cannot_read(windows):
    from core.execution_gate import ExecutionGate
    from core.remote_fetch_policy import remote_fetch_policy_scope

    with remote_fetch_policy_scope({"surface": "openclaw"}):
        verdict = ExecutionGate().evaluate_command("git status %CD%")
    assert verdict["decision"] == "blocked", verdict
    assert verdict["reason"].startswith("Command line refused:"), verdict
