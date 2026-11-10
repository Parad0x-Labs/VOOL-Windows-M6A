"""Windows: a failed grant (or revoke) names the launcher's reason in VOOL's log.

Captest (clean Windows 11 VM, marks "...\\VOOL Test\\Allowed Ä folder", "Marked A", "Marked B" and
"OneDrive\\Documents\\cap test\\Allowed Ä folder"): "run calc.py in ...\\Allowed Ä folder\\proj" was
allowed and then refused with "Windows confinement could not grant this job's folders (the
launcher's grant step failed)". A second job in the same folder eight minutes later ran (exit 0).
`_run_launcher` dropped the launcher's stderr, so nothing said which of the grant's refusals
(`AclException` codes in windows/broker/src/VoolConfine.Core/Acl.cs) fired.

These rows pin:
- a sandbox.run_command job in a subfolder of a mark hands the launcher ONE write root (the job's
  folder), so the grant's `nested_write_roots` refusal cannot be what fired;
- sibling write roots both stay;
- a failed grant or revoke logs the launcher's reason code and message at WARNING, and only its own
  refusal line, while the user-facing sentence stays as it was;
- off Windows none of this runs.

Windows is simulated the way tests/test_windows_confinement_runner.py does it: the job runner's
`_is_windows_platform` seam, a fake launcher script that speaks the launcher's command line, and a
`_check_path` that accepts POSIX absolute paths. Every other rule is the real one.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from core.execution_gate import ExecutionGate
from sandbox import job_runner as jr
from sandbox import windows_confinement as wc
from sandbox.resource_limits import ExecutionPolicy
from sandbox.sandbox_runner import SandboxRunner

_STATUS = wc.STATUS_SCHEMA

#: GRANT_STDERR is what the fake's grant form writes to stderr; GRANT_EXIT its exit code. Every form
#: appends its name and the policy's write roots to forms.jsonl next to the script.
_FAKE = r'''#!{python}
import json, os, subprocess, sys
GRANT_STDERR = {grant_stderr!r}
GRANT_EXIT = {grant_exit!r}
REVOKE_STDERR = {revoke_stderr!r}
REVOKE_EXIT = {revoke_exit!r}
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
def log(form, policy_path):
    policy = json.load(open(policy_path, encoding="utf-8"))
    with open(os.path.join(here, "forms.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(form=form, write_roots=policy["write_roots"], job_read_roots=policy["job_read_roots"],
                                deny_write_paths=policy["deny_write_paths"])) + "\n")
if args and args[0] == "grant":
    log("grant", args[2])
    sys.stdout.write("stdout is never logged\n")
    sys.stderr.write(GRANT_STDERR)
    sys.exit(GRANT_EXIT)
if args and args[0] == "revoke":
    log("revoke", args[2])
    sys.stderr.write(REVOKE_STDERR)
    sys.exit(REVOKE_EXIT)
if args and args[0] == "scan":
    sys.exit(0)
policy = json.load(open(args[1], encoding="utf-8"))
status = os.path.join(os.path.dirname(args[1]), "status.json")
def record(**fields):
    with open(status, "w", encoding="utf-8") as f:
        json.dump(dict(schema={schema!r}, **fields), f)
record(child_started=True)
rc = subprocess.call(args[3:])
record(child_started=True, timed_out=False, exit_code=rc, new_repositories=[], repositories_scanned=True)
sys.exit(rc)
'''


def _fake_launcher(
    folder: Path,
    *,
    grant_stderr: str = "granted: 3 updated, 0 unchanged, 0 repositories denied write\n",
    grant_exit: int = 0,
    revoke_stderr: str = "revoked: 3 updated\n",
    revoke_exit: int = 0,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "VoolConfine-fake"
    path.write_text(
        _FAKE.format(
            python=sys.executable, schema=_STATUS, grant_stderr=grant_stderr, grant_exit=grant_exit,
            revoke_stderr=revoke_stderr, revoke_exit=revoke_exit,
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _forms(launcher: Path) -> list[dict]:
    log = launcher.parent / "forms.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _win(path: str) -> str:
    return "C:" + str(path).replace("/", "\\") if str(path).startswith("/") else str(path)


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
    folder = tmp_path / "systemp"
    folder.mkdir()
    monkeypatch.setattr(jr.tempfile, "tempdir", str(folder))
    jr.reset_isolation_backend_probe_cache()
    yield monkeypatch
    jr.reset_isolation_backend_probe_cache()
    jr._forget_staged_at_exit()


def _use(monkeypatch, launcher: Path) -> None:
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: launcher)
    monkeypatch.setattr(wc, "probe", lambda project_root: True)


def _marks(tmp_path: Path) -> dict[str, Path]:
    """The captest marks, under tmp_path, with the job's folder inside the first."""
    test = tmp_path / "Users" / "VOOLT" / "VOOL Test"
    marks = {
        "allowed": test / "Allowed Ä folder",
        "a": test / "Marked A",
        "b": test / "Marked B",
        "onedrive": tmp_path / "Users" / "VOOLT" / "OneDrive" / "Documents" / "cap test" / "Allowed Ä folder",
    }
    for folder in marks.values():
        folder.mkdir(parents=True)
    marks["proj"] = marks["allowed"] / "proj"
    marks["proj"].mkdir()
    (marks["proj"] / "calc.py").write_text("print(2 + 3)\n", encoding="utf-8")
    return marks


def _confine_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == wc.__name__ and record.levelno == logging.WARNING
    ]


# --- what the grant is handed ------------------------------------------------------------------


def test_a_run_command_job_in_a_subfolder_of_a_mark_grants_one_write_root(windows, tmp_path) -> None:
    """The VM's calc.py job: the folder is `...\\Allowed Ä folder\\proj`, inside a mark. The sandbox
    runner hands the job runner that folder alone, so the grant gets one write root and the grant's
    nested-root refusal can't fire, whichever folders are marked."""
    marks = _marks(tmp_path)
    launcher = _fake_launcher(tmp_path / "bin")
    _use(windows, launcher)
    proj = marks["proj"].resolve()

    result = SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(["python", "calc.py"], cwd=proj)

    assert result.returncode == 0 and result.stdout.strip() == "5", (result.stdout, result.stderr)
    grants = [form for form in _forms(launcher) if form["form"] == "grant"]
    assert [form["write_roots"] for form in grants] == [[str(proj)]], grants
    assert str(proj) in grants[0]["job_read_roots"], "the job's folder keeps its read grant"
    assert str(proj) not in grants[0]["deny_write_paths"], "the job's folder stays writable"
    for name in ("allowed", "a", "b", "onedrive"):
        assert str(marks[name].resolve()) not in grants[0]["write_roots"], name


def test_sibling_write_roots_both_stay(windows, tmp_path) -> None:
    marks = _marks(tmp_path)
    launcher = _fake_launcher(tmp_path / "bin")
    _use(windows, launcher)
    a, b = marks["a"].resolve(), marks["b"].resolve()
    runner = jr.JobRunner(ExecutionPolicy(workspace_root=a, writable_roots=(a, b)))

    assert runner.run(["python", "-c", "print('ok')"], cwd=a).returncode == 0

    grants = [form for form in _forms(launcher) if form["form"] == "grant"]
    assert [form["write_roots"] for form in grants] == [[str(a), str(b)]], grants


# --- a failed grant names its reason --------------------------------------------------------


_GRANT_SENTENCE = (
    "Windows confinement could not grant this job's folders (the launcher's grant step failed), "
    "so the job was not run."
)


@pytest.mark.parametrize(
    ("stderr", "code", "message"),
    [
        (
            "VOOLCONFINE-REFUSED: grant failed (nested_write_roots): Write root C:\\m\\proj lies inside write root C:\\m.\n",
            "nested_write_roots",
            "Write root C:\\m\\proj lies inside write root C:\\m.",
        ),
        (
            "VOOLCONFINE-REFUSED: grant failed (entry_vanished): Cannot open 'x.cpython-312.pyc.2716' (NTSTATUS 0xc0000034).\n",
            "entry_vanished",
            "Cannot open 'x.cpython-312.pyc.2716' (NTSTATUS 0xc0000034).",
        ),
        (
            "VOOLCONFINE-REFUSED: grant failed (reparse_point): Root C:\\Users\\VOOLT\\OneDrive\\x is a reparse point.\n",
            "reparse_point",
            "Root C:\\Users\\VOOLT\\OneDrive\\x is a reparse point.",
        ),
        ("VOOLCONFINE-REFUSED: grant failed: The process cannot access the file.\n", "unspecified",
         "grant failed: The process cannot access the file."),
        ("", "no_reason", ""),
    ],
    ids=["nested", "vanished", "reparse", "no-code", "silent"],
)
def test_a_failed_grant_logs_the_launchers_reason_and_keeps_the_sentence(
    windows, tmp_path, caplog, stderr, code, message
) -> None:
    marks = _marks(tmp_path)
    launcher = _fake_launcher(tmp_path / "bin", grant_stderr=stderr, grant_exit=125)
    _use(windows, launcher)
    proj = marks["proj"].resolve()
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    with pytest.raises(jr.KernelIsolationUnavailableError) as raised:
        SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(["python", "calc.py"], cwd=proj)

    assert str(raised.value) == _GRANT_SENTENCE
    assert _confine_warnings(caplog) == [f"VoolConfine grant failed: exit 125, reason {code}: {message}"]


def test_the_log_keeps_only_the_launchers_refusal_line(windows, tmp_path, caplog) -> None:
    """Other stderr lines and the form's stdout are not logged; the refusal line is capped."""
    long_tail = "y" * 5000
    stderr = (
        "some other line the log must not carry\n"
        f"VOOLCONFINE-REFUSED: grant failed (repo_appeared): Repository layouts appeared {long_tail}\n"
    )
    launcher = _fake_launcher(tmp_path / "bin", grant_stderr=stderr, grant_exit=125)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"write_roots": [], "job_read_roots": [], "deny_write_paths": []}), encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(launcher, "grant", policy) is False

    [warning] = _confine_warnings(caplog)
    assert "reason repo_appeared: Repository layouts appeared" in warning
    assert "must not carry" not in warning and "stdout is never logged" not in warning
    assert len(warning) < 700, len(warning)


def test_a_failed_revoke_logs_its_reason(windows, tmp_path, caplog) -> None:
    launcher = _fake_launcher(
        tmp_path / "bin",
        revoke_stderr="VOOLCONFINE-REFUSED: revoke failed (acl_write_failed): Cannot write the DACL of C:\\w (Win32 error 5)\n",
        revoke_exit=125,
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"write_roots": [], "job_read_roots": [], "deny_write_paths": []}), encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(launcher, "revoke", policy) is False

    assert _confine_warnings(caplog) == [
        "VoolConfine revoke failed: exit 125, reason acl_write_failed: Cannot write the DACL of C:\\w (Win32 error 5)"
    ]


def test_a_grant_that_succeeds_logs_nothing(windows, tmp_path, caplog) -> None:
    marks = _marks(tmp_path)
    _use(windows, _fake_launcher(tmp_path / "bin"))
    proj = marks["proj"].resolve()
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(["python", "calc.py"], cwd=proj).returncode == 0

    assert _confine_warnings(caplog) == []


def test_a_launcher_that_cannot_start_logs_the_failure_without_its_text(windows, tmp_path, caplog) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(tmp_path / "no-such-launcher", "grant", policy) is False

    [warning] = _confine_warnings(caplog)
    assert warning.startswith("VoolConfine grant did not finish (") and "no-such-launcher" not in warning


# --- off Windows ------------------------------------------------------------------------------


def test_off_windows_the_confinement_branch_never_runs(tmp_path, monkeypatch, caplog) -> None:
    """With the seam off, the job runner never builds a Windows policy or runs a launcher form."""
    monkeypatch.setattr(jr, "_is_windows_platform", lambda: False)
    marks = _marks(tmp_path)
    launcher = _fake_launcher(tmp_path / "bin", grant_stderr="VOOLCONFINE-REFUSED: grant failed (x): y\n", grant_exit=125)
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: launcher)
    monkeypatch.setattr(wc, "probe", lambda project_root: True)
    caplog.set_level(logging.WARNING, logger=wc.__name__)
    proj = marks["proj"].resolve()
    runner = SandboxRunner(ExecutionGate(), str(proj)).job_runner

    assert runner._windows_confine_prefix(["python", "calc.py"], (proj,)) is None
    assert runner.policy.writable_roots == (proj,)
    assert _forms(launcher) == [] and _confine_warnings(caplog) == []
