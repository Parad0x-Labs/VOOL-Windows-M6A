"""Off macOS, dictation says what is true on THIS host, never "install the Xcode tools".

The recogniser is a Swift helper over Apple's Speech framework, so a host that is not macOS can
never run it. The chat page probes ``GET /api/chat/dictation`` on every load and shows the answer
in the strip under the composer; before this law a Windows host was told it had "no Swift
toolchain" and to run ``xcode-select --install``, which no Windows user can do.

The host is simulated on ``platform.system``, the exact check ``speech_tool`` makes; macOS keeps
its own reasons (pinned by the macOS-only suites and unchanged here).
"""

from __future__ import annotations

import json
import platform
import subprocess

import pytest

from core.artifact_readers import speech_tool
from core.artifact_readers._types import ReaderUnavailable
from core.web.api.service import dispatch_dictation
from tests.chat_page_js_harness import DOM, run_node, script

WINDOWS_REASON = {
    "available": False,
    "error": "speech_platform_unsupported",
    "message": "Dictation and audio transcription are not available on Windows yet.",
    "remediation": "Type your message instead.",
}


def _host(monkeypatch, system: str) -> None:
    """Simulate the host, and fail loudly if anything tries to probe or build the Swift helper."""
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(speech_tool, "_PROBE_CACHE", {})

    def no_probe(**_kwargs):
        raise AssertionError("a host that is not macOS must not probe the speech helper")

    def no_build(*_args, **_kwargs):
        raise AssertionError("a host that is not macOS must not compile the speech helper")

    monkeypatch.setattr(speech_tool, "probe", no_probe)
    monkeypatch.setattr(subprocess, "run", no_build)


def _get_availability() -> tuple[int, dict]:
    response = dispatch_dictation(
        method="GET", raw_body=b"", headers={}, runtime=None, client_host="127.0.0.1",
        query="locale=en-US",
    )
    return response.status, json.loads(response.body)


def test_windows_availability_probe_names_the_platform_not_xcode(monkeypatch) -> None:
    _host(monkeypatch, "Windows")
    status, body = _get_availability()
    assert status == 200
    assert body == WINDOWS_REASON
    rendered = json.dumps(body)
    assert "Xcode" not in rendered and "Swift" not in rendered and "xcode-select" not in rendered


def test_another_host_that_is_not_macos_is_named_generically(monkeypatch) -> None:
    _host(monkeypatch, "Linux")
    status, body = _get_availability()
    assert status == 200
    assert body["available"] is False
    assert body["error"] == "speech_platform_unsupported"
    assert body["message"] == "Dictation and audio transcription are not available on this system."
    assert body["remediation"] == "Type your message instead."
    assert "Xcode" not in json.dumps(body)


def test_windows_dictation_attempt_is_refused_with_the_platform_reason(monkeypatch) -> None:
    """The failure path: a recording that reaches the door anyway is refused typed (503), and the
    helper is never built or run for it."""
    _host(monkeypatch, "Windows")
    response = dispatch_dictation(
        method="POST",
        raw_body=b"RIFF\x00\x00\x00\x00WAVEfmt ",
        headers={"x-vool-session-id": "chat-1", "x-vool-audio-type": "audio/x-wav"},
        runtime=None,
        client_host="127.0.0.1",
    )
    assert response.status == 503
    body = json.loads(response.body)
    assert body == {
        "ok": False,
        "error": "speech_platform_unsupported",
        "message": WINDOWS_REASON["message"],
        "remediation": WINDOWS_REASON["remediation"],
    }
    with pytest.raises(ReaderUnavailable) as caught:
        speech_tool.ensure_tool()
    assert caught.value.code == "speech_platform_unsupported"
    assert "Xcode" not in caught.value.remediation


def test_windows_chat_page_strip_shows_the_platform_reason(monkeypatch) -> None:
    """The user's view: the real page script, booted with the real door's Windows answer, puts
    the platform reason and a remedy the user can act on in the strip under the composer."""
    with monkeypatch.context() as served_by:
        _host(served_by, "Windows")
        status, body = _get_availability()
    assert status == 200
    program = (
        DOM
        + "\n;(async function(){\n"
        + "const served = " + json.dumps(body) + ";\n"
        + "const baseFetch = globalThis.fetch;\n"
        + "globalThis.fetch = (url, opts) => String(url).indexOf('/api/chat/dictation') === 0\n"
        + "  ? Promise.resolve({ ok: true, status: 200, json: async () => served })\n"
        + "  : baseFetch(url, opts);\n"
        + script()
        + "\nconst tick = () => new Promise((r) => process.nextTick(r));\n"
        + "for (let i = 0; i < 40; i++) await tick();\n"
        + "const note = document.getElementById('dictationNote');\n"
        + "out({ note: note.textContent, noteHidden: note.hidden,"
        + " noticeHidden: document.getElementById('dictationNotice').hidden });\n"
        + "\n})();\n"
    )
    shown = run_node(program, timeout=120)
    assert not shown["errors"], shown["errors"]
    assert shown["note"] == "Dictation and audio transcription are not available on Windows yet. Type your message instead."
    assert shown["noteHidden"] is False and shown["noticeHidden"] is False
    assert "Xcode" not in shown["note"] and "Swift" not in shown["note"]
