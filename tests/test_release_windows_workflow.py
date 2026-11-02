"""The Windows release workflow builds VOOL-Setup.exe from the exact commit it was started for,
proves it with the install smoke, and stops at a workflow artifact.

Publishing a GitHub release stays a human step: the workflow may read the repository and upload a
workflow artifact, never create or edit a release. The installer it uploads is the same file the
smoke installed, named in SHA256SUMS, built from the checked-out commit the run was started for
(the tag's commit on a tag push), not from whatever a branch points at by the time the job runs.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-windows.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # YAML 1.1 reads a bare `on` key as the boolean True.
    return workflow.get("on", workflow.get(True))


def _steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_it_runs_on_a_version_tag_and_by_dispatch_only() -> None:
    triggers = _triggers(_workflow())

    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}


def test_every_checkout_is_the_exact_commit_the_run_was_started_for() -> None:
    checkouts = [step for step in _steps(_workflow()) if str(step.get("uses", "")).startswith("actions/checkout@")]

    assert checkouts
    for step in checkouts:
        assert step.get("with", {}).get("ref") == "${{ github.sha }}"


def _step_index(steps: list[dict], needle: str) -> int:
    found = [i for i, step in enumerate(steps) if needle in str(step.get("run", ""))]
    assert len(found) == 1, f"{needle!r} must be run by exactly one step, found {len(found)}"
    return found[0]


def test_the_smoke_installs_the_same_installer_the_build_kept_after_signing_and_sums() -> None:
    """Build once (stamped commit checked), sign (off for now), write the sums, then install and
    check that exact file, then confirm the sums still match it."""
    steps = _steps(_workflow())
    build = _step_index(steps, "-BuildOnlyTo")
    sign = _step_index(steps, "tools/sign_windows_files.ps1")
    sums = _step_index(steps, "tools/write_sha256sums.py --dir")
    smoke = _step_index(steps, "-SetupPath")
    check = _step_index(steps, "tools/write_sha256sums.py --check")

    assert build < sign < sums < smoke < check
    for index in (build, smoke):
        assert "tools/windows_install_smoke.ps1" in steps[index]["run"]
        assert "-ExpectedCommit" in steps[index]["run"] and "${{ github.sha }}" in steps[index]["run"]


def test_authenticode_signing_is_one_step_that_is_off_until_a_route_is_set() -> None:
    steps = _steps(_workflow())
    sign = steps[_step_index(steps, "tools/sign_windows_files.ps1")]

    assert sign.get("if") == "${{ vars.WINDOWS_SIGNING_ROUTE != '' }}"


def test_it_uploads_the_installer_and_its_sums_as_a_workflow_artifact() -> None:
    uploads = [step for step in _steps(_workflow()) if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    release = [step for step in uploads if "VOOL-Setup.exe" in str(step.get("with", {}).get("path", ""))]

    assert len(release) == 1
    assert "SHA256SUMS" in str(release[0]["with"]["path"])
    assert release[0]["with"].get("if-no-files-found") == "error"


def test_nothing_in_it_can_publish_a_release() -> None:
    workflow = _workflow()
    text = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.get("permissions") == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
    assert "gh release" not in text
    assert "action-gh-release" not in text and "create-release" not in text and "upload-release-asset" not in text
    assert "GITHUB_TOKEN" not in text and "github.token" not in text
