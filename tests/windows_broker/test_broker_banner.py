"""The chat page's broker line (Phase C slice C5, pick 6: "Check and clear", state only).

On Windows, the chat page reads GET /api/windows/broker/status and shows one line when VOOL will not
change files: a change stopped part-way (pending or torn journal rows, listed under "Show details"),
undo records that cannot be kept, or a broker that is not running. A broker that is still starting,
or a process with no broker (every Mac and Linux), shows nothing. What the broker reported (batch
ids, file names) is shown as text, never as markup. Clearing from VOOL is not built: the line says so.
Every string rides a catalog key present in all shipped catalogs.
"""

from __future__ import annotations

import json
import re

import pytest

from core.i18n.catalog import CATALOGS_DIR, MessageCatalog
from core.vool_chat_page import render_vool_chat_html
from tests.chat_page_js_harness import run_node, slice_source

KEYS = (
    "broker.line.stopped",
    "broker.line.no_undo",
    "broker.line.not_running",
    "broker.line.show_details",
    "broker.line.recheck",
    "broker.line.pending_item",
    "broker.line.torn_item",
    "broker.line.clear_note",
)
START, END = "// ---- Windows broker line", "// ---- end Windows broker line"
HTML = render_vool_chat_html(build_commit="t")


def _view(report: object) -> object:
    source = slice_source(HTML, START, END)
    # No page: the boot finds no line and returns, so only the pure view function runs.
    stub = "globalThis.document = { getElementById: () => null, addEventListener() {} };\n"
    program = stub + source + "\nconsole.log(JSON.stringify({ view: brokerLineView(" + json.dumps(report) + ") }));\n"
    return run_node(program)["view"]


def _report(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {"wired": True, "running": True, "reason": "", "flagged": [], "skipped": [],
                               "undo_records": "available", "pending": [], "torn": [], "needs_check": False}
    base.update(fields)
    return base


def test_the_line_is_in_the_footer_hidden_and_wired_to_the_catalog() -> None:
    line = re.search(r'<div id="brokerLine"[^>]*>.*?\n  </div>\n', HTML, re.DOTALL)
    assert line, "broker line markup missing"
    markup = line.group(0)
    assert "hidden" in markup.split(">", 1)[0] and 'role="alert"' in markup
    for key in ("broker.line.show_details", "broker.line.recheck", "broker.line.clear_note"):
        assert f'data-i18n="{key}"' in markup, key
    assert HTML.index('id="brokerLine"') < HTML.index('id="setupLine"'), "the broker line sits above the setup line"


def test_the_page_reads_the_status_route_and_writes_text_only() -> None:
    source = slice_source(HTML, START, END)
    assert "fetch('/api/windows/broker/status')" in source
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source


@pytest.mark.parametrize("report", [None, {}, {"wired": False}, "x", _report(), _report(running=False, reason="not_started")])
def test_nothing_shows_without_a_reason_to(report: object) -> None:
    assert _view(report) is None


def test_a_change_stopped_part_way_lists_what_is_left() -> None:
    view = _view(_report(needs_check=True, pending=[{"batch_id": "b1", "left": ["a.txt", "sub\\b.txt"]}],
                         torn=[{"batch_id": "b2", "bytes": 17, "kept_in": "batch-b2.torn"}]))
    assert view == {
        "kind": "stopped",
        "text": "A file change stopped part-way. VOOL won’t change files until it is checked.",
        "items": ["Batch b1: still out of place: a.txt, sub\\b.txt",
                  "Batch b2: an unfinished record was kept in batch-b2.torn"],
    }


def test_rows_without_a_flag_still_show_the_stopped_line() -> None:
    view = _view(_report(needs_check=False, pending=[{"batch_id": "unknown", "left": []}]))
    assert isinstance(view, dict) and view["kind"] == "stopped"


@pytest.mark.parametrize("undo", ["unavailable", None, "maybe"])
def test_undo_records_that_cannot_be_kept_warn(undo: object) -> None:
    view = _view(_report(undo_records=undo))
    assert isinstance(view, dict) and view["kind"] == "no_undo" and view["items"] == []


@pytest.mark.parametrize("reason", ["not_found", "start_timeout", "refused:reparse_point", "exited:3", ""])
def test_a_broker_that_is_not_running_says_so(reason: str) -> None:
    view = _view(_report(running=False, reason=reason, undo_records="unavailable"))
    assert isinstance(view, dict) and view["kind"] == "not_running"


def test_a_name_that_looks_like_markup_stays_text() -> None:
    view = _view(_report(needs_check=True, pending=[{"batch_id": "<b>x</b>", "left": ["<img src=x onerror=1>"]}]))
    assert isinstance(view, dict)
    assert view["items"] == ["Batch <b>x</b>: still out of place: <img src=x onerror=1>"]


@pytest.mark.parametrize("tag", sorted(path.stem for path in CATALOGS_DIR.glob("*.json")))
def test_every_key_is_in_every_shipped_catalog(tag: str) -> None:
    catalog = MessageCatalog(tag)
    assert not catalog.diagnostic.stale_source, tag
    for key in KEYS:
        assert key in catalog.locale_keys or tag == "en", (tag, key)
        assert key not in catalog.diagnostic.rejected_keys, (tag, key)
    if tag == "en":
        english = json.loads((CATALOGS_DIR / "en.json").read_text(encoding="utf-8"))["messages"]
        assert all(key in english for key in KEYS)
