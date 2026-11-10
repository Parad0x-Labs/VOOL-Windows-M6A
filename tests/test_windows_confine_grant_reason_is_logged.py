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
            "VOOLCONFINE-REFUSED: grant failed (reparse_point): Root C:\\Users\\VOOLT\\OneDrive\\x is a reparse point.\n",
            "reparse_point",
            "Root C:\\Users\\VOOLT\\OneDrive\\x is a reparse point.",
        ),
        ("VOOLCONFINE-REFUSED: grant failed: The process cannot access the file.\n", "unspecified",
         "grant failed: The process cannot access the file."),
        ("", "no_reason", ""),
    ],
    ids=["nested", "reparse", "no-code", "silent"],
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


# --- a transient grant refusal is retried -----------------------------------------------------

#: A launcher whose grant form fails with REFUSAL for its first FAILS calls (every call when FAILS is
#: None), then succeeds. Each grant or revoke call appends its form name to calls.log; SLEEP makes a
#: form outlive the caller's timeout. The run form runs the child, as the fake above does.
_COUNTING_FAKE = r'''#!{python}
import json, os, subprocess, sys, time
FORM_REFUSAL = {refusal!r}
FAILS = {fails!r}
FAILING_FORM = {failing_form!r}
SLEEP = {sleep!r}
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
if args and args[0] in ("grant", "revoke"):
    log = os.path.join(here, "calls.log")
    with open(log, "a", encoding="utf-8") as f:
        f.write(args[0] + "\n")
    if SLEEP:
        time.sleep(SLEEP)
    calls = open(log, encoding="utf-8").read().split().count(args[0])
    if args[0] == FAILING_FORM and (FAILS is None or calls <= FAILS):
        sys.stderr.write(FORM_REFUSAL)
        sys.exit(125)
    sys.exit(0)
if args and args[0] == "scan":
    sys.exit(0)
status = os.path.join(os.path.dirname(args[1]), "status.json")
def record(**fields):
    with open(status, "w", encoding="utf-8") as f:
        json.dump(dict(schema={schema!r}, **fields), f)
record(child_started=True)
rc = subprocess.call(args[3:])
record(child_started=True, timed_out=False, exit_code=rc, new_repositories=[], repositories_scanned=True)
sys.exit(rc)
'''

_TRANSIENT = {
    "entry_vanished": "Cannot open 'x.cpython-312.pyc.2716' (NTSTATUS 0xc0000034).",
    "in_use": "Cannot open 'calc.py' (NTSTATUS 0xc0000043).",
    "root_open_failed": "Cannot open C:\\w: The process cannot access the file (Win32 error 32)",
}


def _counting_launcher(
    folder: Path, *, code: str, message: str, fails: int | None, form: str = "grant", sleep: float = 0
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "VoolConfine-counting"
    refusal = f"VOOLCONFINE-REFUSED: {form} failed ({code}): {message}\n" if code else ""
    path.write_text(
        _COUNTING_FAKE.format(
            python=sys.executable, schema=_STATUS, refusal=refusal, fails=fails, failing_form=form, sleep=sleep
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _calls(launcher: Path) -> list[str]:
    log = launcher.parent / "calls.log"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    """The retry waits, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(wc, "_sleep", slept.append)
    return slept


@pytest.mark.parametrize("code", sorted(_TRANSIENT))
def test_a_transient_grant_refusal_is_retried_and_the_job_runs(windows, tmp_path, caplog, waits, code) -> None:
    marks = _marks(tmp_path)
    launcher = _counting_launcher(tmp_path / "bin", code=code, message=_TRANSIENT[code], fails=1)
    _use(windows, launcher)
    proj = marks["proj"].resolve()
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    result = SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(["python", "calc.py"], cwd=proj)

    assert result.returncode == 0 and result.stdout.strip() == "5", (result.stdout, result.stderr)
    assert _calls(launcher) == ["grant", "grant"]
    assert waits == [0.5]
    assert _confine_warnings(caplog) == [
        f"VoolConfine grant failed: exit 125, reason {code}: {_TRANSIENT[code]} (attempt 1 of 3, retrying in 0.5 s)",
        "VoolConfine grant succeeded on attempt 2 of 3",
    ]


def test_a_grant_that_succeeds_on_the_third_attempt_waits_both_delays(windows, tmp_path, caplog, waits) -> None:
    launcher = _counting_launcher(tmp_path / "bin", code="in_use", message=_TRANSIENT["in_use"], fails=2)
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(launcher, "grant", policy) is True

    assert _calls(launcher) == ["grant"] * 3 and waits == [0.5, 1.5]
    assert _confine_warnings(caplog)[-1] == "VoolConfine grant succeeded on attempt 3 of 3"


def test_a_transient_grant_refusal_gives_up_after_three_attempts(windows, tmp_path, caplog, waits) -> None:
    """The captest's calc.py shape: a grant that keeps failing refuses the job with the same sentence."""
    marks = _marks(tmp_path)
    message = _TRANSIENT["entry_vanished"]
    launcher = _counting_launcher(tmp_path / "bin", code="entry_vanished", message=message, fails=None)
    _use(windows, launcher)
    proj = marks["proj"].resolve()
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    with pytest.raises(jr.KernelIsolationUnavailableError) as raised:
        SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(["python", "calc.py"], cwd=proj)

    assert str(raised.value) == _GRANT_SENTENCE
    assert _calls(launcher) == ["grant"] * 3 + ["revoke"], "then one best-effort revoke"
    assert waits == [0.5, 1.5]
    assert _confine_warnings(caplog) == [
        f"VoolConfine grant failed: exit 125, reason entry_vanished: {message} (attempt 1 of 3, retrying in 0.5 s)",
        f"VoolConfine grant failed: exit 125, reason entry_vanished: {message} (attempt 2 of 3, retrying in 1.5 s)",
        f"VoolConfine grant failed: exit 125, reason entry_vanished: {message} (attempt 3 of 3, giving up)",
    ]
    assert not jr._WINDOWS_STAGED


@pytest.mark.parametrize(
    ("code", "message", "logged"),
    [
        ("nested_write_roots", "Write root C:\\m\\p lies inside write root C:\\m.", "nested_write_roots"),
        ("repo_appeared", "Repository layouts appeared under an already granted write root", "repo_appeared"),
        ("access_denied", "Cannot change the DACL of C:\\w\\.git, which must be denied write.", "access_denied"),
        ("", "", "no_reason"),
    ],
    ids=["nested", "repo-appeared", "access-denied", "no-reason"],
)
def test_a_grant_refusal_that_is_not_transient_is_not_retried(
    windows, tmp_path, caplog, waits, code, message, logged
) -> None:
    launcher = _counting_launcher(tmp_path / "bin", code=code, message=message, fails=None)
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(launcher, "grant", policy) is False

    assert _calls(launcher) == ["grant"] and waits == []
    assert _confine_warnings(caplog) == [f"VoolConfine grant failed: exit 125, reason {logged}: {message}"]


def test_a_grant_that_times_out_is_not_retried(windows, tmp_path, caplog, waits) -> None:
    launcher = _counting_launcher(
        tmp_path / "bin", code="entry_vanished", message=_TRANSIENT["entry_vanished"], fails=None, sleep=5
    )
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(launcher, "grant", policy, timeout=0.5) is False

    assert _calls(launcher) == ["grant"] and waits == []
    assert _confine_warnings(caplog) == ["VoolConfine grant did not finish (TimeoutExpired), so it counts as failed"]


def test_a_revoke_is_never_retried_even_for_a_transient_reason(windows, tmp_path, caplog, waits) -> None:
    message = _TRANSIENT["entry_vanished"]
    launcher = _counting_launcher(tmp_path / "bin", code="entry_vanished", message=message, fails=None, form="revoke")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    assert wc.launcher_form(launcher, "revoke", policy) is False

    assert _calls(launcher) == ["revoke"] and waits == []
    assert _confine_warnings(caplog) == [f"VoolConfine revoke failed: exit 125, reason entry_vanished: {message}"]


# --- a grant that finally fails is revoked once -------------------------------------------------

#: Grant fails with GRANT_REFUSAL while a file named "fail" sits next to the script (and succeeds
#: without it); revoke exits REVOKE_EXIT with REVOKE_REFUSAL. Each grant or revoke call appends
#: "<form>:<policy exists>" to calls.log.
_REVOKING_FAKE = r'''#!{python}
import json, os, subprocess, sys
GRANT_REFUSAL = {grant_refusal!r}
REVOKE_REFUSAL = {revoke_refusal!r}
REVOKE_EXIT = {revoke_exit!r}
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
if args and args[0] in ("grant", "revoke"):
    with open(os.path.join(here, "calls.log"), "a", encoding="utf-8") as f:
        f.write(args[0] + ":" + str(os.path.isfile(args[2])) + "\n")
    if args[0] == "grant":
        if os.path.exists(os.path.join(here, "fail")):
            sys.stderr.write(GRANT_REFUSAL)
            sys.exit(125)
        sys.exit(0)
    sys.stderr.write(REVOKE_REFUSAL)
    sys.exit(REVOKE_EXIT)
if args and args[0] == "scan":
    sys.exit(0)
status = os.path.join(os.path.dirname(args[1]), "status.json")
def record(**fields):
    with open(status, "w", encoding="utf-8") as f:
        json.dump(dict(schema={schema!r}, **fields), f)
record(child_started=True)
rc = subprocess.call(args[3:])
record(child_started=True, timed_out=False, exit_code=rc, new_repositories=[], repositories_scanned=True)
sys.exit(rc)
'''


def _revoking_launcher(
    folder: Path, *, grant_code: str, revoke_exit: int = 0, revoke_refusal: str = "revoked: 2 updated\n",
    failing: bool = True,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "VoolConfine-revoking"
    path.write_text(
        _REVOKING_FAKE.format(
            python=sys.executable, schema=_STATUS,
            grant_refusal=f"VOOLCONFINE-REFUSED: grant failed ({grant_code}): Cannot open 'calc.py' (NTSTATUS 0xc0000043).\n",
            revoke_refusal=revoke_refusal, revoke_exit=revoke_exit,
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    if failing:
        (folder / "fail").write_text("", encoding="utf-8")
    return path


def _staging(tmp_path: Path) -> list[str]:
    return [p.name for p in (tmp_path / "systemp").iterdir() if p.name.startswith("vool-confine-")]


def test_a_grant_that_fails_three_times_is_revoked_once_and_the_job_refused(windows, tmp_path, caplog, waits) -> None:
    marks = _marks(tmp_path)
    launcher = _revoking_launcher(tmp_path / "bin", grant_code="in_use")
    _use(windows, launcher)
    proj = marks["proj"].resolve()
    marker = proj / "ran"
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    with pytest.raises(jr.KernelIsolationUnavailableError) as raised:
        SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(
            ["python", "-c", f"open({str(marker)!r}, 'w')"], cwd=proj
        )

    assert str(raised.value) == _GRANT_SENTENCE
    assert _calls(launcher) == ["grant:True"] * 3 + ["revoke:True"], "one revoke, while the policy still exists"
    assert waits == [0.5, 1.5]
    assert not marker.exists()
    assert _staging(tmp_path) == [] and not jr._WINDOWS_STAGED
    assert not any("revoke" in line for line in _confine_warnings(caplog)), "a revoke that succeeds logs nothing"


def test_a_revoke_after_a_failed_grant_that_fails_too_logs_its_reason(windows, tmp_path, caplog, waits) -> None:
    marks = _marks(tmp_path)
    launcher = _revoking_launcher(
        tmp_path / "bin", grant_code="nested_write_roots", revoke_exit=125,
        revoke_refusal="VOOLCONFINE-REFUSED: revoke failed (acl_write_failed): Cannot write the DACL of C:\\w (Win32 error 5)\n",
    )
    _use(windows, launcher)
    proj = marks["proj"].resolve()
    caplog.set_level(logging.WARNING, logger=wc.__name__)

    with pytest.raises(jr.KernelIsolationUnavailableError) as raised:
        SandboxRunner(ExecutionGate(), str(proj)).job_runner.run(["python", "calc.py"], cwd=proj)

    assert str(raised.value) == _GRANT_SENTENCE
    assert _calls(launcher) == ["grant:True", "revoke:True"], "no retry of either form"
    assert waits == []
    assert _confine_warnings(caplog)[-1] == (
        "VoolConfine revoke failed: exit 125, reason acl_write_failed: Cannot write the DACL of C:\\w (Win32 error 5)"
    )
    assert _staging(tmp_path) == [] and not jr._WINDOWS_STAGED


def test_a_failed_grant_forgets_remembered_grants_so_the_next_job_grants_again(windows, tmp_path, waits) -> None:
    """The revoke strips the job folder's capability ACE that an earlier, remembered grant shares."""
    marks = _marks(tmp_path)
    launcher = _revoking_launcher(tmp_path / "bin", grant_code="repo_appeared", failing=False)
    _use(windows, launcher)
    proj = marks["proj"].resolve()
    runner = SandboxRunner(ExecutionGate(), str(proj)).job_runner

    assert runner.run(["python", "calc.py"], cwd=proj).returncode == 0
    (launcher.parent / "fail").write_text("", encoding="utf-8")
    with pytest.raises(jr.KernelIsolationUnavailableError):
        # Another policy shape that names the same job folder: it shares that folder's capability.
        jr.JobRunner(
            ExecutionPolicy(workspace_root=proj, writable_roots=(proj, marks["b"].resolve()))
        ).run(["python", "calc.py"], cwd=proj)
    (launcher.parent / "fail").unlink()
    assert runner.run(["python", "calc.py"], cwd=proj).returncode == 0

    assert _calls(launcher) == ["grant:True", "grant:True", "revoke:True", "grant:True"]
