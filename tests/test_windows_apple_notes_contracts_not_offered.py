"""Windows: the model is never offered the Apple Notes read and list tools.

The operator registry already reports every ``apple_note_*`` tool unavailable on Windows
(tests/test_windows_posix_program_paths.py), but the runtime tool contracts still declared
``operator.apple_note_read`` and ``operator.apple_note_list`` supported on every host, so they were
seated in the model-facing tool specs on Windows and failed with "osascript not present". On Windows
they are now unsupported with the same Mac-only reason; off Windows they are offered as before.
"""
from __future__ import annotations

import pytest

from core import posix_tools

INTENTS = ("operator.apple_note_read", "operator.apple_note_list")
REASON = "Apple Notes is a Mac app and isn't available on Windows."


def _contracts():
    from core.runtime_tool_contracts import runtime_tool_contracts

    return {contract.intent: contract for contract in runtime_tool_contracts()}


def _offered() -> set[str]:
    from core.execution.capabilities import runtime_tool_specs

    return {str(spec.get("intent") or "") for spec in runtime_tool_specs()}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


def test_on_windows_the_apple_notes_contracts_are_unsupported_and_not_offered(monkeypatch, isolated_home) -> None:
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    contracts = _contracts()
    for intent in INTENTS:
        assert contracts[intent].supported is False, intent
        assert contracts[intent].unsupported_reason == REASON
    assert not set(INTENTS) & _offered()


def test_off_windows_the_apple_notes_contracts_are_offered_as_before(monkeypatch, isolated_home) -> None:
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)
    contracts = _contracts()
    for intent in INTENTS:
        assert contracts[intent].supported is True, intent
        assert contracts[intent].unsupported_reason == ""
    assert set(INTENTS) <= _offered()
