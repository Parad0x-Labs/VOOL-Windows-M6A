"""A workspace mutation ledger is a file Windows can create, whatever the session id says.

The ledger was named by the raw session key. Served chat sessions carry colons
("openclaw:email-mixed:..."), which Windows refuses in a file name (Errno 22), so every workspace
write in such a session failed to record AFTER the file had changed: the effect read as
outcome-unknown and the rollback ledger stayed empty (the served email mixed-request journey). A
backslash in a session id is also a separator on Windows, so the raw key could name a path
outside the ledger directory.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.execution import artifacts

_DIGEST_NAME = re.compile(r"~[0-9a-f]{40}\.json")


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(artifacts, "data_path", lambda *parts: tmp_path.joinpath(*parts))
    return tmp_path / "runtime_execution" / "mutations"


@pytest.mark.parametrize("session_key", [
    "openclaw:email-mixed:be7adfa1aeef441b8d3d8ff0b0f8d445",
    "..\\..\\outside",
    'a<b>c"d|e?f*g',
    "CON",
    "nul",
    "trailing.",
])
def test_a_key_windows_cannot_use_gets_a_digest_name_inside_the_ledger_dir(ledger_dir, monkeypatch, session_key):
    monkeypatch.setattr(artifacts, "_WINDOWS_LEDGER_NAMES", True)
    path = artifacts._mutation_log_path(session_key)
    assert path.parent == ledger_dir
    assert _DIGEST_NAME.fullmatch(path.name), path.name


def test_distinct_keys_never_share_a_ledger(ledger_dir, monkeypatch):
    monkeypatch.setattr(artifacts, "_WINDOWS_LEDGER_NAMES", True)
    keys = ["openclaw:a", "openclaw_a", "openclaw-a", "openclaw:A"]
    assert len({artifacts._mutation_log_path(key) for key in keys}) == len(keys)


def test_a_portable_key_keeps_its_readable_name(ledger_dir, monkeypatch):
    monkeypatch.setattr(artifacts, "_WINDOWS_LEDGER_NAMES", True)
    assert artifacts._mutation_log_path("workspace-0123456789abcdef").name == "workspace-0123456789abcdef.json"
    assert artifacts._mutation_log_path("repoops-session").name == "repoops-session.json"


def test_posix_keeps_the_raw_key_it_always_had(ledger_dir, monkeypatch):
    monkeypatch.setattr(artifacts, "_WINDOWS_LEDGER_NAMES", False)
    assert artifacts._mutation_log_path("openclaw:email-mixed:1").name == "openclaw:email-mixed:1.json"


def test_a_served_chat_session_ledger_round_trips_on_this_host(ledger_dir):
    key = "openclaw:email-mixed:be7adfa1aeef441b8d3d8ff0b0f8d445"
    records = [{"mutation_id": "m1", "path": "orchard-delivery-note.txt", "before_text": ""}]
    artifacts._store_mutation_records(key, records)
    assert artifacts._load_mutation_records(key) == records
