"""Finalization export follows served transcript lineage, not coincidental turn identity."""
from __future__ import annotations

import json

import pytest

from core.session_portability.collect import OverBound, _db_rows, collect_session
from tests.session_portability.support import OTHER, SESSION


@pytest.fixture(autouse=True)
def _isolated_finalization_store(tmp_path):
    from storage.db import configure_default_db_path, reset_default_connection
    from storage.migrations import run_migrations

    configure_default_db_path(tmp_path / "finalizations.db")
    reset_default_connection()
    run_migrations()
    yield
    reset_default_connection()
    configure_default_db_path(None)


def _seed(session_id=SESSION, *, client_turn="client-1", request_id="req:test:export-1"):
    from core.finalization import finalize_answer
    from core.persistent_memory import append_conversation_event, open_transcript_commit_boundary
    from core.semantic.semantic_admissions import bound_request_context
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission
    from storage.dialogue_memory import record_dialogue_turn

    text = "Committed answer for this conversation."
    open_transcript_commit_boundary(request_id)
    with bound_request_context(request_id):
        dialogue_turn = record_dialogue_turn(
            session_id, raw_input="Question?", normalized_input="Question?",
            reconstructed_input="Question?", topic_hints=[], reference_targets=[],
            understanding_confidence=1.0, quality_flags=[],
        )
        append_conversation_event(
            session_id=session_id, user_input="Question?", assistant_output=text,
            source_context={"surface": "test", "transcript_commit_boundary": True},
        )
        reset_admission()
        admit_semantic_result({"response": text}, turn_id=client_turn)
        commit = finalize_answer(turn_id=client_turn, canonical_content=text)
    assert dialogue_turn != commit["turn_id"]
    return commit


def _bind_dialogue_to(client_turn):
    from storage.db import get_connection

    conn = get_connection()
    try:
        row = conn.execute("SELECT turn_id FROM dialogue_turns WHERE session_id = ? LIMIT 1", (SESSION,)).fetchone()
        conn.execute("UPDATE dialogue_turns SET turn_id = ? WHERE turn_id = ?", (client_turn, row[0]))
        conn.commit()
    finally:
        conn.close()


def test_served_finalization_exports_when_dialogue_and_client_turns_differ(tmp_path):
    from core.session_portability import api

    commit = _seed()
    foreign = _seed(OTHER, client_turn="client-other", request_id="req:test:other")
    before = _db_rows("SELECT * FROM a7_finalizations ORDER BY finalization_id", ())
    preview = api.preview_export(SESSION)
    out = tmp_path / "original.voolsession"
    receipt = api.export_session(SESSION, out, passphrase="temporary-export-test-passphrase")
    payload = api.load_payload(out, passphrase="temporary-export-test-passphrase")
    assert preview["counts"] == receipt["counts"]
    assert receipt["counts"]["finalizations"] == 1
    final = payload["finalizations"][0]
    assert final["finalization_id"] == commit["finalization_id"] != foreign["finalization_id"]
    assert final["turn_id"] == commit["turn_id"]
    assert final["content_hash"] == commit["content_hash"]
    assert set(final) == {"finalization_id", "turn_id", "content_hash", "status", "delivery_status", "created_at"}
    assert before == _db_rows("SELECT * FROM a7_finalizations ORDER BY finalization_id", ())


@pytest.mark.parametrize("field,value", [
    ("content_hash", "sha256:wrong"), ("request_id", "req:test:foreign"),
    ("commit_state", "unsealed"), ("content_hash", ""), ("request_id", ""),
])
@pytest.mark.parametrize("canonical_dialogue", [False, True])
def test_mismatched_or_unsealed_transcript_does_not_export_a_finalization(field, value, canonical_dialogue):
    from core.memory.files import conversation_log_path

    commit = _seed()
    if canonical_dialogue:
        _bind_dialogue_to(commit["turn_id"])

    path = conversation_log_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0][field] = value
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    payload, _, counts, _ = collect_session(SESSION)
    assert payload["finalizations"] == []
    assert counts.finalizations == 0


def test_legacy_dialogue_bound_finalization_still_exports():
    commit = _seed()
    _bind_dialogue_to(commit["turn_id"])
    from core.memory.files import conversation_log_path

    path = conversation_log_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0].pop("finalization_id")
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert collect_session(SESSION)[0]["finalizations"][0]["finalization_id"] == commit["finalization_id"]


def test_transcript_and_dialogue_bindings_do_not_duplicate_the_same_seal():
    commit = _seed()
    _bind_dialogue_to(commit["turn_id"])
    payload, _, counts, _ = collect_session(SESSION)
    assert counts.finalizations == 1
    assert [row["finalization_id"] for row in payload["finalizations"]] == [commit["finalization_id"]]


def test_foreign_finalization_claim_with_own_request_is_rejected():
    from core.memory.files import conversation_log_path

    _seed()
    foreign = _seed(OTHER, client_turn="client-other", request_id="req:test:other")
    path = conversation_log_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    own = next(row for row in rows if row["session_id"] == SESSION)
    own["finalization_id"] = foreign["finalization_id"]
    own["content_hash"] = foreign["content_hash"]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert collect_session(SESSION)[0]["finalizations"] == []


def test_finalization_bound_refuses_instead_of_truncating(monkeypatch):
    from core.session_portability import api, schema

    _seed()
    _seed(client_turn="client-2", request_id="req:test:export-2")
    monkeypatch.setitem(schema.BOUNDS, "finalizations", 1)
    with pytest.raises(OverBound, match="finalizations"):
        collect_session(SESSION)
    with pytest.raises(api.PortabilityRefused) as err:
        api.preview_export(SESSION)
    assert err.value.code == "BUNDLE_LIMIT_EXCEEDED"


def test_finalization_lookup_batches_without_dropping_records_at_the_limit():
    from core.session_portability.collect import _collect_finalizations
    from storage.db import get_connection

    turns = [
        {"finalization_id": f"fc:batch-{index}", "content_hash": f"sha256:batch-{index}",
         "request_id": f"req:test:batch-{index}", "commit_state": "committed"}
        for index in range(501)
    ]
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT INTO a7_finalizations (finalization_id, turn_id, content_hash, canonical_content, request_id) "
            "VALUES (?, ?, ?, ?, ?)",
            [(row["finalization_id"], f"client-{index}", row["content_hash"], "test bytes", row["request_id"])
             for index, row in enumerate(turns)],
        )
        conn.commit()
    finally:
        conn.close()
    finalizations = _collect_finalizations(turns, [], 501)
    assert {row["finalization_id"] for row in finalizations} == {row["finalization_id"] for row in turns}
    assert len(finalizations) == 501
    with pytest.raises(OverBound, match="finalizations"):
        _collect_finalizations(turns, [], 500)
