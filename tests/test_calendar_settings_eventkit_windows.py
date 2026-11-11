"""Settings > Calendars offers Apple Calendar (EventKit) only on macOS.

EventKit reads calendars only on a Mac; offered on Windows, adding it worked but finding calendars failed. The account
API still lists eventkit on every OS (tests/pa_beta_gate/test_pc_settings_calendars.py); only the page's Source list
leaves it out off macOS, and the macOS page is unchanged.
"""
from __future__ import annotations

import sys

from core import calendar_settings_fragment as fragment

_OFFER_ALL = "Object.keys(providers).forEach(function(key){"
_OFFER_NO_EVENTKIT = "Object.keys(providers).filter(function(key){ return key !== 'eventkit'; }).forEach(function(key){"


def test_windows_settings_page_leaves_apple_calendar_out_of_the_source_list(monkeypatch):
    from core.vool_settings_page import render_vool_settings_html

    monkeypatch.setattr(sys, "platform", "win32")
    html = render_vool_settings_html()
    assert html.count(_OFFER_NO_EVENTKIT) == 1
    assert _OFFER_ALL not in html


def test_mac_settings_fragment_is_unchanged_and_still_offers_apple_calendar(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    rendered = fragment.render_calendar_settings_fragment()
    assert rendered == "<style>" + fragment._CSS + "</style><script>" + fragment._JS + "</script>"
    assert rendered.count(_OFFER_ALL) == 1
    assert _OFFER_NO_EVENTKIT not in rendered
