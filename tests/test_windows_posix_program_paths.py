"""Windows never runs, stats or looks up a macOS program by its POSIX path.

On Windows a path with no drive letter resolves against the current drive: `/usr/bin/python3`
becomes `C:\\usr\\bin\\python3.exe`, and any account may create `C:\\usr`, `C:\\bin` or `C:\\opt`. PDF
reading ran exactly that interpreter with no platform check; Apple Notes stat'ed and then ran
`/usr/bin/osascript`. `core.posix_tools` refuses those spellings on Windows before any filesystem
call, and every POSIX program constant goes through it.

Each row switches the Windows rule on through `posix_tools._IS_WINDOWS` (never `os.name`, which
pathlib branches on) and records every spawn and every stat, access, exists and which call. A row
passes only when no call named a POSIX program folder (`/usr`, `/bin`, `/sbin`, `/opt`) or its
Windows landing place (`C:\\usr`, `C:\\bin`, `C:\\opt`). Where a feature also has a macOS check in
front of the guard, the row forces that check to say "this is a Mac", so it proves the guard itself
holds. The sabotage rows prove the recorder sees these calls when the guard is taken away, and the
off-Windows rows prove macOS and Linux keep their answers.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import posix_tools

_POSIX_PROGRAM = re.compile(
    r"^(?:[A-Za-z]:)?[\\/](?:usr|bin|sbin|opt)(?:[\\/]|$)", re.IGNORECASE
)


def _named(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    try:
        return os.fspath(value)
    except TypeError:
        return str(value)


class _Recorder:
    """Every spawn and every program lookup whose target is a POSIX program folder."""

    def __init__(self) -> None:
        self.spawns: list[str] = []
        self.lookups: list[tuple[str, str]] = []

    def _spawn(self, real):
        def wrapper(args, *rest, **kwargs):
            target = _named(args)
            if _POSIX_PROGRAM.match(target):
                self.spawns.append(target)
                raise AssertionError(f"spawned {target}")
            return real(args, *rest, **kwargs)

        return wrapper

    def _lookup(self, name, real):
        def wrapper(path, *rest, **kwargs):
            target = _named(path)
            if _POSIX_PROGRAM.match(target):
                self.lookups.append((name, target))
            return real(path, *rest, **kwargs)

        return wrapper

    def install(self, monkeypatch) -> _Recorder:
        monkeypatch.setattr(subprocess, "run", self._spawn(subprocess.run))
        monkeypatch.setattr(subprocess, "Popen", self._spawn(subprocess.Popen))
        monkeypatch.setattr(os, "access", self._lookup("os.access", os.access))
        monkeypatch.setattr(os, "stat", self._lookup("os.stat", os.stat))
        monkeypatch.setattr(os, "lstat", self._lookup("os.lstat", os.lstat))
        monkeypatch.setattr(os.path, "exists", self._lookup("os.path.exists", os.path.exists))
        monkeypatch.setattr(os.path, "isfile", self._lookup("os.path.isfile", os.path.isfile))
        monkeypatch.setattr(shutil, "which", self._lookup("shutil.which", shutil.which))
        return self

    def assert_untouched(self) -> None:
        assert self.spawns == [], self.spawns
        assert self.lookups == [], self.lookups


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    return _Recorder().install(monkeypatch)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)
    return _Recorder().install(monkeypatch)


@pytest.fixture
def a_pdf(tmp_path) -> Path:
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return pdf


def _pretend_mac(monkeypatch) -> None:
    """Every macOS check in front of the guard says "this is a Mac", so only the guard is left."""
    import platform

    from core.artifact_readers import _sandbox, speech_tool

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_sandbox, "_host_is_macos", lambda: True)
    monkeypatch.setattr(speech_tool, "_host_is_macos", lambda: True)


# ---------------------------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/usr/bin/python3", "/bin/sh", "/opt/homebrew/bin/ffmpeg", "\\usr\\bin\\osascript",
    "C:usr\\bin\\x", "\\\\host\\share\\x.exe", "//host/share/x", "\\\\?\\C:\\x.exe", "ffmpeg", "",
])
def test_on_windows_a_program_path_without_a_drive_is_refused(monkeypatch, path) -> None:
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    assert posix_tools.refused(path)
    with pytest.raises(FileNotFoundError) as caught:
        posix_tools.check(path)
    assert caught.value.filename == path
    assert posix_tools.executable(path) is False
    assert posix_tools.exists(path) is False
    assert posix_tools.missing_dependency(path)["status"] == "missing_dependency"


@pytest.mark.parametrize("path", ["C:\\Windows\\System32\\tasklist.exe", "c:/Program Files/x.exe"])
def test_on_windows_a_drive_rooted_program_path_is_used(monkeypatch, path) -> None:
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    assert not posix_tools.refused(path)
    assert posix_tools.check(path) == path
    assert posix_tools.argv([path, "/v"]) == [path, "/v"]


def test_the_refusal_comes_before_any_filesystem_call(windows) -> None:
    for path in ("/usr/bin/osascript", "/usr/bin/sandbox-exec", "/bin/sh", "/opt/homebrew/bin/ffprobe"):
        assert posix_tools.executable(path) is False
        assert posix_tools.exists(path) is False
        with pytest.raises(posix_tools.PosixProgramRefused):
            posix_tools.argv([path, "-v"])
    windows.assert_untouched()


def test_off_windows_every_answer_is_the_plain_os_answer(off_windows, tmp_path) -> None:
    program = tmp_path / "tool"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    for path in (str(program), str(tmp_path / "absent"), "/bin/sh", "/usr/bin/python3"):
        assert posix_tools.refused(path) is False
        assert posix_tools.executable(path) == os.access(path, os.X_OK)
        assert posix_tools.exists(path) == os.path.exists(path)
        assert posix_tools.argv([path, "x"]) == [path, "x"]


# ---------------------------------------------------------------------------------------------
# PDF reading (B2): refused on Windows before any spawn
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["extract_text", "ocr"])
def test_on_windows_pdf_reading_runs_nothing(windows, a_pdf, tool) -> None:
    from core import pdf_tools

    result = getattr(pdf_tools, tool)(str(a_pdf))
    assert result["status"] == "missing_dependency"
    assert result["reason"].startswith(pdf_tools.WINDOWS_UNAVAILABLE)
    assert result["text"] == ""
    windows.assert_untouched()


def test_on_windows_the_pdf_helper_itself_refuses_its_interpreter(windows) -> None:
    from core import pdf_tools

    result = pdf_tools._run_helper(pdf_tools._EXTRACT_HELPER, ["x.pdf", "0"], timeout=5)
    assert result["status"] == "missing_dependency"
    assert result["reason"].startswith(pdf_tools.WINDOWS_UNAVAILABLE)
    windows.assert_untouched()


def test_on_windows_a_missing_pdf_still_says_it_is_missing(windows, tmp_path) -> None:
    from core import pdf_tools

    assert pdf_tools.extract_text(str(tmp_path / "typo.pdf"))["status"] == "not_found"
    assert pdf_tools.ocr(str(tmp_path / "typo.pdf"))["status"] == "not_found"
    windows.assert_untouched()


def test_on_windows_the_pdf_tool_through_the_executor_runs_nothing(windows, a_pdf) -> None:
    """The catalog still lists pdf.* on Windows in 0.7.1; the tool itself refuses, before any spawn."""
    from core import pdf_tools
    from core.runtime_execution_tools import _pdf_extract_text, _pdf_ocr

    for handler in (_pdf_extract_text, _pdf_ocr):
        result = handler({"path": str(a_pdf)})
        assert (result.ok, result.status) == (False, "missing_dependency")
        assert pdf_tools.WINDOWS_UNAVAILABLE in result.response_text
    windows.assert_untouched()


def test_off_windows_the_pdf_helper_is_still_run(off_windows, monkeypatch, a_pdf) -> None:
    from core import pdf_tools

    ran: list[str] = []

    def fake_run(args, **kwargs):
        ran.append(args[0])
        return SimpleNamespace(returncode=0, stdout='{"status": "ok", "pages_total": 1, "pages_read": 1, "page_text": ["hi"]}', stderr="")

    monkeypatch.setattr(pdf_tools.subprocess, "run", fake_run)
    result = pdf_tools.extract_text(str(a_pdf))
    assert result["status"] == "ok"
    assert ran == ["/usr/bin/python3"]


class _FastPathAgent:
    def _fast_path_result(self, **kwargs):
        return dict(kwargs)


def _named_pdf_request(monkeypatch, pdf: Path) -> None:
    from core.agent_runtime import fast_paths_pdf

    monkeypatch.setattr(
        fast_paths_pdf,
        "pdf_read_request",
        lambda text: {"path": pdf, "named": str(pdf), "found": True, "wants_ocr": False},
    )
    monkeypatch.setattr(fast_paths_pdf, "_outside_allowed_roots", lambda path: False)


class _GatedAgent(_FastPathAgent):
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


def _gated_context(**extra):
    from core.mode_permission_policy import set_active_mode

    set_active_mode("s1", "manual")
    return {"session_id": "s1", "surface": "api", "operating_mode": "manual", **extra}


def test_on_windows_the_chat_fast_path_crosses_the_gate_then_says_so(windows, pdf_in_home) -> None:
    """The permission gate runs first, as on the model route; the tool itself then refuses."""
    from core import pdf_tools
    from core.agent_runtime import fast_paths_pdf

    reply = fast_paths_pdf.maybe_handle_pdf_read(
        _GatedAgent(), f"Extract the text from {pdf_in_home}", session_id="s1", source_context=_gated_context()
    )
    assert reply is not None
    assert reply["response"] == pdf_tools.WINDOWS_UNAVAILABLE
    assert dict((reply.get("details") or {}).get("permission") or {}).get("effect") == "allow"
    windows.assert_untouched()


def test_on_windows_a_project_deny_still_wins_on_the_pdf_fast_path(windows, monkeypatch, pdf_in_home) -> None:
    import core.runtime_execution_tools as ret
    from core.agent_runtime import fast_paths_pdf

    monkeypatch.setattr(ret, "_pdf_extract_text", lambda arguments: pytest.fail("ran past a deny"))
    reply = fast_paths_pdf.maybe_handle_pdf_read(
        _GatedAgent(), f"Extract the text from {pdf_in_home}", session_id="s1",
        source_context=_gated_context(project_permissions={"read_files": False}),
    )
    assert reply is not None
    assert dict((reply.get("details") or {}).get("permission") or {}).get("effect") == "deny"
    windows.assert_untouched()


def test_off_windows_the_chat_fast_path_still_runs_the_tool(off_windows, monkeypatch, a_pdf) -> None:
    from core import authorized_tool_execution
    from core.agent_runtime import fast_paths_pdf

    _named_pdf_request(monkeypatch, a_pdf)
    ran: list[str] = []

    def execute(intent, arguments, **kwargs):
        ran.append(intent)
        return None

    monkeypatch.setattr(authorized_tool_execution, "execute_authorized_runtime_tool", execute)
    assert fast_paths_pdf.maybe_handle_pdf_read(
        _FastPathAgent(), f"what does {a_pdf} say?", session_id="s", source_context={}
    ) is None
    assert ran == ["pdf.extract_text"]


# ---------------------------------------------------------------------------------------------
# Apple Notes (B13): not offered on Windows, and osascript is never stat'ed or run
# ---------------------------------------------------------------------------------------------


def test_on_windows_creating_an_apple_note_runs_nothing(windows) -> None:
    from core.operator import apple_notes

    outcome = apple_notes.create_apple_note(title="Harbour roster", body="night shift")
    assert (outcome["ok"], outcome["reason"]) == (False, "osascript_unavailable")
    assert outcome["detail"].startswith(apple_notes.WINDOWS_UNAVAILABLE_DETAIL)
    assert "No note was created." in outcome["detail"]
    windows.assert_untouched()


def test_on_windows_the_notes_bridge_runs_nothing(windows) -> None:
    from core.operator import apple_notes

    outcome = apple_notes.run_notes_bridge('tell application "Notes" to count notes')
    assert (outcome["ok"], outcome["reason"]) == (False, "osascript_unavailable")
    assert outcome["detail"] == apple_notes.WINDOWS_UNAVAILABLE_DETAIL
    windows.assert_untouched()


_APPLE_NOTE_TOOLS = ("apple_note_list", "apple_note_read", "apple_note_append", "apple_note_rename", "apple_note_delete")


def test_on_windows_apple_notes_tools_are_not_advertised(monkeypatch) -> None:
    from core.operator import registry

    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    tools = {tool["tool_id"]: tool for tool in registry.list_operator_tools()}
    ledger = {entry["capability_id"]: entry for entry in registry.operator_capability_ledger()}
    for tool_id in _APPLE_NOTE_TOOLS:
        assert tools[tool_id]["available"] is False
        entry = ledger[f"operator.{tool_id}"]
        assert entry["supported"] is False
        assert entry["support_level"] == "unsupported"
        assert entry["unsupported_reason"] == "Apple Notes is a Mac app and isn't available on Windows."


def test_off_windows_apple_notes_tools_are_advertised_as_before(monkeypatch) -> None:
    from core.operator import registry

    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)
    tools = {tool["tool_id"]: tool for tool in registry.list_operator_tools()}
    ledger = {entry["capability_id"]: entry for entry in registry.operator_capability_ledger()}
    for tool_id in _APPLE_NOTE_TOOLS:
        assert tools[tool_id]["available"] is True
        assert ledger[f"operator.{tool_id}"]["support_level"] == "full"
        assert ledger[f"operator.{tool_id}"]["unsupported_reason"] == (
            f"Operator capability `{tool_id}` is not available on this host/runtime."
        )


# ---------------------------------------------------------------------------------------------
# Every other POSIX program constant, with the macOS checks in front of the guard forced to "Mac"
# ---------------------------------------------------------------------------------------------


def test_on_windows_no_reader_or_toolchain_check_touches_a_posix_program(windows, monkeypatch) -> None:
    from core import notifications_macos
    from core.artifact_readers import _sandbox, archive, office, speech_tool, video, vision_tool
    from core.wallet import device_auth
    from sandbox import job_runner

    _pretend_mac(monkeypatch)
    monkeypatch.setattr(device_auth, "shipped_helper", lambda: None)
    monkeypatch.setattr(video.shutil, "which", lambda name: None)
    assert _sandbox.confinement_available() is False
    assert archive._bsdtar_available() is False
    assert office.doc_converter_available() is False
    assert vision_tool.toolchain_available() is False
    assert speech_tool.toolchain_available() is False
    assert video.ffmpeg_path() == ""
    assert video.ffprobe_path() == ""
    assert notifications_macos.toolchain_available() is False
    assert device_auth.toolchain_available() is False
    assert job_runner._trusted_sandbox_exec() is None
    windows.assert_untouched()


def test_on_windows_every_spawn_site_refuses_before_it_spawns(windows, monkeypatch, tmp_path) -> None:
    """The availability checks above are the first wall; each spawn is checked again on its own."""
    from core import notifications_macos
    from core.artifact_readers import _sandbox, speech_tool, vision_tool
    from core.artifact_readers._types import ReaderUnavailable
    from core.wallet import device_auth

    _pretend_mac(monkeypatch)
    monkeypatch.setattr(vision_tool, "toolchain_available", lambda: True)
    monkeypatch.setattr(vision_tool, "tools_dir", lambda: tmp_path / "vision")
    with pytest.raises(ReaderUnavailable) as caught:
        vision_tool.ensure_tool()
    assert caught.value.code == "vision_build_failed"

    monkeypatch.setattr(speech_tool, "_platform_unsupported", lambda: None)
    monkeypatch.setattr(speech_tool, "toolchain_available", lambda: True)
    monkeypatch.setattr(speech_tool, "tools_dir", lambda: tmp_path / "speech")
    with pytest.raises(ReaderUnavailable) as caught:
        speech_tool.ensure_tool()
    assert caught.value.code == "speech_build_failed"

    monkeypatch.setattr(notifications_macos, "toolchain_available", lambda: True)
    with pytest.raises(FileNotFoundError):
        notifications_macos.build_helper_app(tmp_path / "Helper.app", bundle_identifier="local.vool.test")

    with pytest.raises(FileNotFoundError):
        device_auth.compile_helper(tmp_path / "vool-devauth")

    with pytest.raises(FileNotFoundError):
        _sandbox._limited_argv(["C:\\tools\\decoder.exe"], cpu_seconds=5)

    windows.assert_untouched()


def test_on_windows_a_confined_decoder_with_a_posix_path_is_refused(windows, monkeypatch, tmp_path) -> None:
    from core.artifact_readers import _sandbox
    from core.artifact_readers._types import ReaderFailed

    monkeypatch.setattr(_sandbox, "require_confinement", lambda fmt: None)
    with pytest.raises(ReaderFailed) as caught:
        _sandbox.run_confined(["/usr/bin/bsdtar", "-tvf", "x.rar"], scratch=tmp_path, fmt="rar")
    assert caught.value.code == "decoder_not_executable"
    windows.assert_untouched()


# ---------------------------------------------------------------------------------------------
# Sabotage: the recorder sees these calls once the guard is gone
# ---------------------------------------------------------------------------------------------


def test_sabotage_without_the_guard_apple_notes_stats_osascript(windows, monkeypatch) -> None:
    from core.operator import apple_notes

    monkeypatch.setattr(posix_tools, "refused", lambda path: False)
    apple_notes.create_apple_note(title="t", body="b", runner=None)
    assert ("os.access", "/usr/bin/osascript") in windows.lookups


def test_sabotage_without_the_guard_pdf_reading_spawns_the_posix_interpreter(windows, monkeypatch, a_pdf) -> None:
    from core import pdf_tools

    monkeypatch.setattr(posix_tools, "refused", lambda path: False)
    monkeypatch.setattr(posix_tools, "active", lambda: False)
    with pytest.raises(AssertionError, match="spawned /usr/bin/python3"):
        pdf_tools.extract_text(str(a_pdf))
    assert windows.spawns == ["/usr/bin/python3"]


def test_sabotage_without_the_guard_the_reader_checks_stat_posix_programs(windows, monkeypatch) -> None:
    from core.artifact_readers import _sandbox, archive

    _pretend_mac(monkeypatch)
    monkeypatch.setattr(posix_tools, "refused", lambda path: False)
    _sandbox.confinement_available()
    archive._bsdtar_available()
    seen = {target for _, target in windows.lookups}
    assert {"/usr/bin/sandbox-exec", "/usr/bin/bsdtar"} <= seen


def test_the_recorder_flags_the_windows_landing_places(monkeypatch) -> None:
    recorder = _Recorder().install(monkeypatch)
    for target in ("C:\\usr\\bin\\python3.exe", "c:/bin/sh", "C:\\opt\\homebrew\\bin\\ffmpeg", "/usr/bin/x"):
        os.access(target, os.X_OK)
    os.access("C:\\Users\\me\\usr", os.X_OK)
    assert [target for _, target in recorder.lookups] == [
        "C:\\usr\\bin\\python3.exe", "c:/bin/sh", "C:\\opt\\homebrew\\bin\\ffmpeg", "/usr/bin/x",
    ]


# ---------------------------------------------------------------------------------------------
# Apple Notes asked for in chat on Windows: the Mac-only answer, from the real front door, no model
# ---------------------------------------------------------------------------------------------


_APPLE_NOTES_REQUESTS = (
    "make an Apple note called test",
    "make me an apple note about the trip",
    "add this to Apple Notes: call the bank",
    "put call the bank in apple notes",
    "new apple note: milk",
)
_NOT_APPLE_NOTES_REQUESTS = (
    "what is Apple Notes?",
    'save a note titled "Agenda" with: release checks',
    "make a note called test",
)
#: Phrasings the operator parser claims: their own lane answers them, so this one stands down.
_PARSER_CLAIMED_APPLE_NOTES = (
    "create an apple note called groceries with milk",
    "list my Apple notes",
    'delete my Apple note "Plan"',
)


@pytest.mark.parametrize("text", _APPLE_NOTES_REQUESTS)
def test_on_windows_an_apple_notes_request_gets_the_mac_only_answer(windows, text) -> None:
    from core.agent_runtime.fast_paths_apple_notes_windows import (
        WINDOWS_APPLE_NOTES_ANSWER,
        maybe_refuse_apple_notes_on_windows,
    )

    result = maybe_refuse_apple_notes_on_windows(_FastPathAgent(), text, session_id="s", source_context={})
    assert result is not None, text
    assert result["response"] == WINDOWS_APPLE_NOTES_ANSWER
    assert result["reason"] == "apple_notes_unavailable_on_windows"
    windows.assert_untouched()


@pytest.mark.parametrize("text", _NOT_APPLE_NOTES_REQUESTS + _PARSER_CLAIMED_APPLE_NOTES)
def test_on_windows_a_message_this_lane_does_not_own_is_left_alone(windows, text) -> None:
    from core.agent_runtime.fast_paths_apple_notes_windows import maybe_refuse_apple_notes_on_windows

    assert maybe_refuse_apple_notes_on_windows(_FastPathAgent(), text, session_id="s", source_context={}) is None


@pytest.mark.parametrize("text", _APPLE_NOTES_REQUESTS)
def test_off_windows_the_apple_notes_lane_stands_down(off_windows, text) -> None:
    from core.agent_runtime.fast_paths_apple_notes_windows import maybe_refuse_apple_notes_on_windows

    assert maybe_refuse_apple_notes_on_windows(_FastPathAgent(), text, session_id="s", source_context={}) is None


def _front_door(agent, text: str):
    from types import SimpleNamespace as Namespace
    from unittest import mock

    with mock.patch.object(agent, "_startup_sequence_fast_path", return_value=None), mock.patch(
        "core.agent_runtime.agent.maybe_handle_preference_command", return_value=(False, "")
    ), mock.patch.object(agent, "_maybe_handle_credit_command", return_value=None), mock.patch.object(
        agent, "_maybe_handle_workspace_audit_request", return_value=None
    ):
        return agent._handle_turn_frontdoor(
            raw_user_input=text,
            effective_input=text,
            normalized_input=text,
            source_surface="openclaw",
            session_id="apple-notes-windows",
            source_context={"surface": "openclaw", "_owner_local": True},
            persona=mock.sentinel.persona,
            interpreted=Namespace(understanding_confidence=0.8),
        )


def test_on_windows_the_front_door_answers_make_an_apple_note_without_a_model(windows) -> None:
    """The phrasing measured on Windows 11 that reached qwen3:8b and failed after 263 s."""
    from apps.vool_agent import VoolAgent
    from core.agent_runtime.fast_paths_apple_notes_windows import WINDOWS_APPLE_NOTES_ANSWER

    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    outcome = _front_door(agent, "make an Apple note called test")
    result = outcome.get("result")
    assert result is not None, "the front door let an Apple Notes request fall through to a model"
    assert str(result.get("response") or "") == WINDOWS_APPLE_NOTES_ANSWER
    # The agent itself reads this Linux host's own files on the way in (zoneinfo, a PATH walk), so
    # this row pins only what the Notes request could reach: nothing spawned, osascript never looked at.
    assert windows.spawns == []
    assert not [target for _, target in windows.lookups if "osascript" in target], windows.lookups


def test_on_windows_a_parser_claimed_apple_notes_request_still_runs_nothing(windows) -> None:
    """The operator lane's own Windows answer, for the phrasings this lane leaves to it."""
    from core.operator import apple_notes

    for text in _PARSER_CLAIMED_APPLE_NOTES:
        outcome = apple_notes.run_notes_bridge(text)
        assert outcome["reason"] == "osascript_unavailable", text
    windows.assert_untouched()
