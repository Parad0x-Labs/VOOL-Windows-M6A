"""Settings > Models & Providers names the local-models card after the machine it runs on.

The "What will answer" row shows one card for local models. It was titled "This Mac" on every
host, so a Windows operator was told about a Mac they do not have. The runtime that serves
/settings is the machine whose local models answer, so the page decides the name when it is
served: "This Mac" on macOS, where the page stays exactly as it was, and "This computer" on any
other host, including one this code does not recognise.
"""
from __future__ import annotations

import re
import sys
from urllib.parse import unquote

import pytest

from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get
from tests._windows_served_words import MAC_CARD, NEUTRAL_CARD, as_the_mac_page
from tests.chat_page_js_harness import DOM, run_node, slice_source


def _get_settings(ui_locale: str) -> str:
    res = dispatch_get(
        path="/settings", query={"ui_locale": [ui_locale]}, runtime=RuntimeServices(display_name="VOOL"),
        model_name="vool", client_host="127.0.0.1",
    )
    assert res.status == 200
    return res.body.decode("utf-8") if isinstance(res.body, bytes) else res.body


def _served_settings(monkeypatch, host_platform: str, ui_locale: str = "en") -> str:
    """GET /settings as a runtime on ``host_platform`` serves it.

    The route is served once on the real platform first, so every module it imports lazily is
    imported under the real host and nothing platform-dependent is cached from the simulation.
    The simulated platform is restored as soon as the page is rendered.
    """
    _get_settings(ui_locale)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", host_platform)
        return _get_settings(ui_locale)


@pytest.mark.parametrize(
    ("host_platform", "ui_locale"),
    [("win32", "en"), ("win32", "de"), ("linux", "en"), ("cygwin", "en")],
)
def test_a_host_that_is_not_a_mac_gets_this_computer_and_nothing_else_changes(monkeypatch, host_platform, ui_locale) -> None:
    mac_page = _served_settings(monkeypatch, "darwin", ui_locale)
    page = _served_settings(monkeypatch, host_platform, ui_locale)

    assert NEUTRAL_CARD in page
    assert "'This Mac'" not in page
    # macOS keeps its own wording; apart from the card's title, a host differs only by its own Windows words.
    assert page.count(NEUTRAL_CARD) == 1 and MAC_CARD in mac_page
    assert "'This computer'" not in mac_page
    assert as_the_mac_page(page, host_platform) == mac_page
    # The name is filled in on every host; the page never ships its own template marker.
    assert "__LOCAL_MACHINE_NAME__" not in page
    assert "__LOCAL_MACHINE_NAME__" not in mac_page


def _overview_cards(page: str) -> dict:
    """Run the served page's own Models & Providers overview and read back the cards it draws.

    Every read the overview makes is refused (404), the state a fresh or offline runtime gives it,
    so the card text comes from the served script alone.
    """
    scripts = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page, re.DOTALL))
    source = "\n".join([
        slice_source(scripts, "const el = (tag, cls, text) =>", "\n"),
        slice_source(scripts, "async function getJSON(url) {", "function renderWidget(row, host)"),
        slice_source(scripts, "let mpView = ", "// -- the review sheet"),
        slice_source(scripts, "const USEPOD_DISCOVERY_URL = ", "\n"),
        slice_source(scripts, "function widgetModelsOverview(stack) {", "function mpDrawDetail("),
    ])
    result = run_node(DOM + r"""
const reads = [];
globalThis.fetch = async (url) => { reads.push(url); return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }; };
""" + source + r"""
const stack = el('div', 'stack');
widgetModelsOverview(stack);
for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
const all = (n) => [n, ...(n.children || []).flatMap(all)];
const cls = (n, c) => String(n.className || '').split(' ').includes(c);
const cards = all(stack).filter((n) => cls(n, 'mp-card'));
out({
  reads,
  titles: cards.map((c) => all(c).find((n) => cls(n, 'mp-card-title')).textContent).map(encodeURIComponent),
  local: (cards.length ? all(cards[0]).map((n) => n.__text).filter(Boolean) : []).map(encodeURIComponent),
});
""")
    assert result["errors"] == [], result["errors"]
    # The overview really ran: it asked for its reads, and every one was refused.
    assert "/api/cloud/model" in result["reads"]
    # Card text crosses as ASCII: a Windows runner decodes node's output as cp1252 unless Python
    # runs in UTF-8 mode, which would mangle the title's middle dot.
    return {"titles": [unquote(t) for t in result["titles"]], "local": [unquote(t) for t in result["local"]]}


def test_the_local_card_the_operator_sees_is_named_for_the_host(monkeypatch) -> None:
    windows = _overview_cards(_served_settings(monkeypatch, "win32"))
    mac = _overview_cards(_served_settings(monkeypatch, "darwin"))

    assert windows["titles"] == ["This computer · ready", "UsePod", "OpenRouter"]
    assert windows["local"] == ["This computer · ready", "Local models answer without a network.", "View local models"]
    assert mac["titles"] == ["This Mac · ready", "UsePod", "OpenRouter"]
    assert mac["local"] == ["This Mac · ready", "Local models answer without a network.", "View local models"]
