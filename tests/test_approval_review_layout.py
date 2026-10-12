"""Real approval CSS/DOM/control code: readable diff, reachable buttons, no review grant.

The rendered chat markup and styles are unchanged by the harness. Only unrelated
page scripts and server/state dependencies are replaced; approval handlers run
verbatim. All networking is blocked. This is browser-component proof, not an
installed-app or native input claim.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from core.vool_chat_page import render_vool_chat_html
from tests.chat_page_js_harness import script, slice_source

CHAT = "openclaw:" + "c" * 20
TARGET = r"C:\Users\VOOLT\OneDrive\Desktop\VOOLTEST1\m6b-denied-canary-20261010.txt"
DIFF = "--- a/" + TARGET + "\n+++ b/" + TARGET + "\n@@ -0,0 +1,42 @@\n" + "\n".join(
    f"+M6B_SYNTHETIC_CANARY line {index:02d}" for index in range(41)
) + "\n+END_OF_COMPLETE_REVIEW"
APPROVAL = {
    "approval_id": "ap-review-only", "task_id": "task-review-only", "session_id": CHAT, "intent": "workspace.write_file",
    "action": "Run workspace.write_file", "affected_resources": [TARGET],
    "expected_side_effects": "create files", "reversible": True,
    "scope_options": ["once", "project"], "diff_preview": DIFF,
}

DEPENDENCIES = """
const displayedChat = '""" + CHAT + """';
const reviewState = {chatId: displayedChat, history: [], run: null, approvalToken: ''};
globalThis.reviewRequests = []; globalThis.reviewRuns = [];
function chatState() { return reviewState; }
function isDisplayed(id) { return id === displayedChat; }
function pageT(key, fallback) { return fallback; }
function pageTF(key, fallback, values) {
  return fallback.replace(/\\{([^}]+)\\}/g, (_, key) => values[key]);
}
function releaseApprovalTokenFor() { reviewState.approvalToken = ''; }
function setApprovalToken(id, token) { reviewState.approvalToken = token; }
async function postMode(body) {
  reviewRequests.push(body);
  return {ok: true, approval: Object.assign({}, reviewState.pendingApproval,
    {status: body.decision === 'deny' ? 'denied' : 'approved', scope: body.scope})};
}
async function loadActivityHistory() {}
function isChatBusy() { return false; }
function renderCard() {} function toast() {} function openProjectMenu() {}
function runTurn(...args) { reviewRuns.push(args); }
"""


@pytest.fixture(scope="module")
def review_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def review_page(review_browser):
    page = review_browser.new_page(viewport={"width": 1016, "height": 690})
    page.route("**/*", lambda route: route.abort())
    source = slice_source(script(), "// ---- Exact, expiring permission controls", "panelBtnEl.addEventListener('click', togglePanel);")
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", render_vool_chat_html(), flags=re.DOTALL)
    html = html.replace("</body>", "<script>" + DEPENDENCIES + source + "</script></body>")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(html)
    page.evaluate("approval => showPermBar(displayedChat, {approval})", APPROVAL)
    try:
        yield page
        assert errors == [], errors
    finally:
        page.close()


def screenshot(page, tmp_path, name):
    destination = Path(os.environ.get("VOOL_APPROVAL_REVIEW_EVIDENCE", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / (name + ".png")))


@pytest.mark.parametrize("width,height,sidebar", [(1016, 690, 240), (1280, 860, 240), (360, 640, 0), (1280, 860, 600)])
def test_the_complete_diff_has_a_wide_bounded_scroll_surface_and_reachable_controls(review_page, tmp_path, width, height, sidebar):
    page = review_page
    page.set_viewport_size({"width": width, "height": height})
    if sidebar:
        page.locator("#sidebar").evaluate("(el, width) => { el.style.flexBasis = width + 'px'; el.style.width = width + 'px'; }", sidebar)
    page.locator("#permReview").click()
    detail = page.locator("#permDetail")
    screenshot(page, tmp_path, f"review-{width}x{height}-side{sidebar}-top")
    metrics = detail.evaluate("""el => {
      const r = el.getBoundingClientRect(), bar = document.getElementById('permBar').getBoundingClientRect();
      return {width:r.width, height:r.height, barWidth:bar.width, top:r.top, bottom:r.bottom,
        scrollHeight:el.scrollHeight, clientHeight:el.clientHeight, display:getComputedStyle(el).display};
    }""")
    assert metrics["display"] == "block", metrics
    assert metrics["width"] >= metrics["barWidth"] - 85, metrics
    assert 60 <= metrics["height"] <= 162, metrics
    assert metrics["scrollHeight"] > metrics["clientHeight"], metrics
    assert detail.text_content().endswith(DIFF)
    assert 0 <= metrics["top"] < metrics["bottom"] <= height, metrics
    for composer in ("#input", "#send"):
        box = page.locator(composer).bounding_box()
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= height + 1, (composer, box)
    for control in ("#permOnce", "#permChat", "#permProject", "#permReview", "#permDeny"):
        page.locator(control).scroll_into_view_if_needed()
        box = page.locator(control).bounding_box()
        bar = page.locator("#permBar").bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (control, box)
        assert box["y"] >= bar["y"] and box["y"] + box["height"] <= min(height, bar["y"] + bar["height"]) + 1, (control, box, bar)
    detail.hover()
    page.mouse.wheel(0, 10000)
    page.wait_for_function("document.getElementById('permDetail').scrollTop > 0")
    assert detail.evaluate("el => el.scrollTop + el.clientHeight >= el.scrollHeight - 2")
    screenshot(page, tmp_path, f"review-{width}x{height}-side{sidebar}-complete-end")
    assert page.evaluate("reviewRequests.length") == 0
    assert page.evaluate("reviewRuns.length") == 0


def test_keyboard_review_and_scroll_do_not_send_an_approval(review_page):
    page = review_page
    page.locator("#permReview").focus()
    page.keyboard.press("Enter")
    detail = page.locator("#permDetail")
    assert detail.is_visible()
    assert detail.get_attribute("tabindex") == "0"
    detail.focus()
    page.keyboard.press("End")
    page.wait_for_function("document.getElementById('permDetail').scrollTop > 0")
    page.locator("#permReview").focus()
    page.keyboard.press("Space")
    assert detail.is_hidden()
    assert page.evaluate("reviewRequests.length") == 0


def test_review_keeps_server_scope_exclusions_and_untrusted_diff_as_literal_text(review_page):
    page = review_page
    approval = dict(APPROVAL, scope_options=["once"],
        diff_preview=DIFF + '\n+<script>globalThis.reviewInjection = true</script>',
        excluded_actions=[{"intent": "sandbox.run_command", "target": "synthetic command never executed"}])
    page.evaluate("approval => showPermBar(displayedChat, {approval})", approval)
    page.locator("#permReview").click()
    assert "sandbox.run_command" in page.locator("#permDetail").text_content()
    assert approval["diff_preview"] in page.locator("#permDetail").text_content()
    assert page.locator("#permDetail script").count() == 0
    assert page.evaluate("globalThis.reviewInjection === undefined")
    assert page.locator("#permProject").is_hidden()
    assert page.locator("#permTask").is_hidden()
    assert page.locator("#permRequest").is_hidden()
    assert page.evaluate("reviewRequests.length") == 0


def test_deny_after_review_sends_only_the_original_exact_decision_and_never_resumes(review_page):
    page = review_page
    page.locator("#permReview").click()
    page.locator("#permDeny").click()
    assert page.evaluate("reviewRequests") == [{"op": "resolve_approval", "approval_id": "ap-review-only", "decision": "deny", "scope": "once"}]
    assert page.locator("#permBar").is_hidden()
    assert page.evaluate("reviewState.pendingApproval === null && !reviewState.resumeApprovedTurn")
    assert page.evaluate("reviewState.approvalToken") == ""
    assert page.evaluate("reviewRuns.length") == 0
