"""A failed one-time build of the OCR or speech helper is a typed ReaderUnavailable, never a TypeError.

Mac 0.7.2 raised ``ReaderUnavailable(*_build_failed_words(), code=...)``, which passes the remediation as a
second positional argument to an ``__init__`` that takes it by keyword only. A failed build (the compiler
missing, refused, timing out or exiting non-zero) then crashed with a TypeError instead of telling the person
what to do. Each build failure must raise ReaderUnavailable with the words and the remediation.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from core.artifact_readers import speech_tool, vision_tool
from core.artifact_readers._types import ReaderUnavailable

_TOOLS = [(vision_tool, "vision_build_failed"), (speech_tool, "speech_build_failed")]


def _arm(monkeypatch, tool, tmp_path) -> None:
    if hasattr(tool, "_platform_unsupported"):
        monkeypatch.setattr(tool, "_platform_unsupported", lambda: None)
    monkeypatch.setattr(tool, "toolchain_available", lambda: True)
    monkeypatch.setattr(tool, "_binary_path", lambda: tmp_path / "bin" / "tool")


@pytest.mark.parametrize(("tool", "code"), _TOOLS, ids=["vision", "speech"])
def test_a_compiler_that_cannot_start_is_a_typed_refusal(monkeypatch, tmp_path, tool, code) -> None:
    _arm(monkeypatch, tool, tmp_path)

    def _refuse(*_a, **_k):
        raise OSError(2, "the compiler could not be started")

    monkeypatch.setattr(tool.subprocess, "run", _refuse)
    with pytest.raises(ReaderUnavailable) as caught:
        tool.ensure_tool()
    message, remediation = tool._build_failed_words()
    assert caught.value.code == code
    assert caught.value.message == message
    assert caught.value.remediation == remediation


@pytest.mark.parametrize(("tool", "code"), _TOOLS, ids=["vision", "speech"])
def test_a_compiler_that_times_out_is_a_typed_refusal(monkeypatch, tmp_path, tool, code) -> None:
    _arm(monkeypatch, tool, tmp_path)

    def _slow(*_a, **_k):
        raise subprocess.TimeoutExpired("swiftc", 300)

    monkeypatch.setattr(tool.subprocess, "run", _slow)
    with pytest.raises(ReaderUnavailable) as caught:
        tool.ensure_tool()
    assert caught.value.code == code
    assert caught.value.remediation == tool._build_failed_words()[1]


@pytest.mark.parametrize(("tool", "code"), _TOOLS, ids=["vision", "speech"])
def test_a_compiler_that_fails_is_a_typed_refusal(monkeypatch, tmp_path, tool, code) -> None:
    _arm(monkeypatch, tool, tmp_path)
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stderr=b"error: no"))
    with pytest.raises(ReaderUnavailable) as caught:
        tool.ensure_tool()
    assert caught.value.code == code
    assert caught.value.remediation == tool._build_failed_words()[1]
