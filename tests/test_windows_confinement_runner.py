"""The job runner's Windows confinement branch (docs/WINDOWS_CONFINEMENT.md, chunk 3 of the plan).

These run on every platform with a FAKE launcher, a Python script that speaks the launcher's
command line and status-file contract. It confines nothing. It proves the runner's half:
- the prefix shape;
- that the policy and status files live outside every writable root;
- that a missing or contradicting status record is refused rather than reported as a command result;
- that only Python and pytest run in phase 1;
- that a `.git` the job plants is caught.
Real confinement is proven on Windows by the launcher and ACL chunks.

On a non-Windows host the policy's drive-letter rule can't be met by real temp paths, so the tests
swap `_check_path` for one that still demands an absolute path. Every other rule is the real one.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from sandbox import job_runner as jr
from sandbox import windows_confinement as wc
from sandbox.resource_limits import ExecutionPolicy

_STATUS = wc.STATUS_SCHEMA

#: The fake launcher. MODE is replaced per test:
#: run      - record child_started, run the child, record its exit code, exit with it;
#: refuse   - exit 125 and write no status (setup failed; the child never started);
#: refuse-started-false - write child_started false with an error, exit 125;
#: lie      - run the child but record exit code 0 whatever it returned;
#: newrepo  - run the child, report a new repository and exit 123 (the launcher's own scan);
#: newrepo-status-only - report a new repository but exit with the child's code;
#: exit123  - run the child and exit 123 with no repository listed;
#: grant-fails - the unconfined `grant` form exits 1;
#: unconfined-probe - run the child with no confinement at all (a launcher that confines nothing).
#: Every `grant`/`revoke` call is logged to forms.log next to the launcher.
_FAKE = r'''#!{python}
import json, os, subprocess, sys
MODE = {mode!r}
args = sys.argv[1:]
if args and args[0] in ("grant", "revoke"):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "forms.log"), "a") as f:
        f.write(args[0] + "\n")
    sys.exit(1 if MODE == "grant-fails" and args[0] == "grant" else 0)
if len(args) < 4 or args[0] != "--policy" or args[2] != "--":
    sys.exit(126)
policy = json.load(open(args[1], encoding="utf-8"))
status = os.path.join(os.path.dirname(args[1]), "status.json")
def record(**fields):
    with open(status, "w", encoding="utf-8") as f:
        json.dump(dict(schema={schema!r}, **fields), f)
if MODE == "refuse":
    sys.stderr.write("VOOLCONFINE-REFUSED: fake setup failure\n")
    sys.exit(125)
if MODE == "refuse-started-false":
    record(child_started=False, error="acl verify failed")
    sys.exit(125)
record(child_started=True)
rc = subprocess.call(args[3:])
repos = [policy["write_roots"][0] + "/nested/.git"] if MODE.startswith("newrepo") else []
record(child_started=True, timed_out=False, exit_code=0 if MODE == "lie" else rc, new_repositories=repos)
sys.exit(123 if MODE in ("newrepo", "exit123") else rc)
'''


def _forms(launcher: Path) -> list[str]:
    log = launcher.parent / "forms.log"
    return log.read_text().split() if log.exists() else []


def _win(path: str) -> str:
    """A POSIX absolute path in the launcher's spelling, for the real containment check."""
    return "C:" + str(path).replace("/", "\\") if str(path).startswith("/") else str(path)


def _fake_launcher(folder: Path, mode: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"VoolConfine-{mode}"
    path.write_text(_FAKE.format(python=sys.executable, mode=mode, schema=_STATUS), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Simulate Windows for the runner and accept POSIX absolute paths in the policy."""
    if os.name == "nt":  # pragma: no cover - the fake launcher is a #! script
        pytest.skip("the fake launcher needs a POSIX host; Windows is proven by the real launcher")
    monkeypatch.setattr(jr, "_is_windows_platform", lambda: True)

    def check_path(value, what):
        text = str(value)
        if not os.path.isabs(text) or ".." in Path(text).parts:
            raise wc.PolicyError(f"{what} must be absolute and resolved: {text!r}")
        return text

    monkeypatch.setattr(wc, "_check_path", check_path)
    real_inside = wc._inside
    monkeypatch.setattr(wc, "_inside", lambda child, root: real_inside(_win(child), _win(root)))
    jr.reset_isolation_backend_probe_cache()
    yield monkeypatch
    jr.reset_isolation_backend_probe_cache()
    # `_with_network_isolation` called without `run` leaves its staging folder registered.
    import shutil

    with jr._WINDOWS_STAGED_LOCK:
        for stage in jr._WINDOWS_STAGED.values():
            shutil.rmtree(stage, ignore_errors=True)
        jr._WINDOWS_STAGED.clear()


def _use_launcher(monkeypatch, launcher: Path | None, *, probe: bool = True) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: launcher)

    def fake_probe(project_root):
        calls.append(1)
        return probe

    monkeypatch.setattr(wc, "probe", fake_probe)
    return calls


def _runner(tmp_path: Path, **policy) -> jr.JobRunner:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return jr.JobRunner(ExecutionPolicy(workspace_root=workspace, writable_roots=(workspace,), **policy))


def _inside(path: str, root: str) -> bool:
    return wc._inside(_win(path), _win(root))


# --- the prefix ------------------------------------------------------------------------------


def test_the_prefix_is_the_launcher_its_policy_and_the_interpreter(windows, tmp_path) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    runner = _runner(tmp_path)
    argv = runner._with_network_isolation(["python3", "-c", "print(1)"])
    assert argv[0] == str(launcher) and argv[1] == "--policy" and argv[3] == "--"
    assert argv[4:] == [str(Path(sys.executable).resolve()), "-c", "print(1)"]
    policy = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    ws = str((tmp_path / "ws").resolve())
    assert policy["schema"] == wc.SCHEMA
    assert policy["write_roots"] == [ws]
    assert policy["network"] is False and policy["deny_write_git"] is True
    assert _inside(policy["temp_dir"], ws)
    for written in (argv[2], policy["status_path"]):
        assert not any(_inside(written, root) for root in policy["write_roots"]), written
    assert policy["max_seconds"] >= runner.policy.max_seconds
    assert policy["memory_mb"] == runner.policy.max_memory_mb


def test_pytest_runs_as_this_interpreter_dash_m_pytest(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    argv = _runner(tmp_path)._with_network_isolation(["pytest", "-q", "tests"])
    assert argv[4:] == [str(Path(sys.executable).resolve()), "-m", "pytest", "-q", "tests"]


def test_network_trust_is_the_only_thing_that_grants_network(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    argv = _runner(tmp_path, allow_network_egress=True)._with_network_isolation(["python", "-c", "0"])
    policy = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    assert policy["network"] is True
    assert policy["write_roots"] == [str((tmp_path / "ws").resolve())], "network trust never widens writes"


@pytest.mark.parametrize(
    "program",
    ["git", "npm", "npx", "node", "cargo", "cmd", "powershell", "pwsh", "bash", "git.exe", "./python",
     "C:\\Windows\\System32\\cmd.exe", "/usr/bin/env"],
)
def test_phase_one_refuses_every_program_but_python_and_pytest(windows, tmp_path, program) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
        _runner(tmp_path)._with_network_isolation([program, "status"])
    assert not jr._WINDOWS_STAGED, "a refused program leaves no policy behind"


def test_an_absolute_program_must_be_this_interpreter(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    impostor = tmp_path / "ws" / "python.exe"
    (tmp_path / "ws").mkdir()
    impostor.write_text("", encoding="utf-8")
    with pytest.raises(jr.KernelIsolationUnavailableError, match="Python and pytest"):
        _runner(tmp_path)._with_network_isolation([str(impostor), "-c", "0"])
    argv = _runner(tmp_path)._with_network_isolation([sys.executable, "-c", "0"])
    assert argv[4] == str(Path(sys.executable).resolve())


def test_no_launcher_or_a_failed_probe_is_the_unchanged_refusal(windows, tmp_path) -> None:
    calls = _use_launcher(windows, None)
    with pytest.raises(jr.KernelIsolationUnavailableError) as missing:
        _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])
    assert calls == [], "no launcher means nothing to probe"
    assert "OS-level filesystem confinement and network isolation are required" in str(missing.value)

    calls = _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"), probe=False)
    for _ in range(2):
        with pytest.raises(jr.KernelIsolationUnavailableError, match="required but unavailable"):
            _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])
    assert calls == [1], "the probe verdict is cached per launcher"
    jr.reset_isolation_backend_probe_cache()
    with pytest.raises(jr.KernelIsolationUnavailableError):
        _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])
    assert calls == [1, 1], "resetting the probe cache forgets the Windows verdict too"


def test_a_write_root_inside_vool_code_without_the_exemption_is_refused(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    import core.runtime_state_protection as rsp

    code = (tmp_path / "ws").resolve()
    (tmp_path / "ws").mkdir()
    windows.setattr(rsp, "protected_code_roots", lambda: (code.parent,))
    windows.setattr(rsp, "code_write_exemptions", lambda: ())
    with pytest.raises(jr.KernelIsolationUnavailableError, match="required but unavailable"):
        _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])
    windows.setattr(rsp, "code_write_exemptions", lambda: (code,))
    assert _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])[1] == "--policy"


def test_a_root_holding_vool_state_is_refused(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    import core.runtime_state_protection as rsp

    (tmp_path / "ws").mkdir()
    windows.setattr(rsp, "protected_state_roots", lambda: ((tmp_path / "ws" / "data").resolve(),))
    with pytest.raises(jr.KernelIsolationUnavailableError, match="required but unavailable"):
        _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])


def test_an_unknown_protected_set_fails_closed(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    import core.runtime_state_protection as rsp

    def broken():
        raise RuntimeError("runtime paths unavailable")

    windows.setattr(rsp, "protected_state_roots", broken)
    with pytest.raises(jr.KernelIsolationUnavailableError):
        _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])


def test_a_staging_folder_inside_a_write_root_is_refused(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    (tmp_path / "ws").mkdir()
    windows.setattr(jr.tempfile, "mkdtemp", lambda prefix="": str(_mk(tmp_path / "ws" / f"{prefix}x")))
    with pytest.raises(jr.KernelIsolationUnavailableError):
        _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])
    assert not (tmp_path / "ws" / "vool-confine-x").exists(), "the refused staging folder is removed"


def _mk(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


# --- running ---------------------------------------------------------------------------------


def _staging_folders(tmp_root: Path) -> set[str]:
    return {p.name for p in tmp_root.iterdir() if p.name.startswith("vool-confine-")}


@pytest.fixture
def private_tmp(windows, tmp_path):
    folder = tmp_path / "systemp"
    folder.mkdir()
    windows.setattr(jr.tempfile, "tempdir", str(folder))
    return folder


def test_a_confined_job_returns_its_own_code_and_output(windows, tmp_path, private_tmp) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    result = _runner(tmp_path).run(["python", "-c", "import sys; print('hi'); sys.exit(7)"])
    assert result.returncode == 7 and result.stdout.strip() == "hi"
    assert _staging_folders(private_tmp) == set(), "policy and status are deleted after the job"
    assert not jr._WINDOWS_STAGED


def test_a_child_exiting_125_is_its_own_code_when_the_launcher_recorded_a_start(
    windows, tmp_path, private_tmp
) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    assert _runner(tmp_path).run(["python", "-c", "import sys; sys.exit(125)"]).returncode == 125


@pytest.mark.parametrize("mode", ["refuse", "refuse-started-false"])
def test_a_launcher_with_no_start_record_is_a_refusal_not_a_failed_command(
    windows, tmp_path, private_tmp, mode
) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", mode))
    with pytest.raises(jr.KernelIsolationUnavailableError, match="never ran"):
        _runner(tmp_path).run(["python", "-c", "print('should not run')"])
    assert _staging_folders(private_tmp) == set()


def test_a_status_record_that_contradicts_the_exit_code_is_refused(windows, tmp_path, private_tmp) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "lie"))
    with pytest.raises(jr.KernelIsolationUnavailableError, match="does not match"):
        _runner(tmp_path).run(["python", "-c", "import sys; sys.exit(3)"])


@pytest.mark.parametrize("mode", ["newrepo", "newrepo-status-only", "exit123"])
def test_a_new_repository_the_launcher_reports_refuses_the_result(windows, tmp_path, private_tmp, mode) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", mode))
    with pytest.raises(jr.NestedGitMetadataError, match="git metadata|123"):
        _runner(tmp_path).run(["python", "-c", "print('done')"])
    assert _staging_folders(private_tmp) == set()


def test_the_launcher_grants_once_then_the_run_form_verifies(windows, tmp_path, private_tmp) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "run")
    _use_launcher(windows, launcher)
    for _ in range(2):
        assert _runner(tmp_path).run(["python", "-c", "0"]).returncode == 0
    assert _forms(launcher) == ["grant"], "one grant per policy shape, verified by every run"
    _runner(tmp_path, allow_network_egress=True).run(["python", "-c", "0"])
    assert _forms(launcher) == ["grant"], "network trust does not change the ACLs"


def test_a_failed_grant_refuses_the_job_and_runs_nothing(windows, tmp_path, private_tmp) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "grant-fails")
    _use_launcher(windows, launcher)
    marker = tmp_path / "ws" / "ran"
    with pytest.raises(jr.KernelIsolationUnavailableError, match="grant"):
        _runner(tmp_path).run(["python", "-c", f"open({str(marker)!r}, 'w')"])
    assert not marker.exists()
    assert _staging_folders(private_tmp) == set() and not jr._WINDOWS_STAGED


def test_a_verify_miss_forgets_the_grant_so_the_next_job_grants_again(windows, tmp_path, private_tmp) -> None:
    refusing = _fake_launcher(tmp_path / "bin", "refuse-started-false")
    _use_launcher(windows, refusing)
    with pytest.raises(jr.KernelIsolationUnavailableError, match="acl verify failed"):
        _runner(tmp_path).run(["python", "-c", "0"])
    with pytest.raises(jr.KernelIsolationUnavailableError):
        _runner(tmp_path).run(["python", "-c", "0"])
    assert _forms(refusing) == ["grant", "grant"]


def test_secrets_and_state_are_denied_all_access_and_code_is_denied_writes(windows, tmp_path) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    import core.runtime_state_protection as rsp

    secret = _mk(tmp_path / "home" / ".ssh").resolve()
    state = _mk(tmp_path / "state" / "data").resolve()
    code = _mk(tmp_path / "code").resolve()
    windows.setattr(jr, "_sensitive_read_deny_roots", lambda: [secret, tmp_path / "home" / ".aws"])
    windows.setattr(rsp, "protected_state_roots", lambda: (state,))
    windows.setattr(rsp, "protected_code_roots", lambda: (code,))
    argv = _runner(tmp_path)._with_network_isolation(["python", "-c", "0"])
    policy = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    assert policy["deny_all_paths"] == [str(state), str(secret)], "missing folders are not named"
    assert policy["deny_write_paths"] == [str(code)]


def test_a_status_file_left_in_the_staging_folder_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: Path("/opt/vool/VoolConfine.exe"))
    monkeypatch.setattr(wc, "_check_path", lambda value, what: str(value))
    real_inside = wc._inside
    monkeypatch.setattr(wc, "_inside", lambda child, root: real_inside(_win(child), _win(root)))
    stage = _mk(tmp_path / "stage")
    (stage / "status.json").write_text("{}", encoding="utf-8")
    policy = wc.WindowsConfinementPolicy(
        write_roots=(str(tmp_path / "ws"),), temp_dir=str(tmp_path / "ws" / "t"), status_path=str(stage / "status.json")
    )
    program = str(Path(sys.executable).resolve())
    assert wc.confinement_prefix([program], policy, project_root=tmp_path, policy_path=stage / "p.json") is None
    assert not (stage / "p.json").exists()


def test_a_timed_out_job_is_124_and_still_cleans_up(windows, tmp_path, private_tmp) -> None:
    _use_launcher(windows, _fake_launcher(tmp_path / "bin", "run"))
    result = _runner(tmp_path, max_seconds=1).run(["python", "-c", "import time; time.sleep(4)"])
    assert result.returncode == 124 and "timed out" in result.stderr
    assert _staging_folders(private_tmp) == set()


def test_the_windows_child_environment_points_user_folders_into_the_sandbox(windows, tmp_path) -> None:
    windows.setenv("SYSTEMROOT", "C:\\Windows")
    windows.setenv("USERPROFILE", "C:\\Users\\operator")
    windows.setenv("APPDATA", "C:\\Users\\operator\\AppData\\Roaming")
    runner = _runner(tmp_path)
    env = runner._child_environment(tmp_path / "ws")
    home = tmp_path / "ws" / ".sandbox-home"
    assert env["SYSTEMROOT"] == "C:\\Windows"
    assert env["USERPROFILE"] == str(home)
    assert env["APPDATA"].startswith(str(home)) and env["LOCALAPPDATA"].startswith(str(home))


def test_posix_hosts_never_take_the_windows_branch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: pytest.fail("Windows branch reached"))
    runner = _runner(tmp_path)
    if not jr._is_windows_platform():
        assert runner._windows_confine_prefix(["python", "-c", "0"], (tmp_path / "ws",)) is None
        env = runner._child_environment(tmp_path / "ws")
        assert "USERPROFILE" not in env or env["USERPROFILE"] == os.environ.get("USERPROFILE")


# --- probe and confinement_prefix ------------------------------------------------------------


def test_the_probe_rejects_a_launcher_that_confines_nothing(windows, tmp_path) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "unconfined-probe")
    windows.setattr(wc, "launcher_path", lambda project_root: launcher)
    assert wc.probe(tmp_path) is False, "a write outside the workspace landed, so nothing is confined"
    assert _forms(launcher) == ["grant", "revoke"]


def test_the_probe_rejects_a_launcher_that_refuses(windows, tmp_path) -> None:
    launcher = _fake_launcher(tmp_path / "bin", "refuse")
    windows.setattr(wc, "launcher_path", lambda project_root: launcher)
    assert wc.probe(tmp_path) is False


@pytest.mark.parametrize("revoke_fails", [False, True])
def test_the_probe_accepts_only_a_launcher_whose_child_was_held_in(windows, tmp_path, revoke_fails) -> None:
    """A stand-in for a confining launcher: it performs the in-workspace write the child would,
    and reports the child's out-of-bounds write and loopback connect as refused (exit 0)."""
    stand_in = tmp_path / "bin" / "VoolConfine-confining"
    stand_in.parent.mkdir(parents=True)
    stand_in.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] in ('grant', 'revoke'):\n"
        "    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'forms.log'), 'a').write(args[0] + '\\n')\n"
        f"    sys.exit(1 if args[0] == 'revoke' and {revoke_fails!r} else 0)\n"
        "child = args[3:]\n"
        "open(os.path.join(child[4], 'probe-ok'), 'w').write('ok')\n"
        "status = os.path.join(os.path.dirname(args[1]), 'status.json')\n"
        f"json.dump({{'schema': {_STATUS!r}, 'child_started': True, 'timed_out': False, 'exit_code': 0}},"
        " open(status, 'w'))\n",
        encoding="utf-8",
    )
    stand_in.chmod(0o755)
    windows.setattr(wc, "launcher_path", lambda project_root: stand_in)
    assert wc.probe(tmp_path) is (not revoke_fails), "a probe root left granted is not a clean probe"
    assert _forms(stand_in) == ["grant", "revoke"], "the probe grants its own root and revokes it"


def test_the_unbuilt_launcher_still_probes_unavailable(tmp_path) -> None:
    assert wc.probe(tmp_path) is False


def test_confinement_prefix_never_writes_a_policy_inside_a_write_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: Path("C:\\vool\\broker\\confine\\VoolConfine.exe"))
    policy = wc.WindowsConfinementPolicy(
        write_roots=("C:\\work\\repo",),
        temp_dir="C:\\work\\repo\\.sandbox-tmp",
        status_path="C:\\stage\\status.json",
    )
    program = str(Path(sys.executable).resolve())
    inside = Path("C:\\work\\repo\\policy.json")
    assert wc.confinement_prefix([program], policy, project_root=tmp_path, policy_path=inside) is None
    assert not inside.exists()
    outside = tmp_path / "policy.json"
    prefix = wc.confinement_prefix([program, "-c", "0"], policy, project_root=tmp_path, policy_path=outside)
    assert prefix == ["C:\\vool\\broker\\confine\\VoolConfine.exe" if os.name == "nt" else
                      str(Path("C:\\vool\\broker\\confine\\VoolConfine.exe")),
                      "--policy", str(outside), "--", program, "-c", "0"]
    assert wc.WindowsConfinementPolicy.from_json(outside.read_text(encoding="utf-8")) == policy
    assert wc.confinement_prefix(["python"], policy, project_root=tmp_path, policy_path=outside) is None
