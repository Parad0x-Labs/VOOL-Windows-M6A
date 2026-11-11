"""Settings > Notifications on Windows (bug B10).

This port has no Windows notification backend: the window host's bridge starts only on darwin
(installer/bundle/native_notifications.py start_for_host) and the status route reports supported:false there, and the
lock-screen choice only shapes requests handed to that bridge (core/operator/native_notifications.py _content). So the
Windows panel must not show macOS text or the lock-screen picker, while the macOS panel stays exactly as it was.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys

import pytest

from core import notification_settings_fragment as fragment
from core.i18n.catalog import catalog_for
from installer.bundle import native_notifications as bridge

# sha256 of the panel's render at 379e57f7, before the Windows variant existed: the macOS output must not move.
MAC_RENDER_SHA256 = "74ac19b81364d972511f5fa15e5534b7806bf21350f30f3a8626116711fe1c7c"
WINDOWS_KEYS = ("vns.title.prefs.windows", "vns.saved.quiet.windows", "vns.why.windows")


def _panel_script(html: str) -> str:
    return re.match(r"<style>.*?</style><script>(.*?)</script>", html, re.DOTALL).group(1)


def test_mac_render_is_unchanged():
    html = fragment.render_notification_settings_fragment(windows=False)
    assert hashlib.sha256(html.encode("utf-8")).hexdigest() == MAC_RENDER_SHA256


def test_default_follows_the_host_check(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert fragment.render_notification_settings_fragment() == fragment.render_notification_settings_fragment(windows=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert fragment.render_notification_settings_fragment() == fragment.render_notification_settings_fragment(windows=False)


def test_windows_has_no_notification_bridge(monkeypatch):
    """The premise: no OS channel exists on Windows, so a lock-screen choice there would be a dead control."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert bridge.start_for_host(api_origin="http://127.0.0.1:1") is None


def test_windows_variant_fails_loudly_if_the_mac_script_drifts(monkeypatch):
    monkeypatch.setattr(fragment, "_JS", fragment._JS.replace(fragment._WIN_LOCK_END, ""))
    with pytest.raises(RuntimeError):
        fragment.render_notification_settings_fragment(windows=True)


def test_windows_keys_exist_in_every_locale_and_name_no_mac():
    for tag in ("en", "de", "ja", "ar", "zh-Hans"):
        catalog = catalog_for(tag)
        for key in WINDOWS_KEYS:
            assert key in catalog.locale_keys, (tag, key)
            assert "macOS" not in catalog.text(key), (tag, key)


_DOM_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function el(tag){ return {tagName: tag, children: [], className: '', _t: '', style: {}, attrs: {},
  set textContent(v){ this._t = v; if (v === '') this.children = []; }, get textContent(){ return this._t; },
  appendChild(c){ this.children.push(c); return c; }, setAttribute(k, v){ this.attrs[k] = v; },
  addEventListener(){}, querySelector(){ return {checked: false}; } }; }
global.window = globalThis; global.document = {createElement: el, documentElement: el('html'), body: {contains(){ return true; }},
  querySelectorAll(){ return []; }, addEventListener(){}};
global.setInterval = function(){ return 0; };
const prefs = {native_notifications: true, quiet_hours: {enabled: false}, sound: true, lock_screen: 'title_only', late_grace_minutes: 5};
const native = {ok: true, supported: input.supported, connected: true, authorization: 'authorized', explanation: 'MAC-EXPLANATION', requests: []};
global.fetch = function(url){ return Promise.resolve({ok: true, status: 200, json(){
  return Promise.resolve(url.indexOf('preferences') >= 0 ? {ok: true, preferences: prefs} : native); }}); };
if (input.messages) global.VOOLT = function(key){ return Object.prototype.hasOwnProperty.call(input.messages, key) ? input.messages[key] : key; };
new Function(input.panel)();
const host = el('div');
window.VoolNotificationSettings.mountInto(host);
function walk(n, out){ if (n._t) out.push(n._t); if (n.tagName === 'select') out.push('<select>'); (n.children || []).forEach(c => walk(c, out)); return out; }
setTimeout(() => { process.stdout.write(JSON.stringify(walk(host, []))); }, 50);
"""


def _painted(*, windows: bool, supported: bool, locale: str | None = None) -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run the panel script")
    payload = {
        "panel": _panel_script(fragment.render_notification_settings_fragment(windows=windows)),
        "supported": supported,
        "messages": catalog_for(locale).client_bundle()["messages"] if locale else None,
    }
    out = subprocess.run([node, "-e", _DOM_HARNESS], input=json.dumps(payload), capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=True)
    return json.loads(out.stdout)


def test_windows_panel_shows_no_macos_text_and_no_lock_screen_picker():
    texts = _painted(windows=True, supported=False)
    assert not [t for t in texts if "macOS" in t], texts
    assert "MAC-EXPLANATION" not in texts
    assert "<select>" not in texts
    assert "Quiet hours and sound" in texts
    assert "The bell keeps every VOOL alert. VOOL does not send Windows notifications yet." in texts
    # quiet hours and sound still gate the in-page chime on Windows, and the late-alert setting gates the bell
    assert "Quiet hours from" in texts and "Notification sound" in texts
    assert "Show an alert that comes due late, up to" in texts


def test_windows_panel_reads_its_text_from_the_catalog():
    texts = _painted(windows=True, supported=False, locale="de")
    de = catalog_for("de")
    assert de.text("vns.title.prefs.windows") in texts
    assert de.text("vns.why.windows") in texts
    assert not [t for t in texts if "macOS" in t], texts


def test_mac_panel_still_paints_the_macos_controls():
    texts = _painted(windows=False, supported=True)
    for text in ("macOS notifications", "A macOS notification may show", "Quiet hours, sound and what notifications show",
                 "MAC-EXPLANATION", "<select>"):
        assert text in texts
