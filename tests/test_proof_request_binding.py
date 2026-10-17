"""An approved retry's proof cannot inherit another request's blocked outcome."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from core.invocation.ledger import accept_invocation
from core.proof_projection import STATE_INCOMPLETE, STATE_VERIFIED, _resolve_turn_keys, build_turn_proof
from tests.test_proof_projection import _bind_turn, _emit_turn_event, _insert_finalization


def _accept(session: str, value: str) -> str:
    return accept_invocation(
        external_kind="http", external_value=value, principal="owner_local",
        session_binding=session, privacy_local_only=True,
    )["request_id"]


@pytest.mark.parametrize("status", ["blocked", "failed", "partially_fulfilled", "cancelled"])
@pytest.mark.parametrize("reopen", [False, True])
def test_approved_retry_does_not_inherit_the_prior_requests_failure(status, reopen):
    session, turn = f"proof-approved-retry-{status}-{reopen}", "pact-rig-task"
    prior_raw, approved_raw = f"approval-pending-{status}-{reopen}", f"approval-granted-{status}-{reopen}"
    prior = _accept(session, prior_raw)
    approved = _accept(session, approved_raw)
    for request, content in ((prior, "Approval is required."), (approved, "The file was written.")):
        _bind_turn(session, request, content)
        _insert_finalization(request, turn, content)
    for raw, outcome in ((prior_raw, status), (approved_raw, "fulfilled")):
        _emit_turn_event(session, turn, raw, "turn.trace_completed", {
            "fulfillment_outcome": {"fulfillment_status": outcome},
        })
    if reopen:
        from core.runtime_paths import active_vool_home
        from storage.db import active_default_db_path

        process = subprocess.run([
            sys.executable, "-c",
            "import json,sys\n"
            "from core.runtime_paths import configure_runtime_home\n"
            "from storage.db import configure_default_db_path\n"
            "from core.runtime_continuity import configure_runtime_continuity_db_path\n"
            "configure_runtime_home(sys.argv[1])\n"
            "configure_default_db_path(sys.argv[2])\n"
            "configure_runtime_continuity_db_path(sys.argv[2])\n"
            "from core.proof_projection import build_turn_proof\n"
            "print(json.dumps([build_turn_proof(session_id=sys.argv[3],request_id=r) for r in sys.argv[4:]]))\n",
            str(active_vool_home()), active_default_db_path(), session, approved, prior,
        ], capture_output=True, text=True, check=True, timeout=30)
        retried, original = json.loads(process.stdout)
    else:
        retried = build_turn_proof(session_id=session, request_id=approved)
        original = build_turn_proof(session_id=session, request_id=prior)
    assert retried["state"] == STATE_VERIFIED, retried["state_reasons"]
    assert retried["state_reasons"] == []
    assert retried["expanded"]["finalization"]["content_hash_ok"] is True
    assert retried["expanded"]["witness"]["consistent"] is True
    assert original["state"] == STATE_INCOMPLETE
    assert "terminal_task_unfulfilled" in original["state_reasons"]


@pytest.mark.parametrize("event_identity", ["canonical", "http_external"])
@pytest.mark.parametrize("key_source", ["finalization", "explicit", "event"])
def test_accepted_http_identity_resolves_only_its_own_events(event_identity, key_source):
    request = _accept("proof-http-alias", "approved")
    own = {"request_id": request if event_identity == "canonical" else "approved", "turn_key": "shared"}
    foreign = {"request_id": "earlier", "turn_key": "shared"}
    keys, events = _resolve_turn_keys(
        explicit="shared" if key_source == "explicit" else "",
        finalization_row={"turn_id": "shared"} if key_source == "finalization" else None,
        events=[foreign, own], request_id=request,
    )
    assert keys == ["shared"]
    assert events == [own]


@pytest.mark.parametrize("foreign_request", ["req:http:other", "other", "req:channel:approved"])
def test_an_explicit_foreign_request_cannot_join_through_the_same_turn_key(foreign_request):
    request = _accept("proof-foreign-id", "approved")
    own = {"request_id": request, "turn_key": "shared"}
    foreign = {"request_id": foreign_request, "turn_key": "shared"}
    keys, events = _resolve_turn_keys(
        explicit="shared", finalization_row={"turn_id": "shared"},
        events=[foreign, own], request_id=request,
    )
    assert keys == ["shared"]
    assert events == [own]


def test_a_namespace_string_without_acceptance_does_not_authorize_a_raw_alias():
    own = {"request_id": "req:http:unaccepted", "turn_key": "shared"}
    raw = {"request_id": "unaccepted", "turn_key": "shared"}
    _, events = _resolve_turn_keys(
        explicit="", finalization_row={"turn_id": "shared"},
        events=[raw, own], request_id="req:http:unaccepted",
    )
    assert events == [own]


@pytest.mark.parametrize("key_source", ["finalization", "explicit", "event"])
def test_requestless_legacy_events_keep_the_existing_turn_key_binding(key_source):
    own = {"request_id": "legacy-request", "turn_key": "legacy-turn"}
    requestless = {"turn_key": "legacy-turn", "event_type": "model_usage"}
    unrelated = {"turn_key": "different-turn", "event_type": "model_usage"}
    keys, events = _resolve_turn_keys(
        explicit="legacy-turn" if key_source == "explicit" else "",
        finalization_row={"turn_id": "legacy-turn"} if key_source == "finalization" else None,
        events=[own, requestless, unrelated], request_id="legacy-request",
    )
    assert keys == ["legacy-turn"]
    assert events == [own, requestless]


def test_a_foreign_request_cannot_introduce_a_new_turn_key():
    request = _accept("proof-foreign-key", "approved")
    keys, events = _resolve_turn_keys(
        explicit="", finalization_row=None,
        events=[{"request_id": "req:http:other", "turn_key": "foreign"}, {"turn_key": "foreign"}],
        request_id=request,
    )
    assert keys == []
    assert events == []
