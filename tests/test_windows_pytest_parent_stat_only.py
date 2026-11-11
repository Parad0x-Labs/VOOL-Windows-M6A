"""Windows: a confined pytest job may stat the folder above its own, and nothing more.

`tests/test_windows_pytest_stays_in_run_folder.py` shows why: pytest 9.1.1 stats the parent of the
folder it collects on every run, and the job held no right there. The fix is `stat_only_paths` in
the launcher policy (`docs/WINDOWS_CONFINEMENT.md`): one explicit allow of FILE_READ_ATTRIBUTES |
SYNCHRONIZE (0x00100080) with no inheritance flags, on that folder only, for that folder's own
stat capability, put in place by the run form before the child starts and taken off once its tree
is gone. The kernel side is proven on Windows by
`windows/broker/tests/VoolConfine.Tests/StatOnlyTests.cs`. These rows prove the rest on every host:

- the job runner names exactly the run folder's parent, one level, for a pytest job only;
- it names nothing a root already reaches, nothing in VOOL's state or a credential folder, and no
  volume root;
- both policy twins (Python and C#) take the key with the same rules, and the C# side writes the
  ACE with that mask and no flags and revokes it in the run form's `finally`;
- with exactly the stat the policy names, real pytest passes in the hardware's layout and a
  conftest.py in the parent is never imported.

The job runner is driven through `JobRunner.run` with Windows simulated (`_is_windows_platform`)
and a launcher that never starts: the grant form and the run are recorded, not executed, so no row
depends on the host.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from sandbox import job_runner as jr
from sandbox import windows_confinement as wc
from sandbox.container_adapter import ExecutionResult
from sandbox.resource_limits import ExecutionPolicy
from tests.test_windows_pytest_stays_in_run_folder import (
    OWN_CONFTEST_MARK,
    PARENT_CONFTEST_MARK,
    _run_job,
    _tree,
    _vool_pytest_args,
)

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "windows" / "broker" / "src" / "VoolConfine.Core"


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Windows for the job runner, a launcher that is never started, and the policy paths of this host."""
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
    launcher = tmp_path / "bin" / "VoolConfine.exe"
    monkeypatch.setattr(wc, "launcher_path", lambda project_root: launcher)
    monkeypatch.setattr(wc, "probe", lambda project_root: True)
    policies = _Recorded()
    monkeypatch.setattr(wc, "launcher_form", lambda exe, form, policy_path, **kw: policies.forms.append(form) or True)

    def run_confined(self, argv, cwd_path, cancel_event, stage, grant_key, allowed_roots):
        policies.append(json.loads(Path(argv[2]).read_text(encoding="utf-8")))
        return ExecutionResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(jr.JobRunner, "_run_windows_confined", run_confined)
    jr.reset_isolation_backend_probe_cache()
    yield policies
    jr.reset_isolation_backend_probe_cache()
    jr._forget_staged_at_exit()


class _Recorded(list):
    """The policies the run form was handed, in order; `forms` the unconfined forms that ran."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[str] = []


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """`<tmp>/home/VOOL HW Test/proj`: the mark root and its subfolder, as on the hardware."""
    mark_root = tmp_path / "home" / "VOOL HW Test"
    proj = mark_root / "proj"
    proj.mkdir(parents=True)
    return mark_root.resolve(), proj.resolve()


def _policy_for(policies: list[dict], workspace: Path, cwd: Path, argv: list[str]) -> dict:
    runner = jr.JobRunner(ExecutionPolicy(workspace_root=workspace, writable_roots=(workspace,)))
    runner.run(argv, cwd=cwd)
    assert len(policies) == 1, policies
    return policies.pop()


# --- the job runner ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["pytest", "-q"], id="pytest"),
        pytest.param(["python", "-m", "pytest"], id="python-m-pytest"),
        pytest.param([sys.executable, "-m", "pytest", "-q"], id="interpreter-m-pytest"),
    ],
)
def test_a_pytest_job_may_stat_exactly_the_folder_above_its_own(windows, tmp_path, argv):
    mark_root, proj = _layout(tmp_path)
    policy = _policy_for(windows, proj, proj, argv)
    assert len(policy["stat_only_paths"]) == 1, policy
    assert _same(policy["stat_only_paths"][0], mark_root)
    # One level: never the grandparent, never anything above.
    assert not any(_same(p, mark_root.parent) for p in policy["stat_only_paths"])


def test_the_stat_only_folder_is_in_no_root_and_the_policy_round_trips(windows, tmp_path):
    mark_root, proj = _layout(tmp_path)
    policy = _policy_for(windows, proj, proj, ["pytest"])
    stat = policy["stat_only_paths"][0]
    for key in ("write_roots", "read_roots", "job_read_roots"):
        assert not any(_same(stat, root) for root in policy.get(key, [])), key
    parsed = wc.WindowsConfinementPolicy.from_json(json.dumps(policy))
    assert parsed.stat_only_paths == (stat,)


def test_the_folder_above_a_mark_root_is_named_too_one_level(windows, tmp_path):
    """Run in the mark root itself: its parent sits outside the marks and still gets the stat only."""
    mark_root, _ = _layout(tmp_path)
    policy = _policy_for(windows, mark_root, mark_root, ["pytest"])
    assert len(policy["stat_only_paths"]) == 1 and _same(policy["stat_only_paths"][0], mark_root.parent)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["python", "-c", "print(1)"], id="python-c"),
        pytest.param(["python", "calc.py"], id="script"),
        pytest.param(["python", "-m", "unittest"], id="unittest"),
        pytest.param(["python", "-m", "compileall", "-q", "."], id="compileall"),
    ],
)
def test_no_other_job_gets_a_stat_only_folder(windows, tmp_path, argv):
    _, proj = _layout(tmp_path)
    (proj / "calc.py").write_text("print(1)\n", encoding="utf-8")
    policy = _policy_for(windows, proj, proj, argv)
    assert "stat_only_paths" not in policy, policy


def test_a_parent_a_root_already_reaches_is_not_named(windows, tmp_path):
    """Workspace = the mark root, job folder = its subfolder: the parent is the workspace itself."""
    mark_root, proj = _layout(tmp_path)
    policy = _policy_for(windows, mark_root, proj, ["pytest"])
    assert "stat_only_paths" not in policy, policy


def test_a_parent_inside_vools_state_or_a_credential_folder_is_not_named(windows, tmp_path, monkeypatch):
    mark_root, proj = _layout(tmp_path)
    monkeypatch.setattr(jr, "_sensitive_read_deny_roots", lambda: [mark_root.parent])
    policy = _policy_for(windows, proj, proj, ["pytest"])
    assert "stat_only_paths" not in policy, policy


def test_the_stat_only_folder_does_not_change_the_grant_key(windows, tmp_path):
    """The grant form runs once per set of roots; the stat-only ACE belongs to the run form alone."""
    _, proj = _layout(tmp_path)
    runner = jr.JobRunner(ExecutionPolicy(workspace_root=proj, writable_roots=(proj,)))
    runner.run(["python", "-c", "0"], cwd=proj)
    runner.run(["pytest"], cwd=proj)
    runner.run(["pytest"], cwd=proj)
    assert len(windows) == 3
    assert "stat_only_paths" not in windows[0] and "stat_only_paths" in windows[1] and "stat_only_paths" in windows[2]
    assert windows.forms == ["grant"], "one grant form for the roots; no revoke form between runs"


# --- the policy, both twins -------------------------------------------------------------------


def _policy(**extra) -> wc.WindowsConfinementPolicy:
    return wc.WindowsConfinementPolicy(
        write_roots=("C:\\u\\mark\\proj",),
        temp_dir="C:\\u\\mark\\proj\\.t",
        status_path="C:\\tmp\\vc\\status.json",
        **extra,
    )


def test_the_python_policy_takes_a_stat_only_folder_outside_its_roots():
    policy = _policy(stat_only_paths=("C:\\u\\mark",))
    data = json.loads(policy.to_json())
    assert data["stat_only_paths"] == ["C:\\u\\mark"]
    assert wc.WindowsConfinementPolicy.from_json(policy.to_json()).stat_only_paths == ("C:\\u\\mark",)
    assert "stat_only_paths" not in json.loads(_policy().to_json()), "no key when there is none"


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"stat_only_paths": ("C:\\u\\mark\\proj",)}, id="the-write-root"),
        pytest.param({"stat_only_paths": ("C:\\u\\mark\\proj\\sub",)}, id="inside-the-write-root"),
        pytest.param({"stat_only_paths": ("C:\\",)}, id="volume-root"),
        pytest.param({"stat_only_paths": ("C:\\vool\\state",), "deny_all_paths": ("C:\\vool",)}, id="inside-a-denied-path"),
        pytest.param({"stat_only_paths": ("C:\\u\\..\\x",)}, id="unresolved"),
        pytest.param({"stat_only_paths": ("C:\\py",), "read_roots": ("C:\\py",)}, id="a-read-root"),
    ],
)
def test_the_python_policy_refuses_a_stat_only_folder_a_root_reaches_or_a_volume_root(extra):
    with pytest.raises(wc.PolicyError):
        _policy(**extra).validate()


def _cs(name: str) -> str:
    return (CORE / name).read_text(encoding="utf-8")


def test_both_twins_know_the_same_policy_keys():
    block = re.search(r"static readonly HashSet<string> Keys =\s*\[(.*?)\];", _cs("Policy.cs"), re.S)
    assert block, "Policy.Keys not found"
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == set(wc._KEYS)
    assert "stat_only_paths" in wc._KEYS


def test_the_ace_is_read_attributes_and_synchronize_with_no_inheritance():
    rules = _cs("AclRules.cs")
    assert re.search(r"public const int StatOnlyMask = 0x00100080;", rules)
    want = re.search(r"Want StatOnlyAllow\(SecurityIdentifier sid\) =>\s*new\(([^;]*)\);", rules)
    assert want, "StatOnlyAllow not found"
    assert [part.strip() for part in want.group(1).split(",")] == [
        "sid", "AceQualifier.AccessAllowed", "StatOnlyMask", "AceFlags.None", "Placement.ExplicitAllow",
    ]
    exact = re.search(r"bool HasExactStatOnly\(.*?\n    \}", rules, re.S)
    assert exact and "ace.AceFlags == AceFlags.None" in exact.group(0) and "found == 1" in exact.group(0)
    assert 0x00100080 == 0x80 | 0x100000  # FILE_READ_ATTRIBUTES | SYNCHRONIZE, nothing else


def test_the_ace_goes_on_the_named_folder_only_and_each_job_its_own_capability():
    acl = _cs("Acl.cs")
    grant = re.search(r"public static AclReport GrantStatOnly\(.*?\n    \}", acl, re.S)
    assert grant, "GrantStatOnly not found"
    body = grant.group(0)
    # One handle on the folder itself; no walk, no child opened.
    assert "AclNative.OpenRoot(path, writeDac: true)" in body
    assert "WalkGrant" not in body and "OpenChild" not in body and "AclNative.List" not in body
    assert "sids.StatCapabilityFor(policyPath)" in body
    assert '"vool.stat." + RootHash(path)' in _cs("Identity.cs")
    assert "policy.StatOnlyPaths.Select(Identity.StatCapabilityName)" in _cs("Sids.cs")


def test_the_run_form_revokes_the_ace_after_the_job_and_the_revoke_form_does_too():
    launcher = _cs("Launcher.cs")
    run = re.search(r"static int RunConfined\(.*?\n    \}\n", launcher, re.S)
    assert run, "RunConfined not found"
    body = run.group(0)
    grant_at = body.index("checks.GrantStatOnly(policy, packageSid);")
    child_at = body.index("return RunChild(")
    finally_at = body.index("\n        finally\n")
    revoke_at = body.index("checks.RevokeStatOnly(policy, packageSid);")
    assert grant_at < child_at < finally_at < revoke_at
    # The child's job object lives inside RunChild, so it is closed before the finally runs.
    assert "ConfinementJob.Create" not in body and "ConfinementJob.Create" in launcher
    checks = _cs("AclChecks.cs")
    assert "Acl.RevokeStatOnly(policy, For(policy, packageSid))" in checks
    assert "Acl.VerifyStatOnly(policy, For(policy, packageSid))" in checks
    revoke = re.search(r"public static AclReport Revoke\(.*?\n    \}", _cs("Acl.cs"), re.S)
    assert revoke and "RevokeStatOnly(policy, sids, counts);" in revoke.group(0)


# --- real pytest, with exactly the stat the policy names --------------------------------------


def test_with_the_policys_stat_pytest_passes_and_a_parent_conftest_never_loads(windows, tmp_path):
    base = tmp_path / "hw"
    proj = _tree(base, parent_conftest=True, own_conftest=True).resolve()
    policy = _policy_for(windows, proj, proj, ["python", "-m", "pytest"])
    stat_only = tuple(Path(p) for p in policy["stat_only_paths"])
    assert len(stat_only) == 1 and _same(stat_only[0], proj.parent)
    code, output, denied = _run_job(tmp_path, proj, [*_vool_pytest_args(), "-s"], stat_only=stat_only)
    assert code == 0, output
    assert "1 passed" in output, output
    assert OWN_CONFTEST_MARK in output, output
    assert PARENT_CONFTEST_MARK not in output, output
    assert denied == [], denied
