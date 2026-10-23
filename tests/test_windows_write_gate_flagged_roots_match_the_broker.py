"""On Windows the gate reads the flagged-folder list as the broker does (Phase C picks 1, 5 and 10).

Owner decision (2026-10-23, Phase C picks as written): a bad entry, a network entry (a share, a
device path, a mapped network drive) and an entry that is itself a junction or link are skipped
with a warning naming them, and the other entries stay flagged. A network entry is judged by its
spelling before anything opens it, because on Windows checking whether a share is a folder already
connects to it. POSIX keeps exactly the answers it had. The rule is switched on through the gate's
`_IS_WINDOWS`, as the gate's other tests do.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from core import windows_write_gate as gate


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(gate, "_WARNED", set(), raising=False)


def _env(*entries: str) -> dict[str, str]:
    return {gate.ENV_VAR: os.pathsep.join(entries)}


def _root(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _link(link: Path, target: Path) -> None:
    """A link to the folder `target`: a junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _warned(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_a_share_or_device_entry_is_skipped_with_a_warning_and_never_opened(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    shares = ["//server/share/work", "\\\\server\\share\\work", "\\\\?\\UNC\\server\\share", "\\\\.\\pipe\\x"]
    opened: list[str] = []
    for name in ("isdir", "realpath", "islink", "exists"):
        real = getattr(os.path, name)

        def recording(path, *args, _real=real, **kwargs):
            opened.append(os.fspath(path))
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(os.path, name, recording)
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        roots = gate.flagged_roots(_env(*shares, str(workspace)))
    monkeypatch.undo()
    assert roots == [_root(workspace)]
    assert not [path for path in opened if path in shares], opened
    for share in shares:
        assert share in _warned(caplog)


def test_a_mapped_network_drive_entry_is_skipped_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    mapped, local = tmp_path / "mapped", tmp_path / "local"
    mapped.mkdir()
    local.mkdir()
    real = os.path.realpath

    def resolves_to_a_share(path, *args, **kwargs):
        # Windows resolves a folder on a mapped network drive (Z:\work) to the share behind it.
        if os.fspath(path) == str(mapped):
            return "\\\\server\\share\\work"
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", resolves_to_a_share)
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        roots = gate.flagged_roots(_env(str(mapped), str(local)))
    monkeypatch.undo()
    assert roots == [_root(local)]
    assert str(mapped) in _warned(caplog)


def test_an_entry_that_is_a_junction_or_link_is_skipped_and_its_real_folder_can_be_flagged(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    real_folder, link = tmp_path / "real", tmp_path / "link"
    real_folder.mkdir()
    _link(link, real_folder)
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        assert gate.flagged_roots(_env(str(link))) == []
    assert str(link) in _warned(caplog)
    assert gate.flagged_roots(_env(str(real_folder))) == [_root(real_folder)]
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, real_folder, environ=_env(str(link)))


def test_a_relative_or_missing_entry_is_skipped_with_a_warning_and_the_rest_stay_flagged(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    workspace, missing = tmp_path / "scratch", tmp_path / "does-not-exist"
    workspace.mkdir()
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        roots = gate.flagged_roots(_env("scratch", str(missing), "", str(workspace)))
    assert roots == [_root(workspace)]
    warned = _warned(caplog)
    assert "scratch" in warned and str(missing) in warned


def test_each_skipped_entry_is_warned_about_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    env = _env("//server/share/work", str(tmp_path / "does-not-exist"))
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        for _ in range(3):
            assert gate.flagged_roots(env) == []
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


def test_the_gate_refuses_a_write_when_only_a_share_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    # Which check answers first differs by host (on Windows the share path cannot be placed), but the
    # write is refused either way, because the share is never flagged.
    reason = gate.refusal("machine.write_file", {"path": "//server/share/work/a.txt"}, None,
                          environ=_env("//server/share/work"))
    assert reason


def test_posix_keeps_its_answers_and_warns_about_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    real_folder, link = tmp_path / "real", tmp_path / "link"
    real_folder.mkdir()
    _link(link, real_folder)
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        roots = gate.flagged_roots(_env(str(link), "scratch", str(tmp_path / "does-not-exist")))
    assert roots == [_root(real_folder)]
    assert _warned(caplog) == ""
