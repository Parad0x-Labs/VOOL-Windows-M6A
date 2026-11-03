"""Windows: the Zcash lane's replaces survive a sharing violation, and a lost race to pin the tool is not a failure.

On Windows `os.replace` fails with PermissionError while another thread or process holds the target open or is
replacing it at the same moment (Windows suite 41490181963: 174 of 320 concurrent `write_private` calls failed).
The pinned tool copy is made read-only (0o500), and Windows never replaces a read-only file, so a second thread
that prepared the same copy is refused for good, not for a moment: its copy is not needed, because the winner's
file already holds the pinned bytes. The Windows path is taken through `core.windows_sharing_retry._IS_WINDOWS`,
so these tests run on every host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from core import windows_sharing_retry


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0,) * 10)


def _own(tmp_path: Path, target) -> bool:
    return Path(os.path.abspath(os.fspath(target))).is_relative_to(os.path.abspath(tmp_path))


def test_a_private_write_survives_a_brief_sharing_violation(windows, monkeypatch, tmp_path) -> None:
    from core.zcash.watch import write_private

    target, refused, real_replace = tmp_path / "state.json", {"n": 0}, os.replace

    def replace(src, dst, *args, **kwargs):
        if _own(tmp_path, dst) and refused["n"] < 2:
            refused["n"] += 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", os.fspath(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    write_private(target, json.dumps({"writer": 1}))
    assert json.loads(target.read_text()) == {"writer": 1} and refused["n"] == 2
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp file left behind


def test_a_private_write_still_refused_after_the_bound_raises_and_cleans_up(windows, monkeypatch, tmp_path) -> None:
    from core.zcash.watch import write_private

    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if _own(tmp_path, dst):
            raise PermissionError(13, "denied", os.fspath(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PermissionError):
        write_private(tmp_path / "state.json", "{}")
    assert list(tmp_path.iterdir()) == []


def test_a_thread_that_lost_the_race_to_pin_the_tool_uses_the_winners_copy(windows, monkeypatch, tmp_path) -> None:
    from core.zcash.watch import Devtool

    blob = b"pinned tool bytes"
    pin = hashlib.sha256(blob).hexdigest()
    source = tmp_path / "zcash-devtool"
    source.write_bytes(blob)
    devtool = Devtool(binary=str(source), sha256=pin, wallet_dir=tmp_path / "wallet", network="test", bin_dir=tmp_path / "bin")
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if _own(tmp_path, dst) and Path(dst).name == pin:
            Path(dst).write_bytes(blob)  # another thread pinned the same bytes first, read-only
            raise PermissionError(13, "Access is denied", os.fspath(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    pinned = devtool._pinned_executable()
    assert pinned == tmp_path / "bin" / pin and pinned.read_bytes() == blob
    assert [p.name for p in (tmp_path / "bin").iterdir()] == [pin]  # this thread's temp copy is gone


def test_a_refused_pin_whose_target_holds_other_bytes_still_fails_closed(windows, monkeypatch, tmp_path) -> None:
    from core.zcash.watch import Devtool, ZcashWatchError

    blob = b"pinned tool bytes"
    pin = hashlib.sha256(blob).hexdigest()
    source = tmp_path / "zcash-devtool"
    source.write_bytes(blob)
    devtool = Devtool(binary=str(source), sha256=pin, wallet_dir=tmp_path / "wallet", network="test", bin_dir=tmp_path / "bin")
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if _own(tmp_path, dst) and Path(dst).name == pin:
            Path(dst).write_bytes(b"something else")
            raise PermissionError(13, "Access is denied", os.fspath(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(ZcashWatchError) as refused:
        devtool._pinned_executable()
    assert refused.value.code == "devtool_failed"
