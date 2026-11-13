"""Windows refuses to add an Apple Calendar (EventKit) account, before anything is stored.

Settings > Calendars already leaves EventKit out of the Source list off a Mac
(tests/test_calendar_settings_eventkit_windows.py), but the add route still accepted it: a page or
script that posted provider "eventkit" got an account whose calendars could never be found. On Windows
the account authority now refuses it with a recovery step (add iCloud through CalDAV). CalDAV and the
other sources are unchanged; macOS and Linux are unchanged.
"""
from __future__ import annotations

import sys

import pytest

from core.operator import calendar_accounts


def _no_storage():
    raise AssertionError("a refused add must not open the database")


def test_windows_refuses_eventkit_and_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    for spelling in ("eventkit", " EventKit "):
        refused = calendar_accounts.add_account(provider=spelling, label="This PC", get_connection_fn=_no_storage)
        assert refused["ok"] is False, refused
        assert refused["reason"] == "provider_not_on_this_host", refused
        assert "CalDAV" in refused["detail"] and "macOS" in refused["detail"], refused


@pytest.mark.parametrize("provider", ["google", "graph", "caldav"])
def test_windows_still_accepts_the_other_sources_up_to_storage(monkeypatch, provider) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    class Reached(Exception):
        pass

    def storage():
        raise Reached

    with pytest.raises(Reached):
        calendar_accounts.add_account(provider=provider, base_url="https://caldav.example.test", get_connection_fn=storage)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_off_windows_eventkit_still_reaches_storage(monkeypatch, host) -> None:
    monkeypatch.setattr(sys, "platform", host)

    class Reached(Exception):
        pass

    def storage():
        raise Reached

    with pytest.raises(Reached):
        calendar_accounts.add_account(provider="eventkit", get_connection_fn=storage)
