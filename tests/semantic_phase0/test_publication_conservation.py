"""Publication conservation: every terminal SlotID is visibly represented in the rendered answer."""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.semantic.canonical_text import CanonicalText
from core.semantic.publication_conservation import (
    PublicationConservationError,
    PublishedSlot,
    require_published,
    verify_publication,
)
from core.semantic.request_graph import (
    InterpretationState,
    Request,
    RequestGraph,
    Slot,
    TerminalDisposition,
)
from core.semantic.slot_reconciliation import SlotResult, reconcile


def _graph() -> RequestGraph:
    c = CanonicalText.of("price of gold and price of silver")
    return RequestGraph(
        turn_id="t",  # type: ignore[arg-type]
        canonical=c,
        requests=(Request(id="r0", source_text=c.text, slot_ids=("slot-gold", "slot-silver"),  # type: ignore[arg-type]
                          state=InterpretationState.RESOLVED),),
        slots=(Slot(id="slot-gold", request_id="r0"), Slot(id="slot-silver", request_id="r0")),  # type: ignore[arg-type]
    )


def _conserved_reconciliation():
    # gold answered, silver blocked (retrieval forbidden) — both are terminal, both must be visible.
    return reconcile(_graph(), (
        SlotResult("slot-gold", TerminalDisposition.ANSWERED, content="$2400/oz"),  # type: ignore[arg-type]
        SlotResult("slot-silver", TerminalDisposition.BLOCKED, detail="retrieval forbidden"),  # type: ignore[arg-type]
    ))


def test_every_terminal_slot_visible_is_conserved() -> None:
    rec = _conserved_reconciliation()
    published = (
        PublishedSlot("slot-gold", visible=True, rendering="Gold: $2400/oz"),  # type: ignore[arg-type]
        PublishedSlot("slot-silver", visible=True, rendering="Silver: unavailable (retrieval off)"),  # type: ignore[arg-type]
    )
    verdict = require_published(rec, published)
    assert verdict.conserved and verdict.violations() == ()


def test_a_blocked_slot_omitted_from_the_answer_is_refused() -> None:
    """The exact Astra defect: a non-answered slot silently vanishes from publication."""
    rec = _conserved_reconciliation()
    published = (PublishedSlot("slot-gold", visible=True, rendering="Gold: $2400/oz"),)  # type: ignore[arg-type]
    verdict = verify_publication(rec, published)
    assert not verdict.conserved and verdict.omitted == ("slot-silver",)
    with pytest.raises(PublicationConservationError, match="omitted"):
        require_published(rec, published)


def test_a_slot_rendered_but_hidden_is_refused() -> None:
    rec = _conserved_reconciliation()
    published = (
        PublishedSlot("slot-gold", visible=True, rendering="Gold: $2400/oz"),  # type: ignore[arg-type]
        PublishedSlot("slot-silver", visible=False, rendering=""),  # emitted structurally, not shown  # type: ignore[arg-type]
    )
    verdict = verify_publication(rec, published)
    assert not verdict.conserved and verdict.hidden == ("slot-silver",)
    with pytest.raises(PublicationConservationError, match="not visible"):
        require_published(rec, published)


def test_rendered_terminal_slots_conserve_publication() -> None:
    rec = _conserved_reconciliation()
    published = (
        PublishedSlot("slot-gold", visible=True, rendering="Gold: $2400/oz"),  # type: ignore[arg-type]
        PublishedSlot("slot-silver", visible=True, rendering="Silver: unavailable"),  # type: ignore[arg-type]
    )
    assert verify_publication(rec, published).conserved


def test_publishing_an_unconserved_terminal_set_is_refused_up_front() -> None:
    # silver never terminated -> reconciliation not conserved -> cannot publish.
    rec = reconcile(_graph(), (
        SlotResult("slot-gold", TerminalDisposition.ANSWERED, content="$2400/oz"),))  # type: ignore[arg-type]
    assert not rec.conserved
    with pytest.raises(PublicationConservationError, match="unconserved terminal set"):
        require_published(rec, (PublishedSlot("slot-gold", visible=True),))  # type: ignore[arg-type]


def _bound_publication():
    from core.semantic.bridges.publication import published_slots
    from core.semantic.graph_serialization import graph_digest

    graph = _graph()
    digest = graph_digest(graph)
    results = (
        SlotResult("slot-gold", TerminalDisposition.ANSWERED, content="Gold: $2400/oz",
                   turn_id="t", graph_digest=digest),
        SlotResult("slot-silver", TerminalDisposition.BLOCKED, detail="retrieval forbidden",
                   turn_id="t", graph_digest=digest),
    )
    content = "🌙 Prices\r\nGold: $2400/oz\r\nSilver: unavailable (retrieval off)"
    rec = reconcile(graph, results, require_binding=True)
    shown = published_slots(
        results, answered_units={"slot-gold": "Gold: $2400/oz"},
        rendered_units={"slot-silver": "Silver: unavailable (retrieval off)"}, final_content=content,
    )
    return rec, shown, content


def test_final_payload_binding_uses_utf8_spans_without_changing_bytes() -> None:
    rec, shown, content = _bound_publication()
    assert require_published(rec, shown, final_content=content).conserved
    payload = content.encode("utf-8")
    for record in shown:
        assert payload[record.byte_start:record.byte_end] == record.rendering.encode("utf-8")
    assert shown[0].byte_start > content.index("Gold:")  # UTF-8, not character offsets.


@pytest.mark.parametrize("change", [
    {"turn_id": "foreign"}, {"turn_id": ""}, {"graph_digest": "foreign"}, {"graph_digest": ""},
    {"content_hash": "sha256:stale"}, {"byte_start": -1}, {"byte_start": True},
    {"byte_end": 10000}, {"byte_end": 0}, {"byte_end": "10"}, {"rendering": "fake answer"},
    {"rendering": ""}, {"rendering": 42}, {"byte_start": 1, "byte_end": 2},
])
def test_stale_foreign_malformed_and_invented_span_evidence_is_rejected(change) -> None:
    rec, shown, content = _bound_publication()
    broken = (replace(shown[0], **change), shown[1])
    verdict = verify_publication(rec, broken, final_content=content)
    assert not verdict.conserved
    assert verdict.invalid == ("slot-gold",) and verdict.hidden == ("slot-gold",)
    with pytest.raises(PublicationConservationError, match="final bytes/turn/graph"):
        require_published(rec, broken, final_content=content)


def test_modified_final_bytes_invalidate_even_unchanged_supporting_spans() -> None:
    rec, shown, content = _bound_publication()
    verdict = verify_publication(rec, shown, final_content=content + "\nChanged after the proof.")
    assert not verdict.conserved and verdict.invalid == ("slot-gold", "slot-silver")


@pytest.mark.parametrize("hidden_duplicate", [False, True])
def test_duplicate_publication_rows_cannot_be_masked_by_one_visible_record(hidden_duplicate) -> None:
    rec, shown, content = _bound_publication()
    duplicate = replace(shown[0], visible=False, rendering="") if hidden_duplicate else shown[0]
    verdict = verify_publication(rec, (*shown, duplicate), final_content=content)
    assert not verdict.conserved and verdict.duplicated == ("slot-gold",)


def test_visible_label_without_byte_binding_does_not_pass_the_runtime_boundary() -> None:
    rec, shown, content = _bound_publication()
    unbound = tuple(PublishedSlot(p.slot_id, visible=True, rendering=p.rendering) for p in shown)
    verdict = verify_publication(rec, unbound, final_content=content)
    assert not verdict.conserved and verdict.invalid == ("slot-gold", "slot-silver")


def test_bridge_cannot_bind_absent_text_or_upgrade_an_unverified_answer() -> None:
    from core.semantic.bridges.publication import published_slots

    rec, shown, content = _bound_publication()
    absent = published_slots(
        rec.results, answered_units={"slot-gold": "Gold: $9999/oz"},
        rendered_units={"slot-silver": shown[1].rendering}, final_content=content,
    )
    verdict = verify_publication(rec, absent, final_content=content)
    assert not verdict.conserved and verdict.invalid == ("slot-gold",)
    unverified = (replace(rec.results[0], disposition=TerminalDisposition.UNVERIFIED, content=""),)
    (record,) = published_slots(unverified, answered_units={"slot-gold": shown[0].rendering}, final_content=content)
    assert not record.visible and record.rendering == ""
    assert unverified[0].disposition is TerminalDisposition.UNVERIFIED


def test_invented_slot_and_missing_slot_stay_refused_with_bound_bytes() -> None:
    rec, shown, content = _bound_publication()
    verdict = verify_publication(rec, (replace(shown[0], slot_id="invented"),), final_content=content)
    assert not verdict.conserved
    assert verdict.invented == ("invented",) and verdict.omitted == ("slot-gold", "slot-silver")
