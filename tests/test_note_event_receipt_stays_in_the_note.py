"""An approved calendar event writes its receipt only into the plain note it came from.

A user asks in chat to schedule an action line from a note, then approves the event; after the event is
created and verified, VOOL writes the event's receipt (``linked_event_uid`` and ``linked_action_id``) into
that note's front matter. This file pins, through the real operator route (the chat parser, the operator
dispatcher and a real loopback CalDAV server), that the receipt:

* still lands in a plain ``.md`` note, which keeps its front matter and body byte for byte;
* is not written over a change another editor made to the note just before it (the event itself is created
  and the answer says PARTIAL);
* is not written into the workspace file behind a note that is a link, whether the note was a link when the
  event was proposed or was swapped for one while the event awaited approval.

``link_note_to_event`` on paths the calendar route never stores, and archiving and restoring notes, are
pinned in ``tests/test_notes_archive_stays_in_the_notes_folder.py``, whose notes world and helpers this
file reuses.

The calendar route reads the user's time zone through ``zoneinfo``, which on Windows needs the ``tzdata``
package. The Windows gauntlet installs ``.[dev,companion,evm]``, none of which brings it, so this file runs
on the Linux and macOS CI lanes and is not on the gauntlet's runtime list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.pa_beta_gate._caldav_service import start_caldav_fixture
from tests.test_notes_archive_stays_in_the_notes_folder import (
    EDIT,
    _append,
    _as_restored,
    _file_link,
    _note_bytes,
    _say,
    _stage_edits,
    _stamp_line,
    _tree,
)
from tests.test_notes_archive_stays_in_the_notes_folder import world as notes_world

CALENDAR = "/calendars/vilnius/"
world = notes_world


@pytest.fixture
def calendar(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    from zoneinfo import ZoneInfo

    from core import local_operator_actions
    from core.user_preferences import save_user_timezone

    assert save_user_timezone("Europe/Athens"), \
        "the user's time zone was not accepted; zoneinfo found no time-zone database (on Windows: the tzdata package)"
    server, state, base_url, _port = start_caldav_fixture(calendars={CALENDAR: "Vilnius"})
    monkeypatch.setenv("VOOL_CALENDAR_PROVIDER", "caldav")
    monkeypatch.setenv("VOOL_CALENDAR_URL", base_url)
    monkeypatch.setenv("VOOL_CALENDAR_ID", CALENDAR)
    fixed = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_operator_actions, "_scheduling_now", lambda: fixed.astimezone(ZoneInfo("Europe/Athens")))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _propose(action: str, *, session_id: str):
    _intent, proposal = _say(f'schedule the action "{action}" from my note on Thursday at 10:00 Europe/Athens',
                             session_id=session_id)
    assert proposal.status == "approval_required", proposal.response_text
    return proposal


def _approve(proposal, *, session_id: str):
    _intent, created = _say(f"approve calendar {proposal.details['action_id']}", session_id=session_id)
    assert created.ok and created.status == "executed", created.response_text
    return created


def test_an_approved_event_from_a_plain_note_still_writes_its_receipt_into_that_note(world, calendar) -> None:
    session = "notes-receipt-plain"
    original = _note_bytes("Venue plan", "[action] book the venue call Friday at 15:00 Europe/Athens")
    note = world.notes / "venue.md"
    note.write_bytes(original)

    created = _approve(_propose("book the venue call", session_id=session), session_id=session)

    uid = created.details["uid"]
    assert uid in calendar.snapshot()[CALENDAR]
    assert not created.details.get("note_link_failed"), created.response_text
    receipt = f"linked_event_uid: {uid}\nlinked_action_id: {created.details['action_id']}\n".encode()
    linked = note.read_bytes()
    assert linked == _as_restored(original, _stamp_line(linked, "updated")).replace(
        b"session: notes-owner\n", b"session: notes-owner\n" + receipt), \
        "the note keeps its front matter and body, gains the receipt and a new updated stamp, and nothing else"
    assert f"Linked note: {note}" in created.response_text


def test_a_note_changed_just_before_its_receipt_is_written_keeps_the_change_and_gets_no_receipt(world, calendar, monkeypatch) -> None:
    session = "notes-receipt-changed"
    original = _note_bytes("Venue plan", "[action] book the venue call Friday at 15:00 Europe/Athens")
    note = world.notes / "venue.md"
    note.write_bytes(original)
    proposal = _propose("book the venue call", session_id=session)
    fired = _stage_edits(monkeypatch, before_write=(lambda path: path == note, lambda path: _append(path, EDIT)))

    created = _approve(proposal, session_id=session)

    assert fired == ["before_write"], "the note was changed right before its receipt was written"
    assert note.read_bytes() == original + EDIT, "the change is kept and no receipt is written over it"
    assert sorted(path.name for path in world.notes.iterdir()) == ["venue.md"]
    assert created.details["uid"] in calendar.snapshot()[CALENDAR], "the event itself is created and verified"
    assert created.details.get("note_link_failed") is True, created.response_text
    assert "PARTIAL" in created.response_text and "Linked note:" not in created.response_text


def test_an_approved_event_does_not_write_its_receipt_into_the_workspace_file_behind_a_note_link(world, calendar) -> None:
    session = "notes-receipt-link"
    plan = world.docs / "venue.md"
    plan.write_bytes(_note_bytes("Venue plan", "[action] book the venue call Friday at 15:00 Europe/Athens"))
    _file_link(world.notes / "venue.md", plan)
    proposal = _propose("book the venue call", session_id=session)
    workspace_before = _tree(world.workspace)

    created = _approve(proposal, session_id=session)

    assert plan.read_bytes() == _note_bytes("Venue plan", "[action] book the venue call Friday at 15:00 Europe/Athens"), \
        "the workspace file behind the note link carries no receipt"
    assert _tree(world.workspace) == workspace_before
    assert created.details["uid"] in calendar.snapshot()[CALENDAR], "the event itself is created and verified"
    assert created.details.get("note_link_failed") is True, created.response_text
    assert "PARTIAL" in created.response_text and "Linked note:" not in created.response_text


def test_a_note_swapped_for_a_link_while_its_event_awaits_approval_does_not_carry_the_receipt_elsewhere(world, calendar) -> None:
    session = "notes-receipt-swap"
    note = world.notes / "venue.md"
    note.write_bytes(_note_bytes("Venue plan", "[action] book the venue call Friday at 15:00 Europe/Athens"))
    proposal = _propose("book the venue call", session_id=session)
    other = world.docs / "budget.md"
    other.write_bytes(_note_bytes("Budget", "numbers that are not a note"))
    note.unlink()
    _file_link(note, other)
    workspace_before = _tree(world.workspace)

    created = _approve(proposal, session_id=session)

    assert other.read_bytes() == _note_bytes("Budget", "numbers that are not a note"), \
        "the file the note was swapped for carries no receipt"
    assert _tree(world.workspace) == workspace_before
    assert created.details["uid"] in calendar.snapshot()[CALENDAR]
    assert created.details.get("note_link_failed") is True, created.response_text
