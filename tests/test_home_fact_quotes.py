"""The home screen's rotating fact is shown once, with its own quotation marks only.

The consistency sweep saw “"Temperature" tunes randomness…”: the template wrapped every fact in “…”, and several
facts open or close with a quoted term of their own, in English and in the other shipped languages ("Temperature",
«Галлюцинация», “幻觉”). The line now carries no wrapping marks, so no fact in any language shows a doubled mark.
"""
from __future__ import annotations

import json

import pytest

from core.i18n.catalog import CATALOGS_DIR
from core.vool_chat_page import render_vool_chat_html
from tests.chat_page_js_harness import run_node, slice_source

QUOTE_MARKS = "\"'“”„«»「」‘’‚‹›"
LOCALES = sorted(p.stem for p in CATALOGS_DIR.glob("*.json"))


def _hints(locale: str) -> list[dict]:
    html = render_vool_chat_html()
    code = slice_source(html, "function emptyT(key, fallback) {", "// HH:mm in the viewer's own local timezone")
    messages = json.loads((CATALOGS_DIR / f"{locale}.json").read_text(encoding="utf-8"))["messages"]
    return run_node(f"""
const messages = {json.dumps(messages, ensure_ascii=False)};
globalThis.VOOLT = (key) => Object.prototype.hasOwnProperty.call(messages, key) ? messages[key] : key;
const esc = (s) => String(s);
const logEl = {{ innerHTML: '' }};
""" + code + """
const results = [];
for (let i = 0; i < 20; i++) {
  Math.random = () => (i + 0.5) / 20;
  showEmpty();
  const hint = /<div class="empty-hint">([\\s\\S]*?)<\\/div>/.exec(logEl.innerHTML)[1];
  results.push(hint);
}
console.log(JSON.stringify({ results }));
""")["results"]


def _facts(locale: str) -> set[str]:
    english = json.loads((CATALOGS_DIR / "en.json").read_text(encoding="utf-8"))["messages"]
    messages = json.loads((CATALOGS_DIR / f"{locale}.json").read_text(encoding="utf-8"))["messages"]
    keys = [f"first_run.fact_{n:02d}" for n in range(1, 21)]
    return {messages.get(key) or english[key] for key in keys}


@pytest.mark.parametrize("locale", LOCALES)
def test_every_fact_is_shown_exactly_as_written_with_no_added_marks(locale):
    hints = _hints(locale)
    shown = render_vool_chat_html().count("['first_run.fact_")
    assert set(hints) <= _facts(locale), "a fact must be shown exactly as the catalog writes it"
    assert len(set(hints)) == shown, "every fact on the page must be reachable"
    for hint in hints:
        assert not (hint[0] in QUOTE_MARKS and hint[1] in QUOTE_MARKS), hint
        assert not (hint[-1] in QUOTE_MARKS and hint[-2] in QUOTE_MARKS), hint


def test_the_temperature_fact_opens_with_one_mark():
    hint = next(h for h in _hints("en") if "Temperature" in h)
    assert hint.startswith('"Temperature"') and hint.endswith("wander.")
