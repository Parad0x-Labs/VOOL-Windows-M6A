"""A user bubble's hover actions (Copy, Pin) never cover its attachment chips.

The action row floats in the bubble's top-right corner and catches the pointer even while it is
transparent. A document chip's Preview button once sat under Pin, so a click on Preview hit Pin's
row instead (Windows full suite 42198603815). Every chip control must be the element the pointer
reaches, whatever the action row says ("Pin" or "Pinned") and at a desktop or a phone width.
"""
from __future__ import annotations

import hashlib

import pytest

from tests.test_chat_documents_ui import LONG_PASTE, _open, browser  # noqa: F401  (browser is a fixture)

ATT_ID = "att_" + "7" * 32

# Each chip button's centre and the middle of each of its edges, 1px in (its corners are rounded): the
# element the pointer reaches at each point must be the button itself, never the action row.
_HITS = """() => {
  const bad = [];
  const buttons = document.querySelectorAll('.msg.user .msg-att .att-chip button');
  if (!buttons.length) return ['no chip button rendered'];
  for (const el of buttons) {
    const b = el.getBoundingClientRect();
    const cx = (b.left + b.right) / 2, cy = (b.top + b.bottom) / 2;
    const points = [[cx, cy], [b.left + 1, cy], [b.right - 1, cy], [cx, b.top + 1], [cx, b.bottom - 1]];
    for (const [x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      if (!hit || !(hit === el || el.contains(hit))) {
        bad.push(`${el.className} at ${Math.round(x)},${Math.round(y)} reaches ${hit ? hit.className || hit.tagName : 'nothing'}`);
      }
    }
  }
  return bad;
}"""


def _history_with_a_document(server, chat_id: str, *, content: str) -> None:
    data = LONG_PASTE.encode("utf-8")
    server.bytes[ATT_ID] = data
    server.history[chat_id] = [
        {
            "role": "user",
            "content": content,
            "attachments": [
                {"id": ATT_ID, "name": "pasted-1.txt", "kind": "text", "media_type": "text/plain", "size_bytes": len(data),
                 "outcome": "read", "document": True, "chars": len(LONG_PASTE), "lines": 121,
                 "sha256": hashlib.sha256(data).hexdigest()},
            ],
        },
        {"role": "assistant", "content": "It is a service log."},
    ]


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 860}, {"width": 375, "height": 812}], ids=["desktop", "phone"])
@pytest.mark.parametrize("content", ["summarise this log", "x"], ids=["words", "one_letter"])
def test_no_chip_control_is_under_the_action_row(browser, viewport, content) -> None:  # noqa: F811
    context, page, server, errors = _open(browser, viewport=viewport)
    try:
        chat_id = page.evaluate("displayedChat")
        _history_with_a_document(server, chat_id, content=content)
        page.reload()
        page.wait_for_function("() => document.querySelectorAll('.msg.user .msg-att .att-chip .att-expand').length === 1", timeout=10000)
        page.hover(".msg.user .msg-text")
        page.wait_for_function("() => getComputedStyle(document.querySelector('.msg.user .msg-actions')).opacity === '1'")
        assert page.evaluate(_HITS) == []
        # The widest label the row carries: Pin turns into Pinned.
        page.evaluate("() => setPinBtn(document.querySelector('.msg.user .msg-pin'), true)")
        assert page.evaluate(_HITS) == []
        # A real click on Preview opens the preview, with the action row showing.
        page.click(".msg.user .msg-att .att-chip .att-expand", timeout=5000)
        page.wait_for_function("() => { const p = document.querySelector('.msg.user .att-preview'); return p && !p.hidden; }")
        assert errors == []
    finally:
        context.close()
