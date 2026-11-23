"""0.7.3, Steve row 10: VOOL Drop on Windows.

Drop runs on Windows since 0.7.3 (core/drop/service.py: owner-only ACLs, VOOL's home or a marked folder),
but only from its controller command line with VOOL_DROP=1; no chat tool reaches it on any OS. The 0.7.2
chat answer on Windows still said Drop "isn't available on Windows in this release", which the 0.7.3 note
contradicts. These rows pin the true answer on Windows and leave macOS and Linux as they were.
"""
from __future__ import annotations

import pytest

from core import posix_tools
from core.agent_runtime import fast_paths_windows_unavailable as lane

_DROP_REQUESTS = (
    "send this file to my phone with VOOL Drop",
    "turn on vool drop",
    "use VOOL Drop to send C:\\VOOL-Scratch\\drop-test.txt",
)


class _FastPathAgent:
    def _fast_path_result(self, **kwargs):
        return dict(kwargs)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)


@pytest.mark.parametrize("text", _DROP_REQUESTS)
def test_on_windows_a_drop_request_is_told_drop_runs_from_its_command_line_not_chat(windows, text) -> None:
    result = lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={})
    assert result is not None, text
    answer = result["response"]
    assert answer == lane.WINDOWS_DROP_ANSWER
    assert "doesn't run from chat" in answer
    assert "command-line preview" in answer
    assert "nothing was sent or received" in answer


def test_the_drop_answer_no_longer_says_drop_is_unavailable_on_windows() -> None:
    assert "isn't available on Windows" not in lane.WINDOWS_DROP_ANSWER
    assert "not available on Windows" not in lane.WINDOWS_DROP_ANSWER


@pytest.mark.parametrize("text", _DROP_REQUESTS)
def test_off_windows_the_drop_lane_stands_down(off_windows, text) -> None:
    assert lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={}) is None


# --- The prompts Steve typed for row 10 (0.7.3 at ab0ca48c), verbatim. ---------------------------

_STEVE_MARKED_FILE = (
    "C:\\Users\\Satoshi\\Documents\\VOOL Windows\\missions\\capability-test\\r073-final2\\marked\\drop-test.txt"
)
_STEVE_SEND = f"send the file {_STEVE_MARKED_FILE} to my phone with VOOL Drop"
_STEVE_DROP = f"drop {_STEVE_MARKED_FILE}"


@pytest.fixture
def windows_paths(monkeypatch):
    from core import windows_write_gate

    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)


@pytest.fixture
def posix_paths(monkeypatch):
    from core import windows_write_gate

    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)


def test_on_windows_steves_send_prompt_gets_the_drop_answer_even_with_a_dotted_path_in_it(windows) -> None:
    result = lane.maybe_refuse_windows_unavailable(_FastPathAgent(), _STEVE_SEND, session_id="s", source_context={})
    assert result is not None
    assert result["response"] == lane.WINDOWS_DROP_ANSWER


@pytest.mark.parametrize(
    ("text", "answer"),
    (
        ("open the Skill Codex. Then read C:\\notes\\a.txt", lane.WINDOWS_SKILL_CODEX_ANSWER),
        ("send C:\\x\\cal.ics to my Apple Calendar", lane.WINDOWS_APPLE_CALENDAR_ANSWER),
    ),
)
def test_on_windows_a_dotted_path_does_not_hide_the_other_two_requests_either(windows, text, answer) -> None:
    result = lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={})
    assert result is not None, text
    assert result["response"] == answer


@pytest.mark.parametrize(
    "text",
    (
        "use VOOL Drop. That is all",  # verb and subject in one sentence: still a request
    ),
)
def test_on_windows_a_request_in_one_sentence_still_matches(windows, text) -> None:
    assert lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={}) is not None


@pytest.mark.parametrize(
    "text",
    (
        "I read the notes. VOOL Drop is interesting",  # the verb's sentence ended at ". "
        "tell me about VOOL Drop",
    ),
)
def test_on_windows_a_sentence_break_still_ends_the_request(windows, text) -> None:
    assert lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={}) is None


def test_off_windows_steves_send_prompt_is_not_claimed_by_the_lane(off_windows) -> None:
    assert lane.maybe_refuse_windows_unavailable(_FastPathAgent(), _STEVE_SEND, session_id="s", source_context={}) is None


# --- The workspace read lane never reduces a Windows path to its last name. -----------------------


@pytest.mark.parametrize("text", (_STEVE_SEND, _STEVE_DROP))
def test_on_windows_steves_prompts_are_not_read_as_a_bare_name_in_the_chat_folder(windows_paths, text) -> None:
    from core.agent_runtime import fast_paths_utility as utility

    assert utility._direct_workspace_read_requests(text) == []


@pytest.mark.parametrize(
    "text",
    (
        "read C:\\Users\\me\\Documents\\notes.txt",
        "what is in D:\\work\\VOOL Windows\\plan.md",
        "read \\\\server\\share\\notes.txt",
        "print c:/Users/me/notes.txt",
        "read C:\\x\\e.txt and D:\\y\\f.txt",
    ),
)
def test_on_windows_a_rooted_backslash_path_is_left_to_the_machine_lanes(windows_paths, text) -> None:
    from core.agent_runtime import fast_paths_utility as utility

    assert utility._direct_workspace_read_requests(text) == []


@pytest.mark.parametrize(
    ("text", "paths"),
    (
        ("read src\\calc.py", ["src/calc.py"]),
        ("read notes.txt and src\\a\\b.md", ["notes.txt", "src/a/b.md"]),
        ("read notes.txt and C:\\x\\y.txt", ["notes.txt"]),
        ("read notes.txt", ["notes.txt"]),
    ),
)
def test_on_windows_a_relative_backslash_path_is_read_whole(windows_paths, text, paths) -> None:
    from core.agent_runtime import fast_paths_utility as utility

    assert [item["path"] for item in utility._direct_workspace_read_requests(text)] == paths


def test_on_windows_the_cap_note_never_names_a_bare_tail_of_a_rooted_path(windows_paths) -> None:
    from core.agent_runtime import fast_paths_utility as utility

    text = "read a.txt, b.txt, c.txt, d.txt, C:\\x\\e.txt and f\\g.txt"
    assert utility._named_read_files_beyond_cap(text) == ["f/g.txt"]


def test_on_windows_the_lane_stands_down_for_steves_prompt_with_a_chat_folder(windows_paths, tmp_path) -> None:
    from core.agent_runtime import fast_paths_utility as utility

    class _Agent:
        def _fast_path_result(self, **kwargs):  # pragma: no cover - reaching it is the bug
            raise AssertionError(f"the workspace lane answered: {kwargs.get('response')!r}")

        def _plan_tool_workflow(self, **_kwargs):  # the planner's turn: no workspace intent for a host path
            from types import SimpleNamespace

            return SimpleNamespace(next_payload={})

    (tmp_path / "drop-test.txt").write_text("a decoy with the same name in the chat folder\n", encoding="utf-8")
    for text in (_STEVE_SEND, _STEVE_DROP):
        assert (
            utility.maybe_handle_direct_workspace_runtime_request(
                _Agent(),
                text,
                session_id="s",
                source_surface="openclaw",
                source_context={"surface": "openclaw", "workspace": str(tmp_path)},
            )
            is None
        )


def test_on_windows_the_front_door_answers_steves_send_prompt_with_the_drop_answer(windows_paths, tmp_path) -> None:
    from types import SimpleNamespace as Namespace
    from unittest import mock

    from apps.vool_agent import VoolAgent

    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    with mock.patch.object(agent, "_startup_sequence_fast_path", return_value=None), mock.patch(
        "core.agent_runtime.agent.maybe_handle_preference_command", return_value=(False, "")
    ), mock.patch.object(agent, "_maybe_handle_credit_command", return_value=None), mock.patch.object(
        agent, "_maybe_handle_workspace_audit_request", return_value=None
    ):
        outcome = agent._handle_turn_frontdoor(
            raw_user_input=_STEVE_SEND,
            effective_input=_STEVE_SEND,
            normalized_input=_STEVE_SEND,
            source_surface="openclaw",
            session_id="windows-073-drop-path",
            source_context={
                "surface": "openclaw",
                "_owner_local": True,
                "workspace": str(tmp_path),
                "workspace_root": str(tmp_path),
            },
            persona=mock.sentinel.persona,
            interpreted=Namespace(understanding_confidence=0.8),
        )
    result = outcome.get("result")
    assert result is not None
    response = str(result.get("response") or "")
    assert response == lane.WINDOWS_DROP_ANSWER
    assert "There is no file at" not in response


@pytest.mark.parametrize(
    ("text", "paths"),
    (
        (_STEVE_SEND, ["drop-test.txt"]),
        ("read src\\calc.py", ["calc.py"]),
        ("read notes.txt and C:\\x\\y.txt", ["notes.txt", "y.txt"]),
    ),
)
def test_off_windows_the_read_lane_reads_file_tokens_as_before(posix_paths, text, paths) -> None:
    from core.agent_runtime import fast_paths_utility as utility

    assert [item["path"] for item in utility._direct_workspace_read_requests(text)] == paths
