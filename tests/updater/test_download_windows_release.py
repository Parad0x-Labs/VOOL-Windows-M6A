"""Windows law: the overshoot verdict and its discard must survive the platform's
delete-while-open rule.

core/updater/download.py unlinks the partial inside the ``open(part_path)`` block that
wrote it. On Windows that unlink raises WinError 32, the generic transfer handler turns
the overshoot into FETCH_FAILED (resumable=True) and keeps the hostile bytes staged for
the resume lane. The existing TestVerificationFailures cases pin the verdict; these
twins also pin the DISCARD (part and meta both gone) so the Windows delete rule cannot
regress silently behind a passing POSIX CI.
"""
from __future__ import annotations

import pytest

from core.updater.download import DownloadReason

from .helpers import generate_publisher_keypair
from .test_download_resume import BLOB, FakeServer, _artifact, _downloader


@pytest.fixture()
def keys():
    private_hex, public_hex = generate_publisher_keypair()
    return private_hex, public_hex


def test_size_mismatch_discard_survives_windows_delete_rules(tmp_path, keys):
    """Windows twin of TestVerificationFailures::test_size_mismatch_detected: same
    construction (manifest declares 899 for a 900-byte body), plus the discard
    assertions that only a Windows run can hold the product to."""
    private_hex, public_hex = keys
    server = FakeServer(BLOB)
    artifact = _artifact(BLOB, private_hex=private_hex, size=len(BLOB) - 1)
    result = _downloader(server).download(artifact, tmp_path, public_hex)
    assert result.reason is DownloadReason.SIZE_MISMATCH, result
    assert not (tmp_path / "artifact.part").exists()
    assert not (tmp_path / "artifact.part.meta.json").exists()


def test_overshoot_discard_survives_windows_delete_rules(tmp_path, keys):
    """Windows twin of TestVerificationFailures::test_overshoot_is_never_written: same
    construction (manifest describes the 600-byte prefix, the server keeps sending
    the full 900), plus the discard assertions."""
    private_hex, public_hex = keys
    server = FakeServer(BLOB)
    artifact = _artifact(BLOB[:600], private_hex=private_hex)
    result = _downloader(server).download(artifact, tmp_path, public_hex)
    assert result.reason is DownloadReason.SIZE_MISMATCH, result
    assert not (tmp_path / "artifact.part").exists()
    assert not (tmp_path / "artifact.part.meta.json").exists()
