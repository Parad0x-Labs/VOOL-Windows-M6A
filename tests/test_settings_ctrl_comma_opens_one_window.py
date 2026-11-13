"""Ctrl+, (Cmd+,) opens ONE Settings window, never two.

Seen on Windows 0.7.1: one Ctrl+, press could leave two "VOOL Settings" windows. Two causes
together: the rendered chat page binds Ctrl+, twice (its own keydown listener and the command
palette fragment's), so one press called ``pywebview.api.open_settings`` twice; and pywebview runs
each js_api call on its own thread, while ``open_settings`` checked "is a window open?" and created
one without any guard, so a second call that arrived while the first window was still being
created saw no window and built another. These tests pin both halves.
"""
from __future__ import annotations

import importlib.util
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_window_module():
    spec = importlib.util.spec_from_file_location(
        "vool_window_one_settings_under_test", ROOT / "installer" / "bundle" / "vool_window.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Event:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _Window:
    def __init__(self, title: str, url: str) -> None:
        self.title = title
        self.url = url
        self.shown = 0
        self.events = type("E", (), {})()
        self.events.closed = _Event()

    def show(self) -> None:
        self.shown += 1

    def restore(self) -> None:
        pass

    def load_url(self, url: str) -> None:
        self.url = url

    def destroy(self) -> None:
        pass


class _SlowWebview:
    """create_window blocks the FIRST call until released, like a real native window being built."""

    def __init__(self) -> None:
        self.created: list[_Window] = []
        self.first_started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def create_window(self, title, url=None, **_kw):
        with self._lock:
            first = not self.created and not self.first_started.is_set()
            if first:
                self.first_started.set()
        if first:
            assert self.release.wait(10), "test harness: first create was never released"
        win = _Window(title, url)
        with self._lock:
            self.created.append(win)
        return win


def _bridge(module, fake):
    bridge = module._WindowApi()
    bridge.set_window(_Window("VOOL", module.URL), fake)
    return bridge


def _race(open_name: str) -> tuple[_SlowWebview, list, list]:
    module = _load_window_module()
    fake = _SlowWebview()
    bridge = _bridge(module, fake)
    opener = getattr(bridge, open_name)
    results: list = []
    first = threading.Thread(target=lambda: results.append(opener()))
    first.start()
    assert fake.first_started.wait(10)
    # A second call while the first window is still being created (a second js_api thread).
    second_result = opener()
    fake.release.set()
    first.join(10)
    assert not first.is_alive()
    # And a third call after the window exists focuses it.
    third_result = opener()
    return fake, [results[0], second_result, third_result], fake.created


def test_a_second_open_settings_while_the_first_is_being_created_makes_no_second_window() -> None:
    fake, (first, second, third), created = _race("open_settings")
    assert len(created) == 1, f"{len(created)} Settings windows were created for one window's worth of calls"
    assert first["ok"] and first["mode"] == "created"
    assert second["ok"] and second["mode"] == "creating"
    assert third["ok"] and third["mode"] == "focused"


def test_a_second_open_setup_while_the_first_is_being_created_makes_no_second_window() -> None:
    fake, (first, second, third), created = _race("open_setup")
    assert len(created) == 1, f"{len(created)} Setup windows were created"
    assert first["mode"] == "created" and second["mode"] == "creating" and third["mode"] == "focused"


def test_a_failed_create_does_not_leave_settings_stuck_in_creating() -> None:
    module = _load_window_module()

    class _Failing:
        calls = 0

        def create_window(self, title, url=None, **_kw):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("no window")
            return _Window(title, url)

    bridge = _bridge(module, _Failing())
    assert bridge.open_settings()["ok"] is False
    again = bridge.open_settings()
    assert again["ok"] and again["mode"] == "created"


def _comma_listeners(html: str) -> list[str]:
    """Each keydown listener body in the rendered chat page that opens Settings on Ctrl/Cmd+,."""
    bodies = []
    for match in re.finditer(r"addEventListener\('keydown',\s*(?:function\s*\((\w+)\)|\((\w+)\)\s*=>)\s*\{", html):
        start = match.end()
        depth, i = 1, start
        while depth and i < len(html):
            depth += {"{": 1, "}": -1}.get(html[i], 0)
            i += 1
        body = html[start:i]
        if re.search(r"key === ','", body) and "penSettings" in body:
            bodies.append(body)
    return bodies


def test_one_ctrl_comma_press_opens_settings_through_only_one_listener() -> None:
    from core.vool_chat_page import render_vool_chat_html

    html = render_vool_chat_html()
    listeners = _comma_listeners(html)
    assert listeners, "no Ctrl+, listener found in the chat page"
    if len(listeners) > 1:
        # Several listeners may exist (the page's own and the command palette's) only if each one
        # stands down when another already handled the same press.
        for body in listeners:
            assert "defaultPrevented" in body, (
                "a Ctrl+, listener opens Settings without checking whether another listener already did:\n"
                + body[:400]
            )
