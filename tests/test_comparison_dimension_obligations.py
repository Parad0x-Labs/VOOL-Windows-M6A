"""Comparison facets are obligations; dispatch preserves their shared subject."""
from __future__ import annotations

from itertools import pairwise

import pytest

from core.agent_runtime.answer_coverage import demand_units, interpret_request
from core.agent_runtime.demand_ownership import demand_records, execution_unit_spans


@pytest.mark.parametrize(("text", "facets"), [
    ("compare alpha vs beta on latency, availability, portability and cost", ("latency", "availability", "portability", "cost")),
    ("Compare cedar and oak: grain, density and durability.", ("grain", "density", "durability")),
    ("contrast resin and steel by strength, cost, and weight", ("strength", "cost", "weight")),
    ("compare buses and trains in terms of coverage, capacity and frequency", ("coverage", "capacity", "frequency")),
    ("compare Málaga and Zürich on population, area and elevation", ("population", "area", "elevation")),
    ('Compare "A on B, C" and D on size, cost', ("size", "cost")),
    ("compare alpha and beta on costs, price and sales", ("costs", "price", "sales")),
])
def test_each_facet_has_its_own_source_backed_obligation(text, facets):
    units = demand_units(text)
    assert len(units) == len(facets), [u.text for u in units]
    assert len({u.unit_id for u in units}) == len(units)
    for unit, facet in zip(units, facets, strict=True):
        assert facet in unit.text.lower()
        assert not any(other in unit.text.lower() for other in facets if other != facet)
        assert text[unit.start:unit.end] == unit.text
    assert all(left.end <= right.start for left, right in pairwise(units))
    spans = execution_unit_spans(text)
    assert len(spans) == 1
    assert spans[0].text == text
    assert spans[0].member_unit_ids == tuple(unit.unit_id for unit in units)
    assert demand_records(text)[0].accounted_unit_ids == spans[0].member_unit_ids


@pytest.mark.parametrize(("text", "count"), [
    ("most popuplar regions where sold, engines and so on", 2),
    ("battery capacity, runtime and so forth", 2),
    ("latency, throughput, availability, etc.", 3),
])
def test_an_elliptical_dimension_list_keeps_its_named_slots_but_not_its_filler(text, count):
    units = demand_units(text)
    assert len(units) == count, [u.text for u in units]
    assert all(text[u.start:u.end] == u.text for u in units)
    assert not any(u.text.strip().lower() in {"and so on", "and so forth", "etc."} for u in units)
    assert len(execution_unit_spans(text)) == 1


@pytest.mark.parametrize("text", [
    "compare python and rust for a small cli tool",
    "price of xmr, xlm, bch",
    "weather in Rome, Italy",
    "How much gold and how much silver can I buy with one bitcoin right now?",
    'Explain the quotation "compare alpha and beta on latency, cost and weight".',
    'Create note.txt containing "compare alpha and beta on latency, cost and weight".',
    "Users can compare products on cost, weight and durability.",
])
def test_subject_lists_literals_and_descriptions_do_not_become_facet_requests(text):
    assert len(demand_units(text)) == 1


def test_a_prohibited_comparison_mints_no_facet_requests():
    assert demand_units("Do not compare alpha and beta on latency, cost and weight.") == ()


def test_a_fresh_request_after_the_dimensions_still_has_its_own_execution_group():
    text = "compare alpha and beta on latency, cost, and tell me the weather in Rome"
    units = demand_units(text)
    assert len(units) == 3, [u.text for u in units]
    assert "latency" in units[0].text and "cost" in units[1].text
    assert "weather in Rome" in units[2].text
    groups = execution_unit_spans(text)
    assert len(groups) == 2
    assert groups[0].member_unit_ids == (units[0].unit_id, units[1].unit_id)
    assert groups[1].member_unit_ids == (units[2].unit_id,)


def test_a_constraint_after_the_dimensions_does_not_become_an_obligation():
    text = "compare alpha and beta on latency, cost. Do not browse."
    interpretation = interpret_request(text)
    assert len(interpretation.requests) == 2
    assert any(u.kind == "constraint" and "Do not browse" in u.text for u in interpretation.units)
