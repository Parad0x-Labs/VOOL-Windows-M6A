"""Settings polish found by the Windows claims sweep (fix plan section 10).

- Privacy said keys live in "the system keychain/encrypted store" on every host. Where keys live is the runtime's own
  answer: the credential list reports the live backend, and VOOL's vault is named as the vault.
- About > App updates stayed on "Loading…" after the status had been read, because an idle status carries an empty
  message; an unconfigured updater never said why.
- Calendars and Notifications stayed on "Loading…" for as long as a request went unanswered: their requests now give
  up after a timeout and say VOOL did not answer.
- The built-in Media Studio card painted light text on a white background in the dark Settings page.

The scripts are the same on every host; what changes is decided by what the runtime reports.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from core import calendar_settings_fragment, notification_settings_fragment, settings_extras_fragment
from core.addon_store_fragment import render_addon_store_fragment
from core.i18n.catalog import CATALOGS_DIR, catalog_for
from tests.chat_page_js_harness import DOM, run_node, slice_source

NEW_KEYS = ("update.idle", "vx.privacy.p3.vault")


def _node(program: str, payload: dict) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run the panel scripts")
    out = subprocess.run([node, "-e", program], input=json.dumps(payload), capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=True)
    return json.loads(out.stdout)


def _script(fragment_html: str) -> str:
    return re.match(r"<style>.*?</style><script>(.*?)</script>", fragment_html, re.DOTALL).group(1)


# --------------------------------------------------------------------------------------------
# About > App updates
# --------------------------------------------------------------------------------------------

def _update_status_text(status: dict | None, error: str = "") -> str:
    from core.vool_settings_page import render_vool_settings_html

    source = next(part for part in re.findall(r"<script[^>]*>(.*?)</script>", render_vool_settings_html(), re.DOTALL)
                  if "function widgetUpdates(stack)" in part)
    code = slice_source(source, "function widgetUpdates(stack)", "function widgetBuild(stack)")
    sources = {"/api/update/status": status} if status is not None else {}
    errors = {"/api/update/status": error} if error else {}
    result = run_node(DOM + f"""
const state = {{sources:{json.dumps(sources)},sourceErr:{json.dumps(errors)}}};
const T = (key, fallback) => fallback;
const el = (tag, cls, text) => {{const node = new El(tag); node.textContent = text; return node;}};
const readSource = async () => {{}};
const stack = new El();
""" + code + """
widgetUpdates(stack);
out({status: stack.children[0].textContent});
""")
    assert result["errors"] == []
    return result["status"]


def test_an_idle_update_status_with_no_message_is_not_loading():
    assert _update_status_text({"configured": True, "phase": "idle", "message": ""}) == "No update activity right now."


def test_an_unconfigured_updater_says_why():
    plain = "Updates are not set up in this build."
    status = {"configured": False, "unavailable_plain": plain, "phase": "idle", "message": ""}
    assert _update_status_text(status) == plain


def test_update_status_still_loads_first_and_keeps_its_own_message_and_errors():
    assert _update_status_text(None) == "Loading…"
    assert _update_status_text({"configured": True, "phase": "ready", "message": "Version 0.7.2 ready"}) == "Version 0.7.2 ready"
    assert _update_status_text(None, error="HTTP 500") == "HTTP 500"


# --------------------------------------------------------------------------------------------
# Calendars and Notifications: a request that is never answered
# --------------------------------------------------------------------------------------------

_HANG_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function el(tag){ return {tagName: tag, children: [], className: '', _t: '', style: {}, attrs: {}, dataset: {},
  set textContent(v){ this._t = v; if (v === '') this.children = []; }, get textContent(){ return this._t; },
  appendChild(c){ this.children.push(c); return c; }, setAttribute(k, v){ this.attrs[k] = v; },
  addEventListener(){}, querySelector(){ return {checked: false}; } }; }
global.window = globalThis; global.document = {createElement: el, documentElement: el('html'), body: {contains(){ return true; }},
  querySelectorAll(){ return []; }, addEventListener(){}};
global.setInterval = function(){ return 0; };
// Only the request timeout is fast-forwarded; every other timer keeps its real delay.
const realTimeout = setTimeout;
const delays = [];
global.setTimeout = function(fn, ms){ delays.push(ms); return realTimeout(fn, ms >= 10000 ? 5 : ms); };
// input.answer: the runtime answers every GET with an empty, healthy state. Otherwise it never answers, and the
// promise settles only if the request is aborted.
const ANSWERS = {'/api/calendar/accounts': {ok: true, accounts: [], bindings: [], sync: {}},
  '/api/notifications/preferences': {ok: true, preferences: {quiet_hours: {enabled: false}, sound: true, lock_screen: 'title_only', late_grace_minutes: 5}},
  '/api/notifications/native/status': {ok: true, supported: false, bell_only_windows: true, requests: []}};
global.fetch = function(url, options){ return new Promise(function(resolve, reject){
  if (input.answer) { resolve({ok: true, status: 200, json(){ return Promise.resolve(ANSWERS[url] || {ok: true}); }}); return; }
  if (options && options.signal) options.signal.addEventListener('abort', function(){ reject(new Error('aborted')); });
}); };
new Function(input.panel)();
const host = el('div');
window[input.api].mountInto(host);
function walk(n, out){ if (n._t) out.push(n._t); (n.children || []).forEach(c => walk(c, out)); return out; }
realTimeout(() => { process.stdout.write(JSON.stringify({texts: walk(host, []), delays: delays})); }, 200);
"""


@pytest.mark.parametrize(
    ("fragment", "api", "loading"),
    [
        (notification_settings_fragment.render_notification_settings_fragment, "VoolNotificationSettings", "Loading…"),
        (calendar_settings_fragment.render_calendar_settings_fragment, "VoolCalendarSettings", "Loading calendar accounts…"),
    ],
)
def test_a_panel_whose_request_is_never_answered_stops_loading(fragment, api, loading):
    result = _node(_HANG_HARNESS, {"panel": _script(fragment()), "api": api})
    assert 10000 in result["delays"], result
    assert "VOOL did not answer; nothing was changed." in result["texts"], result
    assert loading not in result["texts"], result


@pytest.mark.parametrize(
    ("fragment", "api", "loading"),
    [
        (notification_settings_fragment.render_notification_settings_fragment, "VoolNotificationSettings", "Loading…"),
        (calendar_settings_fragment.render_calendar_settings_fragment, "VoolCalendarSettings", "Loading calendar accounts…"),
    ],
)
def test_a_panel_that_has_loaded_no_longer_says_loading(fragment, api, loading):
    """The sweep saw the panel painted with "Loading…" still above it: a successful load never cleared the line."""
    result = _node(_HANG_HARNESS, {"panel": _script(fragment()), "api": api, "answer": True})
    assert loading not in result["texts"], result
    assert "VOOL did not answer; nothing was changed." not in result["texts"], result
    assert len(result["texts"]) > 2, result


# --------------------------------------------------------------------------------------------
# Privacy: where keys live
# --------------------------------------------------------------------------------------------

_PRIVACY_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const found = {};
function el(tag){ return {tagName: tag, children: [], className: '', id: '', style: {}, innerHTML: '', textContent: '',
  appendChild(c){ this.children.push(c); c.parentNode = this; return c; }, addEventListener(){},
  querySelector(sel){ return found[sel] || (found[sel] = {textContent: null, addEventListener(){}}); } }; }
global.window = globalThis;
global.document = {readyState: 'complete', createElement: el, body: el('body'), getElementById(){ return null; }, addEventListener(){}};
global.fetch = function(url){ return Promise.resolve({ok: true, json(){
  return Promise.resolve(url === '/api/settings/credentials' ? {credentials: [], backend: input.backend} : {}); }}); };
new Function(input.panel)();
const host = el('div');
window.VoolSettingsExtras.mountInto(host, 'privacy');
setTimeout(() => { process.stdout.write(JSON.stringify({where: (found['.vs-keys-where'] || {}).textContent,
  html: host.children[0].innerHTML})); }, 50);
"""


def _privacy(backend: str) -> dict:
    return _node(_PRIVACY_HARNESS, {"panel": _script(settings_extras_fragment.render_settings_extras_fragment()),
                                    "backend": backend})


def test_privacy_names_the_vault_when_keys_are_in_the_vault():
    result = _privacy("vault")
    assert result["where"] == catalog_for("en").text("vx.privacy.p3.vault")
    assert "keychain" not in result["where"]


def test_privacy_keeps_the_keychain_text_when_keys_are_in_the_keychain():
    result = _privacy("keychain")
    assert result.get("where") is None
    assert catalog_for("en").text("vx.privacy.p3") in result["html"]


def test_the_credential_list_reports_its_backend_and_no_secret():
    from core.command_registry.groups.convergence import _handle_credentials_list
    from core.credential_store import active_backend

    data = _handle_credentials_list(None, None).data
    assert data["backend"] == active_backend() and data["backend"] in {"keychain", "vault"}
    assert set(data) == {"credentials", "backend"}


# --------------------------------------------------------------------------------------------
# Media Studio card, and the catalog
# --------------------------------------------------------------------------------------------

def test_the_media_studio_card_uses_the_dark_panel_colour():
    css = render_addon_store_fragment()
    rule = re.search(r"\.media-studio-card\{([^}]*)\}", css).group(1)
    assert "background:var(--panel" in rule
    assert "#fff" not in rule


def test_the_new_texts_exist_in_every_shipped_language():
    for path in sorted(CATALOGS_DIR.glob("*.json")):
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        for key in NEW_KEYS:
            assert messages.get(key), (path.name, key)
    assert "keychain" not in catalog_for("en").text("vx.privacy.p3.vault").lower()
