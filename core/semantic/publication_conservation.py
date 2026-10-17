"""Publication conservation: every terminal SlotID is VISIBLE in the rendered answer.

Slot reconciliation (``slot_reconciliation``) proves every frozen SlotID reached a terminal
disposition. That is necessary but not sufficient: the Astra review showed a slot can be terminally
``indeterminate``/``blocked``/``unknown`` and then vanish from the published answer — structural
closure coexisting with a missing visible answer. This layer closes that gap at the render boundary.

The law: for every SlotID the reconciliation terminated, the published answer must contain a VISIBLE
representation of its disposition — an answer for ANSWERED, and an explicit "unavailable / blocked /
needs input / …" line for the rest. This must hold for prose, tables, JSON, literal output and
artifacts alike: a structured-output request may not hide a missing slot. The renderer reports which
SlotIDs it actually surfaced (``PublishedSlot``); this refuses to call the answer complete if any
terminal slot was dropped or rendered invisibly.

Nothing here renders — the renderer owns layout and may not add or drop slots. This only verifies
that what was rendered accounts, visibly, for every terminal obligation.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from core.semantic.request_graph import SlotId
from core.semantic.slot_reconciliation import Reconciliation


class PublicationConservationError(ValueError):
    """A rendered answer was declared complete while a terminal SlotID was omitted or hidden."""


@dataclass(frozen=True)
class PublishedSlot:
    """How one SlotID appears in the rendered answer, as the renderer reports it. ``visible`` is
    False when the slot was emitted structurally but not shown (e.g. a JSON key suppressed, a table
    row dropped) — which conservation treats the same as omission."""

    slot_id: SlotId
    visible: bool = True
    rendering: str = ""          # the surfaced text/cell/field, for evidence
    turn_id: str = ""
    graph_digest: str = ""
    content_hash: str = ""
    byte_start: int = -1
    byte_end: int = -1


@dataclass(frozen=True)
class PublicationVerdict:
    conserved: bool
    omitted: tuple[SlotId, ...] = field(default_factory=tuple)   # terminal slots with no rendering
    hidden: tuple[SlotId, ...] = field(default_factory=tuple)    # rendered but visible=False / empty
    invented: tuple[SlotId, ...] = field(default_factory=tuple)  # published ids no terminal slot owns
    duplicated: tuple[SlotId, ...] = field(default_factory=tuple)
    invalid: tuple[SlotId, ...] = field(default_factory=tuple)  # stale/unbound/non-matching byte evidence

    def violations(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.omitted:
            out.append(f"terminal slots omitted from the published answer: {list(self.omitted)}")
        if self.hidden:
            out.append(f"terminal slots rendered but not visible: {list(self.hidden)}")
        if self.invented:
            out.append(f"published slots no terminal result owns: {list(self.invented)}")
        if self.duplicated:
            out.append(f"more than one publication record for slots: {list(self.duplicated)}")
        if self.invalid:
            out.append(f"publication evidence does not bind the final bytes/turn/graph: {list(self.invalid)}")
        return tuple(out)


def verify_publication(
    reconciliation: Reconciliation, published: tuple[PublishedSlot, ...], *, final_content: str | None = None
) -> PublicationVerdict:
    """Report whether every terminal SlotID is visibly published. Never raises.

    Requires a CONSERVED reconciliation as its input universe — publication conservation is only
    meaningful once the terminal set is itself conserved. If the reconciliation was not conserved,
    that is surfaced as the primary violation (there is no valid terminal set to publish).

    The runtime supplies ``final_content``: every visible record must then bind a non-empty UTF-8
    byte span, the entire final payload hash, and its terminal result's turn/graph. This proves byte
    provenance, NOT semantic fulfillment. Pure structural callers may omit the final payload.
    """
    terminal = frozenset(r.slot_id for r in reconciliation.results)
    counts = Counter(p.slot_id for p in published)
    duplicated = tuple(sorted(sid for sid, count in counts.items() if count > 1))
    invalid: set[SlotId] = set()
    if final_content is not None:
        payload = final_content.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        results = {r.slot_id: r for r in reconciliation.results}
        for p in published:
            if not p.visible:
                continue
            result = results.get(p.slot_id)
            if (
                result is None
                or not (p.turn_id and p.graph_digest)
                or p.turn_id != result.turn_id
                or p.graph_digest != result.graph_digest
                or p.content_hash != digest
                or type(p.byte_start) is not int
                or type(p.byte_end) is not int
                or not (0 <= p.byte_start < p.byte_end <= len(payload))
                or not isinstance(p.rendering, str)
                or not str(p.rendering or "").strip()
                or payload[p.byte_start:p.byte_end] != p.rendering.encode("utf-8")
            ):
                invalid.add(p.slot_id)
    # A row that claims visibility must SHOW something: an empty rendering is a label, not a
    # published answer, and counts as hidden. (A visible flag alone certified nothing.)
    visible = {p.slot_id for p in published if p.visible and str(p.rendering or "").strip() and p.slot_id not in invalid}
    present_any = {p.slot_id for p in published}
    omitted = tuple(sorted(terminal - present_any))
    hidden = tuple(sorted(terminal - visible - set(omitted)))
    invented = tuple(sorted(present_any - terminal))
    conserved = reconciliation.conserved and not (omitted or hidden or invented or duplicated or invalid)
    return PublicationVerdict(
        conserved=conserved, omitted=omitted, hidden=hidden, invented=invented,
        duplicated=duplicated, invalid=tuple(sorted(invalid)),
    )


def require_published(
    reconciliation: Reconciliation, published: tuple[PublishedSlot, ...], *, final_content: str | None = None
) -> PublicationVerdict:
    """Fail-closed gate: raise unless every terminal SlotID is visibly published (and the terminal
    set was itself conserved). A finalization/publication boundary calls this before shipping."""
    if not reconciliation.conserved:
        raise PublicationConservationError(
            "cannot publish an unconserved terminal set: " + "; ".join(reconciliation.violations())
        )
    verdict = verify_publication(reconciliation, published, final_content=final_content)
    if not verdict.conserved:
        raise PublicationConservationError("; ".join(verdict.violations()))
    return verdict


__all__ = [
    "PublicationConservationError",
    "PublicationVerdict",
    "PublishedSlot",
    "require_published",
    "verify_publication",
]
