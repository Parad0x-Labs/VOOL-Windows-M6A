"""Windows: a confined pytest run leaves `pytest-cache-files-*` folders it can no longer read.

Real hardware (VOOL 0.7.1 with the stat-only parent right, Windows 11, bundled Python 3.12.7,
pytest 9.1.1). "run pytest in C:\\Users\\Satoshi\\VOOL HW Test\\proj" exited 2:
"_pytest\\main.py:569 in collect / for direntry in scandir(self.path) / _pytest\\pathlib.py:954 in
scandir / E PermissionError: [WinError 5] Access is denied: '...\\proj\\pytest-cache-files-7i5mclpd'".
`proj` held six empty `pytest-cache-files-*` folders left by earlier confined runs, two of them made
by this very run. Their DACL was only SYSTEM F, Administrators F, OWNER RIGHTS F (protected), owner
the user, label Low: the confined job made them and then could not list them.

Why: pytest's cache plugin makes its cache folder atomically, `tempfile.mkdtemp(prefix=
"pytest-cache-files-", dir=<rootdir>)`, fills it and renames it to `.pytest_cache`. CPython 3.12.4
and later (gh-118486) create a folder asked for with mode 0o700 on Windows with the protected DACL
`D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`, and mkdtemp always asks for 0o700. No ACE
there names the job's capability, so the AppContainer job loses every right on the folder it just
made: the README write fails (a PytestCacheWarning), the cleanup fails, and the folder stays. The
next run's collection lists the run folder, walks into the leftover and dies.

The fix runs every confined pytest job with `-p no:cacheprovider` (the cache can never be written in
the job anyway) and `--ignore-glob=pytest-cache-files-*` (a leftover from before the fix never stops
collection again), through `PYTEST_ADDOPTS` in the job's environment: the job's argv, pinned by
other rows, stays what it was.

pytest's `tmp_path`/`tmpdir` hit the same rule: `getbasetemp` and `make_numbered_dir` make their
folders with mode 0o700 too, `--basetemp` included, and so does `tempfile.mkdtemp` in a user's own
test. That is a known limit here; the rows pin it, and one row shows that a mkdir that drops the
0o700 mode (the proposal in the triage) would be enough.

The model rows run the real pytest the suite runs (9.1.1, the bundle's pin) in a subprocess that
models the grant (tests/test_windows_pytest_stays_in_run_folder.py's job, with the run folder's
parent stat-only, as the product grants it now) plus the 0o700 rule: a folder made with mode 0o700
can be stat'ed but not listed, opened, renamed or removed, and nothing inside it can be reached.
Nothing is Windows-only, no path text is compared, and no row uses the `tmp_path` fixture.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from sandbox import job_runner as jr
from sandbox import windows_confinement as wc
from sandbox.container_adapter import ExecutionResult
from sandbox.resource_limits import ExecutionPolicy
from tests.test_windows_pytest_stays_in_run_folder import _JOB, _tree, _vool_pytest_args

# The 0o700 rule, layered on the grant model before pytest starts. The grant file may name folders
# already made 0o700 by an earlier run ("owner_only"); the model prints every such folder at exit.
_OWNER_ONLY = r'''
owner_only = {norm(p) for p in grant.get("owner_only", [])}
plain_mode = bool(grant.get("plain_mode"))


def _reached(path, *, itself):
    if isinstance(path, int):
        return False
    full = norm(os.fsdecode(os.fspath(path)))
    for folder in owner_only:
        if full.startswith(folder.rstrip(os.sep) + os.sep) or (itself and full == folder):
            return True
    return False


def _owner_only_guard(name, *, itself):
    real = getattr(os, name)

    def call(*args, **kwargs):
        target = args[0] if args else kwargs.get("path", ".")
        if _reached(target, itself=itself):
            denied.append(os.fsdecode(os.fspath(target)))
            raise PermissionError(errno.EACCES, "Access is denied", os.fspath(target))
        return real(*args, **kwargs)

    setattr(os, name, call)


_owner_only_guard("stat", itself=False)
_owner_only_guard("lstat", itself=False)
_owner_only_guard("scandir", itself=True)
_owner_only_guard("listdir", itself=True)
_owner_only_guard("open", itself=True)
_owner_only_guard("rmdir", itself=True)
_owner_only_guard("unlink", itself=True)
_owner_only_guard("rename", itself=True)
_owner_only_guard("replace", itself=True)

import builtins, io

_real_io_open = io.open


def _guarded_open(file, *args, **kwargs):
    if not isinstance(file, int) and _reached(file, itself=True):
        denied.append(os.fsdecode(os.fspath(file)))
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(file))
    return _real_io_open(file, *args, **kwargs)


io.open = builtins.open = _guarded_open
_real_mkdir = os.mkdir


def _mkdir(path, mode=0o777, *, dir_fd=None):
    if _reached(path, itself=False):
        denied.append(os.fsdecode(os.fspath(path)))
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(path))
    _real_mkdir(path, mode, dir_fd=dir_fd)
    if mode == 0o700 and not plain_mode:
        owner_only.add(norm(os.fsdecode(os.fspath(path))))


os.mkdir = _mkdir
import atexit

atexit.register(lambda: print("VOOL-JOB-OWNER-ONLY " + json.dumps(sorted(owner_only))))

'''

_MODEL = _JOB.replace("import runpy\n", _OWNER_ONLY + "import runpy\n", 1)
assert _MODEL != _JOB

PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"


@pytest.fixture
def base():
    """A scratch folder made with plain tempfile (the rows never use the tmp_path fixture)."""
    folder = Path(tempfile.mkdtemp(prefix="vool-cache-rows-")).resolve()
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


# --- what VOOL hands a confined pytest job (Windows simulated) ----------------------------------


@pytest.fixture
def windows(monkeypatch, base):
    """Windows for the job runner: a launcher that never starts, and every job's argv and environment
    recorded where `_run_windows_launcher` would start the launcher."""
    monkeypatch.setattr(jr, "_is_windows_platform", lambda: True)

    def check_path(value, what):
        text = str(value)
        if not os.path.isabs(text):
            raise wc.PolicyError(f"{what} must be absolute: {text!r}")
        return text

    monkeypatch.setattr(wc, "_check_path", check_path)
    real_inside = wc._inside

    def win(path: str) -> str:
        text = str(path)
        return "C:" + text.replace("/", "\\") if text.startswith("/") else text

    monkeypatch.setattr(wc, "_inside", lambda child, root: real_inside(win(child), win(root)))
    launcher = base / "bin" / "VoolConfine.exe"
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: launcher)
    monkeypatch.setattr(wc, "probe", lambda project_root: True)
    monkeypatch.setattr(wc, "launcher_form", lambda exe, form, policy_path, **kw: True)
    jobs: list[dict] = []

    def run_wrapped(self, argv, cwd_path, cancel_event, *, creationflags=0, outcome=None, env_filter=None, after_spawn=None):
        env = self._child_environment(cwd_path)
        if env_filter is not None:
            env = env_filter(env)
        assert argv[1] == "--policy" and argv[3] == "--", argv
        jobs.append({"program": list(argv[4:]), "env": dict(env)})
        Path(argv[2]).with_name("status.json").write_text(
            json.dumps({"schema": wc.STATUS_SCHEMA, "child_started": True, "exit_code": 0, "repositories_scanned": True}),
            encoding="utf-8",
        )
        return ExecutionResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(jr.JobRunner, "_run_wrapped", run_wrapped)
    jr.reset_isolation_backend_probe_cache()
    yield jobs
    jr.reset_isolation_backend_probe_cache()
    jr._forget_staged_at_exit()


def _job_for(jobs: list[dict], folder: Path, argv: list[str]) -> dict:
    runner = jr.JobRunner(ExecutionPolicy(workspace_root=folder, writable_roots=(folder,)))
    assert runner.run(argv, cwd=folder).returncode == 0
    assert len(jobs) == 1, jobs
    return jobs.pop()


def _proj(base: Path) -> Path:
    proj = base / "home" / "VOOL HW Test" / "proj"
    proj.mkdir(parents=True)
    (proj / "calc.py").write_text("print(1)\n", encoding="utf-8")
    return proj.resolve()


def _addopts(job: dict) -> list[str]:
    return shlex.split(job["env"].get("PYTEST_ADDOPTS", ""))


_PYTEST_FORMS = [
    pytest.param(["pytest"], id="pytest"),
    pytest.param(["python", "-m", "pytest"], id="python-m-pytest"),
    pytest.param([sys.executable, "-m", "pytest", "-q"], id="interpreter-m-pytest"),
]


@pytest.mark.parametrize("argv", _PYTEST_FORMS)
def test_a_confined_pytest_job_runs_without_the_cache_plugin(windows, base, argv):
    job = _job_for(windows, _proj(base), argv)
    options = _addopts(job)
    assert any(options[i:i + 2] == ["-p", "no:cacheprovider"] for i in range(len(options))), job["env"]


@pytest.mark.parametrize("argv", _PYTEST_FORMS)
def test_a_confined_pytest_job_skips_leftover_cache_folders(windows, base, argv):
    job = _job_for(windows, _proj(base), argv)
    assert "--ignore-glob=pytest-cache-files-*" in _addopts(job), job["env"]


def test_the_job_argv_stays_as_it_was(windows, base):
    """The options travel in the environment: the program the launcher starts is unchanged."""
    job = _job_for(windows, _proj(base), ["pytest", "-q"])
    assert _same(job["program"][0], sys.executable), job
    assert job["program"][1:] == ["-m", "pytest", "-q"], job


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["python", "-c", "print(1)"], id="python-c"),
        pytest.param(["python", "calc.py"], id="script"),
        pytest.param(["python", "-m", "unittest"], id="unittest"),
        pytest.param(["python", "-m", "compileall", "-q", "."], id="compileall"),
    ],
)
def test_no_other_job_gets_pytest_options(windows, base, argv):
    job = _job_for(windows, _proj(base), argv)
    assert "PYTEST_ADDOPTS" not in job["env"], job["env"]


def test_the_job_keeps_its_own_temp_folder(windows, base):
    """The options do not move TEMP: tmp_path and tempfile still use the job's own temp folder."""
    proj = _proj(base)
    job = _job_for(windows, proj, ["pytest"])
    assert _same(job["env"]["TEMP"], proj / ".sandbox-tmp"), job["env"]


# --- the model: real pytest under the grant plus the 0o700 rule --------------------------------


def _vool_job_env(monkeypatch_windows: list[dict], proj: Path) -> dict[str, str]:
    """The PYTEST_ADDOPTS VOOL's confined pytest job gets (empty when it gets none)."""
    job = _job_for(monkeypatch_windows, proj, ["python", "-m", "pytest"])
    value = job["env"].get("PYTEST_ADDOPTS")
    return {} if value is None else {"PYTEST_ADDOPTS": value}


def _run_model(
    base: Path,
    proj: Path,
    args: list[str],
    *,
    job_env: dict[str, str],
    owner_only: list[str] | tuple[str, ...] = (),
    plain_mode: bool = False,
) -> tuple[int, str, list[str], list[str]]:
    """One confined pytest run in *proj*. Returns exit code, output, denied paths, 0o700 folders."""
    job_dir = base / "job"
    job_dir.mkdir(exist_ok=True)
    job = job_dir / "confined_job.py"
    job.write_text(_MODEL, encoding="utf-8")
    grant = job_dir / "grant.json"
    grant.write_text(
        json.dumps(
            {
                "run_folder": str(proj),
                "stat_only": [str(proj.parent)],
                "owner_only": list(owner_only),
                "plain_mode": plain_mode,
            }
        ),
        encoding="utf-8",
    )
    temp = proj / ".sandbox-tmp"
    temp.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "WINSIM"))}
    env.pop("PYTHONPATH", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # the bundle ships pytest and no plugins
    env.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)})
    env.update(job_env)
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
    made: list[str] = []
    for line in output.splitlines():
        if line.startswith("VOOL-JOB-DENIED "):
            denied = json.loads(line[len("VOOL-JOB-DENIED "):])
        if line.startswith("VOOL-JOB-OWNER-ONLY "):
            made = json.loads(line[len("VOOL-JOB-OWNER-ONLY "):])
    return done.returncode, output, denied, made


def _cache_leftovers(proj: Path) -> list[str]:
    return sorted(entry.name for entry in proj.iterdir() if entry.name.startswith("pytest-cache-files-"))


def _leftovers(proj: Path, count: int) -> list[str]:
    """*count* empty 0o700 cache folders from earlier runs, as the hardware's `proj` held."""
    made = []
    for index in range(count):
        folder = proj / f"pytest-cache-files-old{index}"
        folder.mkdir()
        made.append(str(folder))
    return made


def test_the_cache_plugin_leaves_an_unreadable_folder_the_way_the_hardware_did(base):
    """Without the options: the first run leaves a 0o700 folder; the next run's collection dies on it."""
    proj = _tree(base)
    code, output, _, made = _run_model(base, proj, _vool_pytest_args(), job_env={})
    assert code == 0, output
    assert "PytestCacheWarning" in output, output
    assert len(_cache_leftovers(proj)) == 1, output
    assert not (proj / ".pytest_cache").exists()
    code, output, denied, _ = _run_model(base, proj, _vool_pytest_args(), job_env={}, owner_only=made)
    assert code == 2, output
    assert "PermissionError" in output and "pytest-cache-files-" in output, output
    # Every denial is the leftover or something inside one (this run makes another one as well).
    assert denied, denied
    assert all(any(part.startswith("pytest-cache-files-") for part in Path(path).parts) for path in denied), denied


def test_vools_pytest_job_leaves_no_cache_folder_and_runs_again(windows, base):
    proj = _tree(base)
    job_env = _vool_job_env(windows, proj)
    made: list[str] = []
    for _ in range(2):
        code, output, denied, made = _run_model(base, proj, _vool_pytest_args(), job_env=job_env, owner_only=made)
        assert code == 0, output
        assert "1 passed" in output, output
        assert "PytestCacheWarning" not in output, output
        assert denied == [], denied
    assert _cache_leftovers(proj) == [], _cache_leftovers(proj)
    assert not (proj / ".pytest_cache").exists()


def test_a_leftover_unreadable_cache_folder_no_longer_stops_collection(windows, base):
    """Six empty 0o700 `pytest-cache-files-*` folders from before the fix, as on the hardware."""
    proj = _tree(base)
    leftover = _leftovers(proj, 6)
    code, output, denied, _ = _run_model(base, proj, _vool_pytest_args(), job_env=_vool_job_env(windows, proj), owner_only=leftover)
    assert code == 0, output
    assert "1 passed" in output, output
    assert denied == [], denied


def test_tests_in_subfolders_are_still_collected(windows, base):
    proj = _tree(base)
    (proj / "tests").mkdir()
    (proj / "tests" / "test_more.py").write_text(PASSING_TEST, encoding="utf-8")
    nested = proj / "sub" / "pytest-cache-files-notmine"
    nested.mkdir(parents=True)
    (nested / "test_nested.py").write_text(PASSING_TEST, encoding="utf-8")
    code, output, _, _ = _run_model(base, proj, _vool_pytest_args(), job_env=_vool_job_env(windows, proj))
    assert code == 0, output
    # test_calc.py, tests/test_more.py and the nested one: the ignore reaches only the run folder's own names.
    assert "3 passed" in output, output


# --- tmp_path and tempfile: the same 0o700 rule (a known limit, not fixed here) ----------------

TMP_PATH_TEST = "def test_uses_tmp_path(tmp_path):\n    (tmp_path / 'x.txt').write_text('x')\n    assert (tmp_path / 'x.txt').exists()\n"
MKDTEMP_TEST = (
    "import tempfile, pathlib\n\n"
    "def test_uses_mkdtemp():\n"
    "    with tempfile.TemporaryDirectory() as folder:\n"
    "        (pathlib.Path(folder) / 'x.txt').write_text('x')\n"
)


def test_tmp_path_fails_in_the_job_a_known_limit(windows, base):
    proj = _tree(base)
    (proj / "test_tmp.py").write_text(TMP_PATH_TEST, encoding="utf-8")
    code, output, denied, made = _run_model(base, proj, _vool_pytest_args(), job_env=_vool_job_env(windows, proj))
    assert code == 1, output
    assert "1 passed" in output and "1 error" in output, output
    assert "PermissionError" in output, output
    assert any(Path(path).name.startswith("pytest-of-") for path in made), made
    assert denied, denied


def test_basetemp_does_not_avoid_it(windows, base):
    """`--basetemp` inside the run folder: pytest makes that folder with mode 0o700 as well, so the
    test errors at setup and pytest's own session-end cleanup of that folder fails too."""
    proj = _tree(base)
    (proj / "test_tmp.py").write_text(TMP_PATH_TEST, encoding="utf-8")
    args = [*_vool_pytest_args(), f"--basetemp={proj / '.sandbox-tmp' / 'basetemp'}"]
    code, output, _, _ = _run_model(base, proj, args, job_env=_vool_job_env(windows, proj))
    assert code == 1, output
    assert "test_calc.py ." in output and "test_tmp.py E" in output, output
    assert "PermissionError" in output, output


def test_tempfile_in_a_users_own_test_fails_the_same_way(windows, base):
    proj = _tree(base)
    (proj / "test_mkdtemp.py").write_text(MKDTEMP_TEST, encoding="utf-8")
    code, output, _, _ = _run_model(base, proj, _vool_pytest_args(), job_env=_vool_job_env(windows, proj))
    assert code == 1, output
    assert "PermissionError" in output, output


def test_a_mkdir_that_drops_the_0o700_mode_would_be_enough_for_tmp_path(windows, base):
    """The proposal's evidence: with 0o700 made like any other mode (the folder inherits the run
    folder's grant), tmp_path and tempfile both work, and nothing else in the run changes."""
    proj = _tree(base)
    (proj / "test_tmp.py").write_text(TMP_PATH_TEST, encoding="utf-8")
    (proj / "test_mkdtemp.py").write_text(MKDTEMP_TEST, encoding="utf-8")
    code, output, denied, made = _run_model(
        base, proj, _vool_pytest_args(), job_env=_vool_job_env(windows, proj), plain_mode=True
    )
    assert code == 0, output
    assert "3 passed" in output, output
    assert denied == [] and made == [], (denied, made)
