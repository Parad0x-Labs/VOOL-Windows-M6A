"""Windows: pdf.* is not offered, and a request to read a PDF gets one plain, Windows-true answer.

In 0.7.1 the Windows catalog still listed `pdf.extract_text` and `pdf.ocr`; the tool itself then
refused inside `core.pdf_tools`, with a reason that talked about "the macOS system Python". The
model was offered a tool that could never work here, and the user was told about a Mac.

The contract now says so at the source: on Windows the two PDF contracts are unsupported, with the
Windows reason. Every reader of the catalog honours `supported` (the model's tool list, the
native definitions, the capability ledger, the operation catalog, direct dispatch), so the tools
leave all of them at once. The in-tool refusal stays as a second wall.

Windows is switched on through `posix_tools._IS_WINDOWS`, as the other Windows-branch tests do,
never through `os.name` or `sys.platform`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import posix_tools

PDF_INTENTS = {"pdf.extract_text", "pdf.ocr"}


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)


@pytest.fixture
def pdf_tools_never_run(monkeypatch):
    from core import pdf_tools

    def refuse(*args, **kwargs):
        raise AssertionError("a PDF tool ran on Windows")

    monkeypatch.setattr(pdf_tools, "extract_text", refuse)
    monkeypatch.setattr(pdf_tools, "ocr", refuse)


def _specs() -> list[dict]:
    from core.execution.capabilities import runtime_tool_specs

    return list(
        runtime_tool_specs(
            allow_web_fallback_fn=lambda: True,
            allow_browser_fallback_fn=lambda: True,
        )
    )


def _offered_intents() -> set[str]:
    return {str(spec.get("intent") or "") for spec in _specs()}


# ---------------------------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------------------------


def test_on_windows_the_model_is_not_offered_the_pdf_tools(windows) -> None:
    assert not (_offered_intents() & PDF_INTENTS)


def test_on_windows_no_native_pdf_definition_is_published(windows) -> None:
    from core.cloud_tool_call_contract import build_cloud_tool_definitions

    definitions = build_cloud_tool_definitions(_specs())
    assert not [item.name for item in definitions if item.name.startswith("pdf__")]
    assert not [item.intent for item in definitions if item.intent in PDF_INTENTS]


def test_off_windows_the_pdf_tools_are_still_offered(off_windows) -> None:
    assert _offered_intents() >= PDF_INTENTS


def test_on_windows_the_pdf_contracts_say_why_in_windows_words(windows) -> None:
    from core.pdf_tools import WINDOWS_UNAVAILABLE
    from core.runtime_tool_contracts import runtime_tool_contracts

    contracts = {c.intent: c for c in runtime_tool_contracts() if c.intent in PDF_INTENTS}
    assert set(contracts) == PDF_INTENTS
    for contract in contracts.values():
        assert contract.supported is False
        assert contract.unsupported_reason == WINDOWS_UNAVAILABLE
        assert "macos" not in contract.unsupported_reason.lower()


def test_on_windows_the_capability_ledger_lists_pdf_reading_as_unavailable(windows) -> None:
    from core.pdf_tools import WINDOWS_UNAVAILABLE
    from core.runtime_execution_tools import runtime_execution_capability_ledger

    row = next(r for r in runtime_execution_capability_ledger() if r["capability_id"] == "pdf.read")
    assert row["supported"] is False
    assert row["dispatchable_intents"] == []
    assert row["unsupported_reason"] == WINDOWS_UNAVAILABLE


def test_off_windows_the_capability_ledger_still_offers_pdf_reading(off_windows) -> None:
    from core.runtime_execution_tools import runtime_execution_capability_ledger

    row = next(r for r in runtime_execution_capability_ledger() if r["capability_id"] == "pdf.read")
    assert row["supported"] is True
    assert set(row["dispatchable_intents"]) == PDF_INTENTS


def test_on_windows_the_operation_catalog_text_does_not_list_pdf(windows) -> None:
    from core.semantic.operation_catalog import catalog_text

    assert "pdf.extract_text" not in catalog_text()
    assert "pdf.ocr" not in catalog_text()


def test_with_file_reading_off_the_reason_is_still_the_policy_one(windows, monkeypatch) -> None:
    """The Windows reason does not hide a policy refusal that would apply on any host."""
    from core import policy_engine
    from core.runtime_tool_contracts import runtime_tool_contracts

    real_get = policy_engine.get

    def get(key, default=None):
        if key == "filesystem.allow_read_workspace":
            return False
        return real_get(key, default)

    monkeypatch.setattr(policy_engine, "get", get)
    contract = next(c for c in runtime_tool_contracts() if c.intent == "pdf.extract_text")
    assert contract.supported is False
    assert contract.unsupported_reason == "Local file reading is disabled on this runtime."


# ---------------------------------------------------------------------------------------------
# Direct dispatch: a model that names the tool anyway
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("intent", sorted(PDF_INTENTS))
def test_on_windows_a_direct_pdf_call_is_refused_before_the_tool(
    windows, pdf_tools_never_run, tmp_path, intent
) -> None:
    from core.pdf_tools import WINDOWS_UNAVAILABLE
    from core.runtime_execution_tools import execute_runtime_tool

    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = execute_runtime_tool(intent, {"path": str(pdf)}, source_context={})
    assert result is not None
    assert (result.ok, result.status) == (False, "disabled")
    assert result.response_text == WINDOWS_UNAVAILABLE


# ---------------------------------------------------------------------------------------------
# The chat fast path ("what does report.pdf say?")
# ---------------------------------------------------------------------------------------------


class _GatedAgent:
    def _fast_path_result(self, **kwargs):
        return dict(kwargs)

    def _emit_runtime_event(self, source_context, **kwargs) -> None:
        return None


@pytest.fixture
def pdf_in_home(tmp_path, monkeypatch) -> Path:
    import core.runtime_execution_tools as ret
    from core.mode_permission_policy import reset_mode_permission_state

    home = tmp_path / "machinehome"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(ret, "_machine_home", lambda: home)
    pdf = home / "Documents" / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    reset_mode_permission_state()
    yield pdf
    reset_mode_permission_state()


def _gated_context():
    from core.mode_permission_policy import set_active_mode

    set_active_mode("s1", "manual")
    return {"session_id": "s1", "surface": "api", "operating_mode": "manual"}


@pytest.mark.parametrize(
    "message",
    [
        "what does {pdf} say?",
        "Extract the text from {pdf}",
        "OCR the scanned pdf {pdf}",
    ],
)
def test_on_windows_a_pdf_request_gets_one_plain_windows_answer(
    windows, pdf_tools_never_run, pdf_in_home, message
) -> None:
    from core.agent_runtime import fast_paths_pdf
    from core.pdf_tools import WINDOWS_UNAVAILABLE

    reply = fast_paths_pdf.maybe_handle_pdf_read(
        _GatedAgent(), message.format(pdf=pdf_in_home), session_id="s1", source_context=_gated_context()
    )
    assert reply is not None
    assert reply["response"] == WINDOWS_UNAVAILABLE
    assert "macos" not in reply["response"].lower()
    assert "python" not in reply["response"].lower()


# ---------------------------------------------------------------------------------------------
# The second wall: the tool itself, reached without the catalog
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["extract_text", "ocr"])
def test_on_windows_the_tool_itself_still_refuses_in_windows_words(windows, tmp_path, tool) -> None:
    from core import pdf_tools

    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = getattr(pdf_tools, tool)(str(pdf))
    assert result["status"] == "missing_dependency"
    assert result["reason"] == pdf_tools.WINDOWS_UNAVAILABLE
    assert "macos" not in result["reason"].lower()
