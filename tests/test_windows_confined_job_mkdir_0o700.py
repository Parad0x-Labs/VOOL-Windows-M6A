"""Windows: inside a confined job, `os.mkdir(path, 0o700)` becomes a plain mkdir, so tmp_path works.

CPython 3.12.4 and later (gh-118486) create a folder asked for with mode 0o700 on Windows with the
protected DACL `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`. No ACE there names the
confined job's capability, so the AppContainer job loses every right on a folder it just made.
pytest's `tmp_path`, `tmpdir` and `tmp_path_factory` (`getbasetemp`, `make_numbered_dir`, `mktemp`,
`--basetemp` too) and `tempfile.mkdtemp`/`TemporaryDirectory` all ask for 0o700, so every user test
that used them errored in the Windows sandbox (tests/test_windows_pytest_cache_folders_stay_readable.py
rows 17-19 pin that).

The fix is a shim in the bundled Windows Python only: `sandbox/windows_job_mkdir.py`, shipped by
installer/bundle/build_bundle.ps1 as `Lib\\site-packages\\vool_windows_job_mkdir.py` with a one-line
`.pth` that imports it. It does something only when `VOOL_WINDOWS_CONFINED_JOB=1`, which the job
runner sets in the environment of a confined Windows Python job and nowhere else. Then
`os.mkdir(path, 0o700)` runs as `os.mkdir(path)`: the folder inherits its parent's DACL (inside the
write root: the user, SYSTEM, Administrators and this job's own capability), like any other folder
the job makes. Nothing else changes, and the AppContainer and its ACLs stay the only boundary.

Rows:
- the variable: set for confined Python jobs, absent everywhere else, not in the policy;
- the shim: unset or not Windows changes nothing; set, only mode 0o700 is rewritten, only
  `os.mkdir` is touched, its source reads nothing but its own variable and imports only `os`;
- the model (real pytest 9.1.1 under the grant model of the two pytest files before this one, plus
  the 0o700 rule and "no mkdir outside the write root"): tmp_path, tmpdir, tmp_path_factory and
  TemporaryDirectory pass, every folder they make sits in `.sandbox-tmp` inside the write root, a
  mkdir outside the write root is still denied, and without the variable nothing changes;
- the bundle: build_bundle.ps1 ships the shim and checks both states.

No row uses the `tmp_path` fixture, and nothing is Windows-only.
"""
from __future__ import annotations

import ast
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from sandbox import job_runner as jr
from sandbox.resource_limits import ExecutionPolicy

# `base` and `windows` are fixtures: importing them here makes pytest use them in this file.
from tests.test_windows_pytest_cache_folders_stay_readable import (
    _MODEL,
    _job_for,
    _proj,
    base,
    windows,
)
from tests.test_windows_pytest_stays_in_run_folder import _tree, _vool_pytest_args

REPO = Path(__file__).resolve().parents[1]
SHIM = REPO / "sandbox" / "windows_job_mkdir.py"
BUILD = REPO / "installer" / "bundle" / "build_bundle.ps1"
VAR = "VOOL_WINDOWS_CONFINED_JOB"


def _shim():
    return importlib.import_module("sandbox.windows_job_mkdir")


@pytest.fixture(autouse=True)
def _appcontainer_token(monkeypatch):
    """Every row runs as if the process token were an AppContainer's (the shim's token check, which
    is Windows-only, is mocked); the rows about the token check set their own answer."""
    monkeypatch.setattr(_shim(), "_token_is_appcontainer", lambda: True, raising=False)


# --- the variable ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["python", "calc.py"], id="script"),
        pytest.param(["python", "-c", "print(1)"], id="python-c"),
        pytest.param(["pytest", "-q"], id="pytest"),
        pytest.param(["python", "-m", "pytest"], id="python-m-pytest"),
        pytest.param([sys.executable, "-m", "pytest"], id="interpreter-m-pytest"),
        pytest.param(["python", "-m", "unittest"], id="unittest"),
        pytest.param(["python", "-m", "compileall", "-q", "."], id="compileall"),
    ],
)
def test_a_confined_python_job_gets_the_variable(windows, base, argv):
    job = _job_for(windows, _proj(base), argv)
    assert job["env"].get(VAR) == "1", job["env"]


def test_the_job_runner_and_the_shim_name_the_same_variable():
    assert _shim().ENV_VAR == VAR
    assert jr._WINDOWS_CONFINED_JOB_VAR == VAR


def test_a_job_off_windows_never_gets_the_variable(base):
    """`_child_environment` is what every Linux/macOS job gets; the variable is added only by the
    Windows launcher's environment filter."""
    proj = _proj(base)
    runner = jr.JobRunner(ExecutionPolicy(workspace_root=proj, writable_roots=(proj,)))
    assert VAR not in runner._child_environment(proj)


def test_a_users_own_value_never_reaches_a_job_off_windows(base, monkeypatch):
    monkeypatch.setenv(VAR, "1")
    proj = _proj(base)
    runner = jr.JobRunner(ExecutionPolicy(workspace_root=proj, writable_roots=(proj,)))
    assert VAR not in runner._child_environment(proj)


def test_a_program_that_is_not_vools_interpreter_gets_nothing():
    env = {"PATH": "x"}
    assert jr._windows_job_environment([], dict(env)) == env
    assert VAR not in jr._windows_job_environment([str(Path(sys.executable).parent / "not-python.exe"), "-c", "0"], dict(env))


def test_the_variable_is_not_part_of_the_policy(windows, base, monkeypatch):
    """The policy the launcher enforces is the same document with or without the shim: the variable
    is in the child's environment only."""
    policies: list[str] = []
    real = jr.JobRunner._windows_policy

    def record(self, *args, **kwargs):
        policy, stage = real(self, *args, **kwargs)
        policies.append(policy.to_json())
        return policy, stage

    monkeypatch.setattr(jr.JobRunner, "_windows_policy", record)
    _job_for(windows, _proj(base), ["python", "-m", "pytest"])
    assert policies and all(VAR not in text for text in policies), policies


# --- the shim, in process (on a stand-in for os, never the real one) -------------------------


def _fake_os() -> tuple[types.ModuleType, list[tuple]]:
    calls: list[tuple] = []
    fake = types.ModuleType("os")
    fake.__dict__.update({k: v for k, v in os.__dict__.items() if not k.startswith("__")})

    def mkdir(path, mode=0o777, *, dir_fd=None):
        calls.append((path, mode, dir_fd))
        return "made"

    fake.mkdir = mkdir
    return fake, calls


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1", "1 "])
def test_without_the_variable_the_shim_changes_nothing(value):
    fake, _ = _fake_os()
    before = fake.mkdir
    environ = {} if value is None else {VAR: value}
    assert _shim().install(environ=environ, module=fake, nt=True) is False
    assert fake.mkdir is before


def test_off_windows_the_shim_changes_nothing():
    fake, _ = _fake_os()
    before = fake.mkdir
    assert _shim().install(environ={VAR: "1"}, module=fake, nt=False) is False
    assert fake.mkdir is before


def test_importing_the_shim_never_touches_this_process():
    """The module installs itself at import only on Windows with the variable set."""
    code = "import os, sys; real = os.mkdir; import sandbox.windows_job_mkdir; sys.exit(0 if os.mkdir is real else 1)"
    env = {k: v for k, v in os.environ.items() if k != VAR}
    for extra in ({}, {VAR: "1"}):
        done = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env={**env, **extra}, timeout=60)
        assert done.returncode == 0, extra


def test_with_the_variable_only_mode_0o700_becomes_a_plain_mkdir():
    fake, calls = _fake_os()
    assert _shim().install(environ={VAR: "1"}, module=fake, nt=True) is True
    assert fake.mkdir("a", 0o700) == "made"
    fake.mkdir("b", mode=0o700)
    fake.mkdir("c")
    fake.mkdir("d", 0o755)
    fake.mkdir("e", 0o500)
    fake.mkdir("f", 0o777, dir_fd=None)
    assert calls == [("a", 0o777, None), ("b", 0o777, None), ("c", 0o777, None), ("d", 0o755, None), ("e", 0o500, None), ("f", 0o777, None)]


def test_the_shim_wraps_once():
    fake, calls = _fake_os()
    shim = _shim()
    assert shim.install(environ={VAR: "1"}, module=fake, nt=True) is True
    first = fake.mkdir
    assert shim.install(environ={VAR: "1"}, module=fake, nt=True) is True
    assert fake.mkdir is first
    fake.mkdir("x", 0o700)
    assert calls == [("x", 0o777, None)]


def test_the_shim_touches_nothing_but_mkdir():
    fake, _ = _fake_os()
    before = dict(fake.__dict__)
    _shim().install(environ={VAR: "1"}, module=fake, nt=True)
    after = dict(fake.__dict__)
    assert set(before) == set(after)
    assert [key for key in after if after[key] is not before[key]] == ["mkdir"]


def _shim_tree() -> tuple[ast.Module, ast.FunctionDef | None]:
    """The shim's AST without its token check, and the token check (`_token_is_appcontainer`)."""
    tree = ast.parse(SHIM.read_text(encoding="utf-8"))
    token = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_token_is_appcontainer"), None)
    rest = ast.Module(body=[n for n in tree.body if n is not token], type_ignores=[])
    return rest, token


def test_the_shim_source_cannot_switch_off_any_confinement():
    """Outside its one token check it imports only os, reads only its own variable, assigns only
    mkdir, and runs no process, file, network, ctypes or dynamic code. (Updated on the security
    review's condition: ctypes is allowed, but only inside `_token_is_appcontainer`; see
    test_ctypes_serves_only_the_one_token_call.)"""
    tree, _ = _shim_tree()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(f"from {node.module}")
    assert imports <= {"os", "from __future__"}, imports
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("eval", "exec", "compile", "open", "__import__", "ctypes", "subprocess", "system", "popen", "putenv", "unsetenv", "environ_set"):
        assert forbidden not in names and forbidden not in attrs, forbidden
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert {s for s in strings if s.isupper() and s.startswith("VOOL")} == {VAR}, strings
    stores = {
        f"{target.value.id}.{target.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
    }
    assert stores <= {"module.mkdir", "mkdir.__doc__"}, stores
    # Only 0o700 is ever rewritten.
    numbers = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)}
    assert numbers <= {0o700, 0o777}, numbers


# --- the model: real pytest under the grant, the 0o700 rule, and no mkdir outside the write root --

_CONFINED_MKDIR = r'''
made_all = []
_inner_mkdir = os.mkdir


def _confined_mkdir(path, mode=0o777, *, dir_fd=None):
    full = norm(os.fsdecode(os.fspath(path)))
    if not inside(full, run_folder):
        denied.append(full)
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(path))
    _inner_mkdir(path, mode, dir_fd=dir_fd)
    made_all.append(full)


os.mkdir = _confined_mkdir
if grant.get("shim"):
    import importlib.util

    _spec = importlib.util.spec_from_file_location("vool_windows_job_mkdir", grant["shim"])
    _shim = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_shim)
    if grant.get("appcontainer", True):
        _shim._token_is_appcontainer = lambda: True  # the model's job runs in an AppContainer
    else:
        _shim._token_is_appcontainer = lambda: False
    _shim.install(nt=True)  # the bundle's .pth import, as on Windows
atexit.register(lambda: print("VOOL-JOB-MADE " + json.dumps(made_all)))

'''
_MKDIR_MODEL = _MODEL.replace("import runpy\n", _CONFINED_MKDIR + "import runpy\n", 1)
assert _MKDIR_MODEL != _MODEL

FIXTURE_TESTS = '''import os, pathlib, tempfile


def test_tmp_path(tmp_path):
    (tmp_path / "x.txt").write_text("x")
    assert (tmp_path / "x.txt").read_text() == "x"


def test_tmpdir(tmpdir):
    tmpdir.join("y.txt").write("y")
    assert tmpdir.join("y.txt").read() == "y"


def test_tmp_path_factory(tmp_path_factory):
    folder = tmp_path_factory.mktemp("made")
    (folder / "z.txt").write_text("z")
    assert sorted(p.name for p in folder.iterdir()) == ["z.txt"]


def test_temporary_directory():
    with tempfile.TemporaryDirectory() as folder:
        (pathlib.Path(folder) / "w.txt").write_text("w")
        assert os.listdir(folder) == ["w.txt"]
'''

OUTSIDE_TEST = '''import os, tempfile, pytest


def test_a_folder_outside_the_write_root_is_still_denied():
    with pytest.raises(PermissionError):
        tempfile.mkdtemp(dir=os.path.dirname(os.getcwd()))
    with pytest.raises(PermissionError):
        os.mkdir(os.path.join(os.path.dirname(os.getcwd()), "planted"), 0o700)
'''


def _run(
    base: Path, proj: Path, job_env: dict[str, str], *, shim: bool = True, appcontainer: bool = True
) -> tuple[int, str, list[str], list[str]]:
    job_dir = base / "job"
    job_dir.mkdir(exist_ok=True)
    job = job_dir / "confined_job.py"
    job.write_text(_MKDIR_MODEL, encoding="utf-8")
    shim_copy = job_dir / "vool_windows_job_mkdir.py"  # in the bundle it sits in site-packages, a read root
    shutil.copyfile(SHIM, shim_copy)
    grant = job_dir / "grant.json"
    grant.write_text(
        json.dumps(
            {
                "run_folder": str(proj),
                "stat_only": [str(proj.parent)],
                "shim": str(shim_copy) if shim else "",
                "appcontainer": appcontainer,
            }
        ),
        encoding="utf-8",
    )
    temp = Path(job_env["TEMP"])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "WINSIM", "VOOL_"))}
    env.pop("PYTHONPATH", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)})
    env.update({k: v for k, v in job_env.items() if k in ("PYTEST_ADDOPTS", VAR)})
    done = subprocess.run(
        [sys.executable, "-I", str(job), str(grant), *_vool_pytest_args()],
        cwd=str(proj), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env,
    )
    output = done.stdout + done.stderr
    owner_only: list[str] = []
    made: list[str] = []
    for line in output.splitlines():
        if line.startswith("VOOL-JOB-OWNER-ONLY "):
            owner_only = json.loads(line[len("VOOL-JOB-OWNER-ONLY "):])
        if line.startswith("VOOL-JOB-MADE "):
            made = json.loads(line[len("VOOL-JOB-MADE "):])
    return done.returncode, output, owner_only, made


def _vool_env(windows_jobs: list[dict], proj: Path) -> dict[str, str]:
    return _job_for(windows_jobs, proj, ["python", "-m", "pytest"])["env"]


def _inside(path: str | Path, root: Path) -> bool:
    full = Path(path).resolve()
    return full == root.resolve() or root.resolve() in full.parents


def test_tmp_path_tmpdir_factory_and_temporary_directory_work_in_the_job(windows, base):
    proj = _tree(base)
    (proj / "test_fixtures.py").write_text(FIXTURE_TESTS, encoding="utf-8")
    code, output, owner_only, _ = _run(base, proj, _vool_env(windows, proj))
    assert code == 0, output
    assert "5 passed" in output, output
    assert owner_only == [], owner_only


def test_every_folder_they_make_stays_in_the_jobs_temp_folder(windows, base):
    proj = _tree(base)
    (proj / "test_fixtures.py").write_text(FIXTURE_TESTS, encoding="utf-8")
    env = _vool_env(windows, proj)
    temp = Path(env["TEMP"])
    assert _inside(temp, proj) and temp.name == ".sandbox-tmp", temp
    code, output, _, made = _run(base, proj, env)
    assert code == 0, output
    assert made, output
    assert all(_inside(path, proj) for path in made), made  # the write root
    outside_cache = [p for p in made if Path(p).name != "__pycache__"]
    assert outside_cache and all(_inside(path, temp) for path in outside_cache), outside_cache
    assert any(Path(path).name.startswith("pytest-of-") for path in made), made


def test_a_mkdir_outside_the_write_root_is_still_denied(windows, base):
    proj = _tree(base)
    (proj / "test_outside.py").write_text(OUTSIDE_TEST, encoding="utf-8")
    code, output, _, made = _run(base, proj, _vool_env(windows, proj))
    assert code == 0, output
    assert "2 passed" in output, output
    assert not (proj.parent / "planted").exists()
    assert all(_inside(path, proj) for path in made), made


def test_without_the_variable_nothing_changes(windows, base):
    """The same job with the variable removed: the shim stays out, 0o700 folders stay owner-only and
    tmp_path errors, exactly as before the shim."""
    proj = _tree(base)
    (proj / "test_fixtures.py").write_text(FIXTURE_TESTS, encoding="utf-8")
    env = {k: v for k, v in _vool_env(windows, proj).items() if k != VAR}
    code, output, owner_only, _ = _run(base, proj, env)
    assert code == 1, output
    assert "PermissionError" in output, output
    assert owner_only, output


# --- the bundle -------------------------------------------------------------------------------


def _build() -> str:
    return BUILD.read_text(encoding="utf-8")


def test_the_bundle_ships_the_shim_into_the_embedded_pythons_site_packages():
    text = _build()
    assert re.search(r'sandbox\\windows_job_mkdir\.py', text), "the shim's source"
    assert re.search(r'vool_windows_job_mkdir\.py', text), "its name in site-packages"
    assert re.search(r'Lib\\site-packages', text)
    assert re.search(r'vool_windows_job_mkdir\.pth"\)\s+-Value\s+"import vool_windows_job_mkdir"', text), "the .pth line"


def test_the_bundle_checks_the_shim_is_off_without_the_variable_and_on_with_it():
    text = _build()
    block = text[text.index("vool_windows_job_mkdir.pth"):]
    assert "_vool_confined_job_mkdir" in block
    assert re.search(r'\$env:VOOL_WINDOWS_CONFINED_JOB\s*=\s*"1"', block)
    assert "Remove-Item Env:VOOL_WINDOWS_CONFINED_JOB" in block
    # Shipped before the bundle's dependency check, after pytest.
    assert text.index("vool_windows_job_mkdir.pth") > text.index("requirements-windows-pytest.txt")
    assert text.index("vool_windows_job_mkdir.pth") < text.index("& $py -m pip check")


def test_the_shim_marks_its_wrapper_the_way_the_bundle_check_reads_it():
    fake, _ = _fake_os()
    _shim().install(environ={VAR: "1"}, module=fake, nt=True)
    assert getattr(fake.mkdir, "_vool_confined_job_mkdir", False) is True


# --- the token check (security review condition): wrapped only inside an AppContainer -----------


def test_the_variable_in_a_process_that_is_not_an_appcontainer_wraps_nothing(monkeypatch):
    fake, _ = _fake_os()
    before = fake.mkdir
    monkeypatch.setattr(_shim(), "_token_is_appcontainer", lambda: False)
    assert _shim().install(environ={VAR: "1"}, module=fake, nt=True) is False
    assert fake.mkdir is before


@pytest.mark.parametrize("error", [OSError("no token"), AttributeError("no windll"), ValueError("bad"), RuntimeError("x")])
def test_a_token_check_that_raises_wraps_nothing(monkeypatch, error):
    fake, _ = _fake_os()
    before = fake.mkdir

    def check():
        raise error

    monkeypatch.setattr(_shim(), "_token_is_appcontainer", check)
    assert _shim().install(environ={VAR: "1"}, module=fake, nt=True) is False
    assert fake.mkdir is before


@pytest.mark.parametrize("answer", [None, 1, "yes", object()])
def test_only_a_true_answer_counts_as_an_appcontainer(monkeypatch, answer):
    fake, _ = _fake_os()
    monkeypatch.setattr(_shim(), "_token_is_appcontainer", lambda: answer)
    assert _shim().install(environ={VAR: "1"}, module=fake, nt=True) is False


def test_an_appcontainer_token_and_the_variable_wrap_mkdir(monkeypatch):
    fake, calls = _fake_os()
    monkeypatch.setattr(_shim(), "_token_is_appcontainer", lambda: True)
    assert _shim().install(environ={VAR: "1"}, module=fake, nt=True) is True
    assert getattr(fake.mkdir, "_vool_confined_job_mkdir", False) is True
    fake.mkdir("x", 0o700)
    assert calls == [("x", 0o777, None)]


@pytest.mark.parametrize(("environ", "nt"), [({}, True), ({VAR: "0"}, True), ({VAR: "1"}, False)])
def test_the_token_is_never_asked_unless_windows_and_the_variable_say_so(monkeypatch, environ, nt):
    """So VOOL's own process (no variable) never loads ctypes for it."""
    asked: list[int] = []
    monkeypatch.setattr(_shim(), "_token_is_appcontainer", lambda: asked.append(1) or True)
    fake, _ = _fake_os()
    assert _shim().install(environ=environ, module=fake, nt=nt) is False
    assert asked == []


def test_the_real_token_check_answers_false_in_an_unconfined_process():
    """Unmocked, in this unconfined test process: off Windows there is no token API, on Windows the
    token is not an AppContainer's. Either way the answer is False, and it never raises."""
    code = "import sys; import sandbox.windows_job_mkdir as m; sys.exit(0 if m._token_is_appcontainer() is False else 1)"
    done = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), timeout=60)
    assert done.returncode == 0


def test_ctypes_serves_only_the_one_token_call():
    """ctypes appears only in `_token_is_appcontainer`: it opens this process's token for query and
    asks GetTokenInformation(TokenIsAppContainer = 29), and every error answers False."""
    _, token = _shim_tree()
    assert token is not None
    imports = set()
    for node in ast.walk(token):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(f"from {node.module}")
    assert imports == {"ctypes", "from ctypes"}, imports
    called = {
        node.func.attr
        for node in ast.walk(token)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called <= {"WinDLL", "GetCurrentProcess", "OpenProcessToken", "GetTokenInformation", "CloseHandle", "byref", "sizeof", "POINTER", "HANDLE", "DWORD"}, called
    assert {"OpenProcessToken", "GetTokenInformation", "CloseHandle"} <= called
    strings = {n.value for n in ast.walk(token) if isinstance(n, ast.Constant) and isinstance(n.value, str)} - {ast.get_docstring(token)}
    assert strings == {"advapi32", "kernel32"}, strings
    numbers = {n.value for n in ast.walk(token) if isinstance(n, ast.Constant) and isinstance(n.value, int) and not isinstance(n.value, bool)}
    assert numbers <= {0, 0x0008, 29}, numbers  # TOKEN_QUERY and TokenIsAppContainer
    assert 29 in numbers and 0x0008 in numbers
    # The body is one try whose every handler answers False.
    body = [n for n in token.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Try), ast.dump(token)[:200]
    for handler in body[0].handlers:
        assert isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")
        assert [ast.dump(n) for n in handler.body] == [ast.dump(ast.Return(value=ast.Constant(value=False)))]
    # install() asks it only through the module name, so a mock (and the model) can answer it.
    rest, _ = _shim_tree()
    assert "_token_is_appcontainer" in {n.id for n in ast.walk(rest) if isinstance(n, ast.Name)}


def test_outside_an_appcontainer_0o700_folders_stay_owner_only_in_the_model(windows, base):
    """The variable is "1" but the token is not an AppContainer's: nothing is wrapped, the 0o700
    folders stay owner-only and tmp_path errors exactly as without the shim."""
    proj = _tree(base)
    (proj / "test_fixtures.py").write_text(FIXTURE_TESTS, encoding="utf-8")
    env = _vool_env(windows, proj)
    assert env.get(VAR) == "1"
    code, output, owner_only, _ = _run(base, proj, env, appcontainer=False)
    assert code == 1, output
    assert "PermissionError" in output, output
    assert owner_only, output


# --- VOOL's own process: the variable in its environment is logged ------------------------------


def test_vools_server_logs_a_warning_when_its_own_environment_carries_the_variable(caplog):
    from apps import vool_api_server as server

    caplog.set_level(logging.WARNING, logger="vool.api")
    assert server._warn_if_confined_job_variable_set({VAR: "1", "PATH": "x"}) is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and VAR in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("value", ["", "0", "anything"])
def test_any_value_of_the_variable_is_logged(caplog, value):
    from apps import vool_api_server as server

    caplog.set_level(logging.WARNING, logger="vool.api")
    assert server._warn_if_confined_job_variable_set({VAR: value}) is True
    assert any(VAR in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_no_warning_without_the_variable(caplog):
    from apps import vool_api_server as server

    caplog.set_level(logging.WARNING, logger="vool.api")
    assert server._warn_if_confined_job_variable_set({"PATH": "x"}) is False
    assert not [r for r in caplog.records if VAR in r.getMessage()]


def test_the_bundle_build_expects_the_shim_off_with_the_variable_outside_an_appcontainer():
    """The build is not an AppContainer, so with the variable set the shim must stay off and the
    token check must answer False there; "on" is the Windows hardware check in the triage note."""
    text = _build()
    block = text[text.index('$env:VOOL_WINDOWS_CONFINED_JOB = "1"'):text.index("Remove-Item Env:VOOL_WINDOWS_CONFINED_JOB\n")]
    assert "_token_is_appcontainer() is False" in block, block
    assert "not getattr(os.mkdir, '_vool_confined_job_mkdir', False)" in block, block


def test_server_startup_runs_the_check_once_logging_reaches_its_file():
    """`main()` calls the check, after logging is routed to vool_api.log and before the app is made."""
    source = (REPO / "apps" / "vool_api_server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_warn_if_confined_job_variable_set"
    ]
    assert len(calls) == 1
    routed = next(n for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "route_logging_to_file")
    made = next(n for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "create_app")
    assert routed.lineno < calls[0].lineno < made.lineno
