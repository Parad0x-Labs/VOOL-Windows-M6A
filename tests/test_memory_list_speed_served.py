"""Served proof: Settings > Memory answers quickly with many chats, and withholds only what was forgotten.

Measured on Steve (12 Nov, train 8810bbf8): GET /api/memory/entries took 46 to 147 s per call with the memory list
empty. The route reads every active chat, and each chat's listing asked the forget authority four times, each
ask opening the memory store twice with its schema check. The route now reads the authority once per chat for
the whole listing (core/memory/revocation.py _one_reading), the same as the chat list.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import urlopen

from tests._blackbox_served_rig import ServedDaemon, run_in_home
from tests._served_sufficiency_provider import SEED
from tests.test_private_chat_served_workflow import MODEL, _certify, _Provider, _stream

REPO = Path(__file__).resolve().parents[1]
CHATS = 40

_SEED_CHATS = """
from core.context_namespace import ensure_chat_namespace
from core.memory.entries import resolve_memory_access_policy
from core.persistent_memory import append_conversation_event
for index in range({count}):
    chat = "openclaw:" + "e%019d" % index
    ensure_chat_namespace(chat, grant_current_receipts=False)
    policy = resolve_memory_access_policy(chat_id=chat)
    append_conversation_event(session_id=chat, user_input="Hello number %d" % index, assistant_output="Hi.",
                              access_policy=policy)
"""


def _timed_facts(daemon: ServedDaemon) -> tuple[float, list[str]]:
    started = time.monotonic()
    with urlopen(f"{daemon.base_url}/api/memory/entries?limit=100", timeout=300) as response:
        body = json.loads(response.read().decode())
    return time.monotonic() - started, [str(e.get("fact") or "") for e in body.get("entries") or []]


def test_the_memory_list_is_quick_with_many_chats_and_withholds_only_the_forgotten(tmp_path):
    home = tmp_path / "home"
    provider = _Provider({MODEL: "Got it."})
    with provider:
        run_in_home(home, SEED.format(root=REPO, base_url=provider.base_url, models=[(MODEL, {})]))
        run_in_home(home, _SEED_CHATS.format(count=CHATS))
        with ServedDaemon(home, env_extra={"VOOL_MODEL_LOAD_FLOOR_GB": "0"}) as daemon:
            assert _certify(daemon).get("state") == "verified"
            kept_chat = "openclaw:" + "f1" * 10
            forget_chat = "openclaw:" + "f2" * 10
            assert "Locked in" in _stream(daemon, "Remember that my boat is called Juniper.", kept_chat)[0]
            assert "Locked in" in _stream(daemon, "Remember that my locker code is QUILL-77.", forget_chat)[0]
            assert "Forget applied" in _stream(daemon, "Forget QUILL-77.", forget_chat)[0]

            _timed_facts(daemon)   # the first read may set up the stores
            took, facts = _timed_facts(daemon)
            assert any("Juniper" in f for f in facts), facts
            assert not any("QUILL-77" in f for f in facts), facts
            # Before: about four store opens per chat at roughly 0.05 s each, 10 s for 41 chats on Linux.
            assert took < 3.0, f"/api/memory/entries took {took:.2f} s over {CHATS + 2} chats"

    log = (home / "daemon.log").read_text(encoding="utf-8", errors="replace")
    assert "authorize_payload failed" not in log, [line for line in log.splitlines() if "authorize_payload" in line][:5]


def test_the_memory_list_opens_the_memory_store_a_fixed_number_of_times(tmp_path, monkeypatch):
    """The same route in-process: the store opens no longer grow with the number of chats."""
    from core import context_retrieval as cr
    from core import runtime_paths
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_get

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "home" / "workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    try:
        exec(_SEED_CHATS.format(count=8), {})
        opened = []
        real = cr._open_memory

        def counting(*args, **kwargs):
            opened.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(cr, "_open_memory", counting)
        response = dispatch_get(path="/api/memory/entries", query={}, runtime=RuntimeServices(display_name="VOOL"),
                                model_name="vool", client_host="127.0.0.1")
        assert response.status == 200 and json.loads(response.body.decode())["ok"] is True
        # Before: two opens for each of four authority checks per chat (>= 64 for 8 chats).
        assert len(opened) <= 4, len(opened)
    finally:
        runtime_paths.configure_runtime_home(None)
