"""Closing the chat window ends the window host, and native confirm boxes open in front of VOOL.

Bug hunt 0.7.1 (379e57f7):

* B1. pywebview ends ``webview.start`` only when no window is left. The desktop pet is only ever
  hidden, and Settings and Setup are separate windows, so closing the chat with X after the pet was
  detached once (or with Settings or Setup open) left the host running with no chat window. The
  supervisor then found nothing titled "VOOL" to raise and started no new window: VOOL could not be
  reopened. The chat window's ``closed`` event must close every other window the host made.
* B16. The mark-folder confirm was ``MessageBoxW(None, ...)``: no owner, so it could open behind
  Settings with a taskbar button of its own. It must be owned by the VOOL window in front, and
  never by another program's window.

These drive installer/bundle/vool_window.py directly and never run its main(), which starts the
runtime supervisor.
"""
from __future__ import annotations

import os
import types

import pytest

from installer.bundle import vool_window


class _Event:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self) -> None:
        for fn in list(self.handlers):
            fn()


class _Window:
    def __init__(self, title: str, fail: bool = False) -> None:
        self.title = title
        self.destroyed = 0
        self._fail = fail
        self.events = types.SimpleNamespace(closed=_Event())

    def destroy(self) -> None:
        self.destroyed += 1
        if self._fail:
            raise RuntimeError("already gone")


class _Pet:
    def __init__(self) -> None:
        self.hidden = 0

    def hide(self) -> None:
        self.hidden += 1


def _host(**windows):
    api = vool_window._WindowApi()
    chat = _Window("VOOL")
    api.set_window(chat, object())
    for name, window in windows.items():
        setattr(api, name, window)
    return api, chat


def test_closing_the_chat_window_closes_the_pet_settings_and_setup(monkeypatch) -> None:
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    pet, companion, settings, setup = _Pet(), _Window("VOOL Companion"), _Window("VOOL Settings"), _Window("VOOL Setup")
    api, chat = _host(_pet=pet, _companion_window=companion, _settings_window=settings, _setup_window=setup)

    assert vool_window._close_secondaries_with(chat, api) is True
    chat.events.closed.fire()   # the person closed the chat window with X

    assert pet.hidden == 1, "the pet's cursor tracking must stop before its window goes"
    assert (companion.destroyed, settings.destroyed, setup.destroyed) == (1, 1, 1)
    assert api._pet is None and api._companion_window is None
    assert api._settings_window is None and api._setup_window is None
    assert chat.destroyed == 0, "the chat window is already closing; it is not destroyed again"


def test_a_hidden_pet_that_was_returned_to_the_app_is_still_closed(monkeypatch) -> None:
    """The exact B1 path: detach the pet, put it back (its window is only hidden), close the chat."""
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    companion = _Window("VOOL Companion")
    api, chat = _host(_pet=_Pet(), _companion_window=companion)
    api.attach_companion()            # hides the pet window; it still exists
    assert companion.destroyed == 0
    vool_window._close_secondaries_with(chat, api)
    chat.events.closed.fire()
    assert companion.destroyed == 1


def test_one_window_that_will_not_close_does_not_keep_the_others_open(monkeypatch) -> None:
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    companion, settings, setup = _Window("VOOL Companion", fail=True), _Window("VOOL Settings"), _Window("VOOL Setup")
    api, chat = _host(_companion_window=companion, _settings_window=settings, _setup_window=setup)
    vool_window._close_secondaries_with(chat, api)
    chat.events.closed.fire()
    assert (companion.destroyed, settings.destroyed, setup.destroyed) == (1, 1, 1)


def test_closing_the_chat_with_nothing_else_open_is_quiet(monkeypatch) -> None:
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    api, chat = _host()
    vool_window._close_secondaries_with(chat, api)
    chat.events.closed.fire()   # must not raise


def test_macos_keeps_close_to_dock(monkeypatch) -> None:
    """On macOS X hides the chat to the Dock and the app keeps running by design: no hook there."""
    monkeypatch.setattr(vool_window.sys, "platform", "darwin")
    settings = _Window("VOOL Settings")
    api, chat = _host(_settings_window=settings)
    assert vool_window._close_secondaries_with(chat, api) is False
    assert chat.events.closed.handlers == []


def test_a_window_without_events_does_not_stop_the_host(monkeypatch) -> None:
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    api = vool_window._WindowApi()
    assert vool_window._close_secondaries_with(object(), api) is False


def test_main_hooks_the_chat_window_right_after_creating_it() -> None:
    source = open(vool_window.__file__, encoding="utf-8").read()
    main = source.split("def main() -> int:", 1)[1]
    set_window = main.index("api.set_window(window, webview)")
    hook = main.index("_close_secondaries_with(window, api)")
    start = main.index("webview.start(")
    assert set_window < hook < start
    # Hooked to `closed`, never `closing`: a closing handler can veto the close.
    assert "events.closing +=" not in source


def test_the_teardown_is_not_a_page_bridge_call() -> None:
    """Public _WindowApi methods are callable from page script; closing windows is the host's alone."""
    assert not hasattr(vool_window._WindowApi, "close_secondary_windows")
    assert callable(vool_window._WindowApi._close_secondary_windows)


class _Fn:
    """A stand-in for a ctypes function: callable, and takes restype/argtypes like one."""

    def __init__(self, fn) -> None:
        self._fn = fn

    def __call__(self, *args):
        return self._fn(*args)


class _User32:
    def __init__(self, foreground: int, owner_pid: int) -> None:
        self._foreground = foreground
        self._owner_pid = owner_pid
        self.GetForegroundWindow = _Fn(self._get_foreground)
        self.GetWindowThreadProcessId = _Fn(self._get_pid)

    def _get_foreground(self) -> int:
        return self._foreground

    def _get_pid(self, hwnd, pid_ref) -> int:
        pid_ref._obj.value = self._owner_pid
        return 1


def test_a_vool_window_in_front_owns_the_confirm_box() -> None:
    owner = vool_window._own_foreground_window(_User32(0x1234, os.getpid()))
    assert owner is not None and owner.value == 0x1234


def test_another_programs_window_never_owns_the_confirm_box() -> None:
    assert vool_window._own_foreground_window(_User32(0x1234, os.getpid() + 1)) is None


def test_no_foreground_window_means_no_owner() -> None:
    assert vool_window._own_foreground_window(_User32(0, os.getpid())) is None


def test_the_confirm_box_is_given_the_owner(monkeypatch) -> None:
    calls: list[tuple] = []
    sentinel = object()

    def message_box(owner, message, title, flags):
        calls.append((owner, message, title, flags))
        return 1   # IDOK

    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(user32=types.SimpleNamespace(MessageBoxW=message_box)))
    monkeypatch.setattr(vool_window, "ctypes", fake_ctypes)
    monkeypatch.setattr(vool_window, "os", types.SimpleNamespace(name="nt", getpid=os.getpid))
    monkeypatch.setattr(vool_window, "_own_foreground_window", lambda user32: sentinel)

    assert vool_window._WindowApi()._confirm_natively("Mark folder", "Let VOOL change C:\\work?") is True
    assert len(calls) == 1
    owner, message, title, flags = calls[0]
    assert owner is sentinel
    assert (message, title) == ("Let VOOL change C:\\work?", "Mark folder")
    # Unchanged: OK/Cancel, warning icon, Cancel is the default button, brought to the foreground.
    assert flags == 0x1 | 0x30 | 0x100 | 0x10000


@pytest.mark.parametrize("answer, expected", [(1, True), (2, False)])
def test_cancel_still_refuses(monkeypatch, answer, expected) -> None:
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(
        user32=types.SimpleNamespace(MessageBoxW=lambda *a: answer)))
    monkeypatch.setattr(vool_window, "ctypes", fake_ctypes)
    monkeypatch.setattr(vool_window, "os", types.SimpleNamespace(name="nt", getpid=os.getpid))
    monkeypatch.setattr(vool_window, "_own_foreground_window", lambda user32: None)
    assert vool_window._WindowApi()._confirm_natively("t", "m") is expected
