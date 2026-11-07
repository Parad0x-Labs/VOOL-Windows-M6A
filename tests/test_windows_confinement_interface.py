"""The Windows confinement interface (docs/WINDOWS_CONFINEMENT.md): schema rules, and that the
unbuilt backend can only ever say "unavailable", so a confined job is refused, never run bare."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox import windows_confinement as wc

GOOD = dict(
    write_roots=("C:\\work\\repo",),
    temp_dir="C:\\work\\repo\\.vool-tmp",
    status_path="C:\\Users\\me\\AppData\\Local\\Temp\\vc-1\\status.json",
)


def test_a_valid_policy_round_trips_through_its_json() -> None:
    policy = wc.WindowsConfinementPolicy(**GOOD, read_roots=("C:\\Python312",), network=False)
    data = json.loads(policy.to_json())
    assert data["schema"] == wc.SCHEMA
    assert data["deny_write_git"] is True and data["network"] is False
    assert wc.WindowsConfinementPolicy.from_json(policy.to_json()) == policy


@pytest.mark.parametrize(
    "change",
    [
        {"write_roots": ()},
        {"temp_dir": "D:\\elsewhere"},
        {"status_path": "C:\\work\\repo\\status.json"},
        {"write_roots": ("\\\\server\\share",), "temp_dir": "\\\\server\\share\\t"},
        {"write_roots": ("C:\\w\\..\\x",), "temp_dir": "C:\\w\\..\\x\\t"},
        {"write_roots": ("relative",), "temp_dir": "relative\\t"},
        {"max_seconds": 0},
        {"memory_mb": -1},
        {"max_processes": True},
        {"network": "yes"},
    ],
)
def test_a_policy_that_breaks_a_rule_is_refused(change) -> None:
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy(**{**GOOD, **change}).validate()


def test_unknown_keys_and_other_schemas_are_refused() -> None:
    data = json.loads(wc.WindowsConfinementPolicy(**GOOD).to_json())
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy.from_json(json.dumps({**data, "surprise": 1}))
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy.from_json(json.dumps({**data, "schema": "vool.windows_confinement.v2"}))
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy.from_json("not json")


def test_the_unbuilt_backend_is_unavailable_so_jobs_are_refused(tmp_path: Path) -> None:
    policy = wc.WindowsConfinementPolicy(**GOOD)
    assert wc.launcher_path(tmp_path) is None
    assert wc.probe(tmp_path) is False
    assert (
        wc.confinement_prefix(["python", "-c", "0"], policy, project_root=tmp_path, policy_path=tmp_path / "p.json")
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not json",
        '{"child_started": true}',
        '{"schema": "vool.windows_confinement.status.v1", "child_started": "yes"}',
        '{"schema": "vool.windows_confinement.status.v1", "child_started": true, "exit_code": "0"}',
    ],
)
def test_a_missing_or_malformed_status_file_is_no_proof_the_child_started(tmp_path: Path, body) -> None:
    path = tmp_path / "status.json"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    assert wc.read_status(path) is None


def test_a_well_formed_status_file_is_read_back(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps({"schema": wc.STATUS_SCHEMA, "child_started": True, "timed_out": False, "exit_code": 125}),
        encoding="utf-8",
    )
    assert wc.read_status(path) == wc.LaunchStatus(True, False, 125)


def test_each_write_root_gets_its_own_capability_name() -> None:
    a = wc.root_capability_name("C:\\work\\a")
    # The same vector is pinned in windows/broker/tests/VoolConfine.Tests/IdentityTests.cs.
    assert a == "vool.root.b144b923dd859cdb6402070dce0592f6319ce623a8e197cd4a45fb96585d7e40"
    assert a == wc.root_capability_name("c:/work/a/")
    assert a != wc.root_capability_name("C:\\work\\b")
    assert wc.root_capability_name("C:\\straße") != wc.root_capability_name("C:\\STRASSE")
    assert a.startswith("vool.root.") and len(a) == len("vool.root.") + 64


def test_a_status_limit_outside_the_documented_set_is_no_proof(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"schema": wc.STATUS_SCHEMA, "child_started": True, "limit": "cpu"}), encoding="utf-8")
    assert wc.read_status(path) is None


def test_a_new_repository_in_the_status_is_carried_so_the_runner_can_refuse(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    body = {"schema": wc.STATUS_SCHEMA, "child_started": True, "exit_code": 0, "new_repositories": ["C:\\w\\x\\.git"]}
    path.write_text(json.dumps(body), encoding="utf-8")
    assert wc.read_status(path).new_repositories == ("C:\\w\\x\\.git",)
    path.write_text(json.dumps({**body, "new_repositories": "C:\\w\\x\\.git"}), encoding="utf-8")
    assert wc.read_status(path) is None


def test_deny_all_paths_round_trip_and_are_checked() -> None:
    policy = wc.WindowsConfinementPolicy(**GOOD, deny_all_paths=("C:\\Users\\me\\.ssh",))
    assert wc.WindowsConfinementPolicy.from_json(policy.to_json()).deny_all_paths == ("C:\\Users\\me\\.ssh",)
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy(**GOOD, deny_all_paths=("relative",)).validate()


#: Spellings Win32 rewrites before it opens a path (📝 chunk-0 review). Each one could make a status
#: file that is lexically outside `C:\w` land inside it, so each is refused. The C# twin is
#: PolicyTests.A_path_spelling_Win32_would_rewrite_is_refused, with the same list.
NON_CANONICAL = [
    "C:\\w.\\status.json",
    "C:\\w \\status.json",
    "C:\\W~1\\status.json",
    "C:\\w\\.\\status.json",
    "C:\\w\\\\status.json",
    "C:\\w\\",
    "C:/w/status.json",
    "C:\\w/status.json",
    "C:w\\status.json",
    "C:\\w\\status.json:stream",
    "C:\\w\\NUL",
    "C:\\w\\con.txt",
    "C:\\w\\COM1 .log",
    "C:\\w\\a?b",
    "C:\\w\\a\tb",
    "\\\\?\\C:\\w\\status.json",
    "ß:\\w",
]


@pytest.mark.parametrize("spelling", NON_CANONICAL)
def test_a_path_spelling_win32_would_rewrite_is_refused(spelling: str) -> None:
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy(**{**GOOD, "status_path": spelling}).validate()
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy(**{**GOOD, "read_roots": (spelling,)}).validate()


@pytest.mark.parametrize(
    "spelling", ["C:\\", "C:\\Users\\José\\vc\\status.json", "d:\\a b\\c.d\\status.json", "C:\\w\\.vool-tmp"]
)
def test_canonical_spellings_are_accepted(spelling: str) -> None:
    wc.WindowsConfinementPolicy(**{**GOOD, "read_roots": (spelling,)}).validate()


@pytest.mark.parametrize(
    ("root", "status"),
    [("C:\\w", "c:\\W\\s.json"), ("C:\\é", "C:\\É\\s.json"), ("C:\\", "C:\\s.json")],
)
def test_a_status_path_inside_a_write_root_under_any_casing_is_refused(root: str, status: str) -> None:
    with pytest.raises(wc.PolicyError):
        wc.WindowsConfinementPolicy(
            write_roots=(root,), temp_dir=root.rstrip("\\") + "\\t", status_path=status
        ).validate()


def test_a_status_without_a_completed_scan_says_so(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"schema": wc.STATUS_SCHEMA, "child_started": True, "exit_code": 0}), encoding="utf-8")
    assert wc.read_status(path).repositories_scanned is False
    path.write_text(
        json.dumps({"schema": wc.STATUS_SCHEMA, "child_started": True, "exit_code": 0, "repositories_scanned": True}),
        encoding="utf-8",
    )
    assert wc.read_status(path).repositories_scanned is True
    path.write_text(
        json.dumps({"schema": wc.STATUS_SCHEMA, "child_started": True, "repositories_scanned": "yes"}), encoding="utf-8"
    )
    assert wc.read_status(path) is None
