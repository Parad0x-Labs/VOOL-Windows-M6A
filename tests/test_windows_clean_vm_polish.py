"""Clean-VM job 52 polish findings (port 76afbd36), each pinned where it lives.

#14 Setup on Windows still said "On my Mac" / "stays on this Mac": the step sentences came from
    core.setup_progress.snapshot(), whose English never asked for the ``.windows`` catalog key,
    and the setup script's animated example and no-local notice were not in the Windows swap list.
#16 The main window (1220x860) and Setup (920x740) opened partly off-screen at 1024x768: no size
    was fitted to the screen's work area.
#17 The companion sprite covered the live Stop button and the reply: its avoid list found only the
    FIRST ``.tc-stop`` in the page (an old, finished card) and never listed the reply text.
#18 The Memory page showed developer source paths in its help text.
#19 Setup's choices sat below a fixed 240 px example in a pane Page Down could not scroll,
    because nothing ever focused the pane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.chat_page_js_harness import run_node

_CATALOGS = sorted((Path(__file__).resolve().parents[1] / "core" / "i18n" / "catalogs").glob("*.json"))


# ---------------------------------------------------------------------------------------- #14

def _host(monkeypatch, windows: bool) -> None:
    import core.generated_files as generated_files

    monkeypatch.setattr(generated_files, "host_is_windows", lambda *a, **k: windows)


@pytest.mark.parametrize("locale", ["en", "de"])
def test_setup_step_sentences_name_this_computer_on_windows(monkeypatch, locale) -> None:
    from core import setup_progress

    _host(monkeypatch, True)
    steps = {step["id"]: step for step in setup_progress.snapshot(locale)["steps"]}
    for step_id in ("thinking", "name"):
        assert "Mac" not in steps[step_id]["sentence"], (locale, steps[step_id]["sentence"])


def test_setup_step_sentences_on_a_mac_are_unchanged(monkeypatch) -> None:
    from core import setup_progress

    _host(monkeypatch, False)
    steps = {step["id"]: step for step in setup_progress.snapshot("en")["steps"]}
    assert steps["thinking"]["sentence"].startswith("On my Mac")
    assert steps["name"]["sentence"].endswith("stays on this Mac.")


def test_the_windows_setup_script_never_says_on_my_mac(monkeypatch) -> None:
    # The page's i18n bundle ships both siblings of every host key; what the user READS comes from
    # the setup script's literals and T() fallbacks, so that is what must not name a Mac.
    from core.vool_setup_page import _host_setup_script

    _host(monkeypatch, True)
    script = _host_setup_script()
    assert "On my Mac" not in script
    assert "this Mac" not in script
    _host(monkeypatch, False)
    assert "<b>On my Mac</b>" in _host_setup_script()


# ---------------------------------------------------------------------------------------- #16

def test_a_window_bigger_than_the_work_area_is_fitted_and_centred() -> None:
    from installer.bundle.vool_window import _fit_to_work_area

    work = (0, 0, 1024, 728)  # 1024x768 with a 40 px taskbar
    for width, height, minimum in ((1220, 860, (900, 640)), (920, 740, (560, 480)), (1000, 720, (560, 420))):
        fitted = _fit_to_work_area(width, height, minimum, work)
        assert fitted["x"] >= 0 and fitted["y"] >= 0
        assert fitted["x"] + fitted["width"] <= 1024 and fitted["y"] + fitted["height"] <= 728
        assert fitted["min_size"][0] <= fitted["width"] and fitted["min_size"][1] <= fitted["height"]
        assert abs((fitted["x"] * 2 + fitted["width"]) - 1024) <= 1   # centred


def test_a_window_that_fits_keeps_its_size() -> None:
    from installer.bundle.vool_window import _fit_to_work_area

    fitted = _fit_to_work_area(1220, 860, (900, 640), (0, 0, 1920, 1040))
    assert (fitted["width"], fitted["height"], fitted["min_size"]) == (1220, 860, (900, 640))


def test_an_unknown_work_area_changes_nothing() -> None:
    from installer.bundle.vool_window import _fit_to_work_area

    assert _fit_to_work_area(1220, 860, (900, 640), None) == {
        "width": 1220, "height": 860, "min_size": (900, 640),
    }


def test_every_native_window_is_fitted_before_it_is_created() -> None:
    source = (Path(__file__).resolve().parents[1] / "installer" / "bundle" / "vool_window.py").read_text(encoding="utf-8")
    assert source.count("**_fit_to_work_area(") >= 3


# ---------------------------------------------------------------------------------------- #17

def _critical_rects(nodes: dict[str, list[dict]]) -> list[dict]:
    from core.companion_presentation_fragment import render_companion_fragment

    fragment = render_companion_fragment()
    selectors = re.search(r"const VN_CRITICAL_SELECTORS = \[[^\]]*\];", fragment)
    function = re.search(r"function vnCriticalRects\(\) \{[\s\S]+?\n\}", fragment)
    assert selectors and function
    program = """
const nodes = %s;
const box = (r) => ({ getBoundingClientRect: () => r });
globalThis.document = {
  querySelector(sel) { const list = nodes[sel] || []; return list.length ? box(list[0]) : null; },
  querySelectorAll(sel) { return (nodes[sel] || []).map(box); },
};
%s
%s
console.log(JSON.stringify({ rects: vnCriticalRects() }));
""" % (json.dumps(nodes), selectors.group(0), function.group(0))
    return run_node(program)["rects"]


def test_the_pet_avoids_the_live_stop_button_not_only_the_first_one() -> None:
    stale = {"left": 0, "top": 0, "right": 0, "bottom": 0, "width": 0, "height": 0}
    live = {"left": 600, "top": 500, "right": 660, "bottom": 530, "width": 60, "height": 30}
    rects = _critical_rects({".tc-stop": [stale, live]})
    assert live in rects


def test_the_pet_avoids_the_latest_reply() -> None:
    older = {"left": 40, "top": 40, "right": 700, "bottom": 120, "width": 660, "height": 80}
    latest = {"left": 40, "top": 300, "right": 700, "bottom": 460, "width": 660, "height": 160}
    rects = _critical_rects({"#log .msg.assistant": [older, latest]})
    assert latest in rects
    assert older not in rects


# ---------------------------------------------------------------------------------------- #18

def test_memory_help_text_names_no_source_files() -> None:
    from core import vool_settings_page

    source = Path(vool_settings_page.__file__).read_text(encoding="utf-8")
    for leaked in ("(core/memory/entries.py)", "core/user_preferences.py: migrate_legacy_profile_fields"):
        assert leaked not in source


@pytest.mark.parametrize("catalog", _CATALOGS, ids=[c.stem for c in _CATALOGS])
def test_no_catalog_help_text_names_a_source_file(catalog: Path) -> None:
    messages = json.loads(catalog.read_text(encoding="utf-8"))["messages"]
    for key in ("settings.row.learned_facts.help", "settings.row.remembered_identity_note.help"):
        text = messages.get(key, "")
        assert ".py" not in text and "core/" not in text, (catalog.stem, key, text)


# ---------------------------------------------------------------------------------------- #19

def test_setup_focuses_its_scrolling_pane_so_page_down_scrolls() -> None:
    from core.vool_setup_page import render_vool_setup_html

    page = render_vool_setup_html(build_commit="t")
    render_body = re.search(r"function render\(\) \{[\s\S]+?\n\}", page).group(0)
    assert "focusPane()" in render_body
    assert re.search(r"function focusPane\(\) \{[\s\S]*?\$\('#pane'\)\.focus\(\{ ?preventScroll: ?true ?\}\)", page)


def test_a_short_setup_window_shrinks_the_example() -> None:
    from core.vool_setup_page import render_vool_setup_html

    page = render_vool_setup_html(build_commit="t")
    assert re.search(r"@media \(max-height: ?760px\) \{[^}]*\.mock \{ ?height: ?\d+px", page)
