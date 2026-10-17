"""KAS-049: exercise frozen chat ownership through the real HTTP accept-once door."""
import json

import pytest

from core.invocation import ledger
from core.semantic.semantic_admissions import current_request_id
from core.web.api.runtime import RuntimeServices, stable_openclaw_session_id
from core.web.api.service import dispatch_post
from tests.test_approval_resolution_transport import resolve
from tests.test_denied_approval_finalization import rows
from tests.test_denied_approval_lifecycle import assert_closed, pending_write
from tests.test_denied_approval_lifecycle import isolated_runtime as isolated_runtime


@pytest.mark.parametrize(("path", "peer", "surface", "session"), [
    ("/api/chat", "127.0.0.1", "api", "openclaw:aaaaaaaaaaaaaaaaaaaa"),
    ("/v1/chat/completions", "127.0.0.1", "api", "openclaw:aaaaaaaaaaaaaaaaaaaa"),
    ("/api/chat", "192.0.2.10", "api", "openclaw:aaaaaaaaaaaaaaaaaaaa"),
    ("/api/chat", "127.0.0.1", "channel", "openclaw:aaaaaaaaaaaaaaaaaaaa"),
    ("/api/chat", "127.0.0.1", "api", "legacy-client-chat"),
    ("/api/chat", "127.0.0.1", "api", ""),
])
def test_chat_admission_freezes_the_same_server_derived_chat_used_by_execution(tmp_path, path, peer, surface, session):
    seen = {}
    derived = []
    body = {"messages": [{"role": "user", "content": "synthetic chat binding check"}],
            "model": "synthetic:stub", "stream": False, "session_id": session,
            "source_context": {"surface": surface, "owner_local": True,
                               "runtime_session_id": "chat-forged"}}

    def capture(_runtime, _text, *, session_id, source_context, **kwargs):
        seen.update(session=session_id, invocation=ledger.get_invocation(current_request_id()))
        assert source_context["runtime_session_id"] == session_id
        return {"response": "synthetic transport only", "confidence": 1.0}

    def select(**kwargs):
        result = stable_openclaw_session_id(**kwargs)
        derived.append(result)
        return result

    response = dispatch_post(path=path, body=body, headers={"Content-Type": "application/json"},
        runtime=RuntimeServices(), model_name="synthetic:stub", workspace_root_provider=lambda: str(tmp_path),
        run_agent_provider=capture, stable_openclaw_session_id_provider=select,
        client_host=peer, request_id="synthetic-http-owner")
    assert response.status == 200
    assert seen["invocation"]["session_binding"] == seen["session"]
    assert derived == [seen["session"]], "derive once; a stateless request must not mint a second chat"
    if peer != "127.0.0.1" or surface == "channel":
        assert seen["session"] != session
    if session:
        assert seen["session"] == stable_openclaw_session_id(body=body,
            history=[{"role": "user", "content": "synthetic chat binding check"}],
            headers={"Content-Type": "application/json"}, allow_canonical_resume=peer == "127.0.0.1" and surface != "channel")


def test_http_owned_pending_write_can_be_denied_without_forging_its_invocation(tmp_path, monkeypatch):
    seen = {}

    def pause(_runtime, _text, *, session_id, ingress_context, **kwargs):
        request_id = current_request_id()
        seen["invocation"] = ledger.get_invocation(request_id)
        # Reuse the already accepted HTTP row in the owned runtime fixture. The HTTP
        # admission above is real; only the fixture's second accept is suppressed.
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "accept_invocation", lambda **kwargs: {"request_id": request_id})
            seen["pending"] = pending_write(tmp_path, task="synthetic-client-turn", session=session_id)
        assert ingress_context is not None
        yield b'{"event":"approval_pending"}\n'

    response = dispatch_post(path="/api/chat", body={
        "messages": [{"role": "user", "content": "Create the canary."}], "stream": True,
        "stream_task_events": True,
        "session_id": "openclaw:bbbbbbbbbbbbbbbbbbbb", "turn_id": "synthetic-client-turn", "mode": "manual",
    }, headers={"Content-Type": "application/json"}, runtime=RuntimeServices(), model_name="synthetic:stub",
        workspace_root_provider=lambda: str(tmp_path), stream_agent_with_events_provider=pause,
        client_host="127.0.0.1", request_id="synthetic-http-denial")
    assert response.status == 200
    assert b"approval_pending" in b"".join(response.stream)
    pending = seen["pending"]
    request_id = pending.identity["request_id"]
    assert rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (request_id,)) == []
    status, result = resolve(pending)
    assert status == 200, result
    assert seen["invocation"]["session_binding"] == pending.session
    assert_closed(pending)
    final = rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (request_id,))
    assert len(final) == 1 and final[0]["status"] == "no_answer_terminal"
    assert final[0]["terminal_reason"] == "permission_denied"
    assert final[0]["canonical_content"] == "" and final[0]["delivery_status"] == "NOT_ATTEMPTED"


def test_identical_stateless_redelivery_keeps_the_original_frozen_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_ACCEPT_ONCE_REPLAY_WAIT", "0")
    seen = []

    def waiting(_runtime, _text, *, session_id, **kwargs):
        seen.append((session_id, current_request_id()))
        yield b'{"event":"approval_pending"}\n'

    kwargs = dict(path="/api/chat", body={
        "messages": [{"role": "user", "content": "Synthetic pending turn."}], "stream": True,
    }, headers={"Content-Type": "application/json"}, runtime=RuntimeServices(),
        model_name="synthetic:stub", workspace_root_provider=lambda: str(tmp_path),
        stream_agent_with_events_provider=waiting, client_host="127.0.0.1",
        request_id="synthetic-stateless-replay")
    first = dispatch_post(**kwargs)
    assert first.status == 200 and b"approval_pending" in b"".join(first.stream)
    original = ledger.get_invocation(seen[0][1])
    second = dispatch_post(**kwargs)
    assert second.status == 200 and json.loads(second.body)["done_reason"] == "request_in_progress"
    assert len(seen) == 1
    assert original["session_binding"] == seen[0][0]
    assert ledger.get_invocation(seen[0][1]) == original


def test_distinct_stateless_requests_never_share_a_chat_binding(tmp_path):
    seen = []

    def waiting(_runtime, _text, *, session_id, **kwargs):
        seen.append((session_id, ledger.get_invocation(current_request_id())))
        yield b'{"event":"approval_pending"}\n'

    for request_id in ("synthetic-stateless-first", "synthetic-stateless-second"):
        response = dispatch_post(path="/api/chat", body={
            "messages": [{"role": "user", "content": "Same synthetic message."}], "stream": True,
        }, headers={"Content-Type": "application/json"}, runtime=RuntimeServices(),
            model_name="synthetic:stub", workspace_root_provider=lambda: str(tmp_path),
            stream_agent_with_events_provider=waiting, client_host="127.0.0.1", request_id=request_id)
        assert response.status == 200 and b"approval_pending" in b"".join(response.stream)
    assert seen[0][0] != seen[1][0]
    assert all(invocation["session_binding"] == session for session, invocation in seen)
