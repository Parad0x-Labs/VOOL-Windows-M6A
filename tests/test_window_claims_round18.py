"""Steve round 18 (578d185f), window lane.

* ``vool://`` did nothing: the handler started VOOL.cmd through ``cmd /s /c`` with
  CREATE_NO_WINDOW|DETACHED_PROCESS and null handles, and VOOL.cmd never reached the supervisor.
  It now starts ``wscript.exe vool.vbs`` exactly like the Start-menu shortcut.
* Right after marking a folder, Settings said "No folder is marked yet": the focus handler rebuilt
  the pane with the old list and the redraw went into a detached widget.
* Hover icons covered chat titles in the sidebar: six emoji buttons squeezed the title to a sliver,
  and a click meant for the title landed on an action.

The two page fixes are proven in a real browser (tests/served_browser.launch_chromium: skipped
without chromium outside the gate, a failure inside it).
"""
from __future__ import annotations

import ntpath
import re
from pathlib import Path

import pytest

from installer.bundle import vool_protocol_handler as handler

ROOT = Path(__file__).resolve().parents[1]


# --- vool:// starts VOOL the way the shortcut does ------------------------------------------------

@pytest.fixture
def system32(monkeypatch):
    """A Windows SystemRoot whose System32\\wscript.exe exists."""
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    real_isfile = handler.os.path.isfile
    expected = ntpath.join("C:\\Windows", "System32", "wscript.exe")
    monkeypatch.setattr(handler.os.path, "isfile", lambda p: p == expected or real_isfile(p))
    return expected


def _install(tmp_path: Path, *, vbs: bool = True) -> Path:
    root = tmp_path / "VOOL (x) & co"
    root.mkdir()
    (root / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    if vbs:
        (root / "vool.vbs").write_text("' launcher\n", encoding="utf-8")
    return root


def test_a_link_starts_vool_through_wscript_and_vool_vbs(monkeypatch, tmp_path, system32) -> None:
    root = _install(tmp_path)
    order: list = []
    monkeypatch.setattr(handler, "_pass_on_the_foreground", lambda: order.append("allow") or True)
    monkeypatch.setattr(handler.subprocess, "Popen", lambda args, **kw: order.append(("popen", args, kw)))
    handler._start_bundle(root)
    assert order[0] == "allow", "the foreground right is handed on before VOOL starts"
    _, args, kw = order[1]
    assert args == [system32, str(root / "vool.vbs")], "a list: CreateProcess quotes each path itself"
    assert "creationflags" not in kw, "wscript is a GUI program: no console flags, as from the shortcut"
    assert kw["cwd"] == str(root)
    assert len(order) == 2


@pytest.mark.parametrize("system_root", ["", "Windows", "\\\\server\\share\\Windows", "C:", "C:Windows"])
def test_wscript_is_never_taken_from_an_untrusted_place(monkeypatch, system_root) -> None:
    monkeypatch.setenv("SystemRoot", system_root)
    monkeypatch.setattr(handler.os.path, "isfile", lambda p: True)
    with pytest.raises(OSError):
        handler._wscript()


def test_without_a_trusted_wscript_vool_cmd_is_started_instead(monkeypatch, tmp_path) -> None:
    root = _install(tmp_path)
    monkeypatch.setenv("SystemRoot", "\\\\server\\share\\Windows")   # never trusted
    seen: list = []
    monkeypatch.setattr(handler, "_pass_on_the_foreground", lambda: True)
    monkeypatch.setattr(handler.subprocess, "Popen", lambda args, **kw: seen.append(args))
    handler._start_bundle(root)
    assert len(seen) == 1 and isinstance(seen[0], str) and seen[0].endswith(f'/s /c ""{root / "VOOL.cmd"}""')
    assert not seen[0].lower().startswith(("wscript", '"\\\\server'))


@pytest.mark.parametrize("bad", ["100%OS%", "a\rb", "a\nb"])
def test_an_install_path_wscript_would_expand_is_refused_too(monkeypatch, tmp_path, system32, bad) -> None:
    """vool.vbs passes its folder to WScript.Shell.Run, which expands %NAME% like cmd does."""
    class _Named:
        def __init__(self, text): self._t = text
        def __truediv__(self, name): return _Named(f"{self._t}\\{name}")
        def is_file(self): return True
        def __str__(self): return self._t
    monkeypatch.setattr(handler.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may start"))
    with pytest.raises(ValueError):
        handler._start_bundle(_Named(f"C:\\root{bad}"))


def test_an_install_without_vool_vbs_still_starts_vool_cmd(monkeypatch, tmp_path) -> None:
    root = _install(tmp_path, vbs=False)
    seen: list = []
    monkeypatch.setattr(handler, "_pass_on_the_foreground", lambda: True)
    monkeypatch.setattr(handler.subprocess, "Popen", lambda args, **kw: seen.append(args))
    handler._start_bundle(root)
    assert len(seen) == 1 and isinstance(seen[0], str) and seen[0].endswith(f'/s /c ""{root / "VOOL.cmd"}""')


def test_the_installer_still_ships_vool_vbs_next_to_the_handler() -> None:
    iss = (ROOT / "installer" / "bundle" / "vool.iss").read_text(encoding="utf-8")
    assert re.search(r'Source:\s*"\{#Stage\}\\vool\.vbs";\s*DestDir:\s*"\{app\}"', iss)
    assert re.search(r'Source:\s*"\{#Stage\}\\vool_protocol_handler\.py";\s*DestDir:\s*"\{app\}"', iss)


# --- the two page fixes, in a real browser ---------------------------------------------------------

_STUB_CHAT = """
window.__urls = [];
const json = (o) => new Response(JSON.stringify(o), {headers: {'Content-Type': 'application/json'}});
window.fetch = async (url, opts) => {
  url = String(url); window.__urls.push(url);
  if (url.includes('/api/chat/sessions')) return json({sessions: [
    {session_id: 'chat-a', title: 'Alpha planning chat'},
    {session_id: 'chat-b', title: 'Beta release checklist'}]});
  if (url.includes('/api/chat/history')) return json({messages: []});
  return json({ok: true});
};
"""

_STUB_SETTINGS = """
window.__marked = false;
const json = (o) => new Response(JSON.stringify(o), {headers: {'Content-Type': 'application/json'}});
window.fetch = async (url) => {
  url = String(url);
  if (url.includes('/api/windows/disposable-folders')) return json({ok: true, windows: true, problem: '',
    folders: window.__marked ? [{id: 'f1', path: 'D:\\\\scratch', pending_restart: true}] : []});
  return json({});
};
window.pywebview = {api: {
  mark_disposable_folder: async () => {
    window.dispatchEvent(new Event('focus'));            // the picker closes: Settings rebuilds its pane
    await new Promise((r) => setTimeout(r, 200));        // the OS confirm box
    window.__marked = true;
    return {ok: true, id: 'f1', pending_restart: true};
  },
  unmark_disposable_folder: async () => ({ok: true}),
}};
"""


def _page(tmp_path: Path, html: str, stub: str, name: str):
    from tests.served_browser import launch_chromium

    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    manager, browser = launch_chromium()
    page = browser.new_page(viewport={"width": 1100, "height": 760})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(stub)
    page.goto(path.as_uri())
    page.wait_for_timeout(1000)
    return manager, browser, page, errors


def test_hovering_a_chat_keeps_its_title_and_a_click_on_it_opens_it(tmp_path) -> None:
    from core.vool_chat_page import render_vool_chat_html

    manager, browser, page, errors = _page(tmp_path, render_vool_chat_html(), _STUB_CHAT, "chat.html")
    try:
        row = page.locator("#sessions .session", has_text="Beta release checklist").first
        title = row.locator(".s-title")
        before = title.bounding_box()
        row.hover()
        page.wait_for_timeout(100)
        after = title.bounding_box()
        assert after["width"] >= before["width"] * 0.8, (before, after)
        x, y = before["x"] + before["width"] / 2, before["y"] + before["height"] / 2
        hit = page.evaluate("([x, y]) => document.elementFromPoint(x, y).className", [x, y])
        assert hit == "s-title", f"the middle of the title is covered by {hit!r} on hover"
        page.evaluate("window.__urls = []")
        page.mouse.click(x, y)
        page.wait_for_timeout(500)
        assert any("history?session=chat-b" in u for u in page.evaluate("window.__urls"))
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_the_chat_actions_open_from_one_button_and_still_work(tmp_path) -> None:
    from core.vool_chat_page import render_vool_chat_html

    manager, browser, page, errors = _page(tmp_path, render_vool_chat_html(), _STUB_CHAT, "chat.html")
    try:
        page.wait_for_load_state("networkidle")
        row = page.locator("#sessions .session", has_text="Alpha planning chat").first
        row.hover()
        assert row.locator(".s-actions .s-act:visible").count() == 0, "no inline action buttons on hover"
        row.locator(".s-more").click()
        row.locator(".s-actions.open .s-act").nth(5).wait_for(state="visible")
        assert row.locator(".s-actions.open .s-act:visible").count() == 6
        # The list re-renders whenever it refreshes; the open menu comes back on the new row.
        page.evaluate("loadSessions()")
        page.wait_for_timeout(300)
        row = page.locator("#sessions .session", has_text="Alpha planning chat").first
        row.locator(".s-actions.open .s-act").nth(5).wait_for(state="visible")
        assert row.locator(".s-actions.open .s-act:visible").count() == 6
        page.evaluate("window.__urls = []")
        row.locator('.s-actions .s-act[title="Pin chat to the top"]').click()
        page.wait_for_timeout(300)
        assert any("/api/chat/pin-chat" in u for u in page.evaluate("window.__urls"))
        assert page.locator("#sessions .s-actions.open").count() == 0
        # A click anywhere else closes an open menu, and Escape does too.
        row = page.locator("#sessions .session", has_text="Alpha planning chat").first
        row.hover()
        row.locator(".s-more").click()
        page.mouse.click(700, 400)
        assert page.locator("#sessions .s-actions.open").count() == 0
        row.hover()
        row.locator(".s-more").click()
        page.keyboard.press("Escape")
        assert page.locator("#sessions .s-actions.open").count() == 0
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_a_marked_folder_shows_at_once_after_the_pane_rebuilds(tmp_path) -> None:
    from core.vool_settings_page import render_vool_settings_html

    manager, browser, page, errors = _page(tmp_path, render_vool_settings_html(), _STUB_SETTINGS, "settings.html")
    try:
        groups = page.evaluate("[...document.querySelectorAll('.nav-item')].map(n => n.dataset.group)")
        for group in groups:
            page.evaluate("(g) => { location.hash = g; }", group)
            page.wait_for_timeout(250)
            if page.get_by_text("Mark a folder…").count():
                break
        else:
            pytest.fail("no Settings group shows the disposable-folder widget")
        page.get_by_text("Mark a folder…").click()
        page.wait_for_timeout(900)
        assert page.get_by_text("D:\\scratch").count() == 1
        assert page.get_by_text("No folder is marked yet.").count() == 0
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_the_disposable_redraw_renders_the_pane_on_screen() -> None:
    source = (ROOT / "core" / "vool_settings_page.py").read_text(encoding="utf-8")
    assert "stack.textContent = ''; widgetDisposableFolders(stack);" not in source
    assert "const redraw = async () => { disposableMemory.note = null; await readSource(DISPOSABLE_URL); renderPane(); };" in source
