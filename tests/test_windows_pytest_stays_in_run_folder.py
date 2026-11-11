"""Windows: a confined `python -m pytest` in a marked subfolder stats the folder ABOVE it.

Real hardware (VOOL 0.7.1, Windows 11, bundle pytest 9.1.1 on Python 3.12.7). The owner marked
`C:\\Users\\Satoshi\\VOOL HW Test` and asked VOOL to run the tests in its subfolder `proj`. VOOL ran
`...\\VOOL\\python\\python.exe -m pytest` confined, cwd `proj`: exit code 2,
"rootdir: C:\\Users\\Satoshi\\VOOL HW Test\\proj", then
"E PermissionError: [WinError 5] Access is denied: 'C:\\Users\\Satoshi\\VOOL HW Test'" and a
PytestCacheWarning. `python -m unittest` in the same folder runs.

The confined job is granted its own folder (write root `proj`, job read root `proj`) and the
interpreter. It holds no right on `proj`'s parent. A missing name under that parent answers
not-found (traverse is not checked), so pytest's config-file walk up the ancestors
(`findpaths.locate_config`: `<ancestor>/pytest.toml` ... `is_file()`) gets False and goes on. What
fails is a stat of the parent folder ITSELF, which Windows answers only with a right on that folder
or a list right on ITS parent. pytest 9.1.1 does that stat on every run: `Session.collect` builds
the collection chain from the top folder inside `--confcutdir` (`proj`, the rootdir), and
`Session._collect_path(proj)` asks `gethookproxy(proj.parent)` ->
`PytestPluginManager._getconftestmodules` -> `_get_directory(parent)` -> `parent.is_file()`. The
PermissionError is not one pathlib ignores, so collection is interrupted (exit 2).

No pytest option moves that stat: `--rootdir`, `--confcutdir`, `-c <file>`, `--import-mode` and
`-p no:cacheprovider` all leave it in place (rows below). A stat-only right on that one folder is
enough, and with it a conftest.py in the parent is still never imported (confcutdir is `proj`).

The rows run the real pytest the suite runs (the bundle's pin, 9.1.1) in a subprocess whose
`os.stat`/`os.lstat`/`os.scandir`/`os.listdir` model the job's grant: an object that exists outside
the run folder (and outside the interpreter, its packages and anything on `sys.path`) is denied,
a missing name there answers not-found, and the objects named stat-only may be stat'ed but not
listed. Nothing is Windows-only and no path text is compared, so every host reads the rows alike.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.execution.validation_tools import runtime_validation_command, validation_command
from sandbox.network_guard import parse_command

# The confined job, modelled. argv: <grant.json> <pytest args...>; run with cwd = the run folder.
_JOB = r'''
import errno, json, os, sys

grant = json.loads(open(sys.argv[1], encoding="utf-8").read())
del sys.argv[1]
norm = lambda p: os.path.normcase(os.path.abspath(p))
run_folder = norm(grant["run_folder"])
stat_only = {norm(p) for p in grant["stat_only"]}
# The interpreter, its packages, the null device and this model's own folder (the real job's
# interpreter is python.exe -m pytest; its files sit in the bundle, a shared read root).
allowed = [
    norm(p)
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path, os.devnull, os.path.dirname(__file__))
    if p
]
denied = []
real_lstat = os.lstat


def inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def exists(path):
    try:
        real_lstat(path)
        return True
    except OSError:
        return False


resolving = []


def refuse(path, listing):
    if resolving or isinstance(path, int):
        return False
    text = os.fsdecode(os.fspath(path))
    full = norm(text)
    if inside(full, run_folder) or any(inside(full, root) for root in allowed):
        return False
    if full in stat_only and not listing:
        return False
    if not exists(full):
        return False  # a missing name answers not-found
    denied.append(text)
    return True


def guarded(name, listing):
    real = getattr(os, name)

    def call(*args, **kwargs):
        target = args[0] if args else kwargs.get("path", ".")
        if refuse(target, listing):
            raise PermissionError(errno.EACCES, "Access is denied", os.fspath(target))
        return real(*args, **kwargs)

    setattr(os, name, call)


# Windows resolves a path in the kernel when it opens it (GetFinalPathNameByHandle): no stat of each
# ancestor, as posixpath.realpath does with lstat. The model resolves the Windows way on every host.
real_realpath = os.path.realpath


def realpath(*args, **kwargs):
    resolving.append(1)
    try:
        return real_realpath(*args, **kwargs)
    finally:
        resolving.pop()


os.path.realpath = realpath

guarded("stat", False)
guarded("lstat", False)
guarded("scandir", True)
guarded("listdir", True)

import runpy

sys.argv[0] = "pytest"
try:
    runpy.run_module("pytest", run_name="__main__", alter_sys=True)
    code = 0
except SystemExit as stop:
    code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
sys.stdout.flush()
print("VOOL-JOB-DENIED " + json.dumps(sorted(set(denied))))
sys.exit(code)
'''

PARENT_CONFTEST_MARK = "VOOL-PARENT-CONFTEST-IMPORTED"
OWN_CONFTEST_MARK = "VOOL-RUN-FOLDER-CONFTEST-IMPORTED"


def _tree(base: Path, *, package: bool = False, parent_conftest: bool = False, own_conftest: bool = False) -> Path:
    """`<base>/VOOL HW Test/proj` holding one passing test, the hardware's layout."""
    mark_root = base / "VOOL HW Test"
    proj = mark_root / "proj"
    proj.mkdir(parents=True)
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # A unittest TestCase, so pytest and `python -m unittest` both run it.
    test = "import unittest\n\n{imp}\n\nclass TestCalc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual({call}, 5)\n"
    (proj / "test_calc.py").write_text(test.format(imp="from calc import add", call="add(2, 3)"), encoding="utf-8")
    if package:
        (proj / "__init__.py").write_text("", encoding="utf-8")
        (proj / "test_calc.py").write_text(test.format(imp="", call="2 + 3"), encoding="utf-8")
    if parent_conftest:
        (mark_root / "conftest.py").write_text(f"print({PARENT_CONFTEST_MARK!r})\n", encoding="utf-8")
    if own_conftest:
        (proj / "conftest.py").write_text(f"print({OWN_CONFTEST_MARK!r})\n", encoding="utf-8")
    return proj


def _vool_pytest_args() -> list[str]:
    """What VOOL hands the bundled interpreter for "run pytest in <folder>", after `-m pytest`."""
    argv = parse_command(runtime_validation_command(validation_command("workspace.run_tests", {"command": "python -m pytest"})))
    assert argv[1:3] == ["-m", "pytest"], argv
    return argv[3:]


def _run_job(tmp_path: Path, proj: Path, args: list[str], *, stat_only: tuple[Path, ...] = ()) -> tuple[int, str, list[str]]:
    job = tmp_path / "job" / "confined_job.py"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(_JOB, encoding="utf-8")
    grant = tmp_path / "job" / "grant.json"
    grant.write_text(
        json.dumps({"run_folder": str(proj), "stat_only": [str(p) for p in stat_only]}), encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "WINSIM"))}
    env.pop("PYTHONPATH", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # the bundle ships pytest and no plugins
    done = subprocess.run(
        [sys.executable, "-I", str(job), str(grant), *args],
        cwd=str(proj),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    output = done.stdout + done.stderr
    denied: list[str] = []
    for line in output.splitlines():
        if line.startswith("VOOL-JOB-DENIED "):
            denied = json.loads(line[len("VOOL-JOB-DENIED "):])
    return done.returncode, output, denied


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def test_vool_runs_pytest_as_its_own_interpreter():
    """The command the rows run is VOOL's: "run pytest in <folder>" becomes `<VOOL's python> -m pytest`."""
    argv = parse_command(runtime_validation_command(validation_command("workspace.run_tests", {"command": "python -m pytest"})))
    assert _same(argv[0], sys.executable), argv
    assert argv[1:3] == ["-m", "pytest"], argv


def test_a_job_granted_only_its_folder_fails_the_way_the_hardware_did(tmp_path):
    proj = _tree(tmp_path)
    code, output, denied = _run_job(tmp_path, proj, _vool_pytest_args())
    assert code == 2, output
    assert "PermissionError" in output, output
    assert "VOOL HW Test" in output, output
    assert len(denied) == 1 and _same(denied[0], proj.parent), denied


def test_unittest_in_the_same_folder_needs_nothing_above_it(tmp_path):
    proj = _tree(tmp_path)
    job = tmp_path / "job" / "unittest_job.py"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(_JOB.replace('"pytest"', '"unittest"'), encoding="utf-8")
    grant = tmp_path / "job" / "grant.json"
    grant.write_text(json.dumps({"run_folder": str(proj), "stat_only": []}), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-I", str(job), str(grant)], cwd=str(proj), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'VOOL-JOB-DENIED []' in done.stdout, done.stdout


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["--rootdir={cwd}", "--confcutdir={cwd}", "-p", "no:cacheprovider"], id="rootdir-confcutdir-nocache"),
        pytest.param(["--rootdir=.", "--confcutdir=."], id="relative-rootdir-confcutdir"),
        pytest.param(["--rootdir={cwd}", "--confcutdir={cwd}", "-p", "no:cacheprovider", "--import-mode=importlib"], id="importlib"),
        pytest.param(["--rootdir={cwd}", "--confcutdir={cwd}", "-c", "pytest.ini", "-p", "no:cacheprovider"], id="inifile-in-folder"),
        pytest.param(["--rootdir={cwd}", "--confcutdir={cwd}", "-p", "no:cacheprovider", "test_calc.py"], id="named-test-file"),
    ],
)
def test_no_pytest_option_keeps_the_run_inside_its_folder(tmp_path, options):
    """`--rootdir`, `--confcutdir`, an explicit inifile, the import mode and the cache plugin leave the
    stat of the run folder's parent in place, so options alone cannot fix the hardware run."""
    proj = _tree(tmp_path)
    (proj / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    args = [o.format(cwd=str(proj)) for o in options]
    code, output, denied = _run_job(tmp_path, proj, [*_vool_pytest_args(), *args])
    assert code == 2, output
    assert "PermissionError" in output, output
    assert len(denied) == 1 and _same(denied[0], proj.parent), denied


def test_a_stat_only_right_on_the_parent_folder_is_enough(tmp_path):
    proj = _tree(tmp_path)
    code, output, denied = _run_job(tmp_path, proj, _vool_pytest_args(), stat_only=(proj.parent,))
    assert code == 0, output
    assert "1 passed" in output, output
    assert denied == [], denied


def test_a_stat_only_right_on_the_parent_is_enough_for_a_package_folder(tmp_path):
    """`proj/__init__.py`: pytest also asks whether the parent is a package (`resolve_package_path`:
    is it a folder, does it hold `__init__.py`). A stat of the folder and a not-found on the name do."""
    proj = _tree(tmp_path, package=True)
    code, output, denied = _run_job(tmp_path, proj, _vool_pytest_args(), stat_only=(proj.parent,))
    assert code == 0, output
    assert "1 passed" in output, output
    assert denied == [], denied


def test_the_stat_only_right_never_lists_the_parent(tmp_path):
    proj = _tree(tmp_path)
    (proj.parent / "sibling").mkdir()
    code, output, denied = _run_job(tmp_path, proj, _vool_pytest_args(), stat_only=(proj.parent,))
    assert code == 0, output
    assert denied == [], denied
    probe = tmp_path / "job" / "list_parent.py"
    probe.write_text(
        _JOB.replace('runpy.run_module("pytest", run_name="__main__", alter_sys=True)', "os.listdir('..')"),
        encoding="utf-8",
    )
    grant = tmp_path / "job" / "grant.json"
    done = subprocess.run(
        [sys.executable, "-I", str(probe), str(grant)], cwd=str(proj), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert "PermissionError" in done.stderr, done.stderr


def test_a_conftest_in_the_parent_is_never_imported(tmp_path):
    """The mark root's conftest.py sits above the run folder: with the parent stat-only it stays
    unread, because confcutdir is the run folder (its rootdir) and nothing reads above it."""
    proj = _tree(tmp_path, parent_conftest=True)
    code, output, denied = _run_job(tmp_path, proj, [*_vool_pytest_args(), "-s"], stat_only=(proj.parent,))
    assert code == 0, output
    assert PARENT_CONFTEST_MARK not in output, output
    assert denied == [], denied


def test_the_run_folders_own_conftest_still_loads(tmp_path):
    proj = _tree(tmp_path, own_conftest=True, parent_conftest=True)
    code, output, denied = _run_job(tmp_path, proj, [*_vool_pytest_args(), "-s"], stat_only=(proj.parent,))
    assert code == 0, output
    assert OWN_CONFTEST_MARK in output, output
    assert PARENT_CONFTEST_MARK not in output, output
    assert denied == [], denied


def test_the_rootdir_is_the_run_folder(tmp_path):
    proj = _tree(tmp_path)
    code, output, _ = _run_job(tmp_path, proj, _vool_pytest_args(), stat_only=(proj.parent,))
    assert code == 0, output
    line = next(line for line in output.splitlines() if line.startswith("rootdir: "))
    assert _same(line[len("rootdir: "):].split(",")[0].strip(), proj), line
