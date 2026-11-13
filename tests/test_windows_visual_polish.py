"""Visual polish of the chat and Settings pages (bug hunt 0.7.1, rows V3 to V8).

Each test pins one visible defect so it cannot come back: an unstyled Council button, the
"+ New Ctrl+N" / "+ Project" buttons breaking over two lines, a two-row header in a narrow
window, faded disabled voice buttons in the message bar, the Language block eating the Settings
menu, and colour emoji mixed into the monochrome Settings menu icons.
"""
from __future__ import annotations

import re

from core.vool_chat_page import render_vool_chat_html
from core.vool_settings_page import _settings_groups, render_vool_settings_html


def _css(html: str) -> str:
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.S))
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rules_for(css: str, selector: str) -> list[str]:
    """Every declaration block whose selector list names ``selector`` exactly."""
    found = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        names = [s.strip() for s in selectors.split(",")]
        if selector in names:
            found.append(body)
    return found


CHAT_CSS = _css(render_vool_chat_html())
SETTINGS_HTML = render_vool_settings_html()
SETTINGS_CSS = _css(SETTINGS_HTML)


def test_v3_council_button_shares_the_outlined_header_button_style():
    bodies = _rules_for(CHAT_CSS, "#councilBtn")
    joined = " ".join(bodies)
    assert "background:transparent" in joined
    assert "border:1px solid var(--border)" in joined
    assert "border-radius:8px" in joined
    assert any("border-color:var(--accent)" in b for b in _rules_for(CHAT_CSS, "#councilBtn:hover"))


def test_v4_new_chat_and_new_project_never_wrap_their_label():
    for selector in ("#newChat", "#newProject"):
        joined = " ".join(_rules_for(CHAT_CSS, selector))
        assert "white-space:nowrap" in joined, selector
        assert "flex:0 0 auto" in joined, selector


def test_v4_the_key_cap_gives_way_before_the_buttons_overflow_the_sidebar_head():
    assert "container-type:inline-size" in " ".join(_rules_for(CHAT_CSS, "#sidebar .side-head"))
    match = re.search(r"@container \(max-width: (\d+)px\) \{([^}]*\})\s*\}", CHAT_CSS)
    assert match, "the sidebar head needs a container rule for the key cap"
    assert "#newChat kbd" in match.group(2) and "display:none" in match.group(2)


def _media_block(css: str, query: str) -> str:
    start = css.index(query)
    depth, i = 0, css.index("{", start)
    for j in range(i, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[i + 1:j]
    raise AssertionError(f"unterminated {query}")


def test_v6_narrow_window_header_is_one_compact_row():
    block = _media_block(CHAT_CSS, "@media (max-width: 999px)")
    assert "header > button { white-space:nowrap; }" in block
    assert "header .ver { display:none; }" in block
    assert "font-size:0" in block and "header #searchBtn::before" in block and "header #exportBtn::before" in block


def test_v6_narrow_header_keeps_search_and_export_accessible_names():
    html = render_vool_chat_html()
    for button in ("searchBtn", "exportBtn"):
        tag = re.search(rf'<button id="{button}"[^>]*>', html).group(0)
        assert 'aria-label="' in tag and 'title="' in tag, button


def test_v7_disabled_voice_buttons_are_not_painted_in_the_message_bar():
    bodies = _rules_for(CHAT_CSS, "#dictateBtn:disabled") + _rules_for(CHAT_CSS, "#voiceModeBtn:disabled")
    assert any("display:none" in b for b in bodies)


def test_v5_language_block_paints_the_help_once_and_keeps_the_coverage_line():
    css = SETTINGS_CSS
    assert "display:none" in " ".join(_rules_for(css, '#uiLocaleWrap .loc-note[data-i18n-html="settings.language_help"]'))
    assert "display:none" in " ".join(_rules_for(css, "#uiLocaleWrap .loc-word .set-sub"))
    coverage = " ".join(_rules_for(css, "#uiLocaleWrap #uiLocaleCoverage"))
    assert "display:none" not in coverage and "text-overflow:ellipsis" in coverage
    # The hidden help sentence is still what the select says to a screen reader and on hover.
    select = re.search(r'<select id="uiLocaleSelect"[^>]*>', SETTINGS_HTML).group(0)
    assert 'data-i18n-aria-label="settings.language_select_aria"' in select
    assert 'data-i18n-title="settings.language_select_title"' in select
    assert re.search(r'<p class="loc-note" id="uiLocaleCoverage">[^<]+</p>', SETTINGS_HTML)


def _is_colour_emoji(ch: str) -> bool:
    """Supplementary-plane pictographs (U+1F000 and up) are drawn as colour emoji by Windows."""
    return ord(ch) >= 0x1F000


def test_v8_settings_menu_icons_are_all_monochrome_symbols():
    for group in _settings_groups():
        icon = group.get("icon", "")
        assert icon and not any(_is_colour_emoji(ch) for ch in icon), (group["id"], icon)
    assert "&#127760;" not in SETTINGS_HTML, "the Language label must not use the colour globe emoji"
    assert "font-variant-emoji:text" in " ".join(_rules_for(SETTINGS_CSS, ".nav-item .ico"))
