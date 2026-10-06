"""The chat header carries a cloud connection pill and Settings a Test-connection button,
both wired to the real /api/cloud/status and /api/cloud/test endpoints."""
from __future__ import annotations

from core.vool_chat_page import render_vool_chat_html
from tests.chat_page_js_harness import run_node, slice_source

HTML = render_vool_chat_html()


def test_pill_element_and_state_classes_present():
    assert 'id="cloudPill"' in HTML
    for cls in ("#cloudPill.ok", "#cloudPill.bad", "#cloudPill.warn"):
        assert cls in HTML, cls


def test_pill_is_driven_by_the_status_endpoint():
    assert "/api/cloud/status" in HTML
    assert "function refreshCloudStatus" in HTML
    assert "setInterval(refreshCloudStatus, 60000)" in HTML
    # Every real state maps to pill text (no invented labels).
    for state in ("ok", "failed", "untested", "no_key"):
        assert state in HTML, state


def test_saving_a_key_live_probes_it():
    assert "refreshCloudStatus(true)" in HTML  # ?probe=1 after a fresh save


def test_settings_test_button_hits_the_test_endpoint():
    assert 'id="orTest"' in HTML
    assert "function testCloudConnection" in HTML
    assert "/api/cloud/test" in HTML


def test_pill_never_prints_a_key():
    # The pill text set is a fixed vocabulary; no key material or digest is ever rendered.
    assert "key_digest" not in HTML
    # sk-or- appears only as a prefix in the placeholder hint and the client-side detectProvider
    # (prefix matching), never as an actual key. Strip those allowed references, then assert none.
    stripped = (HTML
                .replace("sk-or-... / sk-ant-... / sk-proj-...", "")
                .replace("'sk-or-'", "").replace("'sk-ant-'", "").replace("'sk-proj-'", ""))
    # A real key would be sk-or- followed by many chars; the bare prefixes above are the only ones left.
    import re as _re
    assert not _re.search(r"sk-or-[A-Za-z0-9]{10,}", stripped)


def test_no_cloud_key_does_not_claim_a_local_only_policy():
    rendering = slice_source(HTML, "function renderConnections(conns)", "function setCloudWorking(on)")
    result = run_node(
        """
let lastConnectionRows = [];
let route = {kind: 'auto', provider: ''};
function chatRoute() { return route; }
function pageT(key, fallback) { return fallback; }
const cloudPillEl = {
  children: [], title: '', classList: {remove() {}},
  set innerHTML(value) { this.children = []; },
  appendChild(child) { this.children.push(child); },
};
const document = {createElement() { return {textContent: '', className: ''}; }};
"""
        + rendering
        + """
renderConnections([]);
const noKey = {text: cloudPillEl.children.map(c => c.textContent).join(''), title: cloudPillEl.title};
route = {kind: 'local-only', provider: ''};
renderConnections([]);
const local = {text: cloudPillEl.children.map(c => c.textContent).join(''), title: cloudPillEl.title};
console.log(JSON.stringify({noKey, local}));
"""
    )
    assert result['noKey']['text'] == 'No cloud key'
    assert 'running fully local' not in result['noKey']['title']
    assert 'does not establish Local Only policy' in result['noKey']['title']
    assert result['local']['text'] == 'Local'
    assert 'cloud blocked' in result['local']['title']
    assert 'data-i18n="header.cloud_local_only"' not in HTML.split('id="cloudPill"', 1)[1].split('</button>', 1)[0]
