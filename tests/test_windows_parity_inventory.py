from __future__ import annotations

import json
from copy import deepcopy

from tools.windows_parity_inventory import SNAPSHOT, carry_review_classifications, current_inventory, inventory_drift


def test_windows_source_inventory_detects_new_route_and_tool_candidates() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = current_inventory()
    assert current["route_candidates"]
    assert current["builtin_tool_candidates"]
    assert inventory_drift(snapshot, current) == []
    assert snapshot["limitations"]


def test_windows_source_inventory_detects_changed_effect_declaration() -> None:
    current = current_inventory()
    changed = deepcopy(current)
    changed["builtin_tool_candidates"][0]["side_effect_class"] = "workspace_write"
    assert "changed declarations" in " ".join(inventory_drift(changed, current))


def test_inventory_refresh_keeps_review_only_when_effect_declaration_is_unchanged() -> None:
    current = current_inventory()
    previous = deepcopy(current)
    previous["route_candidates"][0]["classification"] = "windows_adapter"
    previous["builtin_tool_candidates"][0]["classification"] = "default_disabled"
    previous["builtin_tool_candidates"][1]["classification"] = "default_disabled"
    previous["builtin_tool_candidates"][1]["side_effect_class"] = "workspace_write"

    carry_review_classifications(current, previous)

    assert current["route_candidates"][0]["classification"] == "windows_adapter"
    assert current["builtin_tool_candidates"][0]["classification"] == "default_disabled"
    assert current["builtin_tool_candidates"][1]["classification"] == "unreviewed"
