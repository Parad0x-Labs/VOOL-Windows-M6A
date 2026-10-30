"""On Windows the served reader rig gives its daemon its own user profile, never the operator's.

A served turn that writes to "the desktop" lands in the profile the daemon sees. Left inherited,
that is the operator's real Desktop, which a test may never touch, and which the Windows write
gate refuses before any approval is asked (tests/test_manual_mode_approval_served_order.py).
"""
from __future__ import annotations

from types import SimpleNamespace

import tests._reader_served_rig as rig


def _daemon(tmp_path):
    return rig.ServedDaemon(tmp_path / "home", provider=SimpleNamespace(base_url="http://127.0.0.1:9"))


def test_the_daemon_sees_its_own_profile_beside_its_home_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(rig, "_REDIRECT_MACHINE_HOME", True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "operator"))
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    daemon = _daemon(tmp_path)

    env = daemon.env()

    assert env["USERPROFILE"] == env["HOME"] == str(tmp_path / "home-machine-home")
    assert daemon.machine_home == tmp_path / "home-machine-home"
    for name in ("Desktop", "Downloads", "Documents"):
        assert (daemon.machine_home / name).is_dir(), name
    assert daemon.machine_home.parent == daemon.home.parent, "the profile must sit beside VOOL_HOME, not inside it"


def test_a_caller_can_still_hand_the_daemon_a_profile_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(rig, "_REDIRECT_MACHINE_HOME", True)
    chosen = str(tmp_path / "chosen")
    daemon = rig.ServedDaemon(
        tmp_path / "home", provider=SimpleNamespace(base_url="http://127.0.0.1:9"), env_extra={"USERPROFILE": chosen}
    )

    assert daemon.env()["USERPROFILE"] == chosen


def test_off_windows_the_daemon_keeps_the_inherited_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(rig, "_REDIRECT_MACHINE_HOME", False)
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    daemon = _daemon(tmp_path)

    assert daemon.env()["HOME"] == str(tmp_path / "operator")
    assert not daemon.machine_home.exists()
