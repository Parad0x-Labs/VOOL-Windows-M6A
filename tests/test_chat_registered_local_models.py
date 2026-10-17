"""The chat picker names registered installed lanes, not Auto's profile mix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.chat_page_js_harness import DOM, run_node, script, slice_source


def _render(models: list[dict], *, disabled: bool = False) -> dict:
    source = slice_source(script(), "function localModelRows(", "// ---- Boot hydration")
    payload = json.dumps({"models": models, "local_models_disabled": disabled})
    result = run_node(
        DOM
        + """
const MODEL_LABELS = {vool: 'VOOL Auto', 'vool-local-only': 'VOOL Auto Local Only'};
let modelValue = 'vool', localModelIds = new Set();
let reflects = 0;
function reflectModel() { reflects++; }
function positionPopover() {}
const requests = [];
globalThis.fetch = async (url) => {
  requests.push(url);
  return {ok: true, json: async () => (PAYLOAD)};
};
""".replace("PAYLOAD", payload)
        + source
        + """
await renderLocalModels();
const list = document.getElementById('modelPop').children[0];
const rows = list ? list.children.slice(1) : [];
out({
  requests, ids: [...localModelIds], selected: modelValue, reflects,
  rows: rows.map(row => ({id: row.getAttribute('data-model')}))
});
"""
    )
    assert not result["errors"], result["errors"]
    return result


def test_registered_local_lane_is_selectable_outside_autos_default_profile() -> None:
    tag = "vool-qwen3-4b-instruct:nothink-m6b"
    result = _render([
        {"model_name": "qwen3:4b", "registered": True, "enabled": True},
        {"model_name": tag, "registered": True, "enabled": True, "certification_state": "verified"},
    ])
    assert result["requests"] == ["/api/models/local"]
    assert result["ids"] == ["qwen3:4b", tag]
    assert [row["id"] for row in result["rows"]] == result["ids"]
    assert result["selected"] == "vool"  # Discovery does not change Auto or pin a model.


@pytest.mark.parametrize("entry", [
    {"model_name": "unregistered", "registered": False, "enabled": True},
    {"model_name": "disabled", "registered": True, "enabled": False},
    {"model_name": "missing-flags"},
    {"model_name": "wrong-flags", "registered": "false", "enabled": "true"},
])
def test_unregistered_or_disabled_local_lanes_are_not_offered(entry: dict) -> None:
    assert _render([entry])["ids"] == []


def test_disabled_local_policy_never_offers_a_cached_model() -> None:
    result = _render(
        [{"model_name": "qwen3:4b", "registered": True, "enabled": True}], disabled=True
    )
    assert result["ids"] == []
    assert result["rows"] == []


def test_local_discovery_does_not_grant_answer_authorship() -> None:
    result = _render([
        {"model_name": "qwen3:4b", "registered": True, "enabled": True, "certification_state": "degraded"}
    ])
    assert result["ids"] == ["qwen3:4b"]  # The existing server authorship gate still owns eligibility.
    assert result["selected"] == "vool"


def test_opening_the_model_menu_refreshes_local_inventory_without_reloading_chat() -> None:
    source = slice_source(script(), "function initComposerControls(", "// ---- Settings modal")
    result = run_node(
        DOM
        + """
let localRefreshes = 0, credentialRefreshes = 0;
function renderLocalModels() { localRefreshes++; }
function refreshCredentials() { credentialRefreshes++; }
function positionPopover() {}
function reflectMode() {}
function reflectModel() {}
function closeBypassModal() {}
function confirmBypass() {}
function revokeBypass() {}
"""
        + source
        + """
initComposerControls();
const button = document.getElementById('modelBtn');
button.click();  // Open.
button.click();  // Close without probing again.
button.click();  // The next open sees registration/certification changes in Settings.
document.getElementById('modeBtn').click();
out({localRefreshes, credentialRefreshes});
"""
    )
    assert not result["errors"], result["errors"]
    assert result["localRefreshes"] == result["credentialRefreshes"] == 2


def test_registered_local_picker_renders_exact_tag_in_a_browser(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    tag = "vool-qwen3-4b-instruct:nothink-m6b"
    source = slice_source(script(), "function localModelRows(", "// ---- Boot hydration")
    html = """<!doctype html><meta charset="utf-8"><title>Local model picker regression</title>
<div id="modelPop"><div class="pop-hint">Auto keeps its existing profile.</div></div>
<button id="modelBtn">Models</button><script>
const MODEL_LABELS = {vool: 'VOOL Auto', 'vool-local-only': 'VOOL Auto Local Only'};
let modelValue = 'vool', localModelIds = new Set();
function reflectModel() {}
function positionPopover() {}
""" + source + "\nrenderLocalModels();</script>"
    inventory = {
        "models": [
            {"model_name": "qwen3:4b", "registered": True, "enabled": True},
            {"model_name": tag, "registered": True, "enabled": True},
            {"model_name": "unregistered", "registered": False, "enabled": True},
        ],
        "local_models_disabled": False,
    }
    requests: list[str] = []

    def respond(route) -> None:
        path = route.request.url.removeprefix("http://127.0.0.1:11435")
        requests.append(path)
        if path == "/chat":
            route.fulfill(status=200, content_type="text/html", body=html)
        elif path == "/api/models/local":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(inventory))
        else:
            route.fulfill(status=404, body="Not found")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 900, "height": 400})
            page.route("**/*", respond)
            page.goto("http://127.0.0.1:11435/chat")
            row = page.locator('[data-model="' + tag + '"]')
            row.wait_for(state="visible")
            assert tag in row.inner_text()
            assert page.locator('[data-model="unregistered"]').count() == 0
            assert page.locator('#modelPop button').count() == 2
            assert "/api/models/local" in requests and "/api/tags" not in requests
            page.screenshot(path=str(tmp_path / "registered-local-picker.png"))
            inventory["models"][1]["enabled"] = False
            page.evaluate("renderLocalModels()")
            assert page.locator('[data-model="' + tag + '"]').count() == 0
            assert page.locator('#modelPop button').count() == 1
            inventory["local_models_disabled"] = True
            page.evaluate("renderLocalModels()")
            assert page.locator('#modelPop button').count() == 0
        finally:
            browser.close()
