"""GitLab write bodies are encoded before the existing transport and response gates."""
from __future__ import annotations

import json

import pytest

from core.kas.contract import (
    ForgeAcceptedUnreadableError,
    ForgeRefusedError,
    TransportDeniedError,
    TransportUnknownError,
)
from core.kas.registry import forge_adapter
from tests.repoops._forge_fixture import RecordedForge, gitlab_merge_request


def _adapter(forge):
    return forge_adapter(
        "gitlab", namespace="o/r", auth_binding="fixture-binding", transport_factory=forge.factory,
    )


def _assert_write(forge, *, method, purpose, body):
    call = forge.calls[-1]
    assert call["method"] == method
    assert call["purpose"] == purpose
    assert call["mutating"] is True
    assert call["auth"] == "fixture-binding"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["body"] == json.dumps(body).encode("utf-8")
    assert json.loads(call["body"]) == body


@pytest.mark.parametrize("subject,suffix", [("issue", "/issues/7/notes"), ("pull_request", "/merge_requests/7/notes")])
def test_comments_encode_exact_text_on_both_subjects(subject, suffix):
    text = 'Reviewed "text"\nKaunas: \u017e; literal \\ path'
    forge = RecordedForge().route("POST " + suffix, {"id": 91, "body": text})
    result = _adapter(forge).post_comment("7", text, subject=subject)
    assert result.comment_id == "91" and result.body == text
    assert len(forge.calls) == 1
    _assert_write(forge, method="POST", purpose="forge.comment_create", body={"body": text})


@pytest.mark.parametrize("draft", [False, True])
def test_create_encodes_reviewed_fields_and_keeps_draft_translation(draft):
    reply = gitlab_merge_request("7", base_sha="a" * 40, head_sha="b" * 40)
    reply.update(title="Draft: Reviewed" if draft else "Reviewed", draft=draft, description="Exact\nbody")
    forge = RecordedForge().route("POST /merge_requests", reply, status=201)
    result = _adapter(forge).create_pull_request(
        title="Reviewed", body="Exact\nbody", head_ref="feature", base_ref="main", draft=draft,
    )
    assert result.number == "7" and result.title == "Reviewed" and result.draft is draft
    assert len(forge.calls) == 1
    _assert_write(forge, method="POST", purpose="forge.pr_create", body={
        "title": "Draft: Reviewed" if draft else "Reviewed", "source_branch": "feature",
        "target_branch": "main", "description": "Exact\nbody",
    })


def test_update_encodes_put_and_preserves_existing_draft_after_read():
    reply = gitlab_merge_request("7", base_sha="a" * 40, head_sha="b" * 40)
    reply.update(title="Draft: Reviewed", draft=True, description="New body")
    forge = RecordedForge().route("GET /merge_requests/7", reply).route("PUT /merge_requests/7", reply)
    result = _adapter(forge).update_pull_request("7", title="Reviewed", body="New body")
    assert result.draft and result.title == "Reviewed" and result.body == "New body"
    assert [call["method"] for call in forge.calls] == ["GET", "PUT"]
    _assert_write(forge, method="PUT", purpose="forge.pr_update", body={"title": "Draft: Reviewed", "description": "New body"})


@pytest.mark.parametrize("action", ["rerun_ci", "cancel_ci"])
def test_bodyless_ci_actions_send_empty_json(action):
    suffix = "retry" if action == "rerun_ci" else "cancel"
    forge = RecordedForge().route("POST /jobs/7/" + suffix, {})
    assert getattr(_adapter(forge), action)("7")
    assert len(forge.calls) == 1
    _assert_write(forge, method="POST", purpose="forge.ci_" + ("rerun" if action == "rerun_ci" else "cancel"), body={})


@pytest.mark.parametrize("method", ["_post", "_put"])
def test_invalid_request_encoding_is_unsent(method):
    forge = RecordedForge()
    with pytest.raises(TypeError):
        getattr(_adapter(forge), method)("/fixture", purpose="forge.fixture", body={"unsupported": object()})
    assert not forge.calls


def _write(adapter, method):
    if method == "POST":
        return adapter.post_comment("7", "Reviewed", subject="issue")
    return adapter.update_pull_request("7", body="Reviewed")


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("unknown", [False, True])
def test_transport_denial_and_unknown_outcome_are_not_retried(method, unknown):
    forge = RecordedForge()
    fragment = method + " /"
    if unknown:
        forge.arm_unknown(fragment)
    else:
        forge.arm_denial(fragment)
    with pytest.raises(TransportUnknownError if unknown else TransportDeniedError):
        _write(_adapter(forge), method)
    assert len(forge.calls) == 1


@pytest.mark.parametrize("method,suffix", [("POST", "/issues/7/notes"), ("PUT", "/merge_requests/7")])
@pytest.mark.parametrize("status", [401, 403, 429])
def test_definitive_http_refusals_still_cross_the_existing_response_gate(method, suffix, status):
    forge = RecordedForge().route(method + " " + suffix, {"message": "Refused"}, status=status)
    with pytest.raises(ForgeRefusedError) as caught:
        _write(_adapter(forge), method)
    assert caught.value.status_code == status
    assert len(forge.calls) == 1


@pytest.mark.parametrize("method,suffix", [("POST", "/issues/7/notes"), ("PUT", "/merge_requests/7")])
@pytest.mark.parametrize("payload", [b"not JSON", {}, {"id": 91, "author": 47, "diff_refs": 47}])
def test_accepted_unreadable_write_is_unknown_without_resend(method, suffix, payload):
    forge = RecordedForge().route(method + " " + suffix, payload, status=201)
    with pytest.raises(ForgeAcceptedUnreadableError):
        _write(_adapter(forge), method)
    assert len(forge.calls) == 1
