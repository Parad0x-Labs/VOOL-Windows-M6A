"""Portable CLI simulation must not become an always-successful OS verifier."""
from __future__ import annotations

import subprocess

import pytest

from core.updater.macos import MacOSBundleInstaller

from .test_cli_and_tools import _fixture_codesign_runner


@pytest.fixture()
def bundles(tmp_path):
    expected = tmp_path / "expected.app"
    candidate = tmp_path / "candidate.app"
    for root in (expected, candidate):
        (root / "Contents" / "Resources").mkdir(parents=True)
        (root / "Contents" / "Resources" / "marker.txt").write_bytes(b"fixture bytes")
    return expected, candidate


@pytest.mark.parametrize("change", ("unchanged", "modified", "missing", "extra", "absent"))
def test_fixture_verification_requires_exact_bundle_bytes(bundles, change):
    expected, candidate = bundles
    run, commands = _fixture_codesign_runner(expected)
    marker = candidate / "Contents" / "Resources" / "marker.txt"
    if change == "modified":
        marker.write_bytes(b"unreviewed bytes")
    elif change == "missing":
        marker.unlink()
    elif change == "extra":
        (candidate / "extra.txt").write_bytes(b"extra bytes")
    elif change == "absent":
        candidate = candidate.parent / "absent.app"
    result = MacOSBundleInstaller(run=run).verify_bundle(candidate, require_notarization=False)
    assert result.ok is (change == "unchanged")
    assert result.codesigned is (change == "unchanged")
    assert not result.notarized
    if change == "absent":
        assert commands == []
    else:
        assert commands == [["codesign", "--verify", "--deep", "--strict", str(candidate)]]


@pytest.mark.parametrize("command", (["codesign", "-s", "-", "fixture.app"], ["spctl", "--assess", "fixture.app"], ["codesign", "--verify", "--deep", "--strict", "fixture.app", "extra"]))
def test_fixture_runner_refuses_unsupported_commands(bundles, command):
    run, commands = _fixture_codesign_runner(bundles[0])
    with pytest.raises(AssertionError):
        run(command)
    assert commands == []


def test_fixture_runner_does_not_waive_production_notarization(bundles):
    expected, candidate = bundles
    fixture_run, commands = _fixture_codesign_runner(expected)
    assessments = []

    def run(command):
        if command[0] == "spctl":
            assessments.append(command)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="not notarized")
        return fixture_run(command)

    result = MacOSBundleInstaller(run=run).verify_bundle(candidate)
    assert not result.ok
    assert result.codesigned
    assert not result.notarized
    assert len(commands) == 1
    assert assessments == [["spctl", "--assess", "--type", "execute", "-vv", str(candidate)]]


def test_empty_fixture_cannot_create_a_permissive_verifier(tmp_path):
    with pytest.raises(AssertionError, match="must contain actual bundle bytes"):
        _fixture_codesign_runner(tmp_path)
