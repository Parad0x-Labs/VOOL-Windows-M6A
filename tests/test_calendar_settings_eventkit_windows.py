"""Settings > Calendars offers Apple Calendar (EventKit) only on macOS.

EventKit reads calendars only on a Mac; offered on Windows, adding it worked but finding calendars failed. The server's
calendar view now says which sources this host offers (offered_providers); the page is the same on every host and
offers that list. The providers map and the account routes still list eventkit on every OS
(tests/pa_beta_gate/test_pc_settings_calendars.py).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from core import calendar_settings_fragment as fragment
from core.operator import calendar_accounts


def test_off_macos_the_offered_sources_leave_apple_calendar_out() -> None:
    assert calendar_accounts.offered_provider_keys("win32") == ["google", "graph", "caldav"]
    assert calendar_accounts.offered_provider_keys("linux") == ["google", "graph", "caldav"]


def test_on_macos_every_source_is_offered() -> None:
    assert calendar_accounts.offered_provider_keys("darwin") == list(calendar_accounts.PROVIDERS)


def test_the_view_carries_the_offer_and_still_lists_every_provider(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(calendar_accounts, "list_accounts", lambda **k: [])
    monkeypatch.setattr(calendar_accounts, "list_credential_bindings", lambda: [])
    view = calendar_accounts.settings_view()
    assert "eventkit" in view["providers"]
    assert view["offered_providers"] == ["google", "graph", "caldav"]


def test_the_fragment_is_the_same_on_every_host(monkeypatch) -> None:
    rendered = {}
    for host in ("darwin", "win32", "linux"):
        monkeypatch.setattr(sys, "platform", host)
        rendered[host] = fragment.render_calendar_settings_fragment()
    assert rendered["darwin"] == rendered["win32"] == rendered["linux"]
    assert "view.offered_providers" in rendered["win32"]


_HARNESS = """
const src = %s;
const start = src.indexOf("var offered = ");
const end = src.indexOf("\\n", src.indexOf("offered.forEach(", start));
const block = src.slice(start, end);
function run(view) {
  const options = [];
  const providers = view.providers;
  const h = () => ({ value: '' });
  const providerSelect = { appendChild(o) { options.push(o.value); } };
  new Function('view', 'providers', 'h', 'providerSelect', block)(view, providers, h, providerSelect);
  return options;
}
const providers = {google: {label: 'G'}, graph: {label: 'M'}, caldav: {label: 'C'}, eventkit: {label: 'A'}};
console.log(JSON.stringify({
  windows: run({providers, offered_providers: ['google', 'graph', 'caldav']}),
  mac: run({providers, offered_providers: ['google', 'graph', 'caldav', 'eventkit']}),
  older_server: run({providers}),
  unknown_key: run({providers, offered_providers: ['google', 'nope']}),
}));
"""


def test_the_source_list_follows_the_offer() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run the Calendars panel script")
    script = _HARNESS % json.dumps(fragment._JS)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["windows"] == ["google", "graph", "caldav"]
    assert result["mac"] == ["google", "graph", "caldav", "eventkit"]
    assert result["older_server"] == ["google", "graph", "caldav", "eventkit"]
    assert result["unknown_key"] == ["google"]
