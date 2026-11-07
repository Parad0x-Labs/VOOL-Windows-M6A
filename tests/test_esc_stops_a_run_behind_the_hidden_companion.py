"""A bare Esc must stop the run while the companion's closed menus sit hidden in the page.

Clean-VM job 52 (port 76afbd36): Esc did nothing for 56 s+ of a live run despite the on-screen
"Esc stop" hint. The Esc cascade asks ``anyLegacySurfaceOpen()`` at press time and, when it says a
surface is open, leaves the key to that surface's own closer. It answered "open" whenever the
companion's pop, menu or character lab EXISTED -- but the companion mounts all three on boot with
``display:none`` and keeps them, so with the companion on screen every Esc was handed to a closer
that was not open and the run never stopped. These tests drive the real function under node.
"""

from __future__ import annotations

import json
import re

from core.command_palette_fragment import render_palette_fragment
from tests.chat_page_js_harness import run_node


def _surface_probe(companion_displays: list[str]) -> bool:
    fragment = render_palette_fragment()
    match = re.search(r"function anyLegacySurfaceOpen\(\)\{[\s\S]+?\n\}", fragment)
    assert match, "anyLegacySurfaceOpen is the Esc cascade's press-time probe"
    program = """
const displays = %s;
const companion = displays.map((display) => ({ style: { display }, hidden: false }));
const overlay = { hidden: true };
globalThis.document = {
  getElementById() { return { hidden: true }; },
  querySelector(sel) {
    if (sel === '.pop.open') return null;
    return companion.length ? companion[0] : null;
  },
  querySelectorAll(sel) { return sel === '.pop.open' ? [] : companion; },
};
%s
console.log(JSON.stringify({ open: anyLegacySurfaceOpen() }));
""" % (json.dumps(companion_displays), match.group(0))
    return bool(run_node(program)["open"])


def test_hidden_companion_menus_do_not_swallow_esc() -> None:
    assert _surface_probe(["none", "none", "none"]) is False


def test_an_open_companion_menu_still_owns_esc() -> None:
    assert _surface_probe(["none", "block", "none"]) is True
    assert _surface_probe(["none", "none", "flex"]) is True


def test_no_companion_mounted_is_no_open_surface() -> None:
    assert _surface_probe([]) is False
