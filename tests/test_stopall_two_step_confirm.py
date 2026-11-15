"""/stopall asks first, then stops VOOL only on a confirm from the same session within 60 s.

Claims sweep #1: one ``/stopall`` (or "kill vool", "stop everything", ...) shut every VOOL process
down at once, and on Windows the reply named the OpenClaw gateway and the auto-restart task, which
Windows does not have. The first phrase now runs nothing and asks for ``/stopall confirm``; a
per-session pending flag with a 60 s expiry lets the confirm run the shutdown once. The owner-local
gate stands in front of both steps. /stopx402 stays one step. The Windows success reply names
what the bundle supervisor really stops; the Mac reply is unchanged.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from core import vool_agent_brake
from core.vool_agent_brake import maybe_handle_agent_brake


@pytest.fixture(autouse=True)
def _fresh_pending(monkeypatch):
    monkeypatch.setattr(vool_agent_brake, "_pending_stopall", {})


def _spy():
    hits: list[int] = []
    return hits, (lambda: hits.append(1) or True)


def test_the_first_stopall_runs_nothing_and_asks_for_a_confirm() -> None:
    hits, fn = _spy()
    result = maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    assert result is not None and result["intent"] == "agent_brake_stopall"
    assert hits == []
    assert result["response"] == (
        "This stops all of VOOL (the window, the API and the local model runner). "
        "Type `/stopall confirm` within 60 seconds to go ahead."
    )


@pytest.mark.parametrize(
    "phrase",
    ("/stopall", "stop all vool", "kill vool", "stop everything", "shut down vool",
     "shutdown vool", "kill everything", "please /stopall"),
)
def test_every_stopall_phrase_only_asks(phrase) -> None:
    hits, fn = _spy()
    result = maybe_handle_agent_brake(phrase, "s1", stopall_fn=fn)
    assert result is not None and "/stopall confirm" in result["response"]
    assert hits == []


@pytest.mark.parametrize("confirm", ("/stopall confirm", "yes, stop VOOL", "Yes stop vool.", "stopall confirm"))
def test_the_confirm_runs_the_shutdown_once(confirm) -> None:
    hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    result = maybe_handle_agent_brake(confirm, "s1", stopall_fn=fn)
    assert result is not None and result["success"] is True
    assert result["intent"] == "agent_brake_stopall"
    assert hits == [1]
    again = maybe_handle_agent_brake(confirm, "s1", stopall_fn=fn)
    assert again is not None and again["success"] is False
    assert hits == [1]  # the pending flag is used up


def test_a_confirm_with_nothing_pending_does_nothing() -> None:
    hits, fn = _spy()
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)
    assert result is not None and result["success"] is False
    assert "nothing was stopped" in result["response"]
    assert hits == []


def test_a_confirm_after_the_expiry_does_nothing(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(vool_agent_brake, "_now", lambda: clock[0])
    hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    clock[0] += 61.0
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)
    assert result is not None and result["success"] is False
    assert hits == []


def test_a_confirm_just_inside_the_window_runs(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(vool_agent_brake, "_now", lambda: clock[0])
    hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    clock[0] += 59.0
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)
    assert result is not None and result["success"] is True
    assert hits == [1]


def test_a_confirm_from_another_session_does_nothing() -> None:
    hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    result = maybe_handle_agent_brake("/stopall confirm", "s2", stopall_fn=fn)
    assert result is not None and result["success"] is False
    assert hits == []
    # ...and it did not use up s1's pending confirm.
    assert maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)["success"] is True
    assert hits == [1]


def test_a_non_owner_confirm_is_refused() -> None:
    hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn, owner_local=False)
    assert result is not None and result["success"] is False
    assert "own local session" in result["response"]
    assert hits == []


def test_a_non_owner_stopall_arms_nothing() -> None:
    hits, fn = _spy()
    first = maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn, owner_local=False)
    assert first is not None and first["success"] is False
    assert vool_agent_brake._pending_stopall == {}
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)
    assert result is not None and result["success"] is False
    assert hits == []


def test_stopx402_stays_one_step(monkeypatch) -> None:
    flips: list[bool] = []
    monkeypatch.setattr(vool_agent_brake, "_set_frozen", lambda frozen, _w: flips.append(frozen) or False)
    hits, fn = _spy()
    result = maybe_handle_agent_brake("/stopx402", "s1", stopall_fn=fn)
    assert result is not None and result["intent"] == "agent_brake_stop"
    assert flips == [True]
    assert hits == []


def test_the_windows_success_reply_names_only_what_windows_stops(monkeypatch) -> None:
    monkeypatch.setattr(vool_agent_brake, "_is_windows", lambda: True)
    _hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)
    text = result["response"]
    assert text == (
        "Shutting down VOOL now: the window, the API and the local model runner (Ollama). "
        "VOOL stays off until you start it again from the Start menu."
    )
    assert "OpenClaw" not in text
    assert "auto-restart" not in text


def test_off_windows_the_success_reply_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(vool_agent_brake, "_is_windows", lambda: False)
    _hits, fn = _spy()
    maybe_handle_agent_brake("/stopall", "s1", stopall_fn=fn)
    result = maybe_handle_agent_brake("/stopall confirm", "s1", stopall_fn=fn)
    assert result["response"] == (
        "Shutting down all of VOOL now -- the API, the agent, and the "
        "auto-restart task. The dashboard will go offline in a few seconds and stay down until "
        "you launch VOOL again from the desktop shortcut."
    )


def test_a_confirm_while_a_task_runs_passes_through_to_the_brake() -> None:
    from core.followup_continuity import KIND_PASSTHROUGH, classify_followup

    assert classify_followup("/stopall confirm") == KIND_PASSTHROUGH
    assert classify_followup("yes, stop VOOL", awaiting_confirmation=True) == KIND_PASSTHROUGH


def _post(text: str, tmp_path, *, client_host: str = "127.0.0.1"):
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    def _model_must_not_run(*_a, **_k):
        raise AssertionError("the model ran for a /stopall turn")

    return dispatch_post(
        path="/api/chat",
        body={"session_id": "stopall-two-step", "messages": [{"role": "user", "content": text}]},
        headers={},
        runtime=RuntimeServices(display_name="VOOL"),
        model_name="vool",
        workspace_root_provider=lambda: str(tmp_path),
        run_agent_provider=_model_must_not_run,
        resolve_null_domain_provider=lambda name: None,
        client_host=client_host,
    )


def test_over_http_the_first_stopall_launches_nothing_and_the_confirm_launches_once(tmp_path, monkeypatch) -> None:
    spawn_spy = mock.Mock(return_value=True)
    monkeypatch.setattr("installer.vool_stop.spawn_detached_stop", spawn_spy)

    first = _post("/stopall", tmp_path)
    assert first.status == 200
    assert "/stopall confirm" in json.dumps(json.loads(first.body))
    spawn_spy.assert_not_called()

    second = _post("yes, stop VOOL", tmp_path)
    assert second.status == 200
    spawn_spy.assert_called_once()
