"""The blast matrix's file ids are forward-slash on every platform.

`selected` keys are joined into the run set the tool prints alongside the `/`-joined
SENTINELS and are matched as `/`-joined literals by callers; on Windows
`str(relative_to(...))` answers `tests\\...`, so the printed run set mixes separators
and the ids stop matching. Pinned red-first on Windows.
"""
from __future__ import annotations

import pytest

from ops.blast_matrix import BlastMatrix


@pytest.fixture(scope="module")
def matrix() -> BlastMatrix:
    return BlastMatrix()


def test_selected_keys_are_forward_slash_ids(matrix: BlastMatrix) -> None:
    report = matrix.select(["core/within_turn_retraction.py"], max_hops=0)
    assert report["selected"], "the leaf's hop-0 set is empty; selection itself broke"
    for name in report["selected"]:
        assert "\\" not in name and name.startswith("tests/"), name


def test_the_direct_test_is_named_as_the_sentinels_spell_it(matrix: BlastMatrix) -> None:
    report = matrix.select(["core/within_turn_retraction.py"], max_hops=0)
    assert "tests/test_an_instruction_taken_back_is_not_an_instruction.py" in report["selected"]
