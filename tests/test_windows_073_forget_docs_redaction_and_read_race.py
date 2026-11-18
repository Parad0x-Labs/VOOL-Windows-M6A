"""Forget in a chat's documents: Windows keeps the 0.7.2 line redaction, every host closes the read race.

0.7.3 carry (Release Lead, "Windows-only fixes by default; Mac/Linux behave as Mac ships"): off Windows a forget
erases the whole pasted document that carries the words, as Mac 0.7.3 does
(tests/test_forget_erases_pasted_chat_documents_20261020.py). On Windows the port's design is unchanged for now:
the carrying lines are replaced by ``[line removed by forget]``, the rest of the document stays, and a re-paste of
the forgotten words is refused at the paste door.

The one change on every host: a forget whose barrier opens while a turn is reading a document's bytes used to let
the forgotten words into that turn on Windows, because the serve screen was read once, before the bytes. The
screen is now read again after the read, as Mac reads the authority again after it (its carried-documents rule).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.test_forget_scrubs_run_history import command, isolated_pages  # isolated_pages: the runtime home fixture
from tests.test_question_date_time_leg_20261002 import _hash_backend

pytestmark = pytest.mark.usefixtures("_hash_backend")

FACT = "My locker code is 4417."
INVENTORY = "\n".join(f"Inventory note {index:03d}: crate {index} holds spare harbor parts." for index in range(90))


@pytest.fixture(autouse=True)
def _a_chat_that_has_taken_turns(request):
    """A chat's first turn opens its memory, which sets up the home's forget authority."""
    if "isolated_pages" not in request.fixturenames:
        return
    from core.vool_memory import VoolMemory

    VoolMemory(runtime_home=request.getfixturevalue("isolated_pages")).close()


@pytest.fixture
def windows(monkeypatch):
    from core import windows_write_gate as gate

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch):
    from core import windows_write_gate as gate

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)


def _chat(seed: str) -> str:
    return "openclaw:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _paste(chat: str, text: str, *, turn: str) -> dict:
    from core.chat_attachments import bind_to_turn, stage_document
    from core.context_namespace import ensure_chat_namespace

    ensure_chat_namespace(chat)
    record = stage_document(session_id=chat, data=text.encode("utf-8"))
    bind_to_turn(session_id=chat, turn_id=turn, attachment_ids=[record["id"]])
    return record


def _carried(chat: str) -> list[dict]:
    from core.chat_attachments import carried_documents_for_turn

    return carried_documents_for_turn(session_id=chat, exclude_turn_id="the-next-turn")


def _bytes_path(record: dict) -> Path:
    from core.chat_attachments import stage_dir

    return stage_dir() / f"{record['id']}.bin"


def _forget_opens_while_reading(monkeypatch, record: dict, chat: str, home: Path) -> list:
    """Open the same chat's forget barrier while the turn reads the record's bytes."""
    import core.chat_attachments as ca
    from core.memory import revocation

    read = ca._open_regular
    opened: list = []

    def read_while_a_forget_starts(path):
        if Path(path).name == f"{record['id']}.bin" and not opened:
            opened.append(revocation.begin(chat, "4417", home))
        return read(path)

    monkeypatch.setattr(ca, "_open_regular", read_while_a_forget_starts)
    return opened


# --- Windows: the 0.7.2 redaction design, unchanged ---------------------------------------------


def test_windows_forget_redacts_the_carrying_line_and_keeps_the_document(isolated_pages, windows):
    from core.chat_attachments import FORGET_LINE_MARK

    chat = _chat("windows-073-forget-redacts")
    pasted = _paste(chat, INVENTORY[:2000] + "\n" + FACT + "\n" + INVENTORY[2000:], turn="t1")

    handled, reply = command(isolated_pages, chat, "Forget my locker code is 4417.")

    assert handled and reply.startswith("Forget applied"), reply
    assert _bytes_path(pasted).exists(), "Windows keeps the document; only its carrying line goes"
    kept = _bytes_path(pasted).read_bytes().decode("utf-8")
    assert "4417" not in kept and FORGET_LINE_MARK in kept and "crate 89 holds" in kept
    carried = _carried(chat)
    assert [entry["attachment_id"] for entry in carried] == [pasted["id"]], carried
    assert "4417" not in carried[0]["text"] and "crate 89 holds" in carried[0]["text"]


def test_windows_refuses_a_re_paste_of_the_forgotten_words(isolated_pages, windows):
    from core.chat_attachments import AttachmentRefused

    chat = _chat("windows-073-forget-repaste")
    _paste(chat, FACT + "\n" + INVENTORY, turn="t1")
    handled, reply = command(isolated_pages, chat, "Forget my locker code is 4417.")
    assert handled and reply.startswith("Forget applied"), reply

    with pytest.raises(AttachmentRefused) as refused:
        _paste(chat, INVENTORY + "\n" + FACT, turn="t3")
    assert refused.value.code == "forgotten_content"


def test_off_windows_forget_erases_the_whole_document_as_mac_does(isolated_pages, off_windows):
    from core.chat_attachments import FORGET_LINE_MARK

    chat = _chat("windows-073-forget-mac-erase")
    pasted = _paste(chat, INVENTORY[:2000] + "\n" + FACT + "\n" + INVENTORY[2000:], turn="t1")

    handled, reply = command(isolated_pages, chat, "Forget my locker code is 4417.")

    assert handled and reply == "Forget applied. Removed 1 memory entry.", reply
    assert not _bytes_path(pasted).exists()
    assert _carried(chat) == []
    # And a re-paste is kept (the user's own), never carried into a later turn; no redaction mark anywhere.
    again = _paste(chat, INVENTORY + "\n" + FACT, turn="t3")
    assert _bytes_path(again).exists() and _carried(chat) == []
    assert FORGET_LINE_MARK not in _bytes_path(again).read_text(encoding="utf-8")


# --- every host: a forget that opens while a turn reads the bytes keeps its words out --------------


@pytest.mark.parametrize("host", ["windows", "off_windows"])
def test_a_forget_opening_during_the_read_never_reaches_the_carried_documents(isolated_pages, monkeypatch, request, host):
    request.getfixturevalue(host)
    chat = _chat(f"windows-073-race-carried-{host}")
    record = _paste(chat, FACT + "\n" + INVENTORY, turn="t1")
    assert any("4417" in entry["text"] for entry in _carried(chat)), "probe is blind: the paste was never carried"
    opened = _forget_opens_while_reading(monkeypatch, record, chat, isolated_pages)

    carried = _carried(chat)

    assert opened, "probe is blind: the forget never opened during the read"
    assert not any("4417" in str(entry) for entry in carried), carried


@pytest.mark.parametrize("delivery", ["model_attachments_for_turn", "evidence_items_for_turn"])
def test_windows_a_forget_opening_during_the_read_never_reaches_this_turns_attachments(
        isolated_pages, monkeypatch, windows, delivery):
    import core.chat_attachments as ca

    chat = _chat(f"windows-073-race-{delivery}")
    record = _paste(chat, FACT + "\n" + INVENTORY, turn="t1")
    serve = getattr(ca, delivery)
    assert any("4417" in str(entry) for entry in serve(session_id=chat, turn_id="t1")), "probe is blind"
    opened = _forget_opens_while_reading(monkeypatch, record, chat, isolated_pages)

    served = serve(session_id=chat, turn_id="t1")

    assert opened, "probe is blind: the forget never opened during the read"
    assert not any("4417" in str(entry) for entry in served), served


def test_windows_a_turn_with_no_forget_during_the_read_is_served_whole(isolated_pages, windows):
    """The second reading of the screen changes nothing when the ledger did not move."""
    chat = _chat("windows-073-race-quiet")
    record = _paste(chat, FACT + "\n" + INVENTORY, turn="t1")

    carried = _carried(chat)

    assert [entry["attachment_id"] for entry in carried] == [record["id"]]
    assert "4417" in carried[0]["text"] and "crate 89 holds" in carried[0]["text"]
