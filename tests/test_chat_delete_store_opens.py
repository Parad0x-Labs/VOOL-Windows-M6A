"""Deleting one chat opens the memory store a fixed number of times, however many chats the home holds.

Measured 2026-11-16 on a 250-chat home (3 finalized turns per chat, 10 in the deleted one): one delete took
minutes. A chat delete erases each of the chat's finalizations through the A8 traversal, and two of its steps read
the whole home's conversation log, session summaries and heuristics through the forget authority, which asked again
for every row and opened the memory store twice for each ask. Those steps now read the authority once per step
(core/memory/revocation.py _one_reading), inside the fence they already held.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    """These rows drive the Windows-only one reading of the sweep steps (0.7.3: Windows-only fixes)."""
    from core import finalization

    monkeypatch.setattr(finalization, "_SWEEP_ONE_READING", True)

_DELETED = "openclaw:" + "d" * 20
_FORGOTTEN_TOKEN = "quillseventyseven"


def _chat(index: int) -> str:
    return "openclaw:" + "k%019d" % index


def _finalize(text: str, request_id: str) -> str:
    from core.finalization import finalize_answer
    from core.semantic.semantic_admissions import _CURRENT_REQUEST_ID, set_request_context
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission

    reset_admission()
    admit_semantic_result({"response": text, "route_reason": "model_lane"})
    token = set_request_context(request_id)
    try:
        record = finalize_answer(turn_id="t", canonical_content=text)
    finally:
        _CURRENT_REQUEST_ID.reset(token)
        reset_admission()
    return str(getattr(record, "finalization_id", "") or (record or {}).get("finalization_id") or "")


@pytest.fixture
def home(tmp_path, monkeypatch):
    from core import runtime_paths
    from core.memory.revocation import initialize_memory_home

    home = tmp_path / "home"
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(home / "workspace"))
    runtime_paths.configure_runtime_home(home)
    initialize_memory_home(home)
    try:
        yield home
    finally:
        runtime_paths.configure_runtime_home(None)


def _seed(home, chats: int, tag: str) -> None:
    """``chats`` other chats with two turns each, one of them finalized, and a deleted chat with three finalized
    turns. Rows are written straight to the log, as the A8 privacy suites do, so the seed stays fast."""
    from core.context_namespace import ensure_chat_namespace
    from core.memory.files import conversation_log_path

    rows = []
    for index in range(chats):
        chat = _chat(index)
        rows.append({"session_id": chat, "user": "q", "assistant": f"Plain reply {index}."})
        text = f"Kept answer {index}{tag}: the heron count is {1000 + index}."
        _finalize(text, f"req-kept-{index}{tag}")
        rows.append({"session_id": chat, "user": "q", "assistant": text, "request_id": f"req-kept-{index}{tag}"})
    for turn in range(3):
        text = f"Deleted answer {turn}{tag}: the lighthouse is painted cobalt {turn}."
        _finalize(text, f"req-deleted-{turn}{tag}")
        rows.append({"session_id": _DELETED, "user": "q", "assistant": text,
                     "request_id": f"req-deleted-{turn}{tag}"})
    # A row another chat forgot: it must stay withheld through the delete's reads and rewrites.
    rows.append({"session_id": _chat(0), "user": "q", "assistant": f"My locker code is {_FORGOTTEN_TOKEN}."})
    path = conversation_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"ts": "2026-11-01T00:00:00", **row}) + "\n")
    ensure_chat_namespace(_DELETED, grant_current_receipts=False)


def _forget(chat: str, token: str) -> None:
    from core.memory import revocation

    revocation.complete(revocation.begin(chat, token))


def _delete_counting_opens(home, monkeypatch) -> tuple[int, dict]:
    from core import context_retrieval as cr
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    opened = []
    real = cr._open_memory

    def counting(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(cr, "_open_memory", counting)
    response = dispatch_post(path="/api/chat/session", body={"session_id": _DELETED, "operation": "delete"},
                             headers={}, runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                             client_host="127.0.0.1", workspace_root_provider=lambda: str(home / "workspace"))
    monkeypatch.setattr(cr, "_open_memory", real)
    assert response.status == 200, response.body
    return len(opened), json.loads(response.body.decode())


def test_one_delete_opens_the_store_a_bounded_number_of_times_with_many_chats(home, monkeypatch):
    _seed(home, chats=40, tag="-bounded")
    opens, body = _delete_counting_opens(home, monkeypatch)
    assert body["deleted"] is True, body
    # Before: two opens per log row for every authority ask, in each of the chat's three erasures (> 500 here,
    # growing with every chat in the home). Now a few per erasure plus the route's own, whatever the chat count.
    assert opens <= 30, opens


def test_the_store_opens_do_not_grow_with_the_number_of_chats(home, monkeypatch, tmp_path):
    from core import runtime_paths
    from core.memory.revocation import initialize_memory_home

    # Two homes seeded before either delete: 5 other chats, then 60. Each deleted chat has three finalized turns.
    bigger = tmp_path / "bigger"
    initialize_memory_home(bigger)
    _seed(home, chats=5, tag="-few")
    runtime_paths.configure_runtime_home(bigger)
    monkeypatch.setenv("VOOL_HOME", str(bigger))
    _seed(bigger, chats=60, tag="-many")
    many, _ = _delete_counting_opens(bigger, monkeypatch)
    runtime_paths.configure_runtime_home(home)
    monkeypatch.setenv("VOOL_HOME", str(home))
    few, _ = _delete_counting_opens(home, monkeypatch)
    assert many <= few + 2, (few, many)


def test_the_delete_erases_only_the_deleted_chat_and_keeps_the_forgotten_withheld(home, monkeypatch):
    from core.finalization import AVAILABILITY_AVAILABLE, AVAILABILITY_ERASED, payload_availability_for_request_id
    from core.memory.files import conversation_log_path, load_jsonl

    _seed(home, chats=12, tag="-erased")
    _forget(_chat(0), _FORGOTTEN_TOKEN)
    assert not any(_FORGOTTEN_TOKEN in str(row.get("assistant")) for row in load_jsonl(conversation_log_path()))
    _, body = _delete_counting_opens(home, monkeypatch)
    assert body["deleted"] is True, body

    raw = conversation_log_path().read_text(encoding="utf-8")
    # The deleted chat's transcript and finalized payloads are gone.
    assert _DELETED not in raw
    assert "lighthouse" not in raw
    for turn in range(3):
        assert payload_availability_for_request_id(f"req-deleted-{turn}-erased") == AVAILABILITY_ERASED
    # Every other chat's turns are still there and still served.
    served = load_jsonl(conversation_log_path())
    for index in range(12):
        assert f"Plain reply {index}." in raw
        assert f"Kept answer {index}-erased: the heron count is {1000 + index}" in raw
        assert payload_availability_for_request_id(f"req-kept-{index}-erased") == AVAILABILITY_AVAILABLE
        assert any(row.get("assistant") == f"Plain reply {index}." for row in served), index
    # What another chat forgot is not readable after the delete's reads and rewrites.
    assert not any(_FORGOTTEN_TOKEN in str(row.get("assistant")) for row in served)
