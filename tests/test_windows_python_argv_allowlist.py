"""Windows phase 1 runs a Python job only as a .py script, `-c <code>` or `-m pytest`.

The 0.7.1 Windows limits say VOOL runs Python and pytest in its sandbox and refuses pip, ruff and
the rest. The gate (`JobRunner._windows_phase1_argv`) judged only the program name, so
`python -m pip install x`, `-m ensurepip`, `-m venv` and `-m ruff` reached the launcher. These
tests read each command line the way CPython reads it (flags first, combined or not, `-c`/`-m`
ending the options) and require every other form to be refused before any process starts.

They use the fake launcher from test_windows_confinement_runner: it writes env-run.json (or
forms.log for its grant form) every time it is started, so an empty launcher folder proves that
nothing was launched. Real confinement is proven on Windows by the launcher itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sandbox import job_runner as jr
from tests.test_windows_confinement_runner import (
    _fake_launcher,
    _runner,
    _use_launcher,
    windows,
)

_INTERPRETER = str(Path(sys.executable).resolve())

REFUSED = [
    pytest.param(["python", "-m", "pip", "install", "x"], id="python -m pip install"),
    pytest.param(["python", "-m", "pip", "uninstall", "-y", "x"], id="python -m pip uninstall"),
    pytest.param(["python", "-m", "ruff", "check", "."], id="python -m ruff"),
    pytest.param(["python", "-m", "ensurepip"], id="python -m ensurepip"),
    pytest.param(["python", "-m", "venv", "env"], id="python -m venv"),
    pytest.param(["py", "-m", "pip", "install", "x"], id="py -m pip"),
    pytest.param(["python3", "-m", "pip", "list"], id="python3 -m pip"),
    pytest.param(["Python.EXE", "-m", "pip", "list"], id="python.exe -m pip"),
    pytest.param([_INTERPRETER, "-m", "pip", "install", "x"], id="full interpreter path -m pip"),
    pytest.param(["pythonw", "-m", "pip", "install", "x"], id="pythonw -m pip"),
    pytest.param([str(Path(_INTERPRETER).with_name("pythonw.exe")), "-m", "pip"], id="full pythonw path"),
    pytest.param(["python", "-I", "-m", "pip", "install", "x"], id="-I -m pip"),
    pytest.param(["python", "-Im", "pip", "install", "x"], id="-Im pip"),
    pytest.param(["python", "-mpip", "install", "x"], id="-mpip"),
    pytest.param(["python", "-IBmpip", "install", "x"], id="-IBmpip"),
    pytest.param(["python", "-I", "-B", "-s", "-m", "ensurepip"], id="flags then -m ensurepip"),
    pytest.param(["python", "--", "-m", "pip"], id="-- -m pip"),
    pytest.param(["python", "-I", "--", "pip"], id="-I -- pip"),
    pytest.param(["python", "-W", "ignore", "-m", "pip"], id="-W then -m pip"),
    pytest.param(["python", "-Wignore::pip.Foo", "-c", "0"], id="-W imports a category module"),
    pytest.param(["python", "-X", "utf8", "-m", "pip"], id="-X then -m pip"),
    pytest.param(["python", "-Xpresite=pip", "-c", "0"], id="-X presite"),
    pytest.param(["python", "--check-hash-based-pycs", "never", "-m", "pip"], id="long option"),
    pytest.param(["python", "--help"], id="--help"),
    pytest.param(["python", "-i", "-c", "0"], id="-i opens a REPL on stdin"),
    pytest.param(["python", "-3", "-m", "pip"], id="py launcher version flag"),
    pytest.param(["python"], id="no program reads stdin"),
    pytest.param(["python", "-I"], id="flags but no program"),
    pytest.param(["python", "-"], id="- reads stdin"),
    pytest.param(["python", "-m"], id="-m with nothing after it"),
    pytest.param(["python", "-m", "pytest.__main__"], id="-m another module name"),
    pytest.param(["python", "pipdir"], id="a folder or zip as the program"),
    pytest.param(["python", "pip.whl/pip", "install", "x"], id="pip run from a wheel"),
    pytest.param(["python", "get-pip.pyz"], id="a zipapp"),
    pytest.param(["python", ""], id="an empty script name"),
]

ALLOWED = [
    pytest.param(["python", "app.py", "--flag"], [_INTERPRETER, "app.py", "--flag"], id="script"),
    pytest.param(["python", "src\\app.py"], [_INTERPRETER, "src\\app.py"], id="script with a folder"),
    pytest.param(["py", "-u", "App.PY"], [_INTERPRETER, "-u", "App.PY"], id="flag then script"),
    pytest.param(["python", "-c", "print(1)"], [_INTERPRETER, "-c", "print(1)"], id="-c"),
    pytest.param(["python3", "-cprint(1)"], [_INTERPRETER, "-cprint(1)"], id="-c joined"),
    pytest.param(["python", "-IBc", "0"], [_INTERPRETER, "-IBc", "0"], id="flags combined with -c"),
    pytest.param(["python", "-c", "0", "-m", "pip"], [_INTERPRETER, "-c", "0", "-m", "pip"], id="-c then argv"),
    pytest.param(["python", "app.py", "-m", "pip"], [_INTERPRETER, "app.py", "-m", "pip"], id="script then argv"),
    pytest.param(["python", "-m", "pytest", "-q"], [_INTERPRETER, "-m", "pytest", "-q"], id="-m pytest"),
    pytest.param(["python", "-I", "-B", "-m", "pytest"], [_INTERPRETER, "-I", "-B", "-m", "pytest"], id="flags -m pytest"),
    pytest.param(["python", "-Bmpytest", "-x"], [_INTERPRETER, "-Bmpytest", "-x"], id="-Bmpytest"),
    pytest.param([_INTERPRETER, "-m", "pytest"], [_INTERPRETER, "-m", "pytest"], id="full path -m pytest"),
    pytest.param(["pytest", "-q", "-p", "no:cacheprovider"],
                 [_INTERPRETER, "-m", "pytest", "-q", "-p", "no:cacheprovider"], id="bare pytest"),
]


def _launched(launcher: Path) -> list[str]:
    """Every trace the fake launcher leaves when it is started at all."""
    return sorted(p.name for p in launcher.parent.iterdir() if p.name != launcher.name)


@pytest.mark.parametrize("argv", REFUSED)
def test_the_gate_refuses_every_python_form_but_script_c_and_pytest(argv) -> None:
    with pytest.raises(jr.KernelIsolationUnavailableError) as refused:
        jr.JobRunner._windows_phase1_argv(argv)
    message = str(refused.value)
    assert "Python and pytest" in message
    assert "It was not run." in message


@pytest.mark.parametrize("argv", REFUSED)
def test_a_refused_python_form_starts_no_process(windows, tmp_path, argv) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    # Network trust is on so that the egress check in `run` can't be what refuses `pip install`.
    with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
        _runner(tmp_path, allow_network_egress=True).run(argv)
    assert _launched(launcher) == [], "the launcher was started for a refused command"
    assert not jr._WINDOWS_STAGED, "a refused command leaves no policy behind"


def test_a_python_module_refusal_names_the_limit_and_wsl() -> None:
    with pytest.raises(jr.KernelIsolationUnavailableError) as refused:
        jr.JobRunner._windows_phase1_argv(["python", "-Im", "pip", "install", "x"])
    message = str(refused.value)
    assert "'-m pip'" in message
    assert ".py script, -c code or -m pytest" in message
    assert "pip, ruff" in message and "WSL2/Linux" in message


@pytest.mark.parametrize(("argv", "expected"), ALLOWED)
def test_the_gate_accepts_a_script_c_code_and_pytest(argv, expected) -> None:
    assert jr.JobRunner._windows_phase1_argv(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [["python", "-c", "print('ran')"], ["python", "-I", "-m", "pytest", "--version"]],
)
def test_an_allowed_python_form_runs_through_the_launcher(windows, tmp_path, argv) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    result = _runner(tmp_path).run(argv)
    assert result.returncode == 0
    assert "env-run.json" in _launched(launcher)


def test_the_script_form_runs_through_the_launcher(windows, tmp_path) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    runner = _runner(tmp_path)
    (tmp_path / "ws" / "app.py").write_text("print('script ran')\n", encoding="utf-8")
    result = runner.run(["python", "app.py"], cwd=tmp_path / "ws")
    assert result.returncode == 0 and result.stdout.strip() == "script ran"


def test_cpython_reads_the_allowed_edge_forms_the_same_way(tmp_path) -> None:
    """The parser's two edge claims, checked against the real interpreter: whatever follows `-c`
    code or a script is that program's argv, never an interpreter option."""
    code = "import sys; print(sys.argv[1:])"
    after_c = subprocess.run(
        [sys.executable, "-IBc", code, "-m", "pip"], capture_output=True, text=True, timeout=60, check=True
    )
    assert after_c.stdout.strip() == "['-m', 'pip']"
    script = tmp_path / "app.py"
    script.write_text(code + "\n", encoding="utf-8")
    after_script = subprocess.run(
        [sys.executable, "-I", str(script), "-m", "pip"], capture_output=True, text=True, timeout=60, check=True
    )
    assert after_script.stdout.strip() == "['-m', 'pip']"
    clustered = subprocess.run(
        [sys.executable, "-IBmjson.tool", "--help"], capture_output=True, text=True, timeout=60
    )
    assert "json.tool" in clustered.stdout or "usage" in clustered.stdout, "-IBm<module> runs <module>"


#: VOOL's own builder runs `python -m unittest` and `python -m compileall -q`
#: (core/agent_runtime/builder/app_builder.py, builder/controller.py), so phase 1 accepts those two
#: stdlib runners as well as pytest. Neither installs anything.
STDLIB_RUNNERS = [
    pytest.param(["python", "-m", "unittest", "-v", "test_app"],
                 [_INTERPRETER, "-m", "unittest", "-v", "test_app"], id="-m unittest"),
    pytest.param(["python3", "-m", "unittest", "discover"],
                 [_INTERPRETER, "-m", "unittest", "discover"], id="python3 -m unittest"),
    pytest.param(["python", "-Im", "unittest"], [_INTERPRETER, "-Im", "unittest"], id="-Im unittest"),
    pytest.param(["py", "-IBmunittest", "-q"], [_INTERPRETER, "-IBmunittest", "-q"], id="-IBmunittest"),
    pytest.param(["python", "-I", "-B", "-m", "unittest"], [_INTERPRETER, "-I", "-B", "-m", "unittest"],
                 id="flags -m unittest"),
    pytest.param(["python", "-m", "compileall", "-q", "src"],
                 [_INTERPRETER, "-m", "compileall", "-q", "src"], id="-m compileall"),
    pytest.param(["python", "-Im", "compileall", "-q", "src"],
                 [_INTERPRETER, "-Im", "compileall", "-q", "src"], id="-Im compileall"),
    pytest.param([_INTERPRETER, "-m", "compileall", "-q", "src"],
                 [_INTERPRETER, "-m", "compileall", "-q", "src"], id="full path -m compileall"),
]


@pytest.mark.parametrize(("argv", "expected"), STDLIB_RUNNERS)
def test_the_gate_accepts_vools_stdlib_test_and_compile_runners(argv, expected) -> None:
    assert jr.JobRunner._windows_phase1_argv(argv) == expected


def test_a_unittest_job_runs_through_the_launcher(windows, tmp_path) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    runner = _runner(tmp_path)
    workspace = tmp_path / "ws"
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n", encoding="utf-8"
    )
    result = runner.run(["python", "-Bm", "unittest", "-q", "test_ok"], cwd=workspace)
    assert result.returncode == 0, result.stderr
    assert "env-run.json" in _launched(launcher)


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-Im", "pip", "install", "x"],
        ["python", "-m", "ensurepip"],
        ["python", "-Im", "venv", "env"],
        ["python", "-m", "ruff", "check", "."],
        ["python", "-m", "unittest.mock"],
        ["python", "-m", "Unittest"],
        ["python", "-m", "compileall.x"],
    ],
)
def test_allowing_the_stdlib_runners_refuses_every_other_module(argv) -> None:
    with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
        jr.JobRunner._windows_phase1_argv(argv)


# --- the program must be the interpreter running VOOL ------------------------------------------
#
# The launcher accepts any absolute .exe (windows/broker/src/VoolConfine.Core/LauncherCommandLine.cs),
# and config/confine-pin.json pins only the launcher's own files. So the gate is what keeps a job's
# program to the interpreter running VOOL: an absolute program must resolve to sys.executable or its
# base. A Windows venv's Scripts\python.exe is a copied redirector, not a link, so it resolves to
# itself and is refused. These rows build that shape on Linux with a real copy of the interpreter.


def _copied_interpreter(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / "python.exe"
    copy.write_bytes(Path(_INTERPRETER).read_bytes())
    copy.chmod(0o755)
    (folder.parent / "pyvenv.cfg").write_text(f"home = {Path(_INTERPRETER).parent}\n", encoding="utf-8")
    return copy


@pytest.mark.parametrize(
    "rest",
    [["-c", "0"], ["script.py"], ["-m", "pytest"], ["-m", "unittest"]],
    ids=["-c", "script", "-m pytest", "-m unittest"],
)
def test_a_new_venv_python_in_a_marked_folder_is_refused_before_start(windows, tmp_path, rest) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    runner = _runner(tmp_path)
    venv_python = _copied_interpreter(tmp_path / "ws" / "newvenv" / "Scripts")
    (tmp_path / "ws" / "script.py").write_text("print('ran')\n", encoding="utf-8")
    with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
        runner.run([str(venv_python), *rest], cwd=tmp_path / "ws")
    assert _launched(launcher) == [], "the launcher was started for a venv interpreter"
    assert not jr._WINDOWS_STAGED


def test_a_venv_python_outside_the_workspace_is_refused_by_the_gate(tmp_path) -> None:
    venv_python = _copied_interpreter(tmp_path / "work" / "newvenv" / "Scripts")
    for rest in (["script.py"], ["-c", "0"]):
        with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
            jr.JobRunner._windows_phase1_argv([str(venv_python), *rest])


def test_a_relative_venv_python_is_refused(tmp_path) -> None:
    for program in ("newvenv\\Scripts\\python.exe", "newvenv/Scripts/python.exe", ".\\python.exe"):
        with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
            jr.JobRunner._windows_phase1_argv([program, "-c", "0"])


def test_a_linked_venv_python_runs_as_the_interpreter_itself_never_the_venv(tmp_path) -> None:
    """A `--symlinks` venv's python is a link to the base. The gate hands the launcher the resolved
    path, so what starts is the interpreter running VOOL, beside which no pyvenv.cfg sits."""
    scripts = tmp_path / "linkvenv" / "Scripts"
    scripts.mkdir(parents=True)
    link = scripts / "python.exe"
    link.symlink_to(_INTERPRETER)
    argv = jr.JobRunner._windows_phase1_argv([str(link), "-c", "0"])
    assert argv == [_INTERPRETER, "-c", "0"]
    assert str(link) not in argv
