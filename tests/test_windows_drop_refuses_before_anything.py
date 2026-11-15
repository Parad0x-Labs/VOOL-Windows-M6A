"""On Windows, VOOL Drop (Mac 0.7.2, off by default, unreviewed) refuses before it touches anything.

Drop guards its folders and files with POSIX mode bits, the owner's uid, O_NOFOLLOW and ``fcntl.flock``.
Windows has none of these (K4: a mode-bit check never counts as a pass there), and ``import fcntl`` at the top
of core/drop/service.py made the module, and every test file that imports it, fail to import on Windows.

- core/drop/service.py imports on a host without ``fcntl``;
- on Windows, with ``VOOL_DROP=1``, a Drop home or relay is refused with ``DROP_NOT_ON_WINDOWS``
  before any folder is created;
- off Windows nothing changes: Drop stays off unless ``VOOL_DROP=1``.
"""
from __future__ import annotations

import importlib
import sys

import pytest

import core.drop
from core.drop import DropError


def _reload_service_without_fcntl(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", None)   # `import fcntl` raises ImportError, as on Windows
    monkeypatch.delitem(sys.modules, "core.drop.service", raising=False)
    return importlib.import_module("core.drop.service")


def test_the_drop_service_imports_on_a_host_without_fcntl(monkeypatch) -> None:
    service = _reload_service_without_fcntl(monkeypatch)
    assert hasattr(service, "DropHome") and hasattr(service, "LocalRelay")
    monkeypatch.delitem(sys.modules, "core.drop.service", raising=False)


@pytest.mark.parametrize("what", ["home", "relay"])
def test_on_windows_drop_is_refused_before_any_folder_exists(monkeypatch, tmp_path, what) -> None:
    service = _reload_service_without_fcntl(monkeypatch)
    monkeypatch.setenv("VOOL_DROP", "1")
    monkeypatch.setattr(core.drop, "_IS_WINDOWS", True)
    home, relay = tmp_path / "home", tmp_path / "relay"
    with pytest.raises(DropError, match="DROP_NOT_ON_WINDOWS"):
        if what == "home":
            service.DropHome(home, relay)
        else:
            service.LocalRelay(relay, create=True)
    assert not home.exists() and not relay.exists()
    monkeypatch.delitem(sys.modules, "core.drop.service", raising=False)


def test_off_windows_drop_still_stays_off_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("VOOL_DROP", raising=False)
    monkeypatch.setattr(core.drop, "_IS_WINDOWS", False, raising=False)
    with pytest.raises(DropError, match="DROP_DISABLED"):
        core.drop.require_enabled()
