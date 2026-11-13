"""Windows: the usage meter, the X stores, the pin forget and the catalog file wait out a sharing violation (K6).

On Windows ``os.replace`` and an open for reading fail with PermissionError for a moment while another
process (a reader of the same store, an antivirus scan, the search indexer) holds the file. The port
routes those calls through core/windows_sharing_retry.py; these stores came in from Mac without it:

- core/usage_quota.py broke and failed OPEN: a refused read counted as "nothing used today", so the cap
  let one more through and the write-back wiped every other count.
- core/x_actions.py and core/x_usage.py refused a whole action for one refused replace or read.
- core/memory/run_history.py reported a pin forget incomplete; registry_service/authority.py failed a re-stamp.

The Windows rule is driven on any host through ``windows_sharing_retry._IS_WINDOWS`` and a fake
sharing violation that refuses the first calls on the store's own file.
"""
from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path

import pytest

from core import runtime_paths, windows_sharing_retry


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0.0,) * 10)
    yield tmp_path / "home"
    runtime_paths.configure_runtime_home(None)


class _Held:
    """A file another process holds open: the next ``times`` replaces onto it, or reads of it, are refused."""

    def __init__(self, monkeypatch, name: str, *, replaces: int = 0, reads: int = 0) -> None:
        self.name, self.replaces, self.reads = name, replaces, reads
        real_replace, real_open = os.replace, builtins.open

        def replace(src, dst, *args, **kwargs):
            if Path(dst).name == self.name and self.replaces > 0:
                self.replaces -= 1
                raise PermissionError(32, "The process cannot access the file because it is being used")
            return real_replace(src, dst, *args, **kwargs)

        def open_(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and Path(file).name == self.name and "r" in mode \
                    and "+" not in mode and self.reads > 0:
                self.reads -= 1
                raise PermissionError(32, "The process cannot access the file because it is being used")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(builtins, "open", open_)
        monkeypatch.setattr(io, "open", open_)


# --------------------------------------------------------------------------- usage_quota


def _quota(monkeypatch):
    from core import usage_quota

    monkeypatch.setattr(usage_quota, "capability_limit", lambda capability, tier: 3)
    return usage_quota


def test_usage_meter_save_waits_out_a_held_file(home, monkeypatch) -> None:
    usage_quota = _quota(monkeypatch)
    usage_quota.consume_quota("x.post", tier="free", today="2026-11-13")
    _Held(monkeypatch, usage_quota._usage_path().name, replaces=3)
    assert usage_quota.consume_quota("x.post", tier="free", today="2026-11-13").allowed
    assert usage_quota.usage_today("x.post", today="2026-11-13") == 2
    assert [p.name for p in usage_quota._usage_path().parent.glob("*.tmp")] == []


def test_usage_meter_read_waits_out_a_held_file(home, monkeypatch) -> None:
    usage_quota = _quota(monkeypatch)
    for _ in range(3):
        usage_quota.consume_quota("x.post", tier="free", today="2026-11-13")
    _Held(monkeypatch, usage_quota._usage_path().name, reads=3)
    assert usage_quota.consume_quota("x.post", tier="free", today="2026-11-13").allowed is False


def test_usage_meter_that_stays_unreadable_refuses_and_keeps_its_counts(home, monkeypatch) -> None:
    """A read that is still refused is not "nothing used today": the cap holds and nothing is overwritten."""
    usage_quota = _quota(monkeypatch)
    for _ in range(3):
        usage_quota.consume_quota("x.post", tier="free", today="2026-11-13")
    usage_quota.consume_quota("other", tier="free", today="2026-11-13")
    before = usage_quota._usage_path().read_bytes()
    held = _Held(monkeypatch, usage_quota._usage_path().name, reads=10_000)
    assert usage_quota.check_quota("x.post", tier="free", today="2026-11-13").allowed is False
    assert usage_quota.consume_quota("x.post", tier="free", today="2026-11-13").allowed is False
    assert usage_quota.consume_quota("other", tier="free", today="2026-11-13").allowed is False
    usage_quota.release_quota("other", today="2026-11-13")
    assert usage_quota.store_readable() is False
    held.reads = 0  # the other process lets go
    assert usage_quota._usage_path().read_bytes() == before


# --------------------------------------------------------------------------- X stores


def test_x_actions_store_waits_out_a_held_file(home, monkeypatch) -> None:
    from core import x_actions
    from core.runtime_paths import data_path

    state = {"actions": {"a": {"state": "pending"}}}
    x_actions._save({"actions": {}})
    held = _Held(monkeypatch, data_path(x_actions._FILE).name, replaces=3)
    x_actions._save(state)
    held.reads = 3
    assert x_actions._load() == state


def test_x_usage_meter_waits_out_a_held_file(home, monkeypatch) -> None:
    from core import x_usage

    state = x_usage._blank(x_usage._month())
    state["requests"] = 7
    x_usage._save(x_usage._blank(x_usage._month()))
    held = _Held(monkeypatch, x_usage._path().name, replaces=3)
    x_usage._save(state)
    held.reads = 3
    assert x_usage._load()["requests"] == 7


# --------------------------------------------------------------------------- pin forget


def test_pin_forget_waits_out_a_held_pin_file(home, monkeypatch) -> None:
    from core import message_pins
    from core.memory import run_history

    chat = "held-pins"
    message_pins.pin_message(chat, "user", "The Flourstone Bakehouse opens at six.")
    message_pins.pin_message(chat, "user", "My lantern is green.")
    _Held(monkeypatch, message_pins.pins_path().name, replaces=3)
    erasures: dict = {}
    run_history._forget_pins("flourstone", chat_id=chat, erasures=erasures)
    assert erasures["stores"][run_history.STORE_PREFIX + "message_pins"]["status"] == "verified"
    store = json.loads(message_pins.pins_path().read_text(encoding="utf-8"))
    assert "Flourstone" not in json.dumps(store)


# --------------------------------------------------------------------------- registry catalog file


def test_catalog_file_restamp_waits_out_a_held_file(tmp_path, monkeypatch) -> None:
    from core.registry.codec import canonical, decode
    from registry_service import authority

    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0.0,) * 10)
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_bytes(canonical({"schema": "x"}))
    _Held(monkeypatch, catalog_file.name, replaces=3)

    class _Catalog:
        pass

    holder = _Catalog()
    holder.catalog_file = catalog_file
    authority.OnlineCatalog._write(holder, {"signed": {"sequence": 2}})
    assert decode(catalog_file.read_bytes())["registry"] == {"signed": {"sequence": 2}}
