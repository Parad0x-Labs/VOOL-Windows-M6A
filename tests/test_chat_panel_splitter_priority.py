"""The panel drag rail and operator-answer controls must both keep real pointer access."""
from __future__ import annotations

import pytest

from tests import served_browser
from tests.test_chat_page_layout_geometry import SETUP, _route


@pytest.mark.parametrize("viewport", [(900, 420), (520, 620), (1280, 860)])
@pytest.mark.parametrize("surface", ["bypass", "permission"])
def test_the_splitter_drags_without_sacrificing_operator_answer_controls(viewport, surface):
    manager, browser = served_browser.launch_chromium()
    try:
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        page.route("**/*", _route)
        page.goto("http://vool.test/chat")
        # Interactive controls resolve their owner through the real chat-state binding.
        page.evaluate("() => setDisplayedChat('c1')")
        page.evaluate(SETUP)
        if surface == "permission":
            page.evaluate("""() => {
                view.mode = 'manual'; reflectMode();
                showPermBar(displayedChat, {approval: {
                    approval_id: 'splitter-probe', action: 'write notes.txt',
                    affected_resources: ['notes.txt'], expected_side_effects: 'creates a file',
                    reversible: true, intent: 'workspace.write_file', scope_options: ['once', 'task']
                }});
            }""")
        page.wait_for_timeout(80)
        assert page.evaluate("document.body.classList.contains('answer-pending')")
        assert page.locator("footer").evaluate("e => getComputedStyle(e).zIndex") == "35"
        handle = page.locator("#xpResize")
        assert handle.evaluate("""e => {
            const r = e.getBoundingClientRect();
            return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === e;
        }"""), "The answer surface must not cover the panel drag rail"
        before = page.locator("#xpanel").bounding_box()
        rail = handle.bounding_box()
        x, y = rail["x"] + rail["width"] / 2, rail["y"] + rail["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x - 32, y, steps=8)
        page.mouse.up()
        after = page.locator("#xpanel").bounding_box()
        assert after["width"] >= before["width"] + 25, "An ordinary drag must resize the panel"
        # The header toggle must remain usable, and closing the panel exposes the answer control.
        page.click("#panelBtn")
        control_id = "bypassRevoke" if surface == "bypass" else "permDeny"
        control = page.locator("#" + control_id)
        assert control.is_visible()
        assert control.evaluate("""e => {
            const r = e.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return hit === e || e.contains(hit);
        }"""), "The operator-answer control must remain above the companion"
        control.focus()
        assert control.evaluate("e => document.activeElement === e")
        if surface == "bypass":
            control.click()
            page.wait_for_function("document.getElementById('bypassBanner').hidden && view.mode === 'manual'")
        else:
            assert not page.locator("#permBar").is_hidden()
    finally:
        browser.close()
        manager.stop()
